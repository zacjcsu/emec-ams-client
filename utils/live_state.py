"""Sends what the control reports right now to the dashboard's machine_live table, and each change of its run state to
machine_run_state (dashboard migration 079). The dashboard's CNC page shows them.

An alarm also goes to the machine's events, at INFO, so it shows there without raising a problem.
"""
import logging
import re
import threading
import time
from collections import deque

import psycopg
from psycopg.types.json import Jsonb

from db.server_sync import get_server_connection
from utils.timeutil import utc_now

logger = logging.getLogger("haas")

REFRESH_SECONDS = 30   # written at least this often, so the dashboard can tell the Pi is still there
STATES = {"IDLE": "Idle", "FEED HOLD": "Feed hold", "ALARM ON": "Alarm"}


def _after(value, prefix):
    """'USING TOOL, 9' -> '9' for prefix 'USING TOOL'. A '>' left over from the answer before is skipped."""
    m = re.fullmatch(re.escape(prefix) + r",\s*(.*)", (value or "").lstrip("> "))
    return m and m.group(1).strip()


def _number(text):
    try:
        n = float(text)
    except (TypeError, ValueError):
        return None
    return int(n) if n.is_integer() else n


def _macro(values, var):
    # A classic control names the variable: "MACRO, 3027,       0.000000".
    text = _after(values.get(f"Q600 {var}"), "MACRO")
    return _number(text and text.rsplit(",", 1)[-1].strip())


def _seconds(text):
    """'00012:03:45' -> 43425."""
    m = re.fullmatch(r"(\d+):(\d\d):(\d\d)", text or "")
    return m and int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def _flag(n):
    return None if n is None else n != 0


def snapshot(values, mtc):
    """The control's current values by name, leaving out the ones it didn't give. `values` and `mtc` are
    HaasMonitor's."""
    # A classic control with Setting 41 off leaves out the spaces: "PROGRAM,O05005,IDLE,PARTS,13567".
    q500 = values.get("Q500", "").lstrip("> ")
    m = re.fullmatch(r"PROGRAM,\s*(.*?),\s*(.*?),\s*PARTS,\s*(\d+)", q500)
    if re.match(r"STATUS,?\s*BUSY", q500):
        state = "Running"
    elif m:
        state = STATES.get(m.group(2), m.group(2).capitalize())
    else:
        state = None
    condition = mtc.get("MachineCondition")
    if isinstance(condition, tuple) and condition[0] == "Fault":
        state = "Alarm"
    alarm = None
    if state == "Alarm":
        active = mtc.get("ActiveAlarms", "")
        code, text = (condition[1], condition[2]) if isinstance(condition, tuple) else ("", "")
        if active and active != "NO ACTIVE ALARMS":
            text = text or active
        alarm = {"code": code, "text": text}
    doors = [_macro(values, v) for v in (11066, 11067, 1008) if f"Q600 {v}" in values]   # 1008 on a classic
    motion = _macro(values, 4001)
    remaining = _number(mtc.get("CycleRemainingTime"))
    data = {
        "state": state,
        "alarm": alarm,
        "program": mtc.get("Program") or (m and m.group(1)) or None,
        "parts": _number(m and m.group(3)),
        "mode": mtc.get("Mode") or _after(values.get("Q104"), "MODE"),
        "tool": _number(_after(values.get("Q201"), "USING TOOL")),
        "rpm": _macro(values, 3027),
        "load": _macro(values, 1098),
        "motion": None if motion is None else "Rapid" if motion == 0 else "Feed",
        "coolant": _flag(_macro(values, 12018 if "Q600 12018" in values else 1105)),
        "door_open": any(doors) if doors else None,
        "estop": {"TRIGGERED": True, "ARMED": False}.get(mtc.get("EmergencyStop")),
        "single_block": _flag(_macro(values, 3030)),
        "feed_override": _number(mtc.get("FeedrateOverride")),
        "rapid_override": _number(mtc.get("RapidOverride")),
        "spindle_override": _number(mtc.get("SpindleSpeedOverride")),
        "this_cycle_s": _number(mtc.get("ThisCycle")),
        "remaining_s": remaining if remaining is not None and remaining >= 0 else None,
        "last_cycle_s": _seconds(_after(values.get("Q303"), "LAST CYCLE")),
        "prev_cycle_s": _seconds(_after(values.get("Q304"), "PREV CYCLE")),
        "m30_1": _number(_after(values.get("Q402"), "M30 #1")),
        "m30_2": _number(_after(values.get("Q403"), "M30 #2")),
        "tool_changes": _number(_after(values.get("Q200"), "TOOL CHANGES")),
        "power_on_s": _seconds(_after(values.get("Q300"), "P.O. TIME")),
        "cycle_start_s": _seconds(_after(values.get("Q301"), "C.S. TIME")),
        "serial": _after(values.get("Q100"), "SERIAL NUMBER"),
        "software": _after(values.get("Q101"), "SOFTWARE VERSION"),
        "model": _after(values.get("Q102"), "MODEL"),
    }
    return {k: v for k, v in data.items() if v is not None}


class LiveState:
    def __init__(self, machine_id, monitor):
        self.machine_id = machine_id
        self.monitor = monitor
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="live", daemon=True)
        self._runs = deque(maxlen=500)   # run state changes not yet written
        self._state = None
        self._alarm = None
        self._kept = {}                  # program and parts, which Q500 leaves out while running
        self._written = None             # (connected, data) last written
        self._written_at = 0.0

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def read(self):
        """(connected, data), and queues a run state change."""
        connected = self.monitor.connected
        data = snapshot(self.monitor.values, self.monitor.mtc) if connected else {}
        if connected:
            for key in ("program", "parts"):
                if key in data:
                    self._kept[key] = data[key]
                elif key in self._kept:
                    data[key] = self._kept[key]
        else:
            self._kept = {}
        state = data.get("state", "Off" if not connected else None)
        if state and state != self._state:
            self._state = state
            self._runs.append((utc_now(), state))
        alarm = data.get("alarm")
        if alarm != self._alarm:
            self._alarm = alarm
            if alarm:
                name = " ".join(x for x in (alarm["code"], alarm["text"]) if x)
                logger.info(f"[HAAS] Alarm{' ' + name if name else ''}.", extra={"code": "cnc_alarm"})
        return connected, data

    def write(self, conn):
        connected, data = self.read()
        if (connected, data) != self._written or time.monotonic() - self._written_at >= REFRESH_SECONDS:
            conn.execute("""INSERT INTO machine_live (machine_id, connected, data, seen_at) VALUES (%s, %s, %s, now())
                            ON CONFLICT (machine_id) DO UPDATE
                            SET connected = EXCLUDED.connected, data = EXCLUDED.data, seen_at = now()""",
                         (self.machine_id, connected, Jsonb(data)))
            self._written, self._written_at = (connected, data), time.monotonic()
        while self._runs:
            at, state = self._runs[0]
            conn.execute("INSERT INTO machine_run_state (machine_id, at, state) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                         (self.machine_id, at, state))
            self._runs.popleft()

    def _run(self):
        conn = None
        failing = False
        while not self._stop.is_set():
            try:
                if conn is None or conn.closed:
                    conn = get_server_connection(timeout=3)
                    conn.autocommit = True
                    self._written = None
                self.write(conn)
                if failing:
                    logger.info("[HAAS] Sending live data again.")
                    failing = False
            except Exception as e:
                # The heartbeat already reports the server being out of reach.
                self.read()
                if not failing:
                    (logger.info if isinstance(e, psycopg.OperationalError) else logger.warning)(
                        f"[HAAS] Cannot send live data: {e}")
                    failing = True
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                conn = None
            self._stop.wait(self.monitor.poll_seconds)
