"""Reads a Haas control's machine data and logs it to logs/machine_data.log.

The NGC answers Q commands over TCP on the port in its Setting 143. The classic control (Coldfire, Maincon) answers
the same commands on its RS-232 port when Setting 143 is On. Neither sends anything unprompted. Each answer is logged
when it changes, with the raw bytes, and every command is logged again every SNAPSHOT_SECONDS.
utils/live_state.py sends the current values to the dashboard.

On each connect it also asks a wider set once (DISCOVER_NGC or DISCOVER_CLASSIC). An NGC's MTConnect pages are saved
as logs/mtconnect-probe.xml and logs/mtconnect-current.xml, if it serves them.

Set it up in config.json:
    "machine_data": {"control": "haas-ngc", "host": "192.168.50.2", "port": 5051}
    "machine_data": {"control": "haas-classic", "device": "/dev/serial/by-id/usb-FTDI_...", "baud": 9600}
The classic reader also takes "parity" ("none", "even" or "odd"), "data_bits" and "stop_bits", which must match
Settings 12, 37 and 13. It always uses XON/XOFF, Setting 14.

The classic reader also sends programs from the dashboard (utils/program_send.py) on the same port. The control
can't say it's waiting in RECV, because in RECV it answers nothing. So a program goes when the control falls silent
soon after it showed the list of programs, which is where RECV is pressed. It counts as sent once the control
answers again after the closing %.
"""
import logging
import os
import re
import select
import socket
import termios
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from logging.handlers import TimedRotatingFileHandler

logger = logging.getLogger("haas")

# Polled every poll_seconds: program, state and parts; mode; tool in the spindle; spindle RPM and load;
# motion type (#4001 is 0 for G00, 1 to 3 for feed moves).
FAST = ("Q500", "Q104", "Q201", "Q600 3027", "Q600 1098", "Q600 4001")
# The NGC adds coolant pump, right door and left door. The door inputs are the TM-1P's.
FAST_NGC = FAST + ("Q600 12018", "Q600 11066", "Q600 11067")
# Polled every SNAPSHOT_SECONDS. Q600 reads a macro or system variable.
SLOW = (
    "Q100",        # serial number
    "Q101",        # software version
    "Q102",        # model
    "Q200",        # tool changes
    "Q300",        # power-on time
    "Q301",        # motion time
    "Q303",        # last cycle time
    "Q304",        # previous cycle time
    "Q402",        # M30 counter 1
    "Q403",        # M30 counter 2
    "Q600 3020",   # power-on timer
    "Q600 3021",   # cycle start timer
    "Q600 3022",   # feed timer
    "Q600 3026",   # tool in spindle
    "Q600 3030",   # single block
)
# Asked once per connect, to see what answers. Unknown commands get "UNKNOWN".
_Q = tuple(f"Q{n}" for n in (*range(100, 106), *range(200, 204), *range(300, 306), *range(400, 404), 500, 501))
_VARS = (*range(3000, 3036), 3901, 3902, *range(4001, 4022), *range(5021, 5027), *range(5041, 5047), 1094, 1098)
DISCOVER_NGC = _Q + tuple(f"Q600 {n}" for n in (*_VARS, 13013))
# Classic I/O: #1000-1063 discrete inputs, #1080-1097 analog inputs, #1100-1155 discrete outputs.
DISCOVER_CLASSIC = _Q + tuple(f"Q600 {n}" for n in (*_VARS, *range(1000, 1064), *range(1080, 1098),
                                                    *range(1100, 1156)))
MTCONNECT_PORT = 8082
SNAPSHOT_SECONDS = 300
RETRY_SECONDS = 10
ANSWER_SECONDS = 3     # longest wait for an answer
QUIET_SECONDS = 0.3    # without a line end or prompt, an answer is complete after this long with no more bytes
SEND_WAIT_SECONDS = 600     # how long a program waits for RECV
RECV_SECONDS = 1800         # silence this soon after the list of programs counts as RECV
SEND_RETRY_SECONDS = 2
SEND_CHUNK = 256            # written and drained at a time, so a pause from the control is obeyed within this
SEND_STALL_SECONDS = 60     # longest pause the control may ask for
AFTER_SEND_SECONDS = 20     # for the control to answer again after the closing %
XON, XOFF = 0x11, 0x13


def clean(raw):
    """The answer as text, without the framing: STX, ETB, line ends and the '>' prompt."""
    text = raw.decode("latin-1")
    text = re.sub(r"[\x00-\x1f]", " ", text)
    return text.strip().strip(">").strip()


def haas_text(content):
    """A program as the control takes it: CR LF line ends, between lines of %. It ignores anything before the first %
    and stops at the next."""
    lines = content.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines or lines[0].strip() != b"%":
        lines.insert(0, b"%")
    if len(lines) == 1 or lines[-1].strip() != b"%":
        lines.append(b"%")
    return b"\r\n".join(lines) + b"\r\n"


class SendJob:
    """A program for the control. `claim()` is True if it may go now, False if it was cancelled, and raises if
    that can't be told. The monitor moves state from "waiting" to "sending" and then "sent", "failed" or
    "cancelled", with a detail for the dashboard and the bytes sent so far."""

    def __init__(self, job_id, data, claim):
        self.id, self.data, self.claim = job_id, data, claim
        self.state, self.detail, self.sent = "waiting", None, 0
        self.created = time.monotonic()
        self.cancelled = threading.Event()
        self.done = threading.Event()


class HaasClient:
    """One link to the control. Not thread safe. Subclasses open the link and move the bytes."""

    name = ""

    def connect(self):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def _recv(self, timeout):
        """Bytes that arrive within timeout, b'' if none. Raises ConnectionError if the link closed."""
        raise NotImplementedError

    def _send(self, data):
        raise NotImplementedError

    def _drain(self):
        """Drop late bytes from an earlier answer, so they aren't read as this one."""
        while self._recv(0):
            pass

    def ask(self, command):
        """Send '?<command>' and return the raw answer, b'' if none came. Raises OSError if the link is down."""
        self._drain()
        self._send(f"?{command}\r\n".encode("ascii"))
        raw = b""
        deadline = time.monotonic() + ANSWER_SECONDS
        while time.monotonic() < deadline:
            wait = QUIET_SECONDS if raw else deadline - time.monotonic()
            chunk = self._recv(max(0.01, min(wait, deadline - time.monotonic())))
            if not chunk:
                if raw:
                    break
                continue
            raw += chunk
            if clean(raw) and raw.rstrip(b" ").endswith((b"\n", b">")):
                break
        return raw


class TcpClient(HaasClient):
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.name = f"{host}:{port}"
        self.sock = None

    def connect(self):
        self.close()
        self.sock = socket.create_connection((self.host, self.port), timeout=ANSWER_SECONDS)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def _recv(self, timeout):
        if not select.select([self.sock], [], [], timeout)[0]:
            return b""
        chunk = self.sock.recv(4096)
        if not chunk:
            raise ConnectionError("closed by the control")
        return chunk

    def _send(self, data):
        self.sock.sendall(data)


class SerialClient(HaasClient):
    """An RS-232 port, through a USB adapter and a null modem cable."""

    PARITY = {"none": 0, "even": termios.PARENB, "odd": termios.PARENB | termios.PARODD}

    def __init__(self, device, baud=9600, parity="none", data_bits=8, stop_bits=1):
        self.name = self.device = str(device)
        self.speed = getattr(termios, f"B{int(baud)}")
        self.cflag = (self.PARITY[str(parity).lower()] | {7: termios.CS7, 8: termios.CS8}[int(data_bits)]
                      | {1: 0, 2: termios.CSTOPB}[int(stop_bits)])
        self.fd = None

    def connect(self):
        self.close()
        self.fd = os.open(self.device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        iflag = termios.IXON | termios.IXOFF | (termios.INPCK if self.cflag & termios.PARENB else 0)
        cc = termios.tcgetattr(self.fd)[6]
        cc[termios.VMIN] = cc[termios.VTIME] = 0
        try:
            termios.tcsetattr(self.fd, termios.TCSANOW, [iflag, 0, self.cflag | termios.CREAD | termios.CLOCAL, 0,
                                                         self.speed, self.speed, cc])
            termios.tcflush(self.fd, termios.TCIOFLUSH)
        except termios.error as e:   # not an OSError
            self.close()
            raise OSError(*e.args) from e

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None

    def _recv(self, timeout):
        if not select.select([self.fd], [], [], timeout)[0]:
            return b""
        chunk = os.read(self.fd, 4096)
        if not chunk:
            raise ConnectionError("the serial adapter went away")
        return chunk

    def _send(self, data):
        while data:
            select.select([], [self.fd], [], ANSWER_SECONDS)
            data = data[os.write(self.fd, data):]

    def send_program(self, data, progress):
        """Writes a program while the control is in RECV, pausing when it sends XOFF. Raises OSError if it fails."""
        attrs = termios.tcgetattr(self.fd)
        own = list(attrs)
        own[0] &= ~(termios.IXON | termios.IXOFF)   # read XOFF here, so a pause that never ends can time out
        try:
            termios.tcsetattr(self.fd, termios.TCSANOW, own)
            sent, paused_at = 0, None
            while sent < len(data):
                while select.select([self.fd], [], [], 0)[0]:
                    chunk = os.read(self.fd, 4096)
                    if not chunk:
                        raise ConnectionError("the serial adapter went away")
                    for b in chunk:
                        if b == XOFF:
                            paused_at = paused_at or time.monotonic()
                        elif b == XON:
                            paused_at = None
                if paused_at:
                    if time.monotonic() - paused_at > SEND_STALL_SECONDS:
                        raise TimeoutError("the mill paused the send and didn't resume")
                    time.sleep(0.01)
                    continue
                if not select.select([], [self.fd], [], ANSWER_SECONDS)[1]:
                    raise TimeoutError("the serial adapter stopped taking data")
                sent += os.write(self.fd, data[sent:sent + SEND_CHUNK])
                termios.tcdrain(self.fd)
                progress(sent)
        except termios.error as e:   # not an OSError
            raise OSError(*e.args) from e
        finally:
            try:
                termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            except termios.error:
                pass


def data_log(path="logs/machine_data.log"):
    """Its own file, kept as long as sync.log, and kept out of sync.log and the dashboard's events."""
    log = logging.getLogger("machine_data")
    if not log.handlers:
        handler = TimedRotatingFileHandler(path, when="midnight", backupCount=200)
        handler.setFormatter(logging.Formatter("[%(asctime)s.%(msecs)03d] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


class HaasMonitor:
    """Polls the control in its own thread. `values` holds the last clean answer to each command, and `mtc` the last
    value of each MTConnect item. A condition's is (level, alarm number, text). Make one with HaasMonitor.ngc or
    HaasMonitor.classic."""

    def __init__(self, client, fast, discover, mtconnect=False, poll_seconds=2, log=None):
        self.client = client
        self.fast = fast
        self.discover = discover
        self.mtconnect = mtconnect
        self._mtc_next = None      # the next MTConnect change to read, None to start from the current page
        self._mtc_failing = False
        self.poll_seconds = float(poll_seconds)
        self.log = log or data_log()
        self.values = {}
        self.mtc = {}
        self.mode, self.mode_at = None, 0.0   # the last Q104 answer and when
        self.job = None
        self.connected = False
        self._failing = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="haas", daemon=True)

    @classmethod
    def ngc(cls, host, port=5051, poll_seconds=2, log=None):
        return cls(TcpClient(str(host), int(port)), FAST_NGC, DISCOVER_NGC, True, poll_seconds, log)

    @classmethod
    def classic(cls, device, baud=9600, parity="none", data_bits=8, stop_bits=1, poll_seconds=2, log=None):
        return cls(SerialClient(device, baud, parity, data_bits, stop_bits), FAST, DISCOVER_CLASSIC, False,
                   poll_seconds, log)

    @property
    def can_send(self):
        return hasattr(self.client, "send_program")

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def send(self, job):
        """Sends `job` once the control is in RECV. One at a time."""
        if not self.can_send:
            raise ValueError("this control takes no programs")
        if self.job:
            raise ValueError("a program is already waiting")
        self.job = job

    def _finish(self, job, state, detail=None):
        job.state, job.detail = state, detail
        logger.info(f"[HAAS] Program {job.id}: {state}" + (f", {detail}." if detail else "."))
        self.log.info(f"program {job.id} {state}" + (f": {detail}" if detail else ""))
        self.job = None
        job.done.set()

    def _send_round(self, job):
        """Moves a waiting program along. True if it used the port this round."""
        if job.cancelled.is_set():
            self._finish(job, "cancelled")
            return False
        if time.monotonic() - job.created > SEND_WAIT_SECONDS:
            self._finish(job, "failed", "RECV wasn't pressed")
            return False
        if self.connected:
            job.detail = "Press RECV"
            return False
        if not (self.mode and "LISTPROG" in self.mode and time.monotonic() - self.mode_at < RECV_SECONDS):
            job.detail = "Mill off"
            return False
        try:
            self.client.connect()
            if clean(self.client.ask("Q100")):
                return False   # still answering, so the normal round reconnects
            try:
                if not job.claim():
                    self._finish(job, "cancelled")
                    return True
            except Exception as e:
                logger.info(f"[HAAS] Cannot start program {job.id}: {e}")
                self._stop.wait(SEND_RETRY_SECONDS)
                return True
            job.state, job.detail = "sending", None
            logger.info(f"[HAAS] Sending program {job.id}, {len(job.data)} bytes.")
            self.log.info(f"sending program {job.id}, {len(job.data)} bytes")
            self.client.send_program(job.data, lambda n: setattr(job, "sent", n))
            deadline = time.monotonic() + AFTER_SEND_SECONDS
            while time.monotonic() < deadline:
                if clean(self.client.ask("Q100")):
                    self._finish(job, "sent")
                    return True
            self._finish(job, "failed", "No answer after the send")
        except OSError as e:
            if job.state == "sending":
                self._finish(job, "failed", f"Stopped: {e}")
            else:
                job.detail = "Mill off"
                self.client.close()
                self._stop.wait(SEND_RETRY_SECONDS)
        return True

    def _poll(self, command, always=False):
        raw = self.client.ask(command)
        value = clean(raw)
        if always or self.values.get(command) != value:
            self.log.info(f"{command} = {value!r}  raw={raw!r}" + ("  (snapshot)" if always else ""))
        self.values[command] = value
        if command == "Q104" and value:
            self.mode, self.mode_at = value, time.monotonic()
        return value

    def _poll_all(self, commands, always=False):
        # A serial port stays open with the machine off, so silence is how a classic control shows it's gone.
        if not any([self._poll(command, always) for command in commands]):
            raise ConnectionError("no answer")

    def _discover(self):
        for command in self.discover:
            raw = self.client.ask(command)
            self.log.info(f"{command} = {clean(raw)!r}  raw={raw!r}  (discover)")
        if not self.mtconnect:
            return
        for page in ("probe", "current"):
            url = f"http://{self.client.host}:{MTCONNECT_PORT}/{page}"
            try:
                with urllib.request.urlopen(url, timeout=5) as r:
                    body = r.read()
                with open(f"logs/mtconnect-{page}.xml", "wb") as f:
                    f.write(body)
                self.log.info(f"MTConnect {page}: {len(body)} bytes saved")
            except (OSError, ValueError) as e:
                self.log.info(f"MTConnect {page}: {e}")

    def _mtconnect_page(self, page):
        """The page and its next sequence number, which is None on an MTConnectError."""
        with urllib.request.urlopen(f"http://{self.client.host}:{MTCONNECT_PORT}/{page}", timeout=2) as r:
            root = ET.fromstring(r.read())
        header = root.find(".//{*}Header")
        next_ = header.get("nextSequence") if header is not None else None
        return root, int(next_) if next_ else None

    def _mtconnect_changes(self):
        """Log every MTConnect change since the last read. The control keeps the last 333, numbered in order."""
        try:
            next_ = None
            if self._mtc_next:
                root, next_ = self._mtconnect_page(f"sample?from={self._mtc_next}&count=1000")
            current = next_ is None
            if current:
                # The Haas answers OUT_OF_RANGE both when nothing is new and when changes were missed.
                root, next_ = self._mtconnect_page("current")
                if next_ is None:
                    raise ValueError(ET.tostring(root, encoding="unicode")[:200])
        except (OSError, ValueError, ET.ParseError) as e:
            if not self._mtc_failing:
                self.log.info(f"MTConnect: {e}")
            self._mtc_failing, self._mtc_next = True, None
            self.mtc = {}
            return
        self._mtc_failing = False
        if current and next_ == self._mtc_next:
            return
        if current and self._mtc_next:
            self.log.info(f"MTConnect: missed changes from {self._mtc_next}")
        items = sorted((int(e.get("sequence")), e) for e in root.iter() if e.get("sequence"))
        for seq, e in items:
            tag = e.tag.split("}")[-1]
            name = e.get("name") or e.get("dataItemId")
            value = (e.text or "").strip()
            code = e.get("nativeCode") or ""
            if e.get("type"):   # a condition: Normal, Warning or Fault, with the alarm's text
                self.mtc[name] = (tag, code, value)
                value = f"{tag} {value}".strip()
            else:
                self.mtc[name] = value
            self.log.info(f"MTConnect {name} = {value!r}  at {e.get('timestamp')}" + (f"  code={code}" if code else "")
                          + ("  (current)" if current else ""))
        self._mtc_next = next_

    def _run(self):
        # Connection changes go to sync.log at INFO: the control is off most nights, which isn't worth a warning.
        snapshot_at = 0.0
        while not self._stop.is_set():
            job = self.job
            if job and self._send_round(job):
                continue
            try:
                if not self.connected:
                    self.client.connect()
                    # The first command after connecting can go unanswered.
                    if not (clean(self.client.ask("Q100")) or clean(self.client.ask("Q100"))):
                        raise ConnectionError("no answer")
                    self.connected, self._failing = True, False
                    logger.info(f"[HAAS] Connected to {self.client.name}.")
                    self.log.info(f"connected to {self.client.name}")
                    self._discover()
                    self._mtc_next = None
                    snapshot_at = 0.0
                if time.monotonic() >= snapshot_at:
                    self._poll_all(self.fast + SLOW, always=True)
                    snapshot_at = time.monotonic() + SNAPSHOT_SECONDS
                else:
                    self._poll_all(self.fast)
                if self.mtconnect:
                    self._mtconnect_changes()
                self._stop.wait(self.poll_seconds)
            except OSError as e:
                if self.connected:
                    logger.info(f"[HAAS] Lost the control: {e}.")
                    self.log.info(f"disconnected: {e}")
                elif not self._failing:
                    logger.info(f"[HAAS] Cannot reach {self.client.name}: {e}.")
                self.connected, self._failing = False, True
                self.values, self.mtc = {}, {}
                self.client.close()
                self._stop.wait(SEND_RETRY_SECONDS if self.job else RETRY_SECONDS)
