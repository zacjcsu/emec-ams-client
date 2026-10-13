"""Runs utils/haas_mdc.py against a fake Haas control and a fake MTConnect page on this computer. No Pi needed:

    .venv/bin/python tests/sim_haas_mdc.py
"""
import http.server, os, socket, sys, tempfile, threading, time
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


class FakeMTConnect(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"<MTConnectStreams page='{self.path}'/>".encode()
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
haas_mdc.DISCOVER = ("Q101", "Q600 3027")

check("framing is cleaned", haas_mdc.clean(b"\x02PROGRAM, O00001, IDLE, PARTS, 380\x17\r\n>") ==
      "PROGRAM, O00001, IDLE, PARTS, 380")

mon = haas_mdc.HaasMonitor("127.0.0.1", control.port, poll_seconds=0.1)
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
check("serial number read after the unanswered first Q100", "Q100 = 'SERIAL NUMBER, 1234567'" in log())
check("discover pass logged", "Q600 3027 = 'UNKNOWN'" in log() and "(discover)" in log())
check("MTConnect pages saved", open("logs/mtconnect-current.xml").read() == "<MTConnectStreams page='/current'/>"
      and os.path.exists("logs/mtconnect-probe.xml"))
check("raw bytes logged", "raw=b'\\x02PROGRAM" in log())

lines = log().count("Q500 = ")
time.sleep(0.5)
check("unchanged answers are not logged again", log().count("Q500 = ") == lines)

STATE["Q500"] = "PROGRAM, O00001, BUSY, PARTS, 380"
check("a change is logged", wait_for(lambda: "BUSY" in log()))

control.drop.set()
check("a lost link is noticed", wait_for(lambda: "disconnected" in log()))
check("values are cleared while down", not mon.values)
control.drop.clear()
check("reconnects", wait_for(lambda: log().count("connected to") >= 2 and mon.connected))

mon.stop()
print("all passed" if not fails else f"{fails} failed")
sys.exit(1 if fails else 0)
