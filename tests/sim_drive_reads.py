"""Checks utils/drive_reads.py with a fake server: a read the machine starts on the made-up drive is sent with the
file's bytes once, reads wait while the server is down, and a pulled flash drive's file still goes since it was read
at once. Run from the app folder: python tests/sim_drive_reads.py"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils import drive_reads, usb_drive as u  # noqa: E402

failed = 0


def check(name, ok):
    global failed
    print(("ok   " if ok else "FAIL ") + name)
    failed += not ok


class FakeServer:
    def __init__(self):
        self.files, self.reads, self.down, self.sent_bytes = {}, [], False, 0

    def __call__(self):
        if self.down:
            raise OSError("no route to host")
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def commit(self):
        pass

    def execute(self, sql, args):
        server = self

        class Result:
            def fetchone(self):
                return (1,) if args[0] in server.files else None
        if sql.startswith("SELECT 1 FROM cnc_drive_files"):
            return Result()
        if sql.startswith("INSERT INTO cnc_drive_files"):
            self.files.setdefault(args[0], args[1])
            self.sent_bytes += len(args[1])
        elif sql.startswith("INSERT INTO cnc_drive_reads"):
            self.reads.append(args)
        return Result()


def wait(cond, seconds=3):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not cond():
        time.sleep(0.02)
    return cond()


tmp = tempfile.mkdtemp()
pi, usb = f"{tmp}/pi", f"{tmp}/usb"
os.makedirs(pi)
os.makedirs(usb)
program = b"%\nO07777 (TEST)\n(T1 | 1/4 FLAT ENDMILL | H1)\nM30\n%\n"
with open(f"{pi}/O07777.NC", "wb") as f:
    f.write(program)
with open(f"{usb}/zz_test_1.nc", "wb") as f:
    f.write(program)   # the same bytes under another name

server = FakeServer()
drive_reads.RETRY_SECONDS = 0.2
reads = drive_reads.ReadReporter("zz_test_machine", connect=server)
reads.start()
v = u.Volume([("LOCAL", pi), ("USB", usb)], disk_bytes=48 * 1024 * 1024, cluster_sectors=1)
v.on_read = reads.report


def first_sector(path):
    n = v.root
    for part in path.split("/"):
        n = next(c for c in n.children if c.name == part)
    return v.data_start + (n.first - 2) * v.spc


v.sector(first_sector("LOCAL/O07777.NC"))
check("a read is sent", wait(lambda: len(server.reads) == 1))
when, = {r[1] for r in server.reads}
check("with the machine, name and hash", server.reads[0][0] == "zz_test_machine" and server.reads[0][2] == "LOCAL/O07777.NC"
      and server.reads[0][3] in server.files and when.tzinfo is not None)
check("with the file's bytes", list(server.files.values()) == [program])

v.sector(first_sector("LOCAL/O07777.NC"))
time.sleep(0.2)
check("a read again within 30 s isn't sent", len(server.reads) == 1)

server.down = True
v.sector(first_sector("USB/zz_test_1.nc"))
time.sleep(0.2)
os.remove(f"{usb}/zz_test_1.nc")   # the flash drive is pulled
check("nothing is sent while the server is down", len(server.reads) == 1)
server.down = False
check("a pulled flash drive's read goes once the server answers", wait(lambda: len(server.reads) == 2))
check("the same bytes aren't sent twice", server.sent_bytes == len(program) and server.reads[1][2] == "USB/zz_test_1.nc")

reads.stop()
print("all passed" if not failed else f"{failed} failed")
sys.exit(1 if failed else 0)
