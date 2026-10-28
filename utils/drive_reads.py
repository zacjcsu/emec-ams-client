"""Tells the dashboard which files the machine read from the USB drive (utils/usb_drive.py), so a run's tools come
from the file it ran. Each read goes in cnc_drive_reads with the file's sha256, and the file's bytes go in
cnc_drive_files once.

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


class ReadReporter:
    def __init__(self, machine_id, connect=None):
        self.machine_id = machine_id
        self._connect = connect
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
        self._queue.append((datetime.now(timezone.utc), name, path))
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

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            while self._queue:
                item = self._load(*self._queue.popleft())
                if item:
                    self._pending.append(item)
            self._trim()
            if self._pending:
                try:
                    self._send()
                except Exception as e:
                    logger.info(f"Cannot send file reads to the server: {e}")
                    self._stop.wait(RETRY_SECONDS)
                    continue
            self._wake.wait()
