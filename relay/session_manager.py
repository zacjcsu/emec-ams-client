import time
import uuid
import logging
from config.constants import MACHINE_ID
from datetime import datetime
from db.server_sync import (
    sync_session_to_server, push_session_start, push_user_status, push_machine_status, fetch_last_heartbeat,
)
from utils.timeutil import TS_FORMAT
from config.constants import (
    STATUS_NEUTRAL, STATUS_IN_USE, STATUS_MAINTENANCE, LCD_LINE_DELAY, LCD_MESSAGES,
)

logger = logging.getLogger("session")

# Screen text when a running session is ended by the server (16 chars per line).
REVOKED_MESSAGES = {
    "estop": ("EMERGENCY", "SHUTDOWN"),
    "maintenance": (LCD_MESSAGES["maintenance"][0], LCD_MESSAGES["maintenance"][1]),
    "outside_hours": ("Lab closed", "Session ended"),
    "group_disabled": ("Group disabled", ""),
    "no_permission": ("Access revoked", "No permission"),
    "unknown_user": ("Access revoked", "Unknown user"),
    # temporary cards
    "card_lost": ("Card reported", "lost"),
    "card_revoked": ("Card revoked", "Session ended"),
    "card_retired": ("Card retired", "Session ended"),
    "expired": ("Card expired", "Session ended"),
    "no_active_issue": ("Card not active", "Session ended"),
    "unknown_card": ("Card not valid", "Session ended"),
    "not_temporary": ("Card not valid", "Session ended"),
}

# Reasons whose message stays on screen: the next scan of the card still on the reader shows and holds it.
QUIET_END_REASONS = ("user_disabled", "group_disabled")

class SessionManager:
    """Starting and ending sessions: the local and server records, machine status, relay and screen.
    How a running session is watched depends on the machine's kind (relay/kinds.py)."""

    def __init__(self, db, lcd, relay, lockout=None):
        self.lockout = lockout
        self.db = db
        self.lcd = lcd
        self.relay = relay
        self._reset_session_state()

    def _reset_session_state(self):
        self.active_session_id = None
        self.active_csu_id = None
        self.session_start_time = None
        self.display_name = None
        self.active_card_uid = None   # UID of the physical card that started the session
        self.active_temp = False      # True for a temporary card (recognised by UID, not by CSU ID)
        self.active_bypass = False    # a temp card that may run this machine during maintenance
        if self.lockout:
            self.lockout.unwatch()

    def show(self, line1, line2, color, delay=0):
        self.lcd.display(line1, line2, color=color)
        if delay:
            time.sleep(delay)

    def _sync_machine_status(self, status, csu_id):
        if self.lockout and self.lockout.maintenance_active and status != STATUS_MAINTENANCE:
            # The dashboard's maintenance flag is the ground truth until staff clears it; a routine
            # status push (session start/end) must not undo it. Checked live, not from the local cache,
            # so this is correct even mid-session, the moment maintenance is turned on or lifted.
            status = STATUS_MAINTENANCE
        self.db.update_machine_status(MACHINE_ID, status)
        self.db.update_machine_heartbeat(MACHINE_ID)
        push_user_status(self.db, csu_id)
        push_machine_status(self.db, MACHINE_ID)

    def start_session(self, csu_id, display_name, card_uid=None, temp=False, bypass=False):
        if not self.active_session_id:
            self.active_session_id = str(uuid.uuid4())
            self.session_start_time = time.time()
            self.db.mark_user_active(csu_id)
            self.db.insert_session(self.active_session_id, csu_id, MACHINE_ID, card_uid)
            push_session_start(self.active_session_id)     # the dashboard shows who is on the machine from now
            logger.info(f"[SESSION] Started: {display_name} ({csu_id}), session_id: {self.active_session_id}"
                        + (f", TEMP card {card_uid}" if temp else ""))
        else:
            logger.info("[SESSION] Resumed session within grace period.")

        self.active_csu_id = csu_id
        self.display_name = display_name
        self.active_card_uid = card_uid
        self.active_temp = temp
        self.active_bypass = bypass and temp
        if self.lockout:
            self.lockout.watch(csu_id, card_uid if temp else None, bypass_maintenance=self.active_bypass)
        self._sync_machine_status(STATUS_IN_USE, csu_id)

        self.relay.turn_on()
        line2 = "in use MAINT" if self.active_bypass else "in use TEMP CARD" if temp else "in use"
        self.show(display_name[:16], line2, color="green")

    def resume_session(self):
        """Carry on the current session after its card came back."""
        self.start_session(self.active_csu_id, self.display_name, self.active_card_uid, self.active_temp,
                           self.active_bypass)

    def lockout_reason(self):
        """None, 'estop', 'maintenance', or the server's reason the signed-in user lost access."""
        if not self.lockout:
            return None
        if self.lockout.estop_active:
            return "estop"
        if self.lockout.maintenance_active and not self.lockout.bypass_maintenance:
            return "maintenance"
        return self.lockout.revoked_reason

    def end_for_lockout(self, reason):
        logger.warning(f"[SESSION] Ending session: {reason}.")
        if reason == "user_disabled":
            # The dashboard's own two-line message: line 1, a newline, an optional line 2.
            line1, _, line2 = (self.lockout.revoked_via or "").partition("\n")
            line1, line2 = line1[:16] or "User disabled", line2[:16]
        else:
            line1, line2 = REVOKED_MESSAGES.get(reason, ("Access revoked", str(reason)))
        if reason in QUIET_END_REASONS:
            # The message stays up: the next scan of the card still on the reader shows and holds it, so skip
            # the delay, the "Session ended" screen and the startup checks that would flash over it.
            self.show(line1, line2, color="red")
            self.force_end_session(quiet=True)
            return
        if reason == "outside_hours":
            # "Lab closed / Session ended" stays up; the machine's kind decides how long.
            self.show(line1, line2, color="red")
            self.force_end_session(quiet=True)
            return
        self.show(line1, line2, color="red", delay=LCD_LINE_DELAY)
        self.force_end_session()

    def force_end_session(self, quiet=False):
        if not self.active_session_id:
            return

        end_time = time.time()
        duration_sec = int(end_time - self.session_start_time)
        duration_min = max(0, round(duration_sec / 60))

        self.db.end_session(self.active_session_id)
        self.db.mark_user_inactive(self.active_csu_id)
        self._sync_machine_status(STATUS_NEUTRAL, self.active_csu_id)

        sync_session_to_server(self.active_session_id)
        logger.info(f"[SESSION] Ended: {self.display_name} ({self.active_csu_id}), duration: {duration_min} min")

        if not quiet:
            self.show("Session", "ended", color="red", delay=1)

        self._reset_session_state()
        self.relay.turn_off()


def recover_orphaned_sessions(db):
    """Close sessions left open by a crash or power loss, so an empty end_time on the server means "running now".

    A normal shutdown closes its session (main.py's exit handler), so any open local row found at startup
    belongs to a previous run. It is ended at the server's last heartbeat from that run (the heartbeat thread
    beats about every 30 s while the app is up), or at its start if that is earlier. If the server cannot be
    reached nothing is changed and the next start tries again.
    """
    open_rows = db.get_open_sessions()
    if not open_rows:
        return
    heartbeat = fetch_last_heartbeat(MACHINE_ID)
    if heartbeat is None:
        logger.warning(f"[SESSION] {len(open_rows)} unfinished session(s) from a previous run; server unreachable, will retry.")
        return
    for row in open_rows:
        start = datetime.strptime(row["start_time"], TS_FORMAT)
        end = heartbeat if heartbeat > start else start
        db.close_session_at(row["session_id"], end.strftime(TS_FORMAT))
        sync_session_to_server(row["session_id"])
        logger.warning(f"[SESSION] Closed unfinished session {row['session_id']} ({row['csu_id']}) at {end} "
                       f"(last heartbeat of the previous run).")
