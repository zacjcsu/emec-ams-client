import logging
import os
import signal
import threading
import time
from db.server_sync import get_server_connection
from config.constants import HEARTBEAT_PUSH_SECONDS, RESTART_POLL_SECONDS

logger = logging.getLogger("heartbeat")

UTC_NOW = "(now() AT TIME ZONE 'UTC')"


class HeartbeatMonitor:
    """Three jobs over one persistent database connection, so the dashboard never has to reach into the Pi:

    * heartbeat: refresh machine.last_heartbeat every HEARTBEAT_PUSH_SECONDS, using the SERVER's clock.
      The dashboard shows a machine as online while the heartbeat is recent. Only that column is
      written: status stays whatever the Pi or the dashboard (maintenance) last set.
    * restart: if machine.restart_requested_at is later than when this process started, restart the
      app. Comparing to the start time (both on the server clock) means a request is honoured once
      and never loops.
    * screen: set machine.screen_down_since while `screen` (lcd.LCD) is down, and clear it once it answers.
      The dashboard then shows the machine as out of service.
    """

    def __init__(self, machine_id, screen=None):
        self.machine_id = machine_id
        self.screen = screen
        self._screen_reported = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)
        self._started_at = None  # server time when this process first reached the server

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        conn = None
        failing = False
        last_beat = None
        while not self._stop.is_set():
            try:
                if conn is None or conn.closed:
                    conn = get_server_connection(timeout=3)
                    conn.autocommit = True
                    if self._started_at is None:
                        self._started_at = conn.execute(f"SELECT {UTC_NOW} AS t").fetchone()["t"]
                now = time.monotonic()

                if last_beat is None or now - last_beat >= HEARTBEAT_PUSH_SECONDS:
                    conn.execute(
                        f"UPDATE machine SET last_heartbeat = {UTC_NOW} WHERE machine_id = %s",
                        (self.machine_id,))
                    last_beat = now

                row = conn.execute(
                    "SELECT restart_requested_at AS r FROM machine WHERE machine_id = %s",
                    (self.machine_id,)).fetchone()
                if row and row["r"] and row["r"] > self._started_at:
                    logger.warning(f"[HEARTBEAT] Restart requested at {row['r']}; restarting.")
                    # SIGTERM runs main.py's exit handler (relay off, session closed); systemd's
                    # Restart=always brings the app back.
                    os.kill(os.getpid(), signal.SIGTERM)
                    return

                down = bool(self.screen and self.screen.down)
                if down != self._screen_reported:
                    conn.execute(
                        "UPDATE machine SET screen_down_since = CASE WHEN %s THEN COALESCE(screen_down_since, now()) END "
                        "WHERE machine_id = %s", (down, self.machine_id))
                    self._screen_reported = down
                if failing:
                    logger.info("[HEARTBEAT] Server reachable again.")
                    failing = False
            except Exception as e:
                if not failing:
                    logger.error(f"[HEARTBEAT] Server unreachable or query failed: {e}")
                    failing = True
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                conn = None
            self._stop.wait(RESTART_POLL_SECONDS)
