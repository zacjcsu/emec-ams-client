#!/usr/bin/env python

from utils import hardware_stubs  # noqa: F401  (must be imported first, see utils/hardware_stubs.py)
from utils.startup_check import startup_sequence
from rfid.reader import RFIDReader
from rfid.scan_flow import ScanFlow, SessionStart
from utils.card_activity import CardActivity
from relay.session_manager import SessionManager, recover_orphaned_sessions, QUIET_END_REASONS
from relay.kinds import load_kind
from relay.controller import RelayController
from lcd.lcd import LCD
from utils.leds import StatusLEDs
from utils.idle_display import IdleDisplay
from utils.lockout import LockoutMonitor
from utils.heartbeat import HeartbeatMonitor
import time
import signal
import sys
from db.local_db import LocalDB
from config.constants import CARD_POLL_INTERVAL, MACHINE_ID, STATUS_MAINTENANCE, STATUS_OFFLINE
from db.server_sync import push_machine_status, cardless_begin
import logging
from logging.handlers import TimedRotatingFileHandler
import os

os.makedirs("logs", exist_ok=True)
# At midnight, not every 24 h from start: the nightly restart reset that clock, so rotation came at random or not at all.
# 200 days keeps a whole semester, at about 1 MB a day.
file_handler = TimedRotatingFileHandler("logs/sync.log", when="midnight", backupCount=200)
# stdout reaches the journal via systemd.
stream_handler = logging.StreamHandler()
formatter = logging.Formatter(
    '[%(asctime)s] %(levelname)s [%(name)s]: %(message)s',
    datefmt="%Y-%m-%d %H:%M:%S"
)
file_handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)
logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler])
logger = logging.getLogger("main")
kind = load_kind()
logger.info("[STARTUP] EMEC-AMS starting (machine_id=%s, kind=%s)", MACHINE_ID, kind.name)

lcd = LCD()
db = LocalDB()
relay = RelayController()
leds = StatusLEDs()
reader = RFIDReader(leds=leds)
lockout = LockoutMonitor(relay, kind)
heartbeat = HeartbeatMonitor(MACHINE_ID)
session_mgr = SessionManager(db, lcd, relay, lockout)
idle = IdleDisplay(lcd, db, lockout)
activity = CardActivity(MACHINE_ID)
flow = ScanFlow(reader, db, lcd, activity)

def exit_handler(sig, frame):
    # De-energise first: everything below can raise, and the machine must not
    # be left live by a failed shutdown.
    relay.turn_off()
    # Close any open session so its usage is recorded (this is also how a dashboard restart lands).
    try:
        session_mgr.force_end_session(reason="restart")
    except Exception:
        logger.exception("[SHUTDOWN] Could not close the session.")
    lcd.display("Shutting down...")
    # The cached status can predate a maintenance lock, and the server refuses a push that would clear it.
    db.update_machine_status(MACHINE_ID, STATUS_MAINTENANCE if lockout.maintenance_active else STATUS_OFFLINE)
    db.update_machine_heartbeat(MACHINE_ID)
    push_machine_status(db, MACHINE_ID)
    lcd.clear()
    leds.stop()
    sys.exit(0)

signal.signal(signal.SIGINT, exit_handler)
signal.signal(signal.SIGTERM, exit_handler)

def cardless_start():
    """A cardless session from the dashboard to start now, or None."""
    cardless_id = lockout.take_cardless()
    if not cardless_id:
        return None
    row = cardless_begin(cardless_id)
    if not row or not row["ok"]:
        logger.info(f"[MAIN] Cardless access {cardless_id} was not started.")
        return None
    started = SessionStart(row["csu_id"], row["name"] or row["csu_id"] or "Cardless access", None, False)
    started.cardless_id, started.session_id, started.seconds = cardless_id, row["session_id"], row["seconds"]
    return started

def main():
    # Before the heartbeat thread starts: its first beat would replace the previous run's last heartbeat,
    # which is what an unfinished session is closed at.
    try:
        recover_orphaned_sessions(db)
    except Exception:
        logger.exception("[MAIN] Could not recover unfinished sessions.")
    lockout.start()
    heartbeat.start()
    activity.start()
    skip_startup = False   # set after a session ended by a disabled user, to keep their message on the LCD
    while True:
        try:
            if skip_startup:
                skip_startup = False
            elif lockout.estop_active or lockout.maintenance_active:
                # Already known to be locked out (e.g. this loop just restarted right after maintenance
                # or an emergency shutdown ended the previous session): the idle loop below already shows
                # the right screen and keeps checking for it to lift, so a full resync here would only
                # flicker "Syncing online" over that message every few seconds for no reason. A cold boot
                # straight into a lockout still gets its one-time sync from the branch below, since the
                # lockout thread's first poll has not necessarily completed yet at that point.
                pass
            elif not startup_sequence(lcd, db):
                time.sleep(5)
                continue

            idle.reset()
            while True:
                if lockout.estop_active:
                    idle.tick()
                    time.sleep(CARD_POLL_INTERVAL)
                    continue
                job = activity.take_job()
                if job:
                    flow.run_job(job)
                    idle.reset()
                    continue
                started = cardless_start()
                if started:
                    break
                if lockout.maintenance_active:
                    # Only a temp card issued to bypass maintenance on this machine can start a session.
                    scan = reader.read_card_ex()
                    started, drew = flow.process_maintenance(scan) if scan else (None, False)
                    if started:
                        break
                    if not scan:
                        flow.no_card()
                    if drew:
                        idle.reset()
                    idle.tick()
                    time.sleep(CARD_POLL_INTERVAL)
                    continue
                scan = reader.read_card_ex()
                if scan:
                    started = flow.process(scan)
                    if started:
                        break
                    if scan.csu_id is not None:
                        startup_sequence(lcd, db)   # a refused student card: refresh the cache and the screen
                    idle.reset()
                else:
                    flow.no_card()
                    idle.tick()
                time.sleep(CARD_POLL_INTERVAL)

            flow.session_started()
            if started.cardless_id:
                session_mgr.start_session(started.csu_id, started.display_name, session_id=started.session_id,
                                          cardless_id=started.cardless_id)
                ended = kind.run_cardless(session_mgr, reader, started.seconds)
            else:
                session_mgr.start_session(started.csu_id, started.display_name, started.card_uid, started.temp,
                                          started.bypass)
                ended = kind.run(session_mgr, reader)
            skip_startup = ended in QUIET_END_REASONS
        except Exception:
            logger.exception("[MAIN] Unhandled error in main loop; recovering.")
            relay.turn_off()
            try:
                session_mgr.force_end_session(reason="error")
            except Exception:
                logger.exception("[MAIN] Could not cleanly close the session.")
            time.sleep(5)

if __name__ == "__main__":
    main()
