"""Reads a Haas control's machine data and logs it to logs/machine_data.log.

The NGC answers Q commands over TCP on the port in its Setting 143. The classic control (Coldfire, Maincon) answers
the same commands on its RS-232 port when Setting 143 is On. Neither sends anything unprompted. Each answer is logged
when it changes, with the raw bytes, and every command is logged again every SNAPSHOT_SECONDS. Nothing acts on the
values yet. The log is for working out what this control reports.

On each connect it also asks a wider set once (DISCOVER_NGC or DISCOVER_CLASSIC). An NGC's MTConnect pages are saved
as logs/mtconnect-probe.xml and logs/mtconnect-current.xml, if it serves them.

Set it up in config.json:
    "machine_data": {"control": "haas-ngc", "host": "192.168.50.2", "port": 5051}
    "machine_data": {"control": "haas-classic", "device": "/dev/serial/by-id/usb-FTDI_...", "baud": 9600}
The classic reader also takes "parity" ("none", "even" or "odd"), "data_bits" and "stop_bits", which must match
Settings 12, 37 and 13. It always uses XON/XOFF, Setting 14.
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


def clean(raw):
    """The answer as text, without the framing: STX, ETB, line ends and the '>' prompt."""
    text = raw.decode("latin-1")
    text = re.sub(r"[\x00-\x1f]", " ", text)
    return text.strip().strip(">").strip()


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
    """Polls the control in its own thread. `values` holds the last clean answer to each command.
    Make one with HaasMonitor.ngc or HaasMonitor.classic."""

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

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _poll(self, command, always=False):
        raw = self.client.ask(command)
        value = clean(raw)
        if always or self.values.get(command) != value:
            self.log.info(f"{command} = {value!r}  raw={raw!r}" + ("  (snapshot)" if always else ""))
        self.values[command] = value
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

    def _mtconnect_changes(self):
        """Log every MTConnect change since the last read. The control keeps the last 333, numbered in order."""
        page = f"sample?from={self._mtc_next}&count=1000" if self._mtc_next else "current"
        try:
            with urllib.request.urlopen(f"http://{self.client.host}:{MTCONNECT_PORT}/{page}", timeout=2) as r:
                root = ET.fromstring(r.read())
            header = root.find(".//{*}Header")
            if header is None:   # an MTConnectError, e.g. after the control restarted and renumbered
                raise ValueError(ET.tostring(root, encoding="unicode")[:200])
            first, next_ = int(header.get("firstSequence")), int(header.get("nextSequence"))
        except (OSError, ValueError, ET.ParseError, TypeError) as e:
            if not self._mtc_failing:
                self.log.info(f"MTConnect: {e}")
            self._mtc_failing, self._mtc_next = True, None
            return
        self._mtc_failing = False
        if self._mtc_next and first > self._mtc_next:
            self.log.info(f"MTConnect: missed changes {self._mtc_next} to {first - 1}")
        items = sorted((int(e.get("sequence")), e) for e in root.iter() if e.get("sequence"))
        for seq, e in items:
            tag = e.tag.split("}")[-1]
            value = (e.text or "").strip()
            if e.get("type"):   # a condition: Normal, Warning or Fault, with the alarm's text
                value = f"{tag} {value}".strip()
            self.log.info(f"MTConnect {e.get('name') or e.get('dataItemId')} = {value!r}  at {e.get('timestamp')}"
                          + ("" if self._mtc_next else "  (current)"))
        self._mtc_next = next_

    def _run(self):
        # Connection changes go to sync.log at INFO: the control is off most nights, which isn't worth a warning.
        snapshot_at = 0.0
        while not self._stop.is_set():
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
                self.values = {}
                self.client.close()
                self._stop.wait(RETRY_SECONDS)
