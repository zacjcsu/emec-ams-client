"""The maintenance reason, passed from the app to the drive service (utils/usb_drive.py) through MAINTENANCE_FILE.

The file exists while the machine is locked for maintenance. It holds the card reader screen lines, which may be
empty. A maintenance bypass session lifts the lock, so the file is removed while one runs.
"""
import os

MAINTENANCE_FILE = "data/maintenance"


def write(reason, path=MAINTENANCE_FILE):
    """reason is the text, or None for no maintenance."""
    if reason is None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".part", "w") as f:
        f.write(reason)
    os.replace(path + ".part", path)


def read(path=MAINTENANCE_FILE):
    """The reason, or None for no maintenance."""
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None
