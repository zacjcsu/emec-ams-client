"""Sends warnings and errors from the log to the dashboard's pi_events table (dashboard migration 048).

The handler only queues. The heartbeat thread sends the queue over its connection, so logging never waits on the
network. A code repeated within REPEAT_SECONDS is held and sent once a minute with a count.
"""
import logging
import re
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone

REPEAT_SECONDS = 60
QUEUE_SIZE = 200          # kept while the server is unreachable. The oldest are dropped first.
MESSAGE_CHARS = 500

logger = logging.getLogger("pi_events")


class EventHandler(logging.Handler):
    """Warnings and above, plus any record logged with extra={"code": ...}."""

    def __init__(self, machine_id):
        super().__init__(logging.INFO)
        self.machine_id = machine_id
        self._queue = deque(maxlen=QUEUE_SIZE)
        self._held = {}           # code: [when its minute started, repeats held, the last one's row]
        self._lock = threading.Lock()

    def emit(self, record):
        code = getattr(record, "code", None)
        if code is None and record.levelno < logging.WARNING:
            return
        try:
            code = code or re.sub(r"[^a-z0-9_]", "_", record.name.lower())[:40] or "app"
            row = (datetime.now(timezone.utc), record.levelname, code, _message(record))
        except Exception:
            return
        now = time.monotonic()
        with self._lock:
            held = self._held.get(code)
            if held and now - held[0] < REPEAT_SECONDS:
                held[1] += 1
                held[2] = row
                return
            self._held[code] = [now, 0, None]
            self._queue.append((*row, 1))

    def send(self, conn):
        """Send what's queued. A row that fails goes back to the front and the error is raised."""
        self._release()
        while True:
            with self._lock:
                if not self._queue:
                    return
                row = self._queue.popleft()
            try:
                conn.execute("SELECT report_event(%s, %s, %s, %s, %s, %s)", (self.machine_id, *row))
            except Exception:
                with self._lock:
                    self._queue.appendleft(row)
                raise

    def _release(self):
        now = time.monotonic()
        with self._lock:
            for code, held in self._held.items():
                if held[1] and now - held[0] >= REPEAT_SECONDS:
                    self._queue.append((*held[2], held[1]))
                    self._held[code] = [now, 0, None]


def _message(record):
    message = record.getMessage()
    if record.exc_info and record.exc_info[1] is not None:
        message = message.rstrip(".") + ": " + traceback.format_exception_only(*record.exc_info[:2])[-1].strip()
    return message[:MESSAGE_CHARS]


def install(machine_id, connect):
    """Add the handler to the root logger and log crashes. `connect(timeout=...)` opens a server connection."""
    handler = EventHandler(machine_id)
    logging.getLogger().addHandler(handler)

    def crashed(kind, value, tb):
        logging.getLogger("main").critical("[MAIN] Crashed", exc_info=(kind, value, tb), extra={"code": "crash"})
        # The heartbeat thread dies with the process, so this one is sent now.
        try:
            with connect(timeout=3) as conn:
                handler.send(conn)
        except Exception:
            pass

    def thread_crashed(args):
        logging.getLogger(args.thread.name if args.thread else "thread").critical(
            f"[{(args.thread.name if args.thread else 'thread').upper()}] Thread crashed",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback), extra={"code": "crash"})

    sys.excepthook = crashed
    threading.excepthook = thread_crashed
    return handler
