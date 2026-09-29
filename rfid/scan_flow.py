"""What to do with whatever is on the reader while the machine is idle (dashboard contract:
PI_ACCESS_CHECK.md, "Temporary cards"). Runs on the main thread, the only one that touches the reader.

Order: the normal student-card path first and unchanged. Only when the card is not a student card is
temp_card_lookup() called. A temp card that checks out goes through the same access check as anyone else.
Cards that did not start a session are reported to the dashboard so a temp card can be programmed on this reader.
Refused cards are also sent to the dashboard's scan log, once each time a card is put on the reader.
"""
import logging
import time
from config.constants import LCD_LINE_DELAY, LCD_MESSAGES, MACHINE_ID
from db.server_sync import temp_card_lookup, temp_card_verify, temp_card_finish, temp_card_maintenance_bypass
from rfid.card_io import CardIO, CardLost, data_block
from rfid.temp_writer import program_card
from rfid.validator import validate_card

logger = logging.getLogger("scan_flow")

# Screen text for a temporary card that is refused (16 chars per line).
REJECT_TEXT = {
    "card_lost": ("Card reported", "lost"),
    "card_revoked": ("Card revoked", "See staff"),
    "card_retired": ("Card retired", "See staff"),
    "expired": ("Card expired", "See staff"),
    "no_active_issue": ("Card not active", "See staff"),
    "bad_secret": ("Card not valid", "See staff"),
    "read_failed": ("Card not valid", "See staff"),
}

LOOKUP_TTL = 2.0      # seconds a lookup result is reused for a card that stays on the reader
MISSES_TO_REMOVE = 3  # polls with no card before it counts as removed (debounce)


class SessionStart:
    def __init__(self, csu_id, display_name, card_uid, temp, bypass=False):
        self.csu_id, self.display_name, self.card_uid, self.temp = csu_id, display_name, card_uid, temp
        self.bypass = bypass   # may run while this machine is in maintenance
        self.cardless_id = self.session_id = self.seconds = None   # set for cardless access from the dashboard


class ScanFlow:
    def __init__(self, reader, db, lcd, activity):
        self.reader = reader
        self.io = CardIO(reader.reader)
        self.db, self.lcd, self.activity = db, lcd, activity
        self._reset_arrival(None)
        self._misses = 0

    def _reset_arrival(self, uid_hex):
        self._uid = uid_hex
        self._blank = None
        self._lookup = None        # (time, result) cached per arrival
        self._shown = False        # a rejection was already put on the LCD for this arrival
        self._denied = False       # this arrival was refused; do not retry until the card is removed and returns
        self._logged = False       # this arrival's refusal was sent to the scan log

    def _refused(self, scan, csu_id, reason, at=None):
        if not self._logged:
            self._logged = True
            self.activity.refused(scan.uid_hex, csu_id, reason or "refused", at)

    # ------------------------------------------------------------ polling
    def session_started(self):
        """The card in the reader now belongs to a session; forget the arrival."""
        self._reset_arrival(None)
        self.activity.set_present(None)

    def no_card(self):
        """Call on every idle poll that sees no card. After a few in a row the card counts as removed."""
        self._misses += 1
        if self._misses >= MISSES_TO_REMOVE and self._uid is not None:
            self._reset_arrival(None)
            self.activity.set_present(None)

    def _hold_until_removed(self, recheck=None):
        """Hold a refusal on the LCD until the card leaves. True if `recheck` said access came back first."""
        return self.reader.wait_until_removed(poll=0.3, recheck=recheck)

    def process(self, scan):
        """Handle a card on the reader. Returns a SessionStart if the card started a session, else None
        (having shown a message and/or reported the card to the dashboard)."""
        self._misses = 0
        if scan.uid_hex != self._uid:
            self._reset_arrival(scan.uid_hex)

        if scan.csu_id is not None:                      # a student card: the normal path, unchanged
            scanned_at = time.monotonic()                # a refusal can hold the card for a while
            csu_id, name = validate_card(scan.csu_id, scan.uid_num, self.db, self.lcd,
                                         hold_until_removed=self._hold_until_removed)
            if csu_id:
                self.activity.set_present(None)
                return SessionStart(csu_id, name, scan.uid_hex, False)
            self._refused(scan, scan.csu_id, name, scanned_at)
            self.activity.set_present(scan.uid_hex, blank=False)   # a student card is never blank
            return None

        return self._not_a_student_card(scan)

    def process_maintenance(self, scan):
        """While this machine is in maintenance, only a temp card issued to bypass it here can start a session.
        Anything else is ignored. Returns (SessionStart or None, whether the LCD was changed)."""
        self._misses = 0
        if scan.uid_hex != self._uid:
            self._reset_arrival(scan.uid_hex)
        if scan.csu_id is not None:
            self._refused(scan, scan.csu_id, "maintenance")
            return None, False
        if self._denied:
            return None, False
        lk = self._cached_lookup(scan.uid_hex)
        if not lk or not lk["ok"]:
            self._refused(scan, None, lk["reason"] if lk else "server_offline")
            return None, False
        bypass = temp_card_maintenance_bypass(scan.uid_hex, MACHINE_ID)
        if not bypass:
            self._denied = bypass is False    # retry only if the server was unreachable
            if self._denied:
                self._refused(scan, None, "maintenance")
            return None, False
        logger.info(f"[TEMP] {scan.uid_hex} may bypass maintenance on {MACHINE_ID}")
        started = self._temp_login(scan, lk)
        if started:
            started.bypass = True
        return started, True

    # ------------------------------------------------------------ not a student card
    def _cached_lookup(self, uid_hex):
        now = time.monotonic()
        if self._lookup and now - self._lookup[0] < LOOKUP_TTL:
            return self._lookup[1]
        result = temp_card_lookup(uid_hex)
        self._lookup = (now, result)
        return result

    def _reject(self, line1, line2):
        """Show a refusal once per arrival, not on every poll while the card rests on the reader."""
        if not self._shown:
            self.lcd.display(line1, line2, color="red")
            time.sleep(LCD_LINE_DELAY)
            self.lcd.display(*LCD_MESSAGES["startup_next"])
            self._shown = True

    def _not_a_student_card(self, scan):
        lk = self._cached_lookup(scan.uid_hex)
        if lk is None:
            # Temp cards are online only; never fall back to a local copy.
            self._reject("Server offline", "Card not read")
            self._refused(scan, None, "server_offline")   # queued until the server is back
            return None

        if lk["ok"] and not self._denied:
            started = self._temp_login(scan, lk)
            if started:
                # A card that bypasses maintenance here keeps running if maintenance is set mid-session.
                started.bypass = temp_card_maintenance_bypass(scan.uid_hex, MACHINE_ID) is True
                self.activity.set_present(None)
                return started
        elif lk["reason"] in REJECT_TEXT:
            self._reject(*REJECT_TEXT[lk["reason"]])
        # unknown_card / not_temporary: silent, as any unrecognised card is today
        if not lk["ok"]:
            self._refused(scan, None, lk["reason"])

        self._report_presence(scan)
        return None

    def _report_presence(self, scan):
        """Report a card that did not start a session, with the blank check done once per arrival."""
        if self._blank is None:
            try:
                self._blank = self.io.is_blank()
            except CardLost:
                return          # gone again; the next poll starts over
            except Exception:
                logger.exception("[SCAN] Blank check failed")
                self._blank = False
            logger.info(f"[SCAN] Card {scan.uid_hex} on the reader, blank={self._blank}")
        self.activity.set_present(scan.uid_hex, self._blank)

    def _temp_login(self, scan, lk):
        """Steps 3 to 5 of the contract: read the secret with the issue's key, verify it, then the normal access check."""
        sector = int(lk["sector"])
        key = list(lk["sector_key"])
        try:
            self.io.fresh()
            if not self.io.auth(data_block(sector), key):
                logger.warning(f"[TEMP] {scan.uid_hex}: authentication with the issue key failed")
                self._denied = True
                self._reject(*REJECT_TEXT["read_failed"])
                self._refused(scan, None, "read_failed")
                return None
            secret = self.io.read(data_block(sector))
            self.io.r.MFRC522_StopCrypto1()
        except CardLost:
            return None
        if not secret or len(secret) != 16:
            self._denied = True
            self._reject(*REJECT_TEXT["read_failed"])
            self._refused(scan, None, "read_failed")
            return None

        v = temp_card_verify(scan.uid_hex, secret)
        if v is None:
            self._reject("Server offline", "Card not read")
            self._refused(scan, None, "server_offline")
            return None
        if not v["allowed"]:
            logger.warning(f"[TEMP] {scan.uid_hex}: verify refused ({v['reason']})")
            self._denied = True
            self._reject(*REJECT_TEXT.get(v["reason"], ("Card not valid", "See staff")))
            self._refused(scan, v.get("csu_id"), v["reason"])
            return None

        scanned_at = time.monotonic()
        csu_id, name = validate_card(v["csu_id"], None, self.db, self.lcd, temp=True,
                                     hold_until_removed=self._hold_until_removed)
        if not csu_id:
            self._refused(scan, v["csu_id"], name, scanned_at)
            self._denied = True       # the person is not allowed on this machine right now
            self.lcd.display(*LCD_MESSAGES["startup_next"])
            return None
        return SessionStart(csu_id, name, scan.uid_hex, True)

    # ------------------------------------------------------------ programming jobs
    def run_job(self, job):
        """Program the card on the reader for a claimed job, report the outcome, and wait for the card to be removed."""
        logger.info(f"[TEMP] Programming job {job['issue_id']} for card {job['card_uid']}")
        self.activity.set_present(None)
        self.lcd.display("Programming", "card...", color="yellow")
        ok, detail = program_card(self.io, job)
        answer = None
        for attempt in range(3):
            answer = temp_card_finish(job["issue_id"], ok, detail)
            if answer is not None:
                break
            time.sleep(1)
        logger.info(f"[TEMP] Job {job['issue_id']}: ok={ok} detail={detail} server={answer}")

        if ok and answer == "active":
            self.lcd.display("Card ready", "Remove card", color="green")
        elif ok:
            self.lcd.display("Card written", "See dashboard", color="yellow")
        else:
            self.lcd.display("Write failed", str(detail or "")[:16], color="red")
        # The card stays on the reader after programming; do not let it start a session until it is removed.
        self.reader.wait_until_removed(max_seconds=60)
        self._reset_arrival(None)
        self.lcd.display(*LCD_MESSAGES["startup_next"])
