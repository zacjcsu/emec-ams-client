import threading

import RPi.GPIO as GPIO
from config.constants import RELAY_PIN

class RelayController:
    def __init__(self):
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BOARD)
        GPIO.setup(RELAY_PIN, GPIO.OUT)
        GPIO.output(RELAY_PIN, GPIO.LOW)
        self._locked_out = False
        self.is_on = False
        # The lockout thread and the main loop both switch it. Reentrant, since the exit handler can interrupt a switch.
        self._lock = threading.RLock()

    def set_lockout(self, locked):
        """While locked out (emergency shutdown) the relay is forced off and turn_on does nothing."""
        with self._lock:
            self._locked_out = locked
            if locked:
                GPIO.output(RELAY_PIN, GPIO.LOW)
                self.is_on = False

    def turn_on(self):
        with self._lock:
            if self._locked_out:
                return
            GPIO.output(RELAY_PIN, GPIO.HIGH)
            self.is_on = True

    def turn_off(self):
        with self._lock:
            GPIO.output(RELAY_PIN, GPIO.LOW)
            self.is_on = False
