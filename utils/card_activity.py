import logging
import threading
import time
from collections import deque
import psycopg
from db.server_sync import (
    get_server_connection, report_card_present, report_card_removed, report_scan, temp_card_claim_job,
)
from config.constants import MACHINE_ID, CARD_REPORT_SECONDS, CARD_REPORT_MAX_AGE

logger = logging.getLogger("card_activity")


class CardActivity:
    """Tells the dashboard what is on this machine's reader, and fetches programming jobs.

    The main loop (the only code that touches the reader) calls set_present() for a card that did NOT start
    a session, and set_present(None) when it leaves or a session starts. A card held outside its owner's hours
    is reported as held, and no programming job is fetched for it. This thread does the database work,
    so a slow or unreachable server never stalls card polling:

    * every CARD_REPORT_SECONDS while a card is present: report_card_present(), then temp_card_claim_job();
      a claimed job waits in a slot for the main loop (take_job()).
    * report_card_removed() once when the card is gone.
    * presence is only reported while the main loop keeps refreshing it (CARD_REPORT_MAX_AGE), so a stalled
      loop or a session in progress stops it; the server also drops presence after 15 s.

    Refused cards (refused()) wait in a queue until the server takes them, so they survive a network outage but
    not a restart.

    Unreachable server: reporting and polling just skip that cycle.
    """

    def __init__(self, machine_id=MACHINE_ID):
        self.machine_id = machine_id
        self._lock = threading.Lock()
        self._present = None        # (uid_hex, blank, held)
        self._stamp = 0.0
        self._job = None
        self._reported = False
        self._refused = deque(maxlen=500)   # (monotonic time, uid_hex, csu_id, reason)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="card-activity", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def set_present(self, uid_hex, blank=False, held=False):
        with self._lock:
            self._present = (uid_hex, bool(blank), bool(held)) if uid_hex else None
            self._stamp = time.monotonic()

    def refused(self, uid_hex, csu_id, reason, at=None):
        """`at` is the time.monotonic() of the scan, if it was refused a while ago."""
        logger.info(f"[SCAN] Refused card {uid_hex} (CSU ID {csu_id}): {reason}")
        with self._lock:
            self._refused.append((at or time.monotonic(), uid_hex, csu_id, reason))

    def _send_refused(self, conn):
        while True:
            with self._lock:
                if not self._refused:
                    return
                t, uid_hex, csu_id, reason = self._refused[0]
            try:
                report_scan(self.machine_id, uid_hex, csu_id, reason, time.monotonic() - t, conn=conn)
            except psycopg.OperationalError:
                raise                   # the connection: keep it queued and retry
            except psycopg.Error as e:
                logger.warning(f"[SCAN] The server would not take the refused card {uid_hex}, dropping it: {e}")
            with self._lock:
                self._refused.popleft()

    def take_job(self):
        with self._lock:
            job, self._job = self._job, None
            return job

    def _run(self):
        conn = None
        failing = False
        last_report = 0.0
        while not self._stop.is_set():
            with self._lock:
                present, stamp, has_job = self._present, self._stamp, self._job is not None
                queued = bool(self._refused)
            now = time.monotonic()
            live = present is not None and now - stamp <= CARD_REPORT_MAX_AGE
            try:
                if live or self._reported or queued:
                    if conn is None or conn.closed:
                        conn = get_server_connection(timeout=3)
                        conn.autocommit = True
                if live:
                    if now - last_report >= CARD_REPORT_SECONDS:
                        report_card_present(self.machine_id, *present, conn=conn)
                        self._reported = True
                        last_report = now
                        if not has_job and not present[2]:
                            job = temp_card_claim_job(self.machine_id, conn=conn)
                            if job:
                                logger.info(f"[CARD] Claimed programming job {job['issue_id']} for card {job['card_uid']}")
                                with self._lock:
                                    self._job = job
                elif self._reported:
                    report_card_removed(self.machine_id, conn=conn)
                    self._reported = False
                    last_report = 0.0
                if queued:
                    self._send_refused(conn)
                if failing:
                    logger.info("[CARD] Server reachable again.")
                    failing = False
            except Exception as e:
                if not failing:
                    logger.error(f"[CARD] Presence/job poll failed, will retry: {e}")
                    failing = True
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                conn = None
            self._stop.wait(0.5)
