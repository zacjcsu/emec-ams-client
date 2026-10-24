"""Checks utils/live_state.py with answers the TM-1P sent, and a fake server connection. No Pi needed:

    .venv/bin/python tests/sim_live_state.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import utils.live_state  # noqa: E402
from utils.live_state import LiveState, snapshot  # noqa: E402

fails = 0


def check(name, ok):
    global fails
    print(("ok   " if ok else "FAIL ") + name)
    fails += not ok


IDLE = {
    "Q100": "SERIAL NUMBER, 1140233", "Q101": "SOFTWARE VERSION, 100.17.000.2037", "Q102": "MODEL, TM-1P",
    "Q104": "MODE, MEM", "Q200": "TOOL CHANGES, 24107", "Q201": "USING TOOL, 9",
    "Q300": "P.O. TIME, 69020:17:53", "Q301": "C.S. TIME, 01831:48:52", "Q303": "LAST CYCLE, 00000:00:23",
    "Q304": "PREV CYCLE, 00000:04:28", "Q402": "M30 #1, 7789", "Q403": "M30 #2, 7789",
    "Q500": "PROGRAM, O00100, IDLE, PARTS, 7789", "Q600 3027": "MACRO, 0.0", "Q600 1098": "MACRO, 0.0",
    "Q600 4001": "MACRO, 0.0", "Q600 12018": "MACRO, 0.0", "Q600 11066": "MACRO, 0.0", "Q600 11067": "MACRO, 0.0",
    "Q600 3030": "MACRO, 0.0",
}
MTC = {"Program": "Operation 1.nc", "Mode": "AUTOMATIC", "EmergencyStop": "ARMED", "FeedrateOverride": "100",
       "RapidOverride": "25", "SpindleSpeedOverride": "100", "ThisCycle": "0", "CycleRemainingTime": "-12",
       "MachineCondition": ("Normal", "", ""), "ActiveAlarms": "NO ACTIVE ALARMS"}

d = snapshot(IDLE, MTC)
check("idle state", d["state"] == "Idle")
check("MTConnect program wins", d["program"] == "Operation 1.nc")
check("parts", d["parts"] == 7789)
check("tool", d["tool"] == 9)
check("power-on time in seconds", d["power_on_s"] == 69020 * 3600 + 17 * 60 + 53)
check("last cycle", d["last_cycle_s"] == 23)
check("rapid override", d["rapid_override"] == 25)
check("negative remaining left out", "remaining_s" not in d)
check("doors shut", d["door_open"] is False)
check("no alarm", "alarm" not in d)
check("serial", d["serial"] == "1140233")

running = dict(IDLE, **{"Q500": "STATUS BUSY", "Q600 3027": "MACRO, 4200.0", "Q600 1098": "MACRO, 31.5",
                        "Q600 4001": "MACRO, 1.0", "Q600 12018": "MACRO, 1.0"})
d = snapshot(running, {})
check("running state", d["state"] == "Running")
check("rpm", d["rpm"] == 4200 and d["load"] == 31.5)
check("feed move", d["motion"] == "Feed")
check("coolant on", d["coolant"] is True)
check("no program while busy on a classic", "program" not in d)

d = snapshot(dict(IDLE, **{"Q500": "PROGRAM, O00100, DOOR HOLD, PARTS, 7788"}), {})
check("other states in sentence case", d["state"] == "Door hold")
d = snapshot(dict(IDLE, **{"Q500": "PROGRAM, MDI, ALARM ON, PARTS, 7788"}), {})
check("classic alarm has no name", d["state"] == "Alarm" and d["alarm"] == {"code": "", "text": ""})
d = snapshot(IDLE, dict(MTC, MachineCondition=("Fault", "1022", "SPINDLE NOT TURNING AT CORRECT SPEED"),
                        ActiveAlarms=""))
check("NGC alarm name", d["state"] == "Alarm" and d["alarm"] == {"code": "1022", "text": "SPINDLE NOT TURNING AT CORRECT SPEED"})
d = snapshot(dict(IDLE, **{"Q600 11067": "MACRO, 1.0"}), MTC)
check("left door open", d["door_open"] is True)

# TM-2P's answers in a G83 cycle on 2026-10-23, a classic control.
PECK = dict(running, **{
    "Q100": "S/N, 1105673", "Q101": "SOFTWARE, VER M18.14B",
    "Q600 4109": "MACRO, 4109,      10.700000", "Q600 4119": "MACRO, 4119,    4600.000000",
    "Q600 4120": "MACRO, 4120,       7.000000", "Q600 4114": "MACRO, 4114,     635.000000",
    "Q600 4117": "MACRO, 4117,       0.100000", "Q600 4118": "MACRO, 4118,       0.200000",
    "Q600 4003": "MACRO, 4003,      90.000000", "Q600 4008": "MACRO, 4008,      43.000000",
    "Q600 4009": "MACRO, 4009,      83.000000", "Q600 4012": "MACRO, 4012,      54.000000",
    "Q600 5041": "MACRO, 5041,      -2.425000", "Q600 5042": "MACRO, 5042,      -3.825000",
    "Q600 5043": "MACRO, 5043,     -12.356700", "Q600 3022": "MACRO, 3022,  11685649.000000",
    "Q600 3023": "MACRO, 3023,    1550.000000", "Q600 3011": "MACRO, 3011,  261023.000000",
    "Q600 3012": "MACRO, 3012,  192810.000000",
    "Q600 1007": "MACRO, 1007,       0.000000", "Q600 1010": "MACRO, 1010,       0.000000",
    "Q600 1011": "MACRO, 1011,       0.000000", "Q600 1012": "MACRO, 1012,       0.000000",
    "Q600 1013": "MACRO, 1013,       1.000000", "Q600 1100": "MACRO, 1100,       1.000000",
})
d = snapshot(PECK, {}, now=datetime(2026, 10, 23, 19, 28, 10))
check("feed and speed words", d["f_word"] == 10.7 and d["s_word"] == 4600 and d["next_tool"] == 7)
check("block and peck", d["block"] == 635 and d["q_word"] == 0.1 and d["r_word"] == 0.2)
check("G-code groups", (d["g_distance"], d["g_length"], d["g_cycle"], d["g_offset"]) == (90, 43, 83, 54))
check("mill position", d["position"] == {"x": -2.425, "y": -3.825, "z": -12.3567})
check("part and feed timers", d["part_s"] == 1550 and d["feed_time_s"] == 11685649)
check("clock right", d["clock_offset_s"] == 0)
check("classic serial and software", d["serial"] == "1105673" and d["software"] == "M18.14B")
check("classic with no faults", d["faults"] == [] and d["estop"] is False and d["servos"] is True)
d = snapshot(PECK, {}, lathe=True)
check("lathe position is X and Z", d["position"] == {"x": -2.425, "z": -3.825})
d = snapshot(dict(PECK, **{"Q500": "PROGRAM,MDI,ALARM ON,PARTS,13568", "Q600 1007": "MACRO, 1007, 1.000000",
                           "Q600 1100": "MACRO, 1100, 0.000000"}), {})
check("classic e-stop names the alarm", d["estop"] is True and d["faults"] == ["estop"]
      and d["alarm"] == {"code": "", "text": "E-stop"} and d["servos"] is False)
d = snapshot(dict(PECK, **{"Q600 1011": "MACRO, 1011, 1.000000", "Q600 1013": "MACRO, 1013, 0.000000"}), {})
check("low air and regen", d["faults"] == ["low_air", "regen"])
check("NGC has no faults list", "faults" not in snapshot(IDLE, MTC))


class Monitor:
    poll_seconds = 2
    connected = True
    values = IDLE
    mtc = {}
    tables = ()


class Conn:
    def __init__(self):
        self.sql = []

    def execute(self, sql, args):
        self.sql.append((sql.split()[2], args))

    def cursor(self):
        conn = self

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def executemany(self, sql, rows):
                conn.sql.append((sql.split()[2], list(rows)))
        return Cursor()


clock = [datetime.now(timezone.utc)]
utils.live_state.utc_now = lambda: clock[0]
mon = Monitor()
live = LiveState("tm-test", mon)
conn = Conn()
live.write(conn)
check("first write sends live and a state", [s for s, _ in conn.sql] == ["machine_live", "machine_run_state"])
conn.sql.clear()
live.write(conn)
check("nothing new, nothing sent", conn.sql == [])
mon.values = running
live.write(conn)
check("running keeps the program and parts", conn.sql[0][1][2].obj.get("program") == "O00100"
      and conn.sql[0][1][2].obj.get("parts") == 7789)
check("state change sent", conn.sql[-1][0] == "machine_run_state" and conn.sql[-1][1][2] == "Running")
conn.sql.clear()
mon.values = dict(running, **{"Q600 3027": "MACRO, 0.0"})
clock[0] += timedelta(seconds=2)
live.write(conn)
check("spindle stopped a moment is still running", conn.sql[0][1][2].obj.get("state") == "Running"
      and conn.sql[-1][0] == "machine_live")
stopped_at = live._still_at
clock[0] += timedelta(seconds=61)
conn.sql.clear()
live.write(conn)
check("spindle stopped a minute is stopped", conn.sql[0][1][2].obj.get("state") == "Stopped")
check("stopped from when the spindle stopped", conn.sql[-1][1][1:] == (stopped_at, "Stopped"))
conn.sql.clear()
mon.values = running
live.write(conn)
check("spindle on again is running", conn.sql[-1][1][2] == "Running" and live._still_at is None)
conn.sql.clear()
started = LiveState("tm-test", Monitor())
started.monitor.values = dict(running, **{"Q600 3027": "MACRO, 0.0"})
started.write(conn)
clock[0] += timedelta(seconds=61)
started.write(conn)
runs = [a for s, a in conn.sql if s == "machine_run_state"]
check("stopped at start comes after running", [r[2] for r in runs] == ["Running", "Stopped"] and runs[0][1] < runs[1][1])
conn.sql.clear()
mon.connected, mon.values = False, {}
live.write(conn)
check("off", conn.sql[0][1][1] is False and conn.sql[-1][1][2] == "Off")

tables = LiveState("tm-test", Monitor())
tables.monitor.tables = ("Q600 2001", "Q600 2002", "Q600 5401")
tables.monitor.values = dict(IDLE, **{"Q600 2001": "MACRO, 2001, -12.727000", "Q600 2002": "MACRO, 2002, -9.225800"})
conn.sql.clear()
tables.write(conn)
sent = [a for s, a in conn.sql if s == "cnc_values"]
check("table values sent", sent == [[("tm-test", 2001, -12.727), ("tm-test", 2002, -9.2258)]])
conn.sql.clear()
tables.write(conn)
check("unchanged table values not sent again", not [a for s, a in conn.sql if s == "cnc_values"])
tables.monitor.values = dict(tables.monitor.values, **{"Q600 2002": "MACRO, 2002, -9.754200"})
tables.write(conn)
check("a changed one is sent", [a for s, a in conn.sql if s == "cnc_values"] == [[("tm-test", 2002, -9.7542)]])

faulty = LiveState("tm-test", Monitor())
faulty.monitor.values = dict(PECK, **{"Q600 1011": "MACRO, 1011, 1.000000"})
logged = []
utils.live_state.logger.info = lambda msg, **kw: logged.append((msg, kw.get("extra", {}).get("code")))
faulty.read()
clock[0] += timedelta(seconds=2)
faulty.read()
check("a blink of low air isn't an event", not [m for m, c in logged if c == "cnc_input"])
clock[0] += timedelta(seconds=4)
faulty.read()
faulty.read()
check("low air held 5 s is one event", [m for m, c in logged if c == "cnc_input"] == ["[HAAS] Low air."])

sys.exit(1 if fails else 0)
