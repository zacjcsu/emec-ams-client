"""Checks utils/live_state.py with answers the TM-1P sent, and a fake server connection. No Pi needed:

    .venv/bin/python tests/sim_live_state.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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


class Monitor:
    poll_seconds = 2
    connected = True
    values = IDLE
    mtc = {}


class Conn:
    def __init__(self):
        self.sql = []

    def execute(self, sql, args):
        self.sql.append((sql.split()[2], args))


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
mon.connected, mon.values = False, {}
live.write(conn)
check("off", conn.sql[0][1][1] is False and conn.sql[-1][1][2] == "Off")

sys.exit(1 if fails else 0)
