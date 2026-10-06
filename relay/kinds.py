"""How a machine's sessions run and stop. Each kind of machine has its own class.

A kind decides how a running session is watched and whether losing access cuts the power at once.
Starting and ending a session, the relay, status and screen messages stay in SessionManager.
An emergency shutdown or maintenance lock cuts the power for every kind (LockoutMonitor).
"""
import logging
import math
import time

from config.constants import CARD_GRACE_PERIOD_DEFAULT, LCD_LINE_DELAY, MACHINE_KIND

logger = logging.getLogger("kinds")


class Attended:
    """Lathes and manual mills: the card stays on the reader for the whole session. Removing it starts the
    grace period, and losing access (lab closed, permission revoked) cuts the power at once."""

    name = "attended"
    CARD_GONE_SECONDS = 3   # no card for this long counts as removed (single missed reads are common)

    def cut_power_on_revoke(self, reason):
        return True

    def run(self, session, reader):
        """Watch a running session until it ends. Returns why it ended (see SessionManager.QUIET_END_REASONS)."""
        # A resumed session is watched again, so the next removal gets its own grace period.
        ended = self._watch_card(session, reader)
        while ended == "removed":
            ended = self._grace_period(session, reader)
            if ended == "resumed":
                ended = self._watch_card(session, reader)
        return ended

    def _classify(self, session, scan):
        """'same' if `scan` is the card that started this session, 'other' if it is a different card,
        'absent' if there is no card. A temporary card is recognised by its UID (it has no CSU ID), a student
        card by its CSU ID. During a student session, a card with no CSU ID is the same card if its UID matches
        (a weak reader can fail to open the CSU sector), and otherwise counts as absent."""
        if scan is None:
            return "absent"
        if session.active_temp:
            return "same" if scan.uid_hex == session.active_card_uid else "other"
        if scan.csu_id is None:
            return "same" if session.active_card_uid and scan.uid_hex == session.active_card_uid else "absent"
        return "same" if scan.csu_id == session.active_csu_id else "other"

    def _ended_by_lockout(self, session, reader):
        """End the session if the server says so. Returns the reason, or None."""
        reason = session.lockout_reason()
        if reason:
            card = (session.active_card_uid, session.active_csu_id)
            session.end_for_lockout(reason)
            if reason == "outside_hours":
                # "Lab closed / Session ended" stays up until the card is removed, even once hours open.
                session.hold_card(*card)
        return reason

    def _watch_card(self, session, reader):
        """'removed' when the card left the reader, 'new_card' when a different card was presented (the session
        is then ended), or the reason the server ended it."""
        absence_start = None
        while True:
            reason = self._ended_by_lockout(session, reader)
            if reason:
                return reason
            state = self._classify(session, reader.read_card_ex())
            if state == "same":
                absence_start = None
            elif state == "other":
                logger.info("[SESSION] New card detected mid-session.")
                session.show("New card mid-sesh", "Resetting...", color="red", delay=LCD_LINE_DELAY)
                session.force_end_session()
                return "new_card"
            elif absence_start is None:
                absence_start = time.time()
            elif time.time() - absence_start >= self.CARD_GONE_SECONDS:
                session.show("Card removed", "Waiting for reinsert", color="yellow")
                return "removed"
            time.sleep(0.5)

    def _grace_period(self, session, reader):
        grace_period = int(session.db.get_setting("grace_period_seconds", default=CARD_GRACE_PERIOD_DEFAULT))
        end_time = time.time() + grace_period
        while time.time() < end_time:
            reason = self._ended_by_lockout(session, reader)
            if reason:
                return reason
            remaining = int(end_time - time.time())
            session.lcd.display("Remove detected", f"Reinsert: {remaining}s", color="yellow")

            state = self._classify(session, reader.read_card_ex())
            if state == "same":
                session.show("Session", "resumed", color="green", delay=1)
                session.resume_session()
                return "resumed"
            if state == "other":
                session.show("New card at grace", "Resetting...", color="red", delay=LCD_LINE_DELAY)
                session.force_end_session()
                return "new_card"
            time.sleep(1)

        session.force_end_session()
        logger.info("[SESSION] Ended after grace period.")
        return "timeout"

    def run_cardless(self, session, reader, seconds):
        """Watch a cardless session. It ends when its time is up (`seconds`, None for no limit), when the dashboard
        stops it, or when a card is put on the reader. A card already there at the start counts once it has left.
        Returns why it ended."""
        deadline = None if seconds is None else time.monotonic() + seconds
        absence_start = None
        armed = False
        shown = None
        while True:
            reason = session.lockout_reason()
            if reason:
                session.end_for_lockout(reason)
                return reason
            stop = session.lockout.cardless_stop if session.lockout else None
            if stop == "time_up" or (deadline is not None and time.monotonic() >= deadline):
                session.show("Time is up", "Session ended", color="red", delay=LCD_LINE_DELAY)
                session.force_end_session(quiet=True, reason="time_up")
                return "time_up"
            if stop:
                session.show("Stopped from", "the dashboard", color="red", delay=LCD_LINE_DELAY)
                session.force_end_session(quiet=True, reason="stopped")
                return "stopped"

            if reader.read_card_ex() is None:
                if absence_start is None:
                    absence_start = time.monotonic()
                elif time.monotonic() - absence_start >= self.CARD_GONE_SECONDS:
                    armed = True
            elif armed:
                logger.info("[SESSION] Card read; cardless access ends.")
                session.force_end_session(quiet=True, reason="card")
                return "card"
            else:
                absence_start = None

            line2 = "Until card read" if deadline is None else _time_left(deadline - time.monotonic())
            if line2 != shown:
                session.lcd.display(session.display_name[:16], line2, color="green")
                shown = line2
            time.sleep(0.5)


def _time_left(seconds):
    minutes = max(1, math.ceil(seconds / 60))
    return f"{minutes // 60}h{minutes % 60:02d}m left" if minutes >= 60 else f"{minutes}m left"


KINDS = {kind.name: kind for kind in (Attended,)}


def load_kind(name=MACHINE_KIND):
    kind = KINDS.get(name)
    if kind is None:
        logger.error(f"[KIND] Unknown machine kind {name!r} in config.json; running as attended.")
        kind = Attended
    return kind()
