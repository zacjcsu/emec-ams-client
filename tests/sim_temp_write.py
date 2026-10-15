"""Runs a blank card through being written as a temp card, with a fake reader, screen and server. No Pi needed:

    .venv/bin/python tests/sim_temp_write.py
"""
import os, sys, tempfile
from pathlib import Path
from types import SimpleNamespace as NS

repo = str(Path(__file__).resolve().parent.parent)
os.chdir(tempfile.mkdtemp())
sys.path.insert(0, repo)
os.environ["EMEC_HARDWARE_STUBS"] = "1"
import logging
logging.disable(logging.CRITICAL)
from utils import hardware_stubs  # noqa
from rfid import scan_flow

UID = "F4EBB0ZZ"
CARD = NS(uid_hex=UID, csu_id=None, uid_num=None)
SCREEN = []
state = {}


class LCD:
    def display(self, l1="", l2="", color="white"):
        SCREEN.append((l1, l2))


class IO:
    def __init__(self, _): pass
    def is_blank(self): return state["blank"]
    def fresh(self): pass
    def auth(self, block, key): return True
    def read(self, block): return [0] * 16
    r = NS(MFRC522_StopCrypto1=lambda: None)


class Reader:
    reader = None
    def wait_until_removed(self, **kw): state["waited"] = True


class Activity:
    def set_present(self, uid, blank=False, held=False): pass
    def refused(self, *a): pass


scan_flow.CardIO = IO
scan_flow.time.sleep = lambda s: None
scan_flow.temp_card_lookup = lambda uid: state["lookup"]
scan_flow.temp_card_verify = lambda uid, secret: {"allowed": True, "reason": None, "csu_id": "zz_test_1"}
scan_flow.temp_card_maintenance_bypass = lambda uid, m: state.get("bypass", False)
scan_flow.temp_card_finish = lambda issue, ok, detail: state["finish"]
scan_flow.program_card = lambda io, job: (state["write_ok"], None)
scan_flow.validate_card = lambda csu_id, uid_num, db, lcd, temp=False, hold_until_removed=None: (csu_id, "Test User")

HOLD = scan_flow.HOLD_TEXT
JOB = {"issue_id": 1, "card_uid": UID}


def fresh(**s):
    SCREEN.clear()
    state.clear()
    state.update(blank=True, lookup={"ok": False, "reason": "no_active_issue"}, finish="active", write_ok=True)
    state.update(s)
    return scan_flow.ScanFlow(Reader(), None, LCD(), Activity())


def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    return ok


results = []

for reason in ("no_active_issue", "unknown_card"):
    f = fresh(lookup={"ok": False, "reason": reason})
    for _ in range(5):
        assert f.process(CARD) is None
    results.append(check(f"blank card, {reason}: says to wait, once", SCREEN == [HOLD]))

f = fresh(blank=False, lookup={"ok": False, "reason": "no_active_issue"})
f.process(CARD)
results.append(check("written card not active: still says Card not active",
                     SCREEN[0] == scan_flow.REJECT_TEXT["no_active_issue"] and HOLD not in SCREEN))

f = fresh()
f.process(CARD)
f.run_job(JOB)
state["lookup"] = {"ok": True, "reason": None, "sector": 2, "sector_key": [0] * 6}
started = f.process(CARD)
results.append(check("written and ready: no removal, next poll starts the session",
                     not state.get("waited") and started is not None and started.temp and started.csu_id == "zz_test_1"
                     and ("Card ready", "") in SCREEN))

f = fresh(write_ok=False)
f.process(CARD)
f.run_job(JOB)
results.append(check("write failed: waits for removal", state.get("waited") is True))

f = fresh()
f.process(CARD)
gone = [f.no_card() for _ in range(4)]
results.append(check("blank card lifted off: redraw once", gone == [False, False, True, False]))

f = fresh(blank=False, lookup={"ok": False, "reason": "unknown_card"})
f.process(CARD)
gone = [f.no_card() for _ in range(4)]
results.append(check("other card lifted off: no redraw", gone == [False, False, False, False]))

f = fresh()
started, drew = f.process_maintenance(CARD)
started2, drew2 = f.process_maintenance(CARD)
results.append(check("in maintenance: says to wait, leaves the maintenance redraw alone",
                     SCREEN == [HOLD] and not drew and not drew2 and started is None))

print("OK" if all(results) else "FAILED")
sys.exit(0 if all(results) else 1)
