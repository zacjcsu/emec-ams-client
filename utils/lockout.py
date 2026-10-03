import logging
import threading
import time
import psycopg
from db.server_sync import get_server_connection, cardless_claim, cardless_check
from config.constants import (
    EMERGENCY_POLL_SECONDS, ENFORCE_ACCESS_DURING_SESSION, ACCESS_RECHECK_SECONDS, MACHINE_ID,
    STATUS_MAINTENANCE, CARDLESS_START_SECONDS,
)

logger = logging.getLogger("lockout")


class LockoutMonitor:
    """Watches the server for reasons to stop the machine, over one persistent connection.

    * Emergency shutdown: system_settings.emergency_shutdown, written by the dashboard button.
      While 'true' the relay is locked off (nothing can energise it), `estop_active` is set, and the
      rest of the app stops sessions and shows the shutdown message.
    * Maintenance: this machine's own machine.machine_status, set from the dashboard's "Set Maintenance"
      button. While it reads 'maintenance' the relay is locked off, `maintenance_active` is set, and the
      rest of the app blocks new scans and ends a running session, the same as an emergency shutdown but
      for one machine instead of the whole lab. A temp card issued to bypass maintenance on this machine
      is the exception: its session runs with the lock lifted, and the lock returns when it ends.
    * Access revoked mid-session: while a user is being watched (SessionManager calls watch/unwatch),
      the server's access_decision_machine() is asked about them every ACCESS_RECHECK_SECONDS. If they
      no longer have access (lab closed, group disabled, permission revoked) `revoked_reason` is set for the
      session loop to end the session, and the relay is cut if the machine's kind says to.
    * Cardless access (dashboard migration 043): while idle, a request started from the dashboard is claimed
      and waits for the main loop (take_cardless()). While one runs (watch_cardless()), maintenance doesn't
      stop it, and `cardless_stop` is set when the dashboard stops it or its time is up.
    * Message: what the dashboard wants on this machine's screen, from machine_message() (dashboard migration
      028), as `message` (line1, line2), or None. That's the open maintenance record's two lines, or else
      "Service due" for a due service task. It's separate from the lock, so a running machine can have one.

    If the server cannot be reached the last known state is kept: a dropped network neither starts nor
    lifts a lockout, and does not end a running session.
    """

    def __init__(self, relay, kind, machine_id=MACHINE_ID):
        self.relay = relay
        self.kind = kind
        self.machine_id = machine_id
        self.estop_active = False
        self.maintenance_active = False
        self.message = None
        self._message_failing = False
        self.revoked_reason = None
        self.revoked_via = None  # the server's `via` for that reason (a disabled user's two-line message)
        self._watched = None  # csu_id of the user on the machine
        self._watched_card = None  # UID of the temporary card they signed in with, if any
        self.bypass_maintenance = False  # the watched session may run while this machine is in maintenance
        self._cardless = None  # id of the cardless session running now
        self._cardless_job = None  # (id, monotonic time) of a claimed request the main loop hasn't started
        self.cardless_stop = None  # 'stop' or 'time_up' from the server for the running cardless session
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lockout-monitor", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def watch(self, csu_id, temp_card_uid=None, bypass_maintenance=False):
        """Start watching a session. For a temporary card, also pass its UID: the card itself is re-checked
        (lost, revoked, expired) as well as the person's access."""
        with self._lock:
            if self._watched != str(csu_id) or self._watched_card != temp_card_uid:
                self.revoked_reason = None
                self.revoked_via = None
            self._watched = str(csu_id)
            self._watched_card = temp_card_uid
            self.bypass_maintenance = bool(bypass_maintenance and temp_card_uid)
        self.relay.set_lockout(self._relay_should_lock())

    def unwatch(self):
        with self._lock:
            self._watched = None
            self._watched_card = None
            self.revoked_reason = None
            self.revoked_via = None
            self.bypass_maintenance = False
            self._cardless = None
            self.cardless_stop = None
        self.relay.set_lockout(self._relay_should_lock())

    def watch_cardless(self, cardless_id):
        """Start watching a cardless session. It runs during maintenance and has no user to check."""
        with self._lock:
            self._watched = None
            self._watched_card = None
            self.revoked_reason = None
            self.revoked_via = None
            self._cardless = cardless_id
            self.cardless_stop = None
            self.bypass_maintenance = True
        self.relay.set_lockout(self._relay_should_lock())

    def take_cardless(self):
        """A claimed cardless request for the main loop to start, or None. One the loop was too busy to start
        in time is dropped, and the dashboard marks it failed."""
        with self._lock:
            job, self._cardless_job = self._cardless_job, None
        if job and time.monotonic() - job[1] <= CARDLESS_START_SECONDS:
            return job[0]
        return None

    def _relay_should_lock(self):
        return self.estop_active or (self.maintenance_active and not self.bypass_maintenance)

    def _set_estop(self, active):
        if active == self.estop_active:
            return
        self.estop_active = active
        self.relay.set_lockout(self._relay_should_lock())
        logger.warning("[LOCKOUT] Emergency shutdown ACTIVE: relay locked off." if active
                       else "[LOCKOUT] Emergency shutdown lifted.")

    def _set_maintenance(self, active):
        if active == self.maintenance_active:
            return
        self.maintenance_active = active
        self.relay.set_lockout(self._relay_should_lock())
        if active and self.bypass_maintenance:
            logger.warning(f"[LOCKOUT] {self.machine_id} set to maintenance; the current session may continue.")
        else:
            logger.warning(f"[LOCKOUT] {self.machine_id} set to maintenance: relay locked off." if active
                           else f"[LOCKOUT] {self.machine_id} taken out of maintenance.")

    def _read_message(self, conn):
        try:
            row = conn.execute("SELECT line1, line2 FROM machine_message(%s)", (self.machine_id,)).fetchone()
        except psycopg.OperationalError:
            raise
        except psycopg.Error as e:
            # A broken message must not stop the access check below, so it is logged once and skipped.
            if not self._message_failing:
                logger.warning(f"[LOCKOUT] Cannot read the maintenance message: {e}")
                self._message_failing = True
            return
        self._message_failing = False
        message = (row["line1"] or "", row["line2"] or "") if row and (row["line1"] or row["line2"]) else None
        if message != self.message:
            logger.info(f"[LOCKOUT] Maintenance message: {message}")
            self.message = message

    def _check_access(self, conn, csu_id, temp_card_uid=None):
        row = None
        if temp_card_uid:
            card = conn.execute(
                "SELECT ok, reason FROM temp_card_lookup(%s::text)", (temp_card_uid,)).fetchone()
            if card and not card["ok"]:
                row = {"allowed": False, "reason": card["reason"]}
        if row is None:
            row = conn.execute(
                "SELECT allowed, reason, via FROM access_decision_machine(%s, %s)",
                (csu_id, self.machine_id)).fetchone()
        # unknown_machine means a registration problem, not that this user lost access.
        if row and not row["allowed"] and row["reason"] != "unknown_machine":
            with self._lock:
                if self._watched != csu_id:  # session ended or changed while we were asking
                    return
                first = self.revoked_reason is None
                self.revoked_reason = row["reason"]
                self.revoked_via = row.get("via")
            cut = self.kind.cut_power_on_revoke(row["reason"])
            if cut:
                self.relay.turn_off()
            if first:
                logger.warning(f"[LOCKOUT] Access revoked for {csu_id} ({row['reason']})" + (": relay off." if cut else "."))

    def _run(self):
        conn = None
        failing = False
        last_access_check = 0.0
        while not self._stop.is_set():
            try:
                if conn is None or conn.closed:
                    conn = get_server_connection(timeout=3)
                    conn.autocommit = True  # no transaction left open between polls
                row = conn.execute(
                    "SELECT value FROM system_settings WHERE setting = 'emergency_shutdown'"
                ).fetchone()
                self._set_estop(bool(row) and str(row["value"]).strip().lower() == "true")

                mrow = conn.execute(
                    "SELECT machine_status FROM machine WHERE machine_id = %s", (self.machine_id,)
                ).fetchone()
                self._set_maintenance(bool(mrow) and mrow["machine_status"] == STATUS_MAINTENANCE)
                self._read_message(conn)

                with self._lock:
                    watched, watched_card = self._watched, self._watched_card
                    cardless, job = self._cardless, self._cardless_job
                if cardless:
                    state = cardless_check(cardless, conn)
                    if state != "running":
                        with self._lock:
                            if self._cardless == cardless:
                                self.cardless_stop = state
                elif not watched and not job and not self.estop_active:
                    claimed = cardless_claim(self.machine_id, conn)
                    if claimed:
                        logger.info(f"[LOCKOUT] Claimed cardless access {claimed}")
                        with self._lock:
                            self._cardless_job = (claimed, time.monotonic())
                if (ENFORCE_ACCESS_DURING_SESSION and watched
                        and time.monotonic() - last_access_check >= ACCESS_RECHECK_SECONDS):
                    self._check_access(conn, watched, watched_card)
                    last_access_check = time.monotonic()

                if failing:
                    logger.info("[LOCKOUT] Server reachable again.")
                    failing = False
            except Exception as e:
                if not failing:
                    logger.error(f"[LOCKOUT] Cannot reach server, keeping state "
                                 f"(estop {'ACTIVE' if self.estop_active else 'clear'}, "
                                 f"maintenance {'ACTIVE' if self.maintenance_active else 'clear'}): {e}",
                                 extra={"code": "server_unreachable"} if isinstance(
                                     e, (psycopg.OperationalError, psycopg.InterfaceError)) else None)
                    failing = True
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                conn = None
            self._stop.wait(EMERGENCY_POLL_SECONDS)
