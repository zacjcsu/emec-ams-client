"""Reads a Haas NGC control's machine data over TCP and logs it to logs/machine_data.log.

The control answers Q commands on the port in its Setting 143 and sends nothing unprompted. Each answer is logged
when it changes, with the raw bytes, and every command is logged again every SNAPSHOT_SECONDS. Nothing acts on the
values yet. The log is for working out what this control reports.

On each connect it also asks a wider set once (DISCOVER) and saves the control's MTConnect pages, if it serves them,
as logs/mtconnect-probe.xml and logs/mtconnect-current.xml.

Set it up in config.json: "machine_data": {"control": "haas-ngc", "host": "192.168.50.2", "port": 5051}.
"""
import logging
import re
import socket
import threading
import time
import urllib.request
from logging.handlers import TimedRotatingFileHandler

logger = logging.getLogger("haas")

# Polled every poll_seconds: program, state and parts; mode; spindle RPM; tool in the spindle.
FAST = ("Q500", "Q104", "Q600 3027", "Q201")
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
    "Q600 1098",   # spindle load
    "Q600 3020",   # power-on timer
    "Q600 3021",   # cycle start timer
    "Q600 3022",   # feed timer
    "Q600 3026",   # tool in spindle
    "Q600 3030",   # single block
)
# Asked once per connect, to see what answers. Unknown commands get "UNKNOWN".
DISCOVER = (
    tuple(f"Q{n}" for n in (*range(100, 106), *range(200, 204), *range(300, 306), *range(400, 404), 500, 501))
    + tuple(f"Q600 {n}" for n in (*range(3000, 3036), 3901, 3902, *range(4001, 4022), *range(5021, 5027),
                                  *range(5041, 5047), 1094, 1098, 13013))
)
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
    """One TCP connection to the control. Not thread safe."""

    def __init__(self, host, port):
        self.host, self.port = host, port
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

    def _drain(self):
        """Drop late bytes from an earlier answer, so they aren't read as this one."""
        self.sock.setblocking(False)
        try:
            while self.sock.recv(4096):
                pass
        except (BlockingIOError, InterruptedError):
            pass
        finally:
            self.sock.settimeout(ANSWER_SECONDS)

    def ask(self, command):
        """Send '?<command>' and return the raw answer, b'' if none came. Raises OSError if the link is down."""
        self._drain()
        self.sock.sendall(f"?{command}\r\n".encode("ascii"))
        raw = b""
        deadline = time.monotonic() + ANSWER_SECONDS
        while time.monotonic() < deadline:
            wait = QUIET_SECONDS if raw else deadline - time.monotonic()
            self.sock.settimeout(max(0.01, min(wait, deadline - time.monotonic())))
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                if raw:
                    break
                continue
            if not chunk:
                raise ConnectionError("closed by the control")
            raw += chunk
            if clean(raw) and raw.rstrip(b" ").endswith((b"\n", b">")):
                break
        return raw


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
    """Polls the control in its own thread. `values` holds the last clean answer to each command."""

    def __init__(self, host, port=5051, poll_seconds=2, log=None):
        self.client = HaasClient(str(host), int(port))
        self.poll_seconds = float(poll_seconds)
        self.log = log or data_log()
        self.values = {}
        self.connected = False
        self._failing = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="haas", daemon=True)

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

    def _discover(self):
        for command in DISCOVER:
            raw = self.client.ask(command)
            self.log.info(f"{command} = {clean(raw)!r}  raw={raw!r}  (discover)")
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

    def _run(self):
        # Connection changes go to sync.log at INFO: the control is off most nights, which isn't worth a warning.
        snapshot_at = 0.0
        while not self._stop.is_set():
            try:
                if not self.connected:
                    self.client.connect()
                    self.connected, self._failing = True, False
                    logger.info(f"[HAAS] Connected to {self.client.host}:{self.client.port}.")
                    self.log.info(f"connected to {self.client.host}:{self.client.port}")
                    self.client.ask("Q100")   # the first command after connecting can go unanswered
                    self._discover()
                    snapshot_at = 0.0
                if time.monotonic() >= snapshot_at:
                    for command in FAST + SLOW:
                        self._poll(command, always=True)
                    snapshot_at = time.monotonic() + SNAPSHOT_SECONDS
                else:
                    for command in FAST:
                        self._poll(command)
                self._stop.wait(self.poll_seconds)
            except OSError as e:
                if self.connected:
                    logger.info(f"[HAAS] Lost the control: {e}.")
                    self.log.info(f"disconnected: {e}")
                elif not self._failing:
                    logger.info(f"[HAAS] Cannot reach {self.client.host}:{self.client.port}: {e}.")
                self.connected, self._failing = False, True
                self.values = {}
                self.client.close()
                self._stop.wait(RETRY_SECONDS)
