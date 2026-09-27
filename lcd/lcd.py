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

    def display(self, line1="", line2="", color="white"):
        self.lcd.setRGB(*COLORS.get(color, COLORS["white"]))

        self.lcd.clear()
        self.lcd.setCursor(0, 0)
        self.lcd.printout(str(line1)[:16])
        self.lcd.setCursor(0, 1)
        self.lcd.printout(str(line2)[:16])

    def clear(self):
        self.lcd.clear()
