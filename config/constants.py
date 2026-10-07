from dotenv import load_dotenv
load_dotenv()
import os
import json
import logging

logger = logging.getLogger("constants")

# === Device ID (CPU Serial) ===
def get_cpu_serial():
    try:
        with open("/proc/cpuinfo", "r") as f:
            for line in f:
                if line.startswith("Serial"):
                    return line.strip().split(":")[1].strip()
    except Exception as e:
        logger.warning(f"Could not read CPU serial from /proc/cpuinfo: {e}")
        return "0000000000000000"

DEVICE_ID = get_cpu_serial()

# === Load Config from JSON ===
def load_machine_config():
    try:
        with open("config/config.json", "r") as f:
            data = json.load(f)
            return (
                data.get("machine_id", "UNKNOWN"),
                data.get("machine_name", "Unnamed Machine"),
                data.get("machine_type", "Unknown Type"),
                data.get("kind", "attended"),   # how sessions run, see relay/kinds.py
                bool(data.get("contactor_switch", False)),   # a microswitch is fitted, see utils/power_monitor.py
            )
    except Exception as e:
        logger.warning(f"Could not load config/config.json: {e}")
        return ("UNKNOWN", "Unnamed Machine", "Unknown Type", "attended", False)

MACHINE_ID, MACHINE_NAME, MACHINE_TYPE, MACHINE_KIND, CONTACTOR_SWITCH = load_machine_config()
MACHINE_ID = MACHINE_ID.casefold()


# === Relay and Card Constants ===
RELAY_PIN = 11
LED_READER_PIN = 16       # GPIO23, D1
LED_HEARTBEAT_PIN = 18    # GPIO24, D2
CONTACTOR_PIN = 37        # GPIO26. NC microswitches go between this and ground on pin 39, in series if more than one.
CONTACTOR_SETTLE_SECONDS = 0.3
HEARTBEAT_INTERVAL = 0.5  # toggle every 0.5s = 1 Hz blink
READER_BLINK_DURATION = 0.1
CARD_POLL_INTERVAL = 0.5  # seconds
READER_CHECK_SECONDS = 5  # how often the reader chip is checked for having reset itself
CARD_GRACE_PERIOD_DEFAULT = 10  # fallback if not in system_settings
LCD_LINE_DELAY = 2  # seconds
SCREEN_RETRY_SECONDS = 5  # how often a screen that stopped answering is set up again
IDLE_SCAN_SCREEN_SECONDS = 8       # idle: how long "Scan CSU ID" shows...
IDLE_LAST_USED_SCREEN_SECONDS = 4  # ...before "Last Used" shows this long
IDLE_MESSAGE_SCREEN_SECONDS = 4    # ...then the maintenance record's message, if it has one
LOCAL_DB_PATH = "data/local.db"

# === Database (PostgreSQL on the dashboard VM) ===
DB_ENV = {
    "host": os.getenv("DB_HOST"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASS"),
    "database": os.getenv("DB_NAME", "emec_access"),
    "sslmode": os.getenv("DB_SSLMODE", "prefer"),
}

# === Dashboard control (all pull-based: the Pi talks to the database, nothing calls into the Pi) ===
EMERGENCY_POLL_SECONDS = 2       # how often system_settings.emergency_shutdown is checked
ENFORCE_ACCESS_DURING_SESSION = True  # re-check the signed-in user against the server mid-session
ACCESS_RECHECK_SECONDS = 5       # ...this often (lab closing, group disabled, permission revoked)
HEARTBEAT_PUSH_SECONDS = 30      # how often machine.last_heartbeat is refreshed on the server
RESTART_POLL_SECONDS = 5         # how often machine.restart_requested_at is checked
RESTART_MAX_AGE_SECONDS = 600    # an older restart request is ignored
UPDATE_FLAG = ".update-requested"               # watched by emec-ams-update.path: updates now
UPDATE_AFTER_SESSION = ".update-after-session"  # becomes UPDATE_FLAG once no session is open
CARDLESS_START_SECONDS = 20      # a claimed cardless request not started by then is dropped
CARD_REPORT_SECONDS = 2          # temp cards: presence report and job poll interval while a card rests on the reader
CARD_REPORT_MAX_AGE = 3          # ...and presence stops being reported if the main loop has not refreshed it this recently

# === Machine Status Enum ===
STATUS_MAINTENANCE = "maintenance"
STATUS_OFFLINE = "offline"
STATUS_NEUTRAL = "neutral"
STATUS_IN_USE = "in use"

# === LCD Messages ===
LCD_MESSAGES = {
    "start": ["All Clear.", "Welcome to EMEC!"],
    "startup_next": ["Scan CSU ID", "to start"],
    "maintenance": [MACHINE_NAME, "Out of order"],
    "db_error": ["Server Error", "Check conn."],
    "offline": ["Server offline", "Using last sync"],
}



