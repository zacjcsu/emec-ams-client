"""Sends pictures from a USB camera inside the machine while someone watches it in the dashboard's machine popup.

The dashboard marks machine_camera.watched_until while the popup is open. Then this runs v4l2-ctl, which streams the
camera's MJPEG to its stdout, and writes the newest picture to machine_camera twice a second. It stops the camera
once nobody watches. machine.camera says whether a camera is plugged in, checked again every LOOK_SECONDS, so one can
be plugged in without a restart. "camera": false in config.json turns it off.
"""
import logging
import os
import re
import subprocess
import threading
import time

import psycopg

from db.server_sync import get_server_connection

logger = logging.getLogger("camera")

BY_ID = "/dev/v4l/by-id"
WIDTH, HEIGHT, FPS = 1280, 720, 5
FRAME_SECONDS = 0.5   # while watched
IDLE_SECONDS = 2      # how often to check for a watcher
LOOK_SECONDS = 10     # how often to check for a camera
MAX_BUFFER = 8 * 1024 * 1024
JPEG_START = re.compile(rb"\xff\xd8\xff")


def find_camera(folder=BY_ID):
    """The first USB camera's capture device, by a name that stays the same across reboots."""
    try:
        names = sorted(os.listdir(folder))
    except FileNotFoundError:
        return None
    return next((os.path.join(folder, n) for n in names if n.endswith("-video-index0")), None)


class Frames:
    """Splits a stream of JPEGs, one per camera buffer, and keeps the newest whole one."""

    def __init__(self):
        self._buf = bytearray()
        self.latest = None

    def feed(self, chunk):
        self._buf += chunk
        starts = [m.start() for m in JPEG_START.finditer(self._buf)]
        if len(starts) >= 2:   # a picture is whole once the next one starts
            self.latest = bytes(self._buf[starts[-2]:starts[-1]])
            del self._buf[:starts[-1]]
        elif len(self._buf) > MAX_BUFFER:   # not MJPEG
            self._buf.clear()


class Stream:
    """v4l2-ctl streaming the camera, while it runs."""

    def __init__(self, command=None):
        self._command = command
        self._proc = None
        self._lock = threading.Lock()
        self._frames = None
        self._retry_at = 0
        self._warned = False   # once until a picture comes through, since warnings reach the dashboard

    def running(self):
        return self._proc is not None and self._proc.poll() is None

    def _warn(self, message):
        if not self._warned:
            logger.warning(f"[CAMERA] {message}")
            self._warned = True

    def start(self, device):
        if self.running() or time.monotonic() < self._retry_at:
            return
        if self._proc is not None:
            self._warn(f"The camera stopped with exit code {self._proc.returncode}.")
            self._proc = None
            self._retry_at = time.monotonic() + LOOK_SECONDS
            return
        command = self._command or ["v4l2-ctl", "-d", device, f"--set-fmt-video=width={WIDTH},height={HEIGHT},pixelformat=MJPG",
                                    f"--set-parm={FPS}", "--stream-mmap", "--stream-to=-"]
        try:
            self._proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as e:
            self._warn(f"Cannot start the camera: {e}")
            self._retry_at = time.monotonic() + LOOK_SECONDS
            return
        self._frames = Frames()
        threading.Thread(target=self._read, args=(self._proc, self._frames), name="camera-read", daemon=True).start()
        logger.info("[CAMERA] Started.")

    def _read(self, proc, frames):
        while chunk := proc.stdout.read1(65536):
            with self._lock:
                frames.feed(chunk)

    def take(self):
        """The newest picture since the last take, or None."""
        with self._lock:
            if not self._frames:
                return None
            frame, self._frames.latest = self._frames.latest, None
        if frame:
            self._warned = False
        return frame

    def stop(self):
        proc, self._proc = self._proc, None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        logger.info("[CAMERA] Stopped.")


class Camera:
    def __init__(self, machine_id, find=find_camera, stream=None):
        self.machine_id = machine_id
        self._find = find
        self._stream = stream or Stream()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        conn = None
        device = None
        reported = None
        next_look = 0
        failing = False
        while not self._stop.is_set():
            wait = IDLE_SECONDS
            try:
                if conn is None or conn.closed:
                    conn = get_server_connection(timeout=3)
                    conn.autocommit = True
                if time.monotonic() >= next_look:
                    device = self._find()
                    next_look = time.monotonic() + LOOK_SECONDS
                    if bool(device) != reported:
                        conn.execute("UPDATE machine SET camera = %s WHERE machine_id = %s", (bool(device), self.machine_id))
                        logger.info(f"[CAMERA] {'Camera at ' + device if device else 'No camera'}.")
                        reported = bool(device)
                row = conn.execute("SELECT watched_until > now() AS watched FROM machine_camera WHERE machine_id = %s",
                                   (self.machine_id,)).fetchone() if device else None
                if row and row["watched"]:
                    self._stream.start(device)
                    frame = self._stream.take()
                    if frame:
                        conn.execute("UPDATE machine_camera SET frame = %s, frame_at = now() WHERE machine_id = %s",
                                     (frame, self.machine_id))
                    wait = FRAME_SECONDS
                else:
                    self._stream.stop()
                failing = False
            except Exception as e:
                self._stream.stop()
                if isinstance(e, psycopg.OperationalError) and conn is not None:
                    conn.close()
                if not failing:
                    logger.warning(f"[CAMERA] {e}")
                    failing = True
                wait = LOOK_SECONDS
            self._stop.wait(wait)
        self._stream.stop()
