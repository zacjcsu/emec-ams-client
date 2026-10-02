import time
import logging
from db.server_sync import sync_local_from_server, push_access_requests, push_user_update, remote_access_decision
from config.constants import MACHINE_ID
from utils.startup_check import startup_sequence
from config.constants import LCD_LINE_DELAY

logger = logging.getLogger("validator")

# Refusals whose message stays on the LCD until the card is removed (or access returns, except outside hours).
HELD_REASONS = ("user_disabled", "group_disabled", "outside_hours")

def validate_card(csu_id, uid_num, db, lcd, temp=False, hold_until_removed=None):
    """Access check for a person. `temp` marks a temporary card: uid_num is then None, so no student UID is
    recorded against the user or their access request. `hold_until_removed(recheck, csu_id, reason)`, if
    given, is called after a held refusal is shown. It returns True if `recheck()` said the user got access while
    the card was still on the reader (the scan then carries on as a grant), False once the card was removed.
    Returns (csu_id, name), or (None, reason) when refused. Starting the session and the power is the caller's."""
    logger.info(f"[VALIDATOR] {'Temp card' if temp else 'Card'} scanned: {csu_id}")
    # Ask the server so dashboard changes apply to this scan; the local cache is only a fallback.
    decision = remote_access_decision(csu_id, MACHINE_ID)
    source = "server"
    if decision is None:
        decision = db.access_decision(csu_id, MACHINE_ID)
        source = "local cache"
    allowed, reason, via = decision
    logger.info(f"[VALIDATOR] Decision for {csu_id} ({source}): allowed={allowed} reason={reason} via={via}")

    user = db.get_user(csu_id)
    if allowed and not user:
        # Approved on the dashboard since the last sync; pull it so the name and UID are known.
        try:
            sync_local_from_server()
            user = db.get_user(csu_id)
        except Exception as e:
            logger.error(f"[VALIDATOR] Sync for new user failed: {e}")

    if reason in ("unknown_user", "no_permission"):
        lcd.display("Access Denied", "Raising req",  color="red")
        time.sleep(3)
        if db.access_request_exists(csu_id, MACHINE_ID):
            lcd.display("Already sent", "Please wait", color="red")
            logger.info(f"[ACCESS] Request already exists for {csu_id}")
        else:
            db.insert_access_request(csu_id, MACHINE_ID, uid_fallback=uid_num)
            push_access_requests()
            lcd.display("Request raised", "Please wait", color="yellow")
            logger.info(f"[ACCESS] Request raised for {csu_id}")
        time.sleep(LCD_LINE_DELAY)

        startup_sequence(lcd, db)
        return None, reason

    if reason in HELD_REASONS:
        # user_disabled: `via` carries the dashboard's message, line 1, a newline, then an optional line 2.
        if reason == "user_disabled":
            line1, _, line2 = (via or "").partition("\n")
            line1 = line1 or "User disabled"
        elif reason == "outside_hours":
            line1, line2 = "Access Denied", "Outside hours"
        else:
            line1, line2 = "Group disabled", ""
        lcd.display(line1[:16], line2[:16], color="red")
        logger.warning(f"[ACCESS] Denied: {csu_id} ({reason}: {via!r})")
        restored = False
        if hold_until_removed:
            # Keep the message up while the card is on the reader, asking the server whether access came back.
            def recheck():
                d = remote_access_decision(csu_id, MACHINE_ID)
                return bool(d and d[0])
            restored = hold_until_removed(recheck, csu_id, reason)
        else:
            time.sleep(LCD_LINE_DELAY)
        if not restored:
            return None, reason
        logger.info(f"[ACCESS] {csu_id} regained access with the card still present")
        allowed = True           # carry on to the grant below

    if not allowed:
        # outside_hours, unknown_machine
        line2 = {"outside_hours": "Outside hours"}.get(reason, "Contact admin")
        lcd.display("Access Denied", line2, color="red")
        logger.warning(f"[ACCESS] Denied: {csu_id} ({reason})")
        time.sleep(LCD_LINE_DELAY)
        return None, reason

    display_name = user["name"] if user and user["name"] else str(csu_id)

    if uid_num is not None and db.ensure_user_uid(csu_id, uid_num):
        logger.info(f"[SYNC] UID updated for {csu_id}, syncing to server")
        push_user_update(csu_id)

    logger.info(f"[ACCESS] Granted to {csu_id} - {display_name}")
    return csu_id, display_name
