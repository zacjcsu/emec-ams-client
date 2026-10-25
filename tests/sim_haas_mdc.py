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
    """Answers like the TM-2P on a pseudo-terminal: its prompt, then the frame. `off` stops it answering, like a
    machine switched off. `recv` is RECV: it answers nothing and stores what comes from the first % to the next in
    `received`. `delay` holds each frame back after the prompt. `stale` goes out once before the next frame."""

    def __init__(self):
        self.master, self.slave = os.openpty()   # holding the slave open keeps the master readable
        self.device = os.ttyname(self.slave)
        self.off = threading.Event()
        self.recv = threading.Event()
        self.received = None
        self.pause_after = None   # in RECV, send XOFF after this many bytes and XON a second later
        self.delay = 0
        self.stale = None
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        buf = b""
        while True:
            select.select([self.master], [], [])
            buf += os.read(self.master, 1024)
            if self.recv.is_set():
                if self.pause_after is not None and len(buf) >= self.pause_after:
                    self.pause_after = None
                    os.write(self.master, b"\x13")
                    threading.Timer(1, lambda: os.write(self.master, b"\x11")).start()
                lines = buf.split(b"\r\n")
                marks = [i for i, line in enumerate(lines) if line.strip() == b"%"]
                if len(marks) >= 2:
                    self.received = b"\r\n".join(lines[marks[0]:marks[1] + 1]) + b"\r\n"
                    buf = b""
                    self.recv.clear()
                continue
            while b"\r\n" in buf:
                line, buf = buf.split(b"\r\n", 1)
                if self.off.is_set():
                    continue
                cmd = line.decode().lstrip("?")
                unknown = f"MACRO, {cmd[5:]}, INVALID" if cmd.startswith("Q600 ") else "UNKNOWN"
                answer = STATE.get(cmd, "SERIAL NUMBER, 7654321" if cmd == "Q100" else unknown)
                os.write(self.master, b"\r\n\r\n>\r\n")
                time.sleep(self.delay)
                if self.stale:
                    os.write(self.master, self.stale)
                    self.stale = None
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
check("classic: discover pass logged", "Q600 1000 = 'MACRO, 1000, INVALID'" in log())
check("classic: no MTConnect", "MTConnect" not in log())
STATE["Q500"] = "STATUS BUSY"
check("classic: a change is logged", wait_for(lambda: "STATUS BUSY" in log()))
check("classic: no prompt left in a value", not any(v.startswith(">") for v in mon.values.values()))

STATE["Q600 3027"] = "MACRO, 3027, 1200.000000"
classic.delay = 0.6   # longer than QUIET_SECONDS
check("classic: a frame that comes late still lands on its command",
      wait_for(lambda: mon.values.get("Q600 3027") == STATE["Q600 3027"], 20))
classic.delay = 0
for stale in (b"\x02MACRO, 3, -16777215.000000\x17\r\n>", b"\x02MACRO, 4294967295, INVALID\x17\r\n>"):
    classic.stale = stale
    check(f"classic: a stray frame is dropped and logged", wait_for(lambda: classic.stale is None)
          and wait_for(lambda: stale[1:-5].decode() in log() and "dropped" in log()))
time.sleep(1)
check("classic: no value came from a stray frame",
      all(v == STATE.get(k, v) for k, v in mon.values.items()) and not any("4294967295" in v for v in mon.values.values()))
check("haas: belongs", haas_mdc.belongs("Q600 3027", "MACRO, 3027, 1.0") and haas_mdc.belongs("Q600 3027", "MACRO, 1.0")
      and not haas_mdc.belongs("Q600 3027", "MACRO, 3, 1.0") and not haas_mdc.belongs("Q500", "MACRO, 3027, 1.0")
      and haas_mdc.belongs("Q500", "PROGRAM,O1,IDLE,PARTS,3") and not haas_mdc.belongs("Q600 3027", "MODE, MEM")
      and haas_mdc.belongs("Q600 3027", "UNKNOWN"))

STATE["Q600 1008"] = "MACRO, 1008,       1.000000"
STATE["Q600 1105"] = "MACRO, 1105,       0.000000"
check("classic: polls the door switch and coolant pump",
      wait_for(lambda: mon.values.get("Q600 1008") == STATE["Q600 1008"] and mon.values.get("Q600 1105") == STATE["Q600 1105"]))
from utils import live_state  # noqa: E402
snap = live_state.snapshot(mon.values, {})
check("classic: door open and coolant off on the dashboard", snap.get("door_open") is True and snap.get("coolant") is False)
check("classic: polls the feed word and work position", "Q600 4109" in mon.values and "Q600 5043" in mon.values)
STATE["Q600 2004"] = "MACRO, 2004,      -5.123400"
check("classic: reads the tool offsets a few at a time",
      wait_for(lambda: mon.values.get("Q600 2004") == STATE["Q600 2004"] and "Q600 5720" in mon.values, 90)
      and "Q600 5720 = 'MACRO, 5720, INVALID'  raw=b'\\x02MACRO, 5720, INVALID\\x17'  (snapshot)" in log())
check("classic: a lathe gets the lathe's offsets", haas_mdc.LATHE_MODEL.match("MODEL, TL1")
      and haas_mdc.LATHE_MODEL.match("MODEL,ST10") and not haas_mdc.LATHE_MODEL.match("MODEL, TM3P")
      and mon.tables is haas_mdc.TABLES_MILL)
classic.off.set()
check("classic: switched off is noticed", wait_for(lambda: "disconnected: no answer" in log(), 20))
classic.off.clear()
check("classic: reconnects", wait_for(lambda: log().count("connected to") >= 2 and mon.connected, 20))

check("haas_text adds the % lines and CR LF", haas_mdc.haas_text(b"O1 (A)\nM30\n\n") == b"%\r\nO1 (A)\r\nM30\r\n%\r\n")
check("haas_text keeps a program's own % lines", haas_mdc.haas_text(b"%\r\nO1\r\n%\r\n") == b"%\r\nO1\r\n%\r\n")

haas_mdc.SEND_RETRY_SECONDS = 0.2
program = haas_mdc.haas_text(b"%\nO09990 (SIM)\n" + b"".join(b"(LINE %04d PADDING)\n" % n for n in range(300)) + b"M30\n%\n")
claims = []
STATE["Q104"] = "MODE, MEM"
check("send: back on MEM", wait_for(lambda: mon.mode == "MODE, MEM"))
job = haas_mdc.SendJob(1, program, lambda: claims.append(1) or True)
mon.send(job)
check("send: waits for RECV while the mill answers", wait_for(lambda: job.detail == "Press RECV"))
classic.off.set()
check("send: silence off the program list isn't RECV", wait_for(lambda: job.detail == "Mill off", 20) and not claims)
classic.off.clear()
check("send: reconnects", wait_for(lambda: mon.connected, 20))
STATE["Q104"] = "MODE, LISTPROG"
check("send: sees the program list", wait_for(lambda: "LISTPROG" in (mon.mode or "")))
classic.pause_after = 2000
classic.recv.set()
check("send: sent after RECV", wait_for(lambda: job.done.is_set(), 40) and job.state == "sent")
check("send: claimed once", claims == [1])
check("send: the control got the program whole", classic.received == program)
check("send: reconnects after", wait_for(lambda: mon.connected, 20))

job = haas_mdc.SendJob(2, program, lambda: False)
mon.send(job)
classic.recv.set()
check("send: a cancelled claim isn't sent", wait_for(lambda: job.done.is_set(), 40) and job.state == "cancelled")
classic.recv.clear()
check("send: reconnects after a cancel", wait_for(lambda: mon.connected, 20))

job = haas_mdc.SendJob(3, program, lambda: True)
mon.send(job)
classic.recv.set()
classic.off.set()   # stays silent after the send, like a mill switched off
check("send: no answer after is a failure", wait_for(lambda: job.done.is_set(), 60) and job.state == "failed")
classic.recv.clear()
classic.off.clear()
mon.stop()
mon._thread.join(20)
mon.client.close()

mon = haas_mdc.HaasMonitor.classic(classic.device, poll_seconds=1.5)
asked = []
ask = mon.client.ask
mon.client.ask = lambda command: asked.append(command) or ask(command)
STATE["Q104"] = "MODE, MEM"
STATE["Q500"] = "PROGRAM, O00100, IDLE, PARTS, 7"
mon.start()
check("position: connects", wait_for(lambda: mon.values.get("Q500") == STATE["Q500"], 20))
reads, rounds, t = asked.count("Q600 5041"), asked.count("Q500"), time.monotonic()
check("position: rounds go on", wait_for(lambda: asked.count("Q500") - rounds >= 3, 30))
seconds, reads = time.monotonic() - t, asked.count("Q600 5041") - reads
check(f"position: the rest only once a round (3 rounds in {seconds:.1f} s)", seconds >= 2 * mon.poll_seconds)
check(f"position: read back to back between rounds ({reads} in 3 rounds)", reads >= 30)
check("position: N word read with it", asked.count("Q600 4114") >= asked.count("Q600 5041") - 1)
check("position: no arc or cycle words on a line", "Q600 4104" not in asked and "Q600 4126" not in asked)
STATE["Q600 4001"] = "MACRO, 4001,       2.000000"
check("position: an arc's I, J and plane read", wait_for(lambda: "Q600 4104" in asked and "Q600 4002" in asked, 10))
STATE["Q600 4001"] = "MACRO, 4001,       1.000000"
STATE["Q600 4009"] = "MACRO, 4009,      81.000000"
check("position: a canned cycle's Z read", wait_for(lambda: "Q600 4126" in asked, 10))
check("position: macro_value", haas_mdc.macro_value("MACRO, 4009,      81.000000") == 81
      and haas_mdc.macro_value("MACRO, -0.25") == -0.25 and haas_mdc.macro_value("MACRO, 4110, INVALID") is None)
mon.stop()

print("all passed" if not fails else f"{fails} failed")
sys.exit(1 if fails else 0)
