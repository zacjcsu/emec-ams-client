import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone

import RPi.GPIO as GPIO

from config.constants import CONTACTOR_PIN, CONTACTOR_SETTLE_SECONDS
from db.server_sync import get_server_connection, report_power_off, report_power_on

logger = logging.getLogger("power")


class PowerMonitor:
    """Watches normally closed microswitches on the machine's contactors and records when the machine is on.

    The switches are wired in series between CONTACTOR_PIN and ground, with the internal pull-up. They are closed
    while the contactors are out, so the pin reads low when the machine is off. A pulled-in contactor or a broken wire reads
    high, as on. Times are recorded with or without a session, and with whether the relay was on, so a
    contactor that was bypassed shows up.

    Changes wait in a queue until the server takes them, so they survive a network outage but not a restart.
    At start the current state is reported once, which closes a row left open while the app was down.
    """

    def __init__(self, machine_id, relay):
        self.machine_id = machine_id
        self.relay = relay
        self._queue = deque(maxlen=1000)   # (on, at, relay_on)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="power", daemon=True)
        GPIO.setmode(GPIO.BOARD)
        GPIO.setup(CONTACTOR_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _read(self):
        return GPIO.input(CONTACTOR_PIN) == GPIO.HIGH

    def _run(self):
        state = None
        seen, since = self._read(), time.monotonic()
        failing = False
        retry_at = 0.0   # the switch is still read while the server is down
        while not self._stop.is_set():
            now = self._read()
            if now != seen:
                seen, since = now, time.monotonic()
            elif seen != state and time.monotonic() - since >= CONTACTOR_SETTLE_SECONDS:
                state = seen
                logger.info(f"[POWER] Machine {'on' if state else 'off'} (relay {'on' if self.relay.is_on else 'off'})")
                self._queue.append((state, datetime.now(timezone.utc), self.relay.is_on))
            if self._queue and time.monotonic() >= retry_at:
                try:
                    self._send()
                    if failing:
                        logger.info("[POWER] Server reachable again.")
                        failing = False
                except Exception as e:
                    if not failing:
                        logger.error(f"[POWER] Report failed, will retry: {e}")
                        failing = True
                    retry_at = time.monotonic() + 5
            self._stop.wait(0.05)

    def _send(self):
        with get_server_connection(timeout=3) as conn:
            while self._queue:
                on, at, relay_on = self._queue[0]
                if on:
                    report_power_on(self.machine_id, at, relay_on, conn=conn)
                else:
                    report_power_off(self.machine_id, at, conn=conn)
                conn.commit()
                self._queue.popleft()
