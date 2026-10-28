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
        self.drives, self.stick, self.seen = {}, {}, set()

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
        elif sql.startswith("INSERT INTO cnc_usb_drives"):
            self.drives[args[0]] = None
        elif sql.startswith("UPDATE cnc_usb_drives"):
            self.drives[args[1]] = "in" if args[0] else None
        elif sql.startswith("DELETE FROM cnc_stick_files"):
            self.stick = {}
        elif sql.lstrip().startswith("INSERT INTO cnc_stick_files"):
            m, limit, names, folders, sizes, _ = args
            for n, f, z in zip(names, folders, sizes):
                self.stick[n] = {"folder": f, "size": z, "sha": None, "unsent": z > limit and not f}
        elif sql.startswith("UPDATE cnc_drive_files SET seen_at"):
            self.seen.add(args[0])
        elif sql.startswith("UPDATE cnc_stick_files SET sha256"):
            self.stick[args[2]]["sha"] = args[0]
        elif sql.startswith("UPDATE cnc_stick_files SET unsent"):
            self.stick[args[1]]["unsent"] = True
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

v.sector(first_sector("LOCAL/O07777.NC"))
with open(f"{pi}/setup.exe", "wb") as f:
    f.write(b"MZ")
v2 = u.Volume([("LOCAL", pi), ("USB", usb)], disk_bytes=48 * 1024 * 1024, cluster_sectors=1)
v2.on_read = reads.report
v = v2
v.sector(first_sector("LOCAL/setup.exe"))
time.sleep(0.3)
check("a read of a file that isn't a program isn't sent", len(server.reads) == 2)
reads.stop()

# The flash drive's list, and its programs' bytes while the control is idle
os.makedirs(f"{usb}/zz_folder")
second = program.replace(b"TEST", b"TEST 2")
with open(f"{usb}/zz_folder/zz_test_2.nc", "wb") as f:
    f.write(second)
with open(f"{usb}/zz_setup.exe", "wb") as f:
    f.write(b"MZ" * 10)
with open(f"{usb}/zz_big.nc", "wb") as f:
    f.write(b"x" * 300)
idle = [False]
server = FakeServer()
drive_reads.MAX_BYTES = 200
stick = drive_reads.ReadReporter("zz_test_machine", connect=server, can_read=lambda: idle[0])
drive_reads.IDLE_CHECK_SECONDS = 0.1
stick.start()
check("the drive is registered", wait(lambda: "zz_test_machine" in server.drives))
d = u.UsbDrive(pi, f"{tmp}/run_state", on_build=stick.stick)
d.stick.device, d.stick.folder = "/dev/zz", usb
d._build()
check("the stick is listed", wait(lambda: set(server.stick) == {"zz_big.nc", "zz_folder", "zz_folder/zz_test_2.nc",
                                                                 "zz_setup.exe"}) and server.drives["zz_test_machine"])
time.sleep(0.3)
check("no bytes while the control isn't idle", not server.files and not any(r["sha"] for r in server.stick.values()))
idle[0] = True
check("a program's bytes go once it's idle", wait(lambda: server.stick["zz_folder/zz_test_2.nc"]["sha"] in server.files))
check("and are marked seen", server.stick["zz_folder/zz_test_2.nc"]["sha"] in server.seen)
check("a file that isn't a program has no bytes", server.stick["zz_setup.exe"]["sha"] is None
      and len(server.files) == 1)
check("one over the limit is unsent", server.stick["zz_big.nc"]["unsent"] and server.stick["zz_big.nc"]["sha"] is None)
sent = server.sent_bytes
d._build()   # a change to LOCAL builds again with the same stick
d.stick.device, d.stick.folder = None, None
time.sleep(0.3)
d._build()
check("the list is emptied when the stick comes out", wait(lambda: not server.stick)
      and server.drives["zz_test_machine"] is None)
d.stick.device, d.stick.folder = "/dev/zz", usb
d._build()
check("put back, it's listed again without sending bytes again",
      wait(lambda: (server.stick.get("zz_folder/zz_test_2.nc") or {}).get("sha")) and server.sent_bytes == sent)
stick.stop()
print("all passed" if not failed else f"{failed} failed")
sys.exit(1 if failed else 0)
