"""Replays card scenarios through the session code with fake hardware and a fake clock, and compares the
trace with tests/sessions.expected. Runs anywhere, no Pi needed:

    .venv/bin/python tests/sim_sessions.py            # check
    .venv/bin/python tests/sim_sessions.py --update   # after an intended change, save the new trace
"""
import os, re, sys, tempfile
from pathlib import Path
from types import SimpleNamespace as NS

repo = str(Path(__file__).resolve().parent.parent)
os.chdir(tempfile.mkdtemp())   # away from this Pi's config.json, so the trace is the same everywhere
sys.path.insert(0, repo)
os.environ["EMEC_HARDWARE_STUBS"] = "1"
import logging
logging.disable(logging.CRITICAL)
from utils import hardware_stubs  # noqa

TRACE = []

class Clock:
    def __init__(self): self.t = 0.0
    def time(self): return self.t
    def monotonic(self): return self.t
    def sleep(self, s): self.t += s
CLOCK = Clock()

def ev(*a):
    TRACE.append(f"{CLOCK.t:7.1f} " + " ".join(str(x) for x in a))

class LCD:
    def display(self, l1="", l2="", color="white"): ev("lcd", repr(l1), repr(l2), color)
    def clear(self): ev("lcd-clear")

class Relay:
    def turn_on(self): ev("relay", "on")
    def turn_off(self): ev("relay", "off")
    def set_lockout(self, locked): ev("relay-lockout", locked)

class DB:
    def get_setting(self, k, default=None): return "10" if k == "grace_period_seconds" else default
    def __getattr__(self, name):
        return lambda *a, **k: ev("db", name, *a[:1])

class Reader:
    """script: list of (from_time, scan or None); the last entry at or before now wins."""
    def __init__(self, script): self.script = script
    def read_card_ex(self):
        cur = None
        for t, scan in self.script:
            if CLOCK.t >= t: cur = scan
        return cur

class Lockout:
    """events: list of (time, attr, value) applied as the clock passes them."""
    def __init__(self, events):
        self.events = sorted(events, key=lambda e: e[0])
        self._state = {"estop_active": False, "maintenance_active": False, "revoked_reason": None, "revoked_via": None}
        self.bypass_maintenance = False
    def _apply(self):
        while self.events and self.events[0][0] <= CLOCK.t:
            _, k, v = self.events.pop(0); self._state[k] = v
    def __getattr__(self, name):
        if name in ("estop_active", "maintenance_active", "revoked_reason", "revoked_via"):
            self._apply(); return self._state[name]
        raise AttributeError(name)
    def watch(self, csu, card=None, bypass_maintenance=False):
        self.bypass_maintenance = bool(bypass_maintenance and card); ev("watch", csu, card, self.bypass_maintenance)
    def unwatch(self): self.bypass_maintenance = False; ev("unwatch")

import relay.session_manager as sm
sm.time = CLOCK
for f in ("push_session_start", "sync_session_to_server", "push_user_status", "push_machine_status"):
    setattr(sm, f, (lambda name: (lambda *a, **k: ev("server", name)))(f))
import relay.kinds as kinds
kinds.time = CLOCK

STUDENT = NS(uid_hex="AAAA0001", csu_id="830000001", uid_num=1)
OTHER = NS(uid_hex="BBBB0002", csu_id="830000002", uid_num=2)
TEMP = NS(uid_hex="CCCC0003", csu_id=None, uid_num=3)
JUNK = NS(uid_hex="DDDD0004", csu_id=None, uid_num=4)

SCENARIOS = {
    "removed, grace runs out": dict(reader=[(0, STUDENT), (20, None)]),
    "removed and put back": dict(reader=[(0, STUDENT), (5, None), (10, STUDENT), (30, None)]),
    "missed read under 3 s": dict(reader=[(0, STUDENT), (5, None), (6.5, STUDENT), (15, None)]),
    "other student card": dict(reader=[(0, STUDENT), (8, OTHER)]),
    "other card during grace": dict(reader=[(0, STUDENT), (4, None), (9, OTHER)]),
    "non-student card is ignored": dict(reader=[(0, STUDENT), (4, JUNK), (12, None)]),
    "lab closes, card held": dict(reader=[(0, STUDENT), (20, None)], lockout=[(12, "revoked_reason", "outside_hours")]),
    "lab closes during grace": dict(reader=[(0, STUDENT), (4, None)], lockout=[(10, "revoked_reason", "outside_hours")]),
    "emergency shutdown": dict(reader=[(0, STUDENT)], lockout=[(6, "estop_active", True)]),
    "user disabled": dict(reader=[(0, STUDENT)], lockout=[(7, "revoked_via", "Gone\nSee staff"), (7, "revoked_reason", "user_disabled")]),
    "group disabled": dict(reader=[(0, STUDENT)], lockout=[(7, "revoked_reason", "group_disabled")]),
    "permission revoked": dict(reader=[(0, STUDENT)], lockout=[(9, "revoked_reason", "no_permission")]),
    "maintenance, no bypass": dict(reader=[(0, STUDENT)], lockout=[(5, "maintenance_active", True)]),
    "temp card removed": dict(reader=[(0, TEMP), (6, None)], temp=True),
    "temp card, other card": dict(reader=[(0, TEMP), (6, JUNK)], temp=True),
    "temp card lost": dict(reader=[(0, TEMP)], lockout=[(8, "revoked_reason", "card_lost")], temp=True),
    "bypass keeps running in maintenance": dict(reader=[(0, TEMP), (15, None)], lockout=[(5, "maintenance_active", True)], temp=True, bypass=True),
}

for name, sc in SCENARIOS.items():
    CLOCK.t = 0.0
    TRACE.append(f"=== {name}")
    lockout = Lockout(sc.get("lockout", []))
    session = sm.SessionManager(DB(), LCD(), Relay(), lockout)
    card = sc["reader"][0][1]
    temp = sc.get("temp", False)
    session.start_session(card.csu_id or "830000009", "Test User", card.uid_hex, temp, sc.get("bypass", False))
    ended = kinds.load_kind(sc.get("kind", "attended")).run(session, Reader(sc["reader"]))
    ev("ended", ended, "session", session.active_session_id is not None)

trace = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27}", "<session>", "\n".join(TRACE)) + "\n"
expected = Path(repo, "tests", "sessions.expected")
if "--update" in sys.argv:
    expected.write_text(trace)
    print(f"Saved {len(SCENARIOS)} scenarios to {expected}")
elif expected.read_text() == trace:
    print(f"OK: {len(SCENARIOS)} scenarios match")
else:
    import difflib
    sys.stdout.writelines(difflib.unified_diff(expected.read_text().splitlines(True), trace.splitlines(True),
                                               "expected", "now"))
    sys.exit(1)
