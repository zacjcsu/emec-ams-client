import logging
import os
import signal
import threading
import time
import psycopg
from db.server_sync import UTC_NOW, get_server_connection
from config.constants import HEARTBEAT_PUSH_SECONDS, RESTART_MAX_AGE_SECONDS, RESTART_POLL_SECONDS, UPDATE_AFTER_SESSION

logger = logging.getLogger("heartbeat")


class HeartbeatMonitor:
    """Three jobs over one persistent database connection, so the dashboard never has to reach into the Pi:

    * heartbeat: refresh machine.last_heartbeat every HEARTBEAT_PUSH_SECONDS, using the SERVER's clock.
      The dashboard shows a machine as online while the heartbeat is recent. Only that column is
      written: status stays whatever the Pi or the dashboard (maintenance) last set.
    * restart: if machine.restart_requested_at is later than when this process started, restart the
      app. Comparing to the start time (both on the server clock) means a request is honoured once
      and never loops. A request older than RESTART_MAX_AGE_SECONDS is ignored, so a Pi that was offline
      doesn't restart mid-session when it reconnects.
    * update: if machine.update_requested_at is later than when this process started, write UPDATE_AFTER_SESSION.
      The main loop updates once no session is open. The update restarts the app, so a request is honoured once.
    * screen: set machine.screen_down_since while `screen` (lcd.LCD) is down, and clear it once it answers.
    * events: send the log's warnings and errors queued by `events` (utils/pi_events.py).
    """

    def __init__(self, machine_id, screen=None, events=None):
        self.machine_id = machine_id
        self.screen = screen
        self.events = events
        self._screen_reported = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)
        self._started_at = None  # server time when this process first reached the server
        self._ignored_restart = None
        self._update_seen = None

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        conn = None
        failing = False
        events_failing = False
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
                    f"SELECT restart_requested_at AS r, restart_requested_at > {UTC_NOW} - make_interval(secs => %s) AS fresh, "
                    "update_requested_at AS u FROM machine WHERE machine_id = %s",
                    (RESTART_MAX_AGE_SECONDS, self.machine_id)).fetchone()
                if row and row["u"] and row["u"] > self._started_at and row["u"] != self._update_seen:
                    logger.info(f"[HEARTBEAT] Update requested at {row['u']}; it runs once no session is open.")
                    open(UPDATE_AFTER_SESSION, "w").close()
                    self._update_seen = row["u"]
                if row and row["r"] and row["r"] > self._started_at and not row["fresh"]:
                    if row["r"] != self._ignored_restart:
                        logger.info(f"[HEARTBEAT] Ignoring the restart requested at {row['r']}. It is too old.")
                        self._ignored_restart = row["r"]
                elif row and row["r"] and row["r"] > self._started_at:
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
                if self.events:
                    try:
                        self.events.send(conn)
                        events_failing = False
                    except psycopg.OperationalError:
                        raise
                    except Exception as e:
                        if not events_failing:
                            logger.warning(f"[HEARTBEAT] Cannot send events: {e}")
                            events_failing = True
                if failing:
                    logger.info("[HEARTBEAT] Server reachable again.")
                    failing = False
            except Exception as e:
                if not failing:
                    logger.error(f"[HEARTBEAT] Server unreachable or query failed: {e}", extra={"code": "server_unreachable"})
                    failing = True
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                conn = None
            self._stop.wait(RESTART_POLL_SECONDS)
