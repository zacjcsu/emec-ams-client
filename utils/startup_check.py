import time
import socket
import logging
from config.constants import (
    LCD_MESSAGES, STATUS_MAINTENANCE, STATUS_NEUTRAL, MACHINE_ID, MACHINE_NAME, MACHINE_TYPE, LCD_LINE_DELAY, DB_ENV,
    DEVICE_ID,
)
from db.server_sync import sync_local_from_server, sync_finished_sessions, push_machine_status


logger = logging.getLogger("startup")


def get_local_ip():
    """The address this Pi uses to reach the server, shown in the dashboard's Edit dialog."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((DB_ENV["host"], DB_ENV["port"]))  # UDP: sends nothing, just picks the route
            return s.getsockname()[0]
    except Exception:
        return None


def startup_sequence(lcd, db):
    logger.info("[STEP] Starting system checks...")
    device_ip = get_local_ip()
    logger.info(f"[PASS] Device IP (reported to dashboard): {device_ip}")

    online = True
    try:
        lcd.display("Syncing online")
        sync_local_from_server()
        logger.info("[PASS] Server sync complete.")
        sync_finished_sessions()
    except Exception as e:
        logger.error(f"[ERROR] Server sync failed: {e}")
        if not db.get_machine(MACHINE_ID):
            lcd.display(*LCD_MESSAGES["db_error"], color="red")
            return False
        # Card checks already fall back to the last sync, so keep taking cards on it.
        logger.warning("[WARN] Server offline. Using the last sync.")
        lcd.display(*LCD_MESSAGES["offline"], color="yellow")
        time.sleep(LCD_LINE_DELAY)
        online = False

    machine = db.get_machine(MACHINE_ID)
    if not machine:
        lcd.display(f"Machine {MACHINE_ID}", "not registered", color="red")
        db.insert_machine_if_missing(MACHINE_ID, MACHINE_NAME, MACHINE_TYPE)
        push_machine_status(db, MACHINE_ID)
        logger.warning(f"[WARN] Machine {MACHINE_ID} not found. Inserting default.")

    machine = db.get_machine(MACHINE_ID)
    if machine["machine_status"] == STATUS_MAINTENANCE:
        lcd.display(*LCD_MESSAGES["maintenance"], color="yellow")
        logger.warning("[HALT] Machine in maintenance mode.")
        return False

    db.update_machine_status(MACHINE_ID, STATUS_NEUTRAL)
    db.update_machine_heartbeat(MACHINE_ID)
    db.update_machine_device(MACHINE_ID, DEVICE_ID)
    db.update_machine_ip(MACHINE_ID, device_ip)
    if online:
        push_machine_status(db, MACHINE_ID)
    logger.info(f"[PASS] Machine {MACHINE_ID} is neutral, heartbeat and address updated.")

    lcd.display(*LCD_MESSAGES["start"])
    time.sleep(2)
    lcd.display(*LCD_MESSAGES["startup_next"])

    time.sleep(LCD_LINE_DELAY)
    return True
