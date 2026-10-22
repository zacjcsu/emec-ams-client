"""Runs utils/yaskawa_v1000.py against a fake V1000 on a pseudo-terminal, checks the power monitor takes its run state,
and checks the "machine_data" settings for a drive and a contactor switch. No Pi needed:

    .venv/bin/python tests/sim_v1000.py
"""
import json, logging, os, select, subprocess, sys, tempfile, termios, threading, time
from pathlib import Path

repo = str(Path(__file__).resolve().parent.parent)
os.chdir(tempfile.mkdtemp())
os.makedirs("logs")
sys.path.insert(0, repo)
os.environ["EMEC_HARDWARE_STUBS"] = "1"   # this machine has no GPIO
from utils import hardware_stubs  # noqa: E402,F401
from utils import yaskawa_v1000 as v1000  # noqa: E402
from utils import power_monitor  # noqa: E402

fails = 0


def check(name, ok):
    global fails
    print(("ok   " if ok else "FAIL ") + name)
    fails += not ok


def wait_for(cond, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def log():
    return open("logs/machine_data.log").read()


# Registers as the drive holds them: stopped, 0 Hz, ready.
REGS = {0x20: 0x0004, 0x23: 4500, 0x24: 0, 0x25: 0, 0x26: 0, 0x27: 0, 0x2B: 0b0000101, 0x31: 312, 0x68: 31,
        0x7F: 0, 0x80: 0, 0x81: 0x07, 0x98: 1234, 0x99: 5, 0x5C: 6789, 0x5D: 12}


class FakeDrive:
    """Answers function 03 for address 31 like the manual's Appendix C. `off` stops it answering, like a drive
    without power. `garble` spoils the next answer's CRC."""

    def __init__(self):
        self.master, self.slave = os.openpty()
        self.device = os.ttyname(self.slave)
        self.off = threading.Event()
        self.garble = threading.Event()
        self.asked = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        buf = b""
        while True:
            select.select([self.master], [], [])
            buf += os.read(self.master, 1024)
            while len(buf) >= 8:
                frame, buf = buf[:8], buf[8:]
                if self.off.is_set() or v1000.crc16(frame) or frame[0] != 31 or frame[1] != 3:
                    continue
                start, count = int.from_bytes(frame[2:4], "big"), int.from_bytes(frame[4:6], "big")
                self.asked.append((start, count))
                if count > 16:
                    answer = bytes([31, 0x83, 0x03])
                else:
                    data = b"".join(REGS.get(start + i, 0).to_bytes(2, "big") for i in range(count))
                    answer = bytes([31, 3, 2 * count]) + data
                crc = v1000.crc16(answer).to_bytes(2, "little")
                if self.garble.is_set():
                    self.garble.clear()
                    crc = bytes([crc[0] ^ 0xFF, crc[1]])
                os.write(self.master, answer + crc)


check("CRC of the usual example", v1000.crc16(bytes.fromhex("01030000000A")).to_bytes(2, "little") == b"\xc5\xcd")

warnings = []
handler = logging.Handler()
handler.emit = lambda r: warnings.append(r.getMessage()) if r.levelno >= logging.WARNING else None
logging.getLogger("vfd").addHandler(handler)

drive = FakeDrive()
v1000.ANSWER_SECONDS = 0.2
mon = v1000.V1000Monitor(drive.device, address=31, poll_seconds=0.1)
mon.start()

check("connects", wait_for(lambda: mon.connected and "connected to" in log()))
attrs = termios.tcgetattr(mon.port.client.fd)
check("9600 8N1 without XON/XOFF", attrs[4] == termios.B9600 and not attrs[0] & termios.IXON
      and attrs[2] & termios.CS8 and not attrs[2] & termios.PARENB)
check("reads at most 16 registers at once", all(c <= 16 for _, c in drive.asked))
check("stopped", not mon.running and "run = 'off'  (snapshot)" in log())
check("reference in Hz", "reference Hz = '45.0'" in log())
check("inputs by terminal", "inputs = 'S1 S3'" in log())
check("run hours from two registers", "run hours = '12345'" in log())
check("kWh from two registers", "kWh = '12678.9'" in log())
check("previous fault by code", "previous fault = 'oC'" in log())

REGS.update({0x20: 0x0005, 0x24: 4512, 0x26: 1234, 0x27: 0xFFF2})
check("running", wait_for(lambda: mon.running))
check("output Hz logged", "output Hz = '45.1'" in log() and "current A = '12.3'" in log())
check("power is signed", "power = '-14'" in log())
lines = log().count("output Hz = ")
time.sleep(0.5)
check("unchanged values are not logged again", log().count("output Hz = ") == lines)

drive.garble.set()
time.sleep(0.5)
check("one bad answer doesn't drop the drive", mon.connected and mon.running and "disconnected" not in log())

REGS.update({0x20: 0x000C, 0x80: 0x07, 0x24: 0})
check("a fault is logged with its code", wait_for(lambda: "fault = 'oC'" in log()))
check("and goes to the machine's events", wait_for(lambda: "[VFD] Drive fault oC." in warnings))
check("not running after the fault", not mon.running)

REGS.update({0x20: 0x0005, 0x80: 0})
check("running again", wait_for(lambda: mon.running))
drive.off.set()
check("a drive without power is off", wait_for(lambda: not mon.running and not mon.connected and "disconnected" in log()))
check("values are cleared while it's gone", not mon.values)
drive.off.clear()
check("seen as soon as it answers again", wait_for(lambda: mon.connected and mon.running, 3))

# The power monitor records the drive's run state the way it records a contactor switch.
power_monitor.PowerMonitor._send = lambda self: (_ for _ in ()).throw(OSError("no server here"))
pm = power_monitor.PowerMonitor("zz_test", type("Relay", (), {"is_on": True})(), read=lambda: mon.running,
                                summary=mon.run_summary)
pm.start()
check("power monitor sees the spindle on", wait_for(lambda: [q[0] for q in pm._queue] == [True]))
REGS.update({0x20: 0x0004})
check("and off", wait_for(lambda: [q[0] for q in pm._queue] == [True, False]))

# A run of about 1 s forward at 30 Hz and 0.5 kW, then 1 s in reverse at 60 Hz and 1.5 kW.
REGS.update({0x20: 0x0005, 0x24: 3000, 0x27: 50})
check("on again", wait_for(lambda: [q[0] for q in pm._queue] == [True, False, True]))
time.sleep(1)
REGS.update({0x20: 0x0007, 0x24: 6000, 0x27: 150})
time.sleep(1)
REGS.update({0x20: 0x0004, 0x24: 0, 0x27: 0})
check("off with the run's totals", wait_for(lambda: len(pm._queue) == 4 and pm._queue[3][3]))
run = pm._queue[3][3]
check("the on has none", pm._queue[2][3] is None)
check("reverse time", 0.7 < run["reverse_seconds"] < 1.5)
check("speed", 35 < run["avg_hz"] < 55 and run["max_hz"] == 60)
check("power", 0.7 < run["avg_kw"] < 1.3 and run["max_kw"] == 1.5)
check("energy", abs(run["kwh"] - run["avg_kw"] * (run["reverse_seconds"] + 1) / 3600) < 0.0003)

REGS.update({0x20: 0x0005, 0x24: 3000, 0x27: 0xFFF2})
check("a new run", wait_for(lambda: len(pm._queue) == 5))
time.sleep(0.5)
REGS.update({0x20: 0x0004})
check("starts its totals afresh", wait_for(lambda: len(pm._queue) == 6 and pm._queue[5][3]
                                           and pm._queue[5][3]["reverse_seconds"] == 0
                                           and pm._queue[5][3]["max_kw"] == -0.14))
pm.stop()
mon.stop()


def settings(config):
    """The constants a config.json gives, read in a fresh process."""
    d = tempfile.mkdtemp()
    os.makedirs(f"{d}/config")
    with open(f"{d}/config/config.json", "w") as f:
        json.dump({"machine_id": "zz_test", **config}, f)
    code = ("import json; from config import constants as c; print(json.dumps([c.CONTACTOR_SWITCH, c.CONTACTOR_INVERTED, "
            "c.CONTACTOR_PIN, c.SPINDLE_SENSED, c.MACHINE_DATA]))")
    out = subprocess.run([sys.executable, "-c", code], cwd=d, env={**os.environ, "PYTHONPATH": repo},
                         capture_output=True, text=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


check("contactor in machine_data", settings({"machine_data": {"control": "contactor", "pin": 40, "inverted": True}})
      == [True, True, 40, True, None])
check("contactor the old way", settings({"contactor_switch": "inverted", "contactor_pin": 40}) == [True, True, 40, True, None])
check("contactor default pin", settings({"machine_data": {"control": "contactor"}}) == [True, False, 37, True, None])
drive_cfg = {"control": "yaskawa-v1000", "device": "/dev/ttyUSB0", "address": 31}
check("drive senses the spindle", settings({"machine_data": drive_cfg}) == [False, False, 37, True, drive_cfg])
check("a Haas doesn't", settings({"machine_data": {"control": "haas-ngc", "host": "x"}})[3] is False)

print("all passed" if not fails else f"{fails} failed")
sys.exit(1 if fails else 0)
