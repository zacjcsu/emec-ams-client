"""Sends the programs picked with Send in the dashboard's machine popup to a classic Haas over RS-232.

The dashboard queues them in cnc_program_sends, one open at a time per machine. The reader in utils/haas_mdc.py owns
the port, so it does the sending. This hands it the program and keeps the dashboard's row up to date.
"""
import logging
import threading

import psycopg

from db.server_sync import get_server_connection
from utils.haas_mdc import SendJob, haas_text

logger = logging.getLogger("programs")

POLL_SECONDS = 2
OPEN = ("waiting", "sending")


class ProgramSender:
    def __init__(self, machine_id, monitor, poll_seconds=POLL_SECONDS):
        self.machine_id = machine_id
        self.monitor = monitor
        self.poll_seconds = poll_seconds
        self.job = None
        self.reported = None
        self.restarted = True   # a row still sending was cut off by this Pi stopping
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="program-send", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _claim(self, send_id):
        with get_server_connection(timeout=3) as conn:
            return conn.execute("""UPDATE cnc_program_sends SET state = 'sending', detail = NULL, updated_at = now()
                                   WHERE id = %s AND state = 'waiting' RETURNING 1""", (send_id,)).fetchone() is not None

    def _report(self, conn, job):
        report = (job.state, job.detail, job.sent)
        if report != self.reported:
            conn.execute("""UPDATE cnc_program_sends SET state = %s, detail = %s, sent_bytes = %s, updated_at = now()
                            WHERE id = %s AND state = ANY(%s)""", (*report, job.id, list(OPEN)))
            self.reported = report

    def step(self, conn):
        if self.restarted:
            conn.execute("""UPDATE cnc_program_sends SET state = 'failed', detail = 'The Pi restarted', updated_at = now()
                            WHERE machine_id = %s AND state = 'sending'""", (self.machine_id,))
            self.restarted = False
        job = self.job
        if job:
            if job.done.is_set():
                self._report(conn, job)
                self.job = None
                return
            row = conn.execute("SELECT state FROM cnc_program_sends WHERE id = %s", (job.id,)).fetchone()
            if not row or row["state"] not in OPEN:
                job.cancelled.set()
                return
            self._report(conn, job)
            return
        row = conn.execute("""SELECT id, program_id, sha256 FROM cnc_program_sends
                              WHERE machine_id = %s AND state = 'waiting' ORDER BY id LIMIT 1""",
                           (self.machine_id,)).fetchone()
        if not row:
            return
        program = conn.execute("SELECT content FROM cnc_programs WHERE id = %s AND sha256 = %s",
                               (row["program_id"], row["sha256"])).fetchone()
        if not program:
            conn.execute("""UPDATE cnc_program_sends SET state = 'failed', detail = 'Changed after Send', updated_at = now()
                            WHERE id = %s AND state = 'waiting'""", (row["id"],))
            return
        send_id = row["id"]
        self.job = SendJob(send_id, haas_text(bytes(program["content"])), lambda: self._claim(send_id))
        self.reported = None
        self.monitor.send(self.job)
        logger.info(f"[PROGRAMS] Program {send_id} is waiting for RECV.")

    def _run(self):
        conn = None
        failing = False
        while not self._stop.is_set():
            try:
                if conn is None or conn.closed:
                    conn = get_server_connection(timeout=3)
                    conn.autocommit = True
                self.step(conn)
                failing = False
            except Exception as e:
                # The heartbeat already reports the server being out of reach.
                if not failing:
                    (logger.info if isinstance(e, psycopg.OperationalError) else logger.warning)(
                        f"[PROGRAMS] Cannot check for programs to send: {e}")
                    failing = True
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                conn = None
            self._stop.wait(self.poll_seconds)
