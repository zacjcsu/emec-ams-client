"""Runs utils/camera.py with a fake camera and a fake machine_camera table. No Pi, camera or server needed:

    .venv/bin/python tests/sim_camera.py
"""
import os, sys, time
from pathlib import Path

repo = str(Path(__file__).resolve().parent.parent)
os.chdir(repo)
sys.path.insert(0, repo)
os.environ["EMEC_HARDWARE_STUBS"] = "1"   # this machine has no GPIO
from utils import camera as cam  # noqa: E402

fails = 0


def check(name, ok):
    global fails
    print(("ok   " if ok else "FAIL ") + name)
    fails += not ok


def jpeg(n):
    return b"\xff\xd8\xff\xe0" + bytes([n]) * 50 + b"\xff\xd9"


frames = cam.Frames()
frames.feed(jpeg(1) + jpeg(2)[:10])
check("a picture is kept once the next starts", frames.latest == jpeg(1))
frames.feed(jpeg(2)[10:] + jpeg(3))
check("then the newest whole one", frames.latest == jpeg(2))
other = cam.Frames()
other.feed(b"\x00" * (cam.MAX_BUFFER + 1))
check("a stream that isn't MJPEG doesn't grow", len(other._buf) == 0 and other.latest is None)


class FakeServer:
    """The statements Camera sends."""

    def __init__(self):
        self.camera = None
        self.watched = False
        self.frames = []
        self.closed = False
        self.autocommit = False

    def execute(self, sql, args):
        self.sql = sql
        if sql.startswith("UPDATE machine SET camera"):
            self.camera = args[0]
        elif sql.startswith("UPDATE machine_camera"):
            self.frames.append(args[0])
        return self

    def fetchone(self):
        return {"watched": self.watched} if "watched_until" in self.sql else None

    def close(self):
        self.closed = True


server = FakeServer()
cam.get_server_connection = lambda timeout: server
cam.LOOK_SECONDS = 0.2
cam.IDLE_SECONDS = 0.1
# A camera that writes pictures 10 and up, five a second
fake = [sys.executable, "-c", "import sys,time\nfor n in range(10, 250):\n"
        "    sys.stdout.buffer.write(b'\\xff\\xd8\\xff\\xe0' + bytes([n]) * 50 + b'\\xff\\xd9'); sys.stdout.flush(); time.sleep(0.2)"]
plugged = ["/dev/v4l/by-id/usb-Fake-video-index0"]
stream = cam.Stream(command=fake)
camera = cam.Camera("zz_test_1", find=lambda: plugged[0], stream=stream)
camera.start()
time.sleep(0.5)
check("a plugged-in camera is reported", server.camera is True)
check("nothing streams while nobody watches", not stream.running() and not server.frames)
server.watched = True
time.sleep(2.5)
check("pictures go up while watched", len(server.frames) >= 3 and all(f.startswith(b"\xff\xd8") for f in server.frames))
check("each one newer than the last", all(a[4] < b[4] for a, b in zip(server.frames, server.frames[1:])))
server.watched = False
time.sleep(0.5)
sent = len(server.frames)
check("the camera stops once nobody watches", not stream.running())
time.sleep(0.5)
check("and nothing more goes up", len(server.frames) == sent)
plugged[0] = None
time.sleep(0.5)
check("an unplugged camera is reported", server.camera is False)
camera.stop()

broken = cam.Stream(command=[sys.executable, "-c", "pass"])
broken.start("/dev/x")
time.sleep(0.3)
broken.start("/dev/x")
check("a camera that quits waits before trying again", not broken.running() and broken._retry_at > time.monotonic())

print("FAILS:", fails)
sys.exit(1 if fails else 0)
