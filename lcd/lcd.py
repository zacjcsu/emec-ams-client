import logging
import time
from lcd.RGB1602 import RGB1602
from config.constants import SCREEN_RETRY_SECONDS

logger = logging.getLogger("lcd")

COLORS = {
    "green": (0, 255, 0),
    "red": (255, 0, 0),
    "yellow": (255, 100, 0),
    "gray": (80, 80, 80),
    "white": (255, 255, 255),
}


class LCD:
    """A screen that stops answering is marked `down` instead of raising. It is set up again every
    SCREEN_RETRY_SECONDS. main.py takes no new sessions until it answers."""

    def __init__(self):
        self.lcd = RGB1602(16, 2)
        self.needs_setup = True
        self.down = False
        self._tried = 0

    def display(self, line1="", line2="", color="white"):
        if self.down and (time.monotonic() - self._tried < SCREEN_RETRY_SECONDS or not self.retry()):
            return
        try:
            if not self.needs_setup:
                try:
                    self._show(line1, line2, color)
                    return
                except OSError:
                    # An I2C glitch can reset the screen chip. It comes back with the display off until it is set up again.
                    self.needs_setup = True
            time.sleep(0.1)
            self.lcd.begin(16, 2)
            self.needs_setup = False
            self._show(line1, line2, color)
        except OSError as e:
            self.down = True
            self._tried = time.monotonic()
            logger.error(f"[LCD] Screen not answering: {e}")

    def retry(self):
        """Set the screen up again. True once it answers."""
        self._tried = time.monotonic()
        try:
            self.lcd.begin(16, 2)
        except OSError:
            return False
        self.needs_setup = False
        self.down = False
        logger.info("[LCD] Screen answers again.")
        return True

    def _show(self, line1, line2, color):
        self.lcd.setRGB(*COLORS.get(color, COLORS["white"]))

        self.lcd.clear()
        self.lcd.setCursor(0, 0)
        self.lcd.printout(str(line1)[:16])
        self.lcd.setCursor(0, 1)
        self.lcd.printout(str(line2)[:16])

    def clear(self):
        try:
            self.lcd.clear()
        except OSError:
            pass
