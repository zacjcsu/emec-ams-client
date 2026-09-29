import time
from lcd.RGB1602 import RGB1602

COLORS = {
    "green": (0, 255, 0),
    "red": (255, 0, 0),
    "yellow": (255, 100, 0),
    "gray": (80, 80, 80),
    "white": (255, 255, 255),
}


class LCD:
    def __init__(self):
        self.lcd = RGB1602(16, 2)
        self.needs_setup = False

    def display(self, line1="", line2="", color="white"):
        try:
            self._show(line1, line2, color)
        except OSError:
            # An I2C glitch can reset the screen chip. It comes back with the display off until it is set up again.
            self.needs_setup = True
        if self.needs_setup:
            time.sleep(0.1)
            self.lcd.begin(16, 2)
            self.needs_setup = False
            self._show(line1, line2, color)

    def _show(self, line1, line2, color):
        self.lcd.setRGB(*COLORS.get(color, COLORS["white"]))

        self.lcd.clear()
        self.lcd.setCursor(0, 0)
        self.lcd.printout(str(line1)[:16])
        self.lcd.setCursor(0, 1)
        self.lcd.printout(str(line2)[:16])

    def clear(self):
        self.lcd.clear()
