"""Shows the machine one USB drive with two folders: LOCAL, the dashboard's programs in PROGRAM_FOLDER, and USB, the
flash drive plugged into the Pi, left out while there's none. The Pi's USB-C port acts as the drive (USB gadget mode).
The pendant's USB jack is wired to one of the Pi's USB-A ports.

USB storage works in 512-byte sectors, not files. The drive is made up as it's read: the partition table, the FAT32
boot sector, the FATs and the folders come from the file names and sizes, and a file's sectors are read from the file
when the machine asks for them. Nothing is copied. Writes are refused, so the machine can't save to it.

A change to either folder rebuilds the drive, and the machine sees it unplugged and plugged back in. A classic Haas
keeps showing the old folders with no signal, and handles a medium swap badly. A program can run straight off the drive
(FNC), so that waits until the control is idle, as the app writes to RUN_STATE_FILE, and has read nothing for
QUIET_SECONDS. Until then a file that's gone, like one on a pulled flash drive, answers with a read error.

It needs root for the gadget (configfs), the block device (/dev/nbd0) and mounting the flash drive, so it runs as its
own service. usb_drive_setup.sh installs it. Each file the machine reads is logged to logs/usb_drive.log. When this
file changes, it starts itself again once the drive could change, so new code needs no sudo.
Run it with: sudo .venv/bin/python -m utils.usb_drive
"""
import bisect
import fcntl
import logging
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from logging.handlers import TimedRotatingFileHandler

logger = logging.getLogger("usb_drive")

SECTOR = 512
PART_START = 2048           # sectors before the partition, as on a bought flash drive
RESERVED = 32
FATS = 2
CLUSTER_SECTORS = 8         # 4 KiB clusters
DISK_BYTES = 4 * 1024 ** 3  # what the machine sees. Files that don't fit are left out
EOC = 0x0FFFFFFF
MAX_ENTRIES = 10000         # files and folders taken from one folder tree
MAX_DEPTH = 8
SKIP = {"system volume information", "$recycle.bin"}
QUIET_SECONDS = 5
RUN_STATE_MAX_AGE = 90   # an older run state file means the app isn't running, so the state is unknown
POLL_SECONDS = 0.25
READ_LOG_SECONDS = 30       # a file read again within this isn't logged again
STICK_DIR = "/run/emec-usb-drive/stick"
GADGET = "/sys/kernel/config/usb_gadget/emec"
NBD_DEVICE = "/dev/nbd0"
RUN_STATE_FILE = "data/run_state"   # written by utils/live_state.py
STICK_DELAY = "/sys/module/usb_storage/parameters/delay_use"

# linux/nbd.h and linux/fs.h
NBD_SET_SOCK, NBD_SET_BLKSIZE, NBD_DO_IT, NBD_CLEAR_SOCK, NBD_CLEAR_QUE = 0xAB00, 0xAB01, 0xAB03, 0xAB04, 0xAB05
NBD_SET_SIZE_BLOCKS, NBD_DISCONNECT, NBD_SET_TIMEOUT, NBD_SET_FLAGS = 0xAB07, 0xAB08, 0xAB09, 0xAB0A
NBD_FLAG_HAS_FLAGS, NBD_FLAG_READ_ONLY = 1, 2
NBD_CMD_READ, NBD_CMD_WRITE, NBD_CMD_DISC, NBD_CMD_FLUSH, NBD_CMD_TRIM = 0, 1, 2, 3, 4
NBD_REQUEST_MAGIC, NBD_REPLY_MAGIC = 0x25609513, 0x67446698
BLKFLSBUF = 0x1261
EPERM, EIO, EINVAL = 1, 5, 22

_SHORT_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!#$%&'()-@^_`{}~")


class Node:
    """A file, or a folder when children isn't None. path is where its bytes come from."""

    def __init__(self, name, path=None, size=0, mtime=0.0, children=None):
        self.name, self.path, self.size, self.mtime, self.children = name, path, size, mtime, children
        self.short = b""
        self.lfn = False        # needs a long name entry
        self.first = 0          # first cluster, 0 for an empty file
        self.clusters = 0
        self.parent = None
        self.shown = ""         # its path on the drive, for the log
        self.entries = b""      # a folder's directory entries


def scan(folder, depth=0, budget=None):
    """The files and folders under folder, sorted, without hidden ones and links. budget limits how many."""
    budget = budget if budget is not None else [MAX_ENTRIES]
    out = []
    try:
        entries = sorted(os.scandir(folder), key=lambda e: e.name.casefold())
    except OSError as e:
        logger.info(f"Cannot list {folder}: {e}")
        return out
    for e in entries:
        if budget[0] <= 0:
            break
        if e.name.startswith(".") or e.name.casefold() in SKIP or e.is_symlink():
            continue
        try:
            st = e.stat()
            if e.is_dir():
                if depth < MAX_DEPTH:
                    budget[0] -= 1
                    out.append(Node(e.name, e.path, mtime=st.st_mtime, children=scan(e.path, depth + 1, budget)))
            elif e.is_file() and st.st_size < 2 ** 32:
                budget[0] -= 1
                out.append(Node(e.name, e.path, st.st_size, st.st_mtime))
        except OSError:
            continue
    return out


def signature(folder):
    """Changes when anything the drive shows from folder changes."""
    out = []
    for root, dirs, files in os.walk(folder) if folder else ():
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            try:
                st = os.stat(os.path.join(root, name))
                out.append((root, name, st.st_size, st.st_mtime_ns))
            except OSError:
                pass
    return tuple(out)


def _fat_date(t):
    lt = time.localtime(t)
    year = min(max(lt.tm_year, 1980), 2107)
    return ((year - 1980) << 9) | (lt.tm_mon << 5) | lt.tm_mday, (lt.tm_hour << 11) | (lt.tm_min << 5) | (lt.tm_sec // 2)


def _short_part(text, length):
    return "".join(c if c in _SHORT_OK else "_" for c in text.upper().replace(" ", "").replace(".", ""))[:length]


def short_name(name, used):
    """The 8.3 name for name, unique among used, as 11 bytes, and whether name needs a long name entry too."""
    base, dot, ext = name.rpartition(".") if "." in name.lstrip(".") else (name, "", "")
    b, x = _short_part(base, 8), _short_part(ext, 3)
    plain = (b.ljust(8) + x.ljust(3)).encode("ascii")
    if b and name.upper() == name and b + ("." + x if x else "") == name and plain not in used:
        return plain, False
    for n in range(1, 1000000):
        tail = f"~{n}"
        candidate = ((b or "_")[:8 - len(tail)] + tail).ljust(8) + x.ljust(3)
        candidate = candidate.encode("ascii")
        if candidate not in used:
            return candidate, True
    raise ValueError("no short name left")


def _checksum(short):
    s = 0
    for c in short:
        s = (((s & 1) << 7) + (s >> 1) + c) & 0xFF
    return s


def _lfn_entries(name, short):
    chars = list(name.encode("utf-16-le"))
    units = [bytes(chars[i:i + 2]) for i in range(0, len(chars), 2)]
    if len(units) % 13:
        units += [b"\x00\x00"] + [b"\xff\xff"] * (12 - len(units) % 13)
    chunks = [b"".join(units[i:i + 13]) for i in range(0, len(units), 13)]
    check = _checksum(short)
    out = []
    for i, chunk in enumerate(chunks, 1):
        seq = i | (0x40 if i == len(chunks) else 0)
        out.append(struct.pack("<B10sBBB12sH4s", seq, chunk[:10], 0x0F, 0, check, chunk[10:22], 0, chunk[22:26]))
    return b"".join(reversed(out))


def _dirent(short, attr, cluster, size, mtime):
    date, tod = _fat_date(mtime)
    return struct.pack("<11sBBBHHHHHHHI", short, attr, 0, 0, tod, date, date, cluster >> 16, tod, date,
                       cluster & 0xFFFF, size)


class Volume:
    """The drive's bytes, made up from folders: [(name on the drive, folder on the Pi or None), ...]."""

    def __init__(self, folders, disk_bytes=DISK_BYTES, cluster_sectors=CLUSTER_SECTORS, label="EMEC"):
        self.total = disk_bytes // SECTOR
        self.part = self.total - PART_START
        self.spc = cluster_sectors
        self.cluster_bytes = cluster_sectors * SECTOR
        fat = 1
        while True:
            clusters = (self.part - RESERVED - FATS * fat) // self.spc
            need = -(-(clusters + 2) * 4 // SECTOR)
            if need <= fat:
                break
            fat = need
        if clusters < 65525:
            raise ValueError("too small for FAT32")
        self.fat_sectors, self.cluster_count = fat, clusters
        self.fat_start = PART_START + RESERVED
        self.data_start = self.fat_start + FATS * fat
        self.label = label.upper()[:11].ljust(11).encode("ascii")
        self.volume_id = int(time.time()) & 0xFFFFFFFF
        self.root = Node("", children=[Node(name, path, mtime=time.time(), children=scan(path) if path else [])
                                       for name, path in folders])
        self._name(self.root, "")
        self._allocate()
        self._files = {}           # path: fd, the last few read
        self._logged = {}          # shown path: when its read was logged
        self.last_read = 0.0

    def _name(self, folder, shown):
        used = set()
        for child in folder.children:
            child.parent = folder
            child.short, child.lfn = short_name(child.name, used)
            used.add(child.short)
            child.shown = f"{shown}/{child.name}" if shown else child.name
            if child.children is not None:
                self._name(child, child.shown)

    def _dir_size(self, folder):
        n = 1 if folder is self.root else 2   # the label, or . and ..
        for c in folder.children:
            n += 1 + (len(_lfn_entries(c.name, c.short)) // 32 if c.lfn else 0)
        return n * 32

    def _allocate(self):
        """Folders and files get runs of clusters in tree order. Files past the end are dropped."""
        self.extents = []          # (first cluster, clusters, node), sorted
        next_cluster, last = 2, self.cluster_count + 1
        order = []

        def walk(folder):
            order.append(folder)
            for c in folder.children:
                if c.children is not None:
                    walk(c)
                else:
                    order.append(c)
        walk(self.root)
        dropped = set()
        dir_clusters = sum(-(-self._dir_size(f) // self.cluster_bytes) for f in order if f.children is not None)
        room = last - 1 - dir_clusters
        for node in order:
            if node.children is None and node.size:
                need = -(-node.size // self.cluster_bytes)
                if need > room:
                    dropped.add(id(node))
                    continue
                room -= need
        if dropped:
            logger.info(f"{len(dropped)} files don't fit on the drive and are left out")
            for f in order:
                if f.children is not None:
                    f.children = [c for c in f.children if id(c) not in dropped]
            order = [n for n in order if id(n) not in dropped]
        for node in order:
            size = self._dir_size(node) if node.children is not None else node.size
            node.clusters = -(-size // self.cluster_bytes) if size else 0
            if node.children is not None:
                node.clusters = max(node.clusters, 1)
            if node.clusters:
                node.first = next_cluster
                next_cluster += node.clusters
                self.extents.append((node.first, node.clusters, node))
        self.used = next_cluster - 2
        self._starts = [e[0] for e in self.extents]
        for node in order:
            if node.children is not None:
                node.entries = self._entries(node)

    def _entries(self, folder):
        out = []
        if folder is self.root:
            out.append(_dirent(self.label, 0x08, 0, 0, time.time()))
        else:
            parent = 0 if folder.parent is self.root else folder.parent.first
            out.append(_dirent(b".".ljust(11), 0x10, folder.first, 0, folder.mtime))
            out.append(_dirent(b"..".ljust(11), 0x10, parent, 0, folder.mtime))
        for c in folder.children:
            if c.lfn:
                out.append(_lfn_entries(c.name, c.short))
            if c.children is not None:
                out.append(_dirent(c.short, 0x10 | 0x01, c.first, 0, c.mtime))
            else:
                out.append(_dirent(c.short, 0x20 | 0x01, c.first, c.size, c.mtime))
        return b"".join(out)

    # The sectors

    def _mbr(self):
        entry = struct.pack("<B3sB3sII", 0, b"\xfe\xff\xff", 0x0C, b"\xfe\xff\xff", PART_START, self.part)
        mbr = bytearray(SECTOR)
        struct.pack_into("<I", mbr, 440, self.volume_id)
        mbr[446:462] = entry
        mbr[510:512] = b"\x55\xaa"
        return bytes(mbr)

    def _boot(self):
        b = bytearray(SECTOR)
        struct.pack_into("<3s8sHBHBHHBHHHII", b, 0, b"\xeb\x58\x90", b"MSDOS5.0", SECTOR, self.spc, RESERVED, FATS, 0, 0,
                         0xF8, 0, 63, 255, PART_START, self.part)
        struct.pack_into("<IHHIHH12sBBBI11s8s", b, 36, self.fat_sectors, 0, 0, 2, 1, 6, bytes(12), 0x80, 0, 0x29,
                         self.volume_id, self.label, b"FAT32   ")
        b[510:512] = b"\x55\xaa"
        return bytes(b)

    def _fsinfo(self):
        b = bytearray(SECTOR)
        struct.pack_into("<I", b, 0, 0x41615252)
        struct.pack_into("<IIII", b, 484, 0x61417272, self.cluster_count - self.used, 2 + self.used, 0)
        struct.pack_into("<I", b, 508, 0xAA550000)
        return bytes(b)

    def _find(self, cluster):
        i = bisect.bisect_right(self._starts, cluster) - 1
        if i >= 0:
            first, count, node = self.extents[i]
            if cluster < first + count:
                return first, count, node
        return None

    def _fat(self, index):
        out = []
        for cluster in range(index * 128, index * 128 + 128):
            if cluster < 2:
                out.append(0x0FFFFFF8 if cluster == 0 else EOC)
                continue
            hit = self._find(cluster)
            out.append(0 if not hit else EOC if cluster == hit[0] + hit[1] - 1 else cluster + 1)
        return struct.pack("<128I", *out)

    def _file(self, node, offset, length):
        fd = self._files.get(node.path)
        try:
            if fd is None:
                if len(self._files) >= 4:
                    os.close(self._files.pop(next(iter(self._files))))
                fd = self._files[node.path] = os.open(node.path, os.O_RDONLY)
            data = os.pread(fd, length, offset)
        except OSError:
            self._files.pop(node.path, None)
            raise
        return data.ljust(length, b"\x00")

    def _logged_read(self, node):
        now = time.monotonic()
        if now - self._logged.get(node.shown, -READ_LOG_SECONDS) >= READ_LOG_SECONDS:
            logger.info(f"Read {node.shown}")
        self._logged[node.shown] = now

    def sector(self, n):
        if n == 0:
            return self._mbr()
        if n < PART_START or n >= self.total:
            return bytes(SECTOR)
        if n < self.fat_start:
            rel = n - PART_START
            return {0: self._boot, 1: self._fsinfo, 6: self._boot, 7: self._fsinfo}.get(rel, lambda: bytes(SECTOR))()
        if n < self.data_start:
            return self._fat((n - self.fat_start) % self.fat_sectors)
        cluster, within = divmod(n - self.data_start, self.spc)
        hit = self._find(cluster + 2)
        if not hit:
            return bytes(SECTOR)
        first, _, node = hit
        offset = (cluster + 2 - first) * self.cluster_bytes + within * SECTOR
        if node.children is not None:
            return node.entries[offset:offset + SECTOR].ljust(SECTOR, b"\x00")
        if offset == 0:
            self._logged_read(node)
        return self._file(node, offset, SECTOR) if offset < node.size else bytes(SECTOR)

    def read(self, offset, length):
        self.last_read = time.monotonic()
        first, end = offset // SECTOR, -(-(offset + length) // SECTOR)
        data = b"".join(self.sector(n) for n in range(first, end))
        start = offset - first * SECTOR
        return data[start:start + length]

    def close(self):
        for fd in self._files.values():
            os.close(fd)
        self._files = {}


def _recv(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data


def serve_nbd(sock, read):
    """Answers the kernel's NBD requests on sock until it disconnects. read(offset, length) gives the bytes."""
    while True:
        magic, kind, handle, offset, length = struct.unpack(">II8sQI", _recv(sock, 28))
        if magic != NBD_REQUEST_MAGIC:
            raise ConnectionError(f"bad request magic {magic:#x}")
        kind &= 0xFFFF
        if kind == NBD_CMD_DISC:
            return
        if kind == NBD_CMD_READ:
            try:
                data, error = read(offset, length), 0
            except Exception:
                logger.exception("Read failed")
                data, error = b"", EIO
            sock.sendall(struct.pack(">II8s", NBD_REPLY_MAGIC, error, handle) + (data if not error else b""))
        elif kind == NBD_CMD_WRITE:
            _recv(sock, length)
            sock.sendall(struct.pack(">II8s", NBD_REPLY_MAGIC, EPERM, handle))
        elif kind in (NBD_CMD_FLUSH, NBD_CMD_TRIM):
            sock.sendall(struct.pack(">II8s", NBD_REPLY_MAGIC, 0, handle))
        else:
            sock.sendall(struct.pack(">II8s", NBD_REPLY_MAGIC, EINVAL, handle))


class NbdDevice:
    """NBD_DEVICE, answered by this process through a socket pair, without nbd-client."""

    def __init__(self, read, size, device=NBD_DEVICE):
        self.read, self.size, self.device = read, size, device
        self.fd = None
        self.stopped = threading.Event()

    def start(self):
        subprocess.run(["modprobe", "nbd", "nbds_max=2"], check=True)
        self.fd = os.open(self.device, os.O_RDWR)
        mine, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        fcntl.ioctl(self.fd, NBD_CLEAR_SOCK)
        fcntl.ioctl(self.fd, NBD_SET_BLKSIZE, SECTOR)
        fcntl.ioctl(self.fd, NBD_SET_SIZE_BLOCKS, self.size // SECTOR)
        fcntl.ioctl(self.fd, NBD_SET_FLAGS, NBD_FLAG_HAS_FLAGS | NBD_FLAG_READ_ONLY)
        fcntl.ioctl(self.fd, NBD_SET_TIMEOUT, 30)
        fcntl.ioctl(self.fd, NBD_SET_SOCK, theirs.fileno())
        self._sock, self._theirs = mine, theirs
        threading.Thread(target=self._do_it, name="nbd-kernel", daemon=True).start()
        threading.Thread(target=self._serve, name="nbd-serve", daemon=True).start()
        self._wait_for_size()

    def _wait_for_size(self, timeout=10):
        """The kernel sets the size only once NBD_DO_IT runs, then reads the partition table. The gadget can't open the
        device before the size, and opening it during the scan makes NBD_DO_IT fail with EBUSY."""
        name = os.path.basename(self.device)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.stopped.is_set():
            with open(f"/sys/block/{name}/size") as f:
                sized = int(f.read()) > 0
            if sized and os.path.exists(f"/sys/block/{name}/{name}p1"):
                return
            time.sleep(0.05)
        if not sized or self.stopped.is_set():
            raise OSError(f"{self.device} didn't start")
        logger.info(f"{self.device} has no partition yet")

    def _do_it(self):
        # A signal interrupts NBD_DO_IT, and the kernel then shuts the socket. Python handles them on the main thread.
        signal.pthread_sigmask(signal.SIG_BLOCK, signal.valid_signals())
        try:
            fcntl.ioctl(self.fd, NBD_DO_IT)   # returns when the device disconnects
        except OSError as e:
            logger.info(f"NBD stopped: {e}")
        finally:
            self.stopped.set()
            for request in (NBD_CLEAR_QUE, NBD_CLEAR_SOCK):
                try:
                    fcntl.ioctl(self.fd, request)
                except OSError:
                    pass

    def _serve(self):
        signal.pthread_sigmask(signal.SIG_BLOCK, signal.valid_signals())
        try:
            serve_nbd(self._sock, self.read)
        except (OSError, ConnectionError) as e:
            logger.info(f"NBD connection ended: {e}")

    def flush(self):
        """Drops the kernel's cached sectors, so a rebuilt drive is read afresh."""
        fcntl.ioctl(self.fd, BLKFLSBUF, 0)

    def stop(self):
        if self.fd is not None:
            try:
                fcntl.ioctl(self.fd, NBD_DISCONNECT)
            except OSError:
                pass


def _write(path, value):
    with open(path, "w") as f:
        f.write(value)


class Gadget:
    """The USB-C port as a read-only flash drive backed by NBD_DEVICE, through configfs."""

    def __init__(self, backing=NBD_DEVICE, root=GADGET):
        self.backing, self.root = backing, root
        self.lun = f"{root}/functions/mass_storage.0/lun.0"

    def setup(self):
        subprocess.run(["modprobe", "libcomposite"], check=True)
        if os.path.isdir(self.root):
            self.teardown()
        os.makedirs(f"{self.root}/strings/0x409")
        for name, value in (("idVendor", "0x1d6b"), ("idProduct", "0x0104"), ("bcdDevice", "0x0100"),
                            ("bcdUSB", "0x0200"), ("strings/0x409/manufacturer", "EMEC"),
                            ("strings/0x409/product", "EMEC Drive"), ("strings/0x409/serialnumber", "EMEC0001")):
            _write(f"{self.root}/{name}", value)
        os.makedirs(f"{self.root}/configs/c.1/strings/0x409")
        _write(f"{self.root}/configs/c.1/strings/0x409/configuration", "Drive")
        _write(f"{self.root}/configs/c.1/bmAttributes", "0xC0")   # self powered, from PoE
        _write(f"{self.root}/configs/c.1/MaxPower", "2")
        os.makedirs(self.lun.rsplit("/", 1)[0])
        for name, value in (("removable", "1"), ("ro", "1"), ("cdrom", "0"), ("nofua", "1")):
            _write(f"{self.lun}/{name}", value)
        os.symlink(f"{self.root}/functions/mass_storage.0", f"{self.root}/configs/c.1/mass_storage.0")

    def attach(self):
        _write(f"{self.lun}/file", self.backing)
        udcs = os.listdir("/sys/class/udc")
        if not udcs:
            raise OSError("no USB device controller. Is dtoverlay=dwc2,dr_mode=peripheral in config.txt?")
        _write(f"{self.root}/UDC", udcs[0])

    def detach(self):
        try:
            _write(f"{self.root}/UDC", "\n")
        except OSError:
            pass
        _write(f"{self.lun}/file", "\n")

    def teardown(self):
        try:
            self.detach()
        except OSError:
            pass
        for path in (f"{self.root}/configs/c.1/mass_storage.0",):
            if os.path.islink(path):
                os.unlink(path)
        for path in (f"{self.root}/configs/c.1/strings/0x409", f"{self.root}/configs/c.1",
                     f"{self.root}/functions/mass_storage.0", f"{self.root}/strings/0x409", self.root):
            if os.path.isdir(path):
                os.rmdir(path)


def find_stick():
    """The first USB flash drive's first partition, or the whole drive if it has none. None without one."""
    for name in sorted(os.listdir("/sys/block")):
        if not name.startswith("sd") or "/usb" not in os.path.realpath(f"/sys/block/{name}"):
            continue
        parts = sorted(p for p in os.listdir(f"/sys/block/{name}") if p.startswith(name))
        return f"/dev/{parts[0] if parts else name}"
    return None


class Stick:
    """Mounts the flash drive read-only at STICK_DIR while it's plugged in."""

    def __init__(self, folder=STICK_DIR):
        self.folder = folder
        self.device = None
        self._failed = None
        self._seen = None

    def check(self):
        device = find_stick()
        if not device:
            self._failed = None
        if device == self.device:
            return
        if self.device:
            self.unmount()
        if device != self._seen:   # partitions come and go over a moment, so a device must stay for two checks
            self._seen = device
            return
        if device and device != self._failed:
            os.makedirs(self.folder, exist_ok=True)
            options = "ro,nosuid,nodev,noexec,noatime"
            for extra in (",utf8", ""):   # utf8 is only for FAT
                if subprocess.run(["mount", "-o", options + extra, device, self.folder],
                                  capture_output=True).returncode == 0:
                    self.device, self._failed = device, None
                    logger.info(f"Flash drive {device} mounted")
                    return
            logger.info(f"Cannot mount the flash drive {device}")
            self._failed = device

    def unmount(self):
        subprocess.run(["umount", "-l", self.folder], capture_output=True)
        logger.info(f"Flash drive {self.device} removed")
        self.device = None

    @property
    def path(self):
        return self.folder if self.device else None


def _code_stamp():
    try:
        st = os.stat(__file__)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def machine_running(run_state_file, now=None):
    """Whether the control is anything but idle or off. An unknown state counts as idle."""
    try:
        if (now or time.time()) - os.stat(run_state_file).st_mtime > RUN_STATE_MAX_AGE:
            return False
        with open(run_state_file) as f:
            state = f.read().strip()
    except OSError:
        return False
    return state not in ("", "Idle", "Off")


class UsbDrive:
    def __init__(self, program_folder, run_state_file):
        self.program_folder = program_folder
        self.run_state_file = run_state_file
        self.stick = Stick()
        self.volume = None
        self.nbd = NbdDevice(self._read, DISK_BYTES)
        self.gadget = Gadget()
        self._stop = threading.Event()

    def _read(self, offset, length):
        return self.volume.read(offset, length)

    def _build(self):
        folders = [("LOCAL", self.program_folder)] + ([("USB", self.stick.path)] if self.stick.path else [])
        volume = Volume(folders)
        counts = ", ".join(f"{sum(1 for _ in _files(n))} in {n.name}" for n in volume.root.children)
        logger.info(f"Drive built: {counts}")
        return volume

    def _signature(self):
        return self.stick.device, signature(self.program_folder)

    def run(self):
        try:
            subprocess.run(["modprobe", "usb_storage"], check=True)
            _write(STICK_DELAY, "0")   # the kernel waits 1 s by default before reading a new flash drive
        except (OSError, subprocess.CalledProcessError) as e:
            logger.info(f"Cannot drop the flash drive delay: {e}")
        self.stick.check()
        time.sleep(POLL_SECONDS)
        self.stick.check()
        self.volume, shown = self._build(), self._signature()
        self.nbd.start()
        self.gadget.setup()
        self.gadget.attach()
        logger.info("Drive attached")
        waiting = False
        code = _code_stamp()
        while not self._stop.wait(POLL_SECONDS):
            if self.nbd.stopped.is_set():
                logger.info("NBD stopped. Starting again")
                return True
            self.stick.check()
            current = self._signature()
            new_code = _code_stamp() != code
            if (current == shown and not new_code) or time.monotonic() - self.volume.last_read < QUIET_SECONDS:
                continue
            if machine_running(self.run_state_file):
                if not waiting:
                    logger.info("Waiting for the machine to be idle before changing the drive")
                    waiting = True
                continue
            waiting = False
            if new_code:
                # A file that doesn't import would leave systemd restarting it every 10 s, with no drive.
                code = _code_stamp()
                try:
                    imports = subprocess.run([sys.executable, "-c", "import utils.usb_drive"], capture_output=True,
                                             timeout=60).returncode == 0
                except subprocess.TimeoutExpired:
                    imports = False
                if imports:
                    logger.info("New code. Starting again")
                    return True
                logger.info("New code doesn't import. Keeping this one")
                if current == shown:
                    continue
            volume = self._build()
            self.gadget.detach()
            old, self.volume, shown = self.volume, volume, current
            self.nbd.flush()
            old.close()
            self.gadget.attach()
            logger.info("Drive plugged in again")
        return False

    def stop(self, *_):
        self._stop.set()

    def shutdown(self):
        for step in (self.gadget.teardown, self.nbd.stop, lambda: self.stick.device and self.stick.unmount()):
            try:
                step()
            except OSError as e:
                logger.info(f"Shutdown: {e}")


def _files(node):
    for c in node.children:
        if c.children is None:
            yield c
        else:
            yield from _files(c)


def main():
    from config.constants import load_program_folder
    handler = TimedRotatingFileHandler("logs/usb_drive.log", when="midnight", backupCount=200)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()])
    folder = load_program_folder()
    if not folder:
        logger.info("No program folder, so LOCAL is empty. usb_drive_setup.sh makes /home/emec/haas-programs.")
    drive = UsbDrive(folder, RUN_STATE_FILE)
    signal.signal(signal.SIGTERM, drive.stop)
    signal.signal(signal.SIGINT, drive.stop)
    try:
        again = drive.run()
    finally:
        drive.shutdown()
    if again:   # sooner than systemd's restart, and loads new code without sudo
        os.execv(sys.executable, [sys.executable, "-m", "utils.usb_drive"])


if __name__ == "__main__":
    main()
