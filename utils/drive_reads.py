"""Tells the dashboard which files the machine read from the USB drive (utils/usb_drive.py), so a run's tools come
from the file it ran. Each read goes in cnc_drive_reads with the file's sha256, and the file's bytes go in
cnc_drive_files once. Only program files count.

It also lists the flash drive in cnc_stick_files, and sends the bytes of its program files, so the dashboard can show
and open them. Those bytes are read only while the control is idle or off, since the machine reads the same stick.

The drive's thread only queues a read. This thread reads the file at once, since a flash drive can be pulled right
after, and sends it when the server answers. A slow or missing network never holds up the drive."""
import hashlib
import logging
import os
import threading
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger("usb_drive")

MAX_BYTES = 64 * 1024 * 1024   # the dashboard's upload limit
HELD_BYTES = 128 * 1024 * 1024  # file bytes kept while the server can't be reached. The oldest go past this
HELD_READS = 5000
RETRY_SECONDS = 30
UNSET = object()
MAX_STICK_BYTES = 512 * 1024 * 1024   # sent from one flash drive. The rest are listed only
IDLE_CHECK_SECONDS = 5
# The dashboard's PROGRAM_TYPES and MEDIA_TYPES in backend/routes/machines.py. No extension counts too.
PROGRAM_TYPES = {"cnc", "dnc", "eia", "fan", "fnc", "g", "gcode", "h", "iso", "min", "mpf", "nc", "ncf", "ngc", "prg",
                 "pu", "spf", "tap", "txt", "png", "jpg", "jpeg", "mp4", "mov"}


def program_file(name):
    base = name.rsplit("/", 1)[-1]
    return "." not in base or base.rsplit(".", 1)[-1].lower() in PROGRAM_TYPES


def forget(machine_id, connect=None):
    """This Pi has no drive any more."""
    if connect is None:
        from db.server_sync import get_server_connection
        connect = get_server_connection
    try:
        with connect() as conn:
            conn.execute("DELETE FROM cnc_usb_drives WHERE machine_id = %s", (machine_id,))
            conn.commit()
    except Exception as e:
        logger.info(f"Cannot tell the server there's no drive: {e}")


class ReadReporter:
    def __init__(self, machine_id, connect=None, can_read=None):
        self.machine_id = machine_id
        self._connect = connect
        self._can_read = can_read or (lambda: True)   # whether the stick can be read for its bytes now
        self._registered = False
        self._stick = UNSET        # (label, files), or None with no stick, from the drive's thread
        self._listed = UNSET       # what the server lists
        self._unsent = []          # (name, path) of the listed files whose bytes haven't been sent
        self._stick_bytes = 0
        self._queue = deque()      # (when, name on the drive, path), from the drive's thread
        self._pending = []         # [when, name, sha256, bytes or None once the server has them]
        self._known = set()        # sha256s the server has
        self._shas = {}            # path: (mtime_ns, size, sha256), so an unchanged file the server has isn't read
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="drive-reads", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def report(self, name, path):
        """The machine started reading the file at path, shown to it as name."""
        if program_file(name):
            self._queue.append((datetime.now(timezone.utc), name, path))
            self._wake.set()

    def stick(self, files, label=None):
        """What's on the flash drive now: [(name, path, size, mtime, folder)], or None with none in."""
        self._stick = None if files is None else (label, tuple(files))
        self._wake.set()

    def _load(self, when, name, path):
        try:
            st = os.stat(path)
            if st.st_size > MAX_BYTES:
                logger.info(f"{name} is over {MAX_BYTES // 1024 ** 2} MB, so the dashboard doesn't get it")
                return None
            cached = self._shas.get(path)
            if cached and cached[:2] == (st.st_mtime_ns, st.st_size) and cached[2] in self._known:
                return [when, name, cached[2], None]
            with open(path, "rb") as f:
                content = f.read(MAX_BYTES + 1)
        except OSError as e:
            logger.info(f"Cannot read {name} for the dashboard: {e}")
            return None
        sha = hashlib.sha256(content).hexdigest()
        self._shas[path] = (st.st_mtime_ns, st.st_size, sha)
        return [when, name, sha, None if sha in self._known else content]

    def _trim(self):
        held = sum(len(p[3]) for p in self._pending if p[3])
        while self._pending and (held > HELD_BYTES or len(self._pending) > HELD_READS):
            dropped = self._pending.pop(0)
            held -= len(dropped[3] or b"")
            logger.info(f"Dropped the read of {dropped[1]}, as the server hasn't answered")

    def _send(self):
        if self._connect is None:
            from db.server_sync import get_server_connection
            self._connect = get_server_connection
        with self._connect() as conn:
            while self._pending:
                when, name, sha, content = self._pending[0]
                if sha not in self._known:
                    if not conn.execute("SELECT 1 FROM cnc_drive_files WHERE sha256 = %s", (sha,)).fetchone():
                        conn.execute("INSERT INTO cnc_drive_files (sha256, content) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                                     (sha, content))
                    self._known.add(sha)
                conn.execute("INSERT INTO cnc_drive_reads (machine_id, read_at, name, sha256) VALUES (%s, %s, %s, %s)",
                             (self.machine_id, when, name, sha))
                conn.commit()
                self._pending.pop(0)

    def _list_stick(self, conn, stick):
        label, files = stick or (None, None)
        conn.execute("DELETE FROM cnc_stick_files WHERE machine_id = %s", (self.machine_id,))
        if files:
            conn.execute("""
                INSERT INTO cnc_stick_files (machine_id, name, folder, size, modified_at, unsent)
                SELECT %s, n, f, s, to_timestamp(t), s > %s AND NOT f
                FROM unnest(%s::text[], %s::bool[], %s::bigint[], %s::float8[]) AS x(n, f, s, t)""",
                (self.machine_id, MAX_BYTES, [f[0] for f in files], [f[4] for f in files],
                 [0 if f[4] else f[2] for f in files], [f[3] for f in files]))
        conn.execute("""UPDATE cnc_usb_drives SET stick_at = CASE WHEN %s THEN now() END, stick_label = %s
                        WHERE machine_id = %s""", (files is not None, label, self.machine_id))
        conn.commit()
        self._listed = stick
        self._unsent = [(f[0], f[1]) for f in files or () if not f[4] and f[2] <= MAX_BYTES and program_file(f[0])]
        self._stick_bytes = 0

    def _send_stick_file(self, conn, name, path):
        """False when it has to wait for the control to be idle."""
        try:
            st = os.stat(path)
            cached = self._shas.get(path)
            if cached and cached[:2] == (st.st_mtime_ns, st.st_size) and cached[2] in self._known:
                sha = cached[2]
            elif self._stick_bytes + st.st_size > MAX_STICK_BYTES:
                logger.info(f"Over {MAX_STICK_BYTES // 1024 ** 2} MB from this flash drive, so {name} isn't sent")
                sha = None
            elif not self._can_read():
                return False
            else:
                with open(path, "rb") as f:
                    content = f.read(MAX_BYTES + 1)
                self._stick_bytes += len(content)
                sha = hashlib.sha256(content).hexdigest()
                self._shas[path] = (st.st_mtime_ns, st.st_size, sha)
                if sha not in self._known and not conn.execute(
                        "SELECT 1 FROM cnc_drive_files WHERE sha256 = %s", (sha,)).fetchone():
                    conn.execute("INSERT INTO cnc_drive_files (sha256, content) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                                 (sha, content))
                self._known.add(sha)
        except OSError as e:
            logger.info(f"Cannot read {name} for the dashboard: {e}")
            sha = None
        if sha:
            conn.execute("UPDATE cnc_drive_files SET seen_at = now() WHERE sha256 = %s", (sha,))
            conn.execute("UPDATE cnc_stick_files SET sha256 = %s WHERE machine_id = %s AND name = %s",
                         (sha, self.machine_id, name))
        else:
            conn.execute("UPDATE cnc_stick_files SET unsent = true WHERE machine_id = %s AND name = %s",
                         (self.machine_id, name))
        conn.commit()
        return True

    def _send_stick(self):
        """True when files are waiting for the control to be idle."""
        if self._registered and (self._stick is UNSET or self._stick == self._listed) and not self._unsent:
            return False
        if self._connect is None:
            from db.server_sync import get_server_connection
            self._connect = get_server_connection
        with self._connect() as conn:
            if not self._registered:
                conn.execute("""INSERT INTO cnc_usb_drives (machine_id) VALUES (%s)
                                ON CONFLICT (machine_id) DO UPDATE SET started_at = now()""", (self.machine_id,))
                conn.commit()
                self._registered, self._listed = True, UNSET
            while not self._stop.is_set() and not self._queue:   # a read the machine starts goes first
                files = self._stick
                if files is not UNSET and files != self._listed:
                    self._list_stick(conn, files)
                elif not self._unsent:
                    return False
                elif self._send_stick_file(conn, *self._unsent[0]):
                    self._unsent.pop(0)
                else:
                    return True
        return bool(self._queue)

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            while self._queue:
                item = self._load(*self._queue.popleft())
                if item:
                    self._pending.append(item)
            self._trim()
            try:
                if self._pending:
                    self._send()
                waiting = self._send_stick()
            except Exception as e:
                logger.info(f"Cannot send file reads to the server: {e}")
                self._stop.wait(RETRY_SECONDS)
                continue
            self._wake.wait(IDLE_CHECK_SECONDS if waiting else None)
