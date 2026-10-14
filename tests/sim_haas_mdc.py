"""Runs utils/haas_mdc.py against a fake Haas NGC with a fake MTConnect page, and a fake classic control on a
pseudo-terminal. No Pi needed:

    .venv/bin/python tests/sim_haas_mdc.py
"""
import http.server, os, select, socket, sys, tempfile, termios, threading, time
from pathlib import Path

repo = str(Path(__file__).resolve().parent.parent)
os.chdir(tempfile.mkdtemp())
os.makedirs("logs")
sys.path.insert(0, repo)
from utils import haas_mdc  # noqa: E402

STATE = {"Q500": "PROGRAM, O00001, IDLE, PARTS, 380"}
fails = 0


def check(name, ok):
    global fails
    print(("ok   " if ok else "FAIL ") + name)
    fails += not ok


class FakeControl:
    """Answers like the manual: STX, the CSV line, ETB, CR LF, then '>'. The first Q100 goes unanswered."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen()
        self.port = self.srv.getsockname()[1]
        self.conns = []
        self.drop = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            conn, _ = self.srv.accept()
            self.conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        first = True
        buf = b""
        while True:
            try:
                data = conn.recv(1024)
            except OSError:
                return
            if not data:
                return
            buf += data
            while b"\r\n" in buf:
                line, buf = buf.split(b"\r\n", 1)
                cmd = line.decode().lstrip("?")
                if first and cmd == "Q100":
                    first = False
                    continue
                if self.drop.is_set():
                    conn.close()
                    return
                answer = STATE.get(cmd, "SERIAL NUMBER, 1234567" if cmd == "Q100" else "UNKNOWN")
                conn.sendall(b"\x02" + answer.encode() + b"\x17\r\n>")


# (sequence, element) changes, as the control numbers them
CHANGES = [(1, '<Execution dataItemId="rstat" name="RunStatus" sequence="1" timestamp="t1">STOPPED</Execution>'),
           (2, '<Normal dataItemId="mcond" name="MachineCondition" type="SYSTEM" sequence="2" timestamp="t2"/>')]


class FakeMTConnect(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        first, next_ = CHANGES[0][0], CHANGES[-1][0] + 1
        start = int(self.path.split("from=")[1].split("&")[0]) if "from=" in self.path else first
        rows = CHANGES if self.path == "/current" else [c for c in CHANGES if c[0] >= start]
        if not first <= start < next_:   # like the Haas, also when nothing is new yet
            body = ('<MTConnectError xmlns="urn:mtconnect.org:MTConnectError:1.2"><Header bufferSize="333"/>'
                    f'<Errors><Error errorCode="OUT_OF_RANGE">from must be at least {first}</Error></Errors>'
                    '</MTConnectError>').encode()
        else:
            body = ('<MTConnectStreams xmlns="urn:mtconnect.org:MTConnectStreams:1.2">'
                    f'<Header firstSequence="{first}" nextSequence="{next_}"/><Streams><DeviceStream>'
                    + "".join(r[1] for r in rows) + "</DeviceStream></Streams></MTConnectStreams>").encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


control = FakeControl()
web = http.server.HTTPServer(("127.0.0.1", 0), FakeMTConnect)
threading.Thread(target=web.serve_forever, daemon=True).start()
haas_mdc.MTCONNECT_PORT = web.server_address[1]
haas_mdc.RETRY_SECONDS = 0.2
haas_mdc.DISCOVER_NGC = ("Q101", "Q600 3027")

check("framing is cleaned", haas_mdc.clean(b"\x02PROGRAM, O00001, IDLE, PARTS, 380\x17\r\n>") ==
      "PROGRAM, O00001, IDLE, PARTS, 380")

mon = haas_mdc.HaasMonitor.ngc("127.0.0.1", control.port, poll_seconds=0.1)
mon.start()


def wait_for(cond, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def log():
    return open("logs/machine_data.log").read()


check("connects and reads Q500", wait_for(lambda: mon.values.get("Q500") == STATE["Q500"]))
check("serial number read after the unanswered first Q100",
      wait_for(lambda: "Q100 = 'SERIAL NUMBER, 1234567'" in log()))
check("discover pass logged", "Q600 3027 = 'UNKNOWN'" in log() and "(discover)" in log())
check("MTConnect pages saved", "RunStatus" in open("logs/mtconnect-current.xml").read()
      and os.path.exists("logs/mtconnect-probe.xml"))
check("MTConnect current values logged", wait_for(lambda: "MTConnect RunStatus = 'STOPPED'  at t1  (current)" in log()))
check("raw bytes logged", "raw=b'\\x02PROGRAM" in log())

lines = log().count("Q500 = ")
time.sleep(0.5)
check("unchanged answers are not logged again", log().count("Q500 = ") == lines)

STATE["Q500"] = "PROGRAM, O00001, BUSY, PARTS, 380"
check("a change is logged", wait_for(lambda: "BUSY" in log()))
CHANGES.append((3, '<Fault dataItemId="mcond" name="MachineCondition" type="SYSTEM" sequence="3" '
                   'timestamp="t3">107 EMERGENCY OFF</Fault>'))
check("an MTConnect change is logged once", wait_for(lambda: "MachineCondition = 'Fault 107 EMERGENCY OFF'" in log())
      and (time.sleep(0.5) or log().count("EMERGENCY OFF") == 1))
check("old MTConnect changes are not logged again", log().count("RunStatus = 'STOPPED'") == 1)
check("nothing new is not logged as an error", "MTConnect: " not in log())

CHANGES[:] = [(seq + 10, row.replace(f'sequence="{seq}"', f'sequence="{seq + 10}"')) for seq, row in CHANGES]
check("missed changes reread the current values",
      wait_for(lambda: "MTConnect: missed changes from 4" in log())
      and wait_for(lambda: log().count("RunStatus = 'STOPPED'") == 2))

control.drop.set()
check("a lost link is noticed", wait_for(lambda: "disconnected" in log()))
check("values are cleared while down", not mon.values)
control.drop.clear()
check("reconnects", wait_for(lambda: log().count("connected to") >= 2 and mon.connected))

mon.stop()
mon._thread.join(10)


class FakeClassic:
    """The same answers on a pseudo-terminal. `off` stops it answering, like a machine switched off."""

    def __init__(self):
        self.master, self.slave = os.openpty()   # holding the slave open keeps the master readable
        self.device = os.ttyname(self.slave)
        self.off = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        buf = b""
        while True:
            select.select([self.master], [], [])
            buf += os.read(self.master, 1024)
            while b"\r\n" in buf:
                line, buf = buf.split(b"\r\n", 1)
                if self.off.is_set():
                    continue
                cmd = line.decode().lstrip("?")
                answer = STATE.get(cmd, "SERIAL NUMBER, 7654321" if cmd == "Q100" else f"?, ?{cmd}")
                os.write(self.master, b"\x02" + answer.encode() + b"\x17\r\n>")


classic = FakeClassic()
classic.off.set()
haas_mdc.DISCOVER_CLASSIC = ("Q101", "Q600 1000")
haas_mdc.ANSWER_SECONDS = 1   # a round with the machine off waits this long for each command
STATE["Q500"] = "PROGRAM, O00100, IDLE, PARTS, 7"
start = len(log())


def log():
    return open("logs/machine_data.log").read()[start:]


# A pty refuses 7 data bits after the first time, so the port runs 8N1 here.
port = haas_mdc.SerialClient(classic.device, baud=38400, parity="even", data_bits=7)
check("classic: 7E1 flags", port.cflag == termios.CS7 | termios.PARENB and port.speed == termios.B38400)
mon = haas_mdc.HaasMonitor.classic(classic.device, poll_seconds=0.1)
mon.start()

time.sleep(7)
check("classic: no answer means not connected", not mon.connected and "connected to" not in log())
classic.off.clear()
check("classic: connects when the machine answers", wait_for(lambda: mon.values.get("Q500") == STATE["Q500"]))
attrs = termios.tcgetattr(mon.client.fd)
check("classic: port set to 9600 with XON/XOFF", attrs[4] == termios.B9600 and attrs[0] & termios.IXON)
check("classic: discover pass logged", "Q600 1000 = '?, ?Q600 1000'" in log())
check("classic: no MTConnect", "MTConnect" not in log())
STATE["Q500"] = "STATUS BUSY"
check("classic: a change is logged", wait_for(lambda: "STATUS BUSY" in log()))
classic.off.set()
check("classic: switched off is noticed", wait_for(lambda: "disconnected: no answer" in log(), 20))
classic.off.clear()
check("classic: reconnects", wait_for(lambda: log().count("connected to") >= 2 and mon.connected, 20))
mon.stop()

print("all passed" if not fails else f"{fails} failed")
sys.exit(1 if fails else 0)
