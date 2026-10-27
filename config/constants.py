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
                data.get("contactor_switch") == "inverted",  # it is closed while the contactor is in
                int(data.get("contactor_pin", 37)),          # header pin, ground is pin 39
            )
    except Exception as e:
        logger.warning(f"Could not load config/config.json: {e}")
        return ("UNKNOWN", "Unnamed Machine", "Unknown Type", "attended", False, False, 37)

MACHINE_ID, MACHINE_NAME, MACHINE_TYPE, MACHINE_KIND, CONTACTOR_SWITCH, CONTACTOR_INVERTED, CONTACTOR_PIN = load_machine_config()
MACHINE_ID = MACHINE_ID.casefold()


def load_machine_data_config():
    """How to read the machine, from "machine_data" in config.json, or None.
    "control" picks the reader (CONTROLS in main.py), or "contactor" for a contactor switch. The other keys are its
    settings."""
    try:
        with open("config/config.json", "r") as f:
            data = json.load(f).get("machine_data")
    except Exception:
        return None   # load_machine_config has already warned
    return dict(data) if isinstance(data, dict) else None

MACHINE_DATA = load_machine_data_config()
UPLOADS = ("share", "usb", "serial", "none")


def load_program_upload():
    """How the dashboard's programs reach the control, from "program_upload" in config.json: "share" (Samba, on an
    NGC), "usb" (utils/usb_drive.py), "serial" (RS-232, on a classic) or "none". Without it, a classic control takes
    them over RS-232 and other machines share their program folder."""
    try:
        with open("config/config.json", "r") as f:
            upload = json.load(f).get("program_upload")
    except Exception:
        upload = None
    if upload in UPLOADS:
        return upload
    return "serial" if (MACHINE_DATA or {}).get("control") == "haas-classic" else "share"


PROGRAM_UPLOAD = load_program_upload()
# Programs go over the reader's port.
PROGRAM_SERIAL = PROGRAM_UPLOAD == "serial" and (MACHINE_DATA or {}).get("control") == "haas-classic"


def load_program_folder():
    """Where the CNC programs from the dashboard go, from "program_folder" in config.json. Off unless it exists."""
    try:
        with open("config/config.json", "r") as f:
            folder = json.load(f).get("program_folder", "/home/emec/haas-programs")
    except Exception:
        folder = "/home/emec/haas-programs"
    return folder if folder and os.path.isdir(folder) else None

PROGRAM_FOLDER = load_program_folder() if PROGRAM_UPLOAD in ("share", "usb") else None


def load_fitted():
    """Whether the screen and the card reader are fitted, from "screen" and "reader" in config.json. Both are unless
    set to false."""
    try:
        with open("config/config.json", "r") as f:
            data = json.load(f)
    except Exception:
        data = {}
    return data.get("screen", True) is not False, data.get("reader", True) is not False

SCREEN_FITTED, READER_FITTED = load_fitted()


def load_camera():
    """Whether to look for a USB camera, from "camera" in config.json. On unless set to false."""
    try:
        with open("config/config.json", "r") as f:
            return json.load(f).get("camera", True) is not False
    except Exception:
        return True

CAMERA = load_camera()
PROGRAM_MANIFEST = "data/programs.json"
PROGRAM_POLL_SECONDS = 5

# A contactor switch can also be set as "machine_data": {"control": "contactor", "pin": 40, "inverted": true}.
if MACHINE_DATA and MACHINE_DATA.get("control") == "contactor":
    CONTACTOR_SWITCH = True
    CONTACTOR_INVERTED = bool(MACHINE_DATA.get("inverted", False))
    CONTACTOR_PIN = int(MACHINE_DATA.get("pin", 37))
    MACHINE_DATA = None
# The Pi knows when the spindle runs, from a contactor switch or the drive (utils/power_monitor.py).
SPINDLE_SENSED = CONTACTOR_SWITCH or bool(MACHINE_DATA and MACHINE_DATA.get("control") == "yaskawa-v1000")

# === Relay and Card Constants ===
RELAY_PIN = 11
LED_READER_PIN = 16       # GPIO23, D1
LED_HEARTBEAT_PIN = 18    # GPIO24, D2
# CONTACTOR_PIN is set above from "contactor_pin" in config.json. 37 is GPIO26, 40 is GPIO21.
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



