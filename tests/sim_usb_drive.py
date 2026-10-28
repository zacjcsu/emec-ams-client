"""Checks utils/usb_drive.py without a gadget or NBD: the made-up drive passes fsck.fat (if installed), shows the
files with their bytes, and answers NBD requests. Run from the app folder: python tests/sim_usb_drive.py"""
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils import usb_drive as u  # noqa: E402

failed = 0


def check(name, ok):
    global failed
    print(("ok   " if ok else "FAIL ") + name)
    failed += not ok


tmp = tempfile.mkdtemp()
pi, usb = f"{tmp}/pi", f"{tmp}/usb"
os.makedirs(f"{pi}/Job 1")
os.makedirs(f"{usb}/deep/er")
files = {f"{pi}/O01000.NC": b"%\nO01000 (TEST)\nM30\n%\n",
         f"{pi}/Job 1/Operation 1.nc": b"G0 X0\n" * 1000,
         f"{usb}/zz_test_1.nc": os.urandom(100000),
         f"{usb}/deep/er/Teil ä.nc": b"unicode",
         f"{usb}/empty.nc": b"",
         f"{usb}/.hidden": b"left out"}
for path, data in files.items():
    with open(path, "wb") as f:
        f.write(data)

v = u.Volume([("LOCAL", pi), ("USB", usb)], disk_bytes=48 * 1024 * 1024, cluster_sectors=1)
image = b"".join(v.sector(n) for n in range(v.total))
check("partition table", image[510:512] == b"\x55\xaa" and image[450] == 0x0C)
boot = image[u.PART_START * 512:u.PART_START * 512 + 512]
check("FAT32 boot sector", boot[82:90] == b"FAT32   " and boot[510:512] == b"\x55\xaa")
check("read() matches the sectors", v.read(u.PART_START * 512 + 100, 1000) == image[u.PART_START * 512 + 100:
                                                                                      u.PART_START * 512 + 1100])


def node(path):
    n = v.root
    for part in path.split("/"):
        n = next(c for c in n.children if c.name == part)
    return n


for path, data in files.items():
    rel = path[len(tmp) + 1:].replace("pi/", "LOCAL/", 1).replace("usb/", "USB/", 1)
    if "/." in rel:
        check(f"{rel} left out", not any(c.name == ".hidden" for c in node("USB").children))
        continue
    n = node(rel)
    start = (v.data_start + (n.first - 2) * v.spc) * 512
    check(f"{rel} has its bytes", n.size == len(data) and (not data or image[start:start + len(data)] == data))
check("long and short names", node("LOCAL/Job 1/Operation 1.nc").short == b"OPERAT~1NC "
      and node("LOCAL/O01000.NC").short == b"O01000  NC " and not node("LOCAL/O01000.NC").lfn)

if shutil.which("fsck.fat") or os.path.exists("/usr/sbin/fsck.fat"):
    part = f"{tmp}/part.img"
    with open(part, "wb") as f:
        f.write(image[u.PART_START * 512:])
    r = subprocess.run([shutil.which("fsck.fat") or "/usr/sbin/fsck.fat", "-n", part], capture_output=True, text=True)
    check("fsck.fat finds nothing wrong", r.returncode == 0 and "files," in r.stdout)
else:
    print("skip fsck.fat, not installed")

small = u.Volume([("LOCAL", None), ("USB", usb)], disk_bytes=34 * 1024 * 1024, cluster_sectors=1)
check("an empty LOCAL folder", small.root.children[0].children == [])

big = f"{tmp}/big"
os.makedirs(big)
with open(f"{big}/huge.nc", "wb") as f:
    f.truncate(40 * 1024 * 1024)
dropped = u.Volume([("LOCAL", None), ("USB", big)], disk_bytes=34 * 1024 * 1024, cluster_sectors=1)
check("a file that doesn't fit is left out", dropped.root.children[1].children == [])

full = u.Volume([("LOCAL", pi), ("USB", usb)])
check("4 GiB drive", full.total == u.DISK_BYTES // 512 and full.cluster_count >= 65525)

mine, theirs = socket.socketpair()
threading.Thread(target=u.serve_nbd, args=(mine, v.read), daemon=True).start()


def request(kind, offset, length, payload=b""):
    theirs.sendall(struct.pack(">II8sQI", u.NBD_REQUEST_MAGIC, kind, b"handle01", offset, length) + payload)
    magic, error, handle = struct.unpack(">II8s", u._recv(theirs, 16))
    data = u._recv(theirs, length) if kind == u.NBD_CMD_READ and not error else b""
    return magic == u.NBD_REPLY_MAGIC and handle == b"handle01", error, data


ok, error, data = request(u.NBD_CMD_READ, 4096, 8192)
check("NBD read", ok and not error and data == image[4096:4096 + 8192])
ok, error, _ = request(u.NBD_CMD_WRITE, 0, 512, bytes(512))
check("NBD write refused", ok and error == u.EPERM)
ok, error, _ = request(u.NBD_CMD_FLUSH, 0, 0)
check("NBD flush", ok and not error)
theirs.sendall(struct.pack(">II8sQI", u.NBD_REQUEST_MAGIC, u.NBD_CMD_DISC, b"handle01", 0, 0))

sig = u.signature(pi)
with open(f"{pi}/O01001.NC", "wb") as f:
    f.write(b"%\n%\n")
check("a new program changes the signature", u.signature(pi) != sig)

# A restart for new code waits for the control to sit idle with no drive program open
state, program = f"{tmp}/run_state", f"{tmp}/run_program"
d = u.UsbDrive(pi, state, run_program_file=program)


def control(s, p=""):
    with open(state, "w") as f:
        f.write(s)
    with open(program, "w") as f:
        f.write(p)


control("Idle", "O01001")
check("idle just now isn't settled", not d.settled(now=1000))
check("idle 15 minutes is", d.settled(now=1000 + u.RESTART_IDLE_SECONDS))
for s, p, why in (("Feed hold", "O07777", "feed hold"), ("Off", "", "off"), ("", "", "unknown"),
                  ("Idle", u.FROM_DRIVE, "a drive program open")):
    control(s, p)
    check(f"{why} isn't settled", not d.settled(now=5000))
    control("Idle", "O01001")
    check(f"and idle starts over after {why}", not d.settled(now=5001) and d.settled(now=5001 + u.RESTART_IDLE_SECONDS))
    d._idle_since = None
control("Idle", "O01001")
os.utime(state, (time.time() - u.RUN_STATE_MAX_AGE - 1,) * 2)
check("a stale idle isn't settled", not d.settled(now=9000))
control("Feed hold")
check("files still wait while running", u.machine_running(state))
control("Off")
check("files can change with the control off", not u.machine_running(state))

shutil.rmtree(tmp)
print(f"{failed} failed" if failed else "all passed")
sys.exit(1 if failed else 0)
