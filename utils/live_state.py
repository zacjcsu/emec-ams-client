"""Sends what the control reports right now to the dashboard's machine_live table, and each change of its run state to
machine_run_state (dashboard migration 079). The dashboard's CNC page shows them.

An alarm also goes to the machine's events, at INFO, so it shows there without raising a problem.
"""
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb

from db.server_sync import get_server_connection
from utils.haas_mdc import TABLES_LATHE
from utils.timeutil import utc_now
from utils.usb_drive import RUN_PROGRAM_FILE, RUN_STATE_FILE, run_state

logger = logging.getLogger("haas")

REFRESH_SECONDS = 30   # written at least this often, so the dashboard can tell the Pi is still there
# A control stopped at M00 or M01 still says busy. The spindle stops there, and in a tool change for a few seconds.
STOPPED_AFTER_SECONDS = 60
STATES = {"IDLE": "Idle", "FEED HOLD": "Feed hold", "ALARM ON": "Alarm"}
# The USB drive takes a lost link as Off only after this. A classic misses a few answers in a row now and then.
OFF_AFTER_SECONDS = 120
# A classic's inputs, named from the 2007 electrical service manual, and the value that means trouble. Regen overheat
# reads 1 on all three classics with no alarm, so it's taken as active at 0.
CLASSIC_FAULTS = {"estop": (1007, 1), "low_air": (1011, 1), "low_lube": (1012, 1), "over_voltage": (1010, 1),
                  "regen": (1013, 0)}
FAULT_NAMES = {"estop": "E-stop", "low_air": "Low air", "low_lube": "Low lube", "over_voltage": "Over voltage",
               "regen": "Regen overheat"}
FAULT_EVENT_SECONDS = 5   # held this long, a fault goes to the machine's events. Low air blinks at some tool changes.
# The hourly table values, each sent when it changes (dashboard migration 101).
UPSERT_VALUE = """INSERT INTO cnc_values (machine_id, var, value) VALUES (%s, %s, %s)
                  ON CONFLICT (machine_id, var) DO UPDATE SET value = EXCLUDED.value, at = now()
                  WHERE cnc_values.value IS DISTINCT FROM EXCLUDED.value"""


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


def _clock_offset(values, now):
    """Seconds the control's clock is ahead of the Pi's. #3011 is YYMMDD and #3012 HHMMSS."""
    d, t = _macro(values, 3011), _macro(values, 3012)
    if not d or t is None:
        return None
    d, t = int(d), int(t)
    try:
        control = datetime(2000 + d // 10000, d // 100 % 100, d % 100, t // 10000, t // 100 % 100, t % 100)
    except ValueError:
        return None
    return round((control - now).total_seconds())


def snapshot(values, mtc, lathe=False, now=None):
    """The control's current values by name, leaving out the ones it didn't give. `values` and `mtc` are
    HaasMonitor's. `now` is the Pi's local time, for the control's clock."""
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
    faults = [name for name, (var, bad) in CLASSIC_FAULTS.items() if _macro(values, var) == bad]
    if alarm and not alarm["text"] and faults:
        alarm["text"] = ", ".join(FAULT_NAMES[f] for f in faults)
    doors = [_macro(values, v) for v in (11066, 11067, 1008) if f"Q600 {v}" in values]   # 1008 on a classic
    motion = _macro(values, 4001)
    remaining = _number(mtc.get("CycleRemainingTime"))
    classic = "Q600 1007" in values
    estop = {"TRIGGERED": True, "ARMED": False}.get(mtc.get("EmergencyStop"))
    position = {"x": _macro(values, 5041), "z": _macro(values, 5042)} if lathe else \
        {"x": _macro(values, 5041), "y": _macro(values, 5042), "z": _macro(values, 5043)}
    position = {k: v for k, v in position.items() if v is not None}
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
        "estop": "estop" in faults if classic else estop,
        "faults": faults if classic else None,
        "servos": _flag(_macro(values, 1100)),   # the lathe's hydraulic pump
        "single_block": _flag(_macro(values, 3030)),
        "f_word": _macro(values, 4109),
        "s_word": _macro(values, 4119),
        "next_tool": _macro(values, 4120),
        "h_word": _macro(values, 4111),
        "block": _macro(values, 4114),
        "q_word": _macro(values, 4117),
        "r_word": _macro(values, 4118),
        "g_distance": _macro(values, 4003),   # 90 or 91
        "g_length": _macro(values, 4008),     # 43, 44 or 49
        "g_cycle": _macro(values, 4009),      # 80 with no canned cycle
        "g_offset": _macro(values, 4012),     # 54 to 59, or 154
        "position": position or None,
        "part_s": _macro(values, 3023),
        "feed_time_s": _macro(values, 3022),
        "clock_offset_s": _clock_offset(values, now or datetime.now()),
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
        # A classic answers "S/N, 1105673" and "SOFTWARE, VER M18.14B".
        "serial": _after(values.get("Q100"), "SERIAL NUMBER") or _after(values.get("Q100"), "S/N"),
        "software": _after(values.get("Q101"), "SOFTWARE VERSION")
        or (_after(values.get("Q101"), "SOFTWARE") or "").removeprefix("VER ") or None,
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
        self._state_at = None
        self._alarm = None
        self._kept = {}                  # program and parts, which Q500 leaves out while running
        self._still_at = None            # when the spindle stopped while running
        self._written = None             # (connected, data) last written
        self._written_at = 0.0
        self._sent = {}                  # table var -> value last sent
        self._fault_since = {}           # fault -> when it came on
        self._shared = None              # ((state, program), when) last written for the USB drive
        self._held = run_state(RUN_STATE_FILE) or None   # what the drive goes by, from before a restart if fresh
        self._held_program = None
        self._off_since = None

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def read(self):
        """(connected, data), and queues a run state change."""
        connected = self.monitor.connected
        lathe = self.monitor.tables is TABLES_LATHE
        data = snapshot(self.monitor.values, self.monitor.mtc, lathe) if connected else {}
        if connected:
            for key in ("program", "parts"):
                if key in data:
                    self._kept[key] = data[key]
                elif key in self._kept:
                    data[key] = self._kept[key]
        else:
            self._kept = {}
        now = utc_now()
        if data.get("state") == "Running" and data.get("rpm") == 0:
            self._still_at = self._still_at or now
            if (now - self._still_at).total_seconds() >= STOPPED_AFTER_SECONDS:
                data["state"] = "Stopped"
        else:
            self._still_at = None
        state = data.get("state", "Off" if not connected else None)
        if self.monitor.tried:
            self._for_drive(state if connected else None, data.get("program"), connected)
        if state and state != self._state:
            at = now
            if state == "Stopped":
                # After Running, which has the same time when the spindle was already stopped
                at = max(self._still_at, self._state_at + timedelta(milliseconds=1))
            self._state, self._state_at = state, at
            self._runs.append((at, state))
        faults = data.get("faults", [])
        for fault in faults:
            since = self._fault_since.setdefault(fault, now)
            if since and (now - since).total_seconds() >= FAULT_EVENT_SECONDS:
                logger.info(f"[HAAS] {FAULT_NAMES[fault]}.", extra={"code": "cnc_input"})
                self._fault_since[fault] = None   # once until it clears
        for fault in [f for f in self._fault_since if f not in faults]:
            del self._fault_since[fault]
        alarm = data.get("alarm")
        if alarm != self._alarm:
            self._alarm = alarm
            if alarm:
                name = " ".join(x for x in (alarm["code"], alarm["text"]) if x)
                logger.info(f"[HAAS] Alarm{' ' + name if name else ''}.", extra={"code": "cnc_alarm"})
        return connected, data

    def _for_drive(self, state, program, connected):
        """A blank answer keeps the last state, and so does a lost link until OFF_AFTER_SECONDS. Either could
        otherwise read as idle and let the drive change under a running program."""
        if connected:
            self._off_since = None
            self._held = state or self._held
            self._held_program = program or self._held_program
        else:
            self._off_since = self._off_since or time.monotonic()
            if time.monotonic() - self._off_since >= OFF_AFTER_SECONDS:
                self._held, self._held_program = "Off", None
        if self._held:
            self._share(self._held, self._held_program)

    def _share(self, state, program):
        """The USB drive service changes the drive only while this says Idle, so a program running from it isn't cut
        off. It counts the file as unknown once it's older than REFRESH_SECONDS three times over. The program goes
        first, so the state's time stands for both."""
        if self._shared and self._shared[0] == (state, program) and time.monotonic() - self._shared[1] < REFRESH_SECONDS:
            return
        try:
            for path, value in ((RUN_PROGRAM_FILE, program), (RUN_STATE_FILE, state)):
                with open(path + ".tmp", "w") as f:
                    f.write(value or "")
                os.replace(path + ".tmp", path)
            self._shared = ((state, program), time.monotonic())
        except OSError as e:
            if self._shared is not False:
                logger.info(f"[HAAS] Cannot write {RUN_STATE_FILE}: {e}")
            self._shared = False

    def write(self, conn):
        connected, data = self.read()
        if (connected, data) != self._written or time.monotonic() - self._written_at >= REFRESH_SECONDS:
            conn.execute("""INSERT INTO machine_live (machine_id, connected, data, seen_at) VALUES (%s, %s, %s, now())
                            ON CONFLICT (machine_id) DO UPDATE
                            SET connected = EXCLUDED.connected, data = EXCLUDED.data, seen_at = now()""",
                         (self.machine_id, connected, Jsonb(data)))
            self._written, self._written_at = (connected, data), time.monotonic()
        if connected:
            values = self.monitor.values
            changed = []
            for command in self.monitor.tables:
                text = _after(values.get(command), "MACRO")
                v = _number(text and text.rsplit(",", 1)[-1].strip())
                var = int(command.split()[1])
                if v is not None and self._sent.get(var) != v:
                    changed.append((var, v))
            if changed:
                with conn.cursor() as cur:
                    cur.executemany(UPSERT_VALUE, [(self.machine_id, var, v) for var, v in changed])
                self._sent.update(changed)
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
                    self._written, self._sent = None, {}
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
