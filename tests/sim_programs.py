"""Runs utils/program_sync.py against a fake cnc_programs table. No Pi or server needed:

    .venv/bin/python tests/sim_programs.py
"""
import hashlib, os, sys, tempfile
from pathlib import Path

repo = str(Path(__file__).resolve().parent.parent)
work = tempfile.mkdtemp()
os.chdir(work)
sys.path.insert(0, repo)
os.environ["EMEC_HARDWARE_STUBS"] = "1"   # this machine has no GPIO
from utils.program_sync import ProgramSync  # noqa: E402

fails = 0


def check(name, ok):
    global fails
    print(("ok   " if ok else "FAIL ") + name)
    fails += not ok


class FakeServer:
    """The statements ProgramSync sends, over a dict of programs by name and one of cnc_share_files by name."""

    def __init__(self):
        self.programs = {}
        self.folders = set()
        self.share = {}
        self.writes = 0   # to cnc_share_files
        self.next_id = 1
        self.probation = None   # what visible_programs() gives a session on probation, or None for a session that isn't

    def upload(self, name, text):
        parts = name.split("/")
        self.folders |= {"/".join(parts[:i]) for i in range(1, len(parts))}
        content = text.encode()
        old = self.programs.get(name)
        sha = hashlib.sha256(content).hexdigest()
        synced = old["synced"] if old and old["sha256"] == sha else False
        self.programs[name] = {"id": old["id"] if old else self.next_id, "name": name, "content": content, "sha256": sha,
                               "synced": synced}
        self.next_id += not old

    def execute(self, sql, params):
        rows = list(self.programs.values())
        sql = sql.strip()
        if sql.startswith("SELECT stick_allowed"):
            result = {"ok": self.probation is None}
            return type("Cursor", (), {"fetchone": lambda self: result})()
        if sql.startswith("SELECT visible_programs"):
            result = {"ids": self.probation}
            return type("Cursor", (), {"fetchone": lambda self: result})()
        if "cnc_share_files" in sql:
            result = []
            if sql.startswith("SELECT name, folder, sha256"):
                result = [{"name": n, **r} for n, r in self.share.items() if r["deleting"]]
            elif sql.startswith("SELECT"):
                result = [{"name": n, **r} for n, r in self.share.items()]
            elif sql.startswith("INSERT"):
                _, name, folder, size, sha, _ = params
                self.share[name] = {"folder": folder, "size": size, "sha256": sha, "deleting": False}
                self.writes += 1
            elif sql.startswith("DELETE"):
                for name in params[1]:
                    del self.share[name]
                self.writes += 1
            else:
                self.share[params[1]]["deleting"] = False
            return type("Cursor", (), {"fetchall": lambda self: result})()
        if sql.startswith("SELECT path"):
            result = [{"path": f} for f in self.folders]
        elif sql.startswith("SELECT id, name"):
            result = [{k: p[k] for k in ("id", "name", "sha256", "synced")} for p in rows]
        elif sql.startswith("SELECT content"):
            result = [{"content": p["content"]} for p in rows if (p["id"], p["sha256"]) == params]
        else:
            for p in rows:
                if (p["id"], p["sha256"]) == params:
                    p["synced"] = True
            result = []
        return type("Cursor", (), {"fetchall": lambda self: result, "fetchone": lambda self: result[0] if result else None})()


folder = os.path.join(work, "share")
os.makedirs(folder)
server = FakeServer()
sync = ProgramSync("zz_test", folder=folder, manifest=os.path.join(work, "programs.json"))


def files():
    return sorted(os.listdir(folder))


def read(name):
    return open(os.path.join(folder, name)).read()


server.upload("O01234.nc", "O01234 (ONE)\nM30\n%\n")
sync.sync(server)
check("writes an upload", files() == ["O01234.nc"] and "ONE" in read("O01234.nc"))
check("and marks it synced", server.programs["O01234.nc"]["synced"])

server.upload("O01234.nc", "O01234 (TWO)\nM30\n%\n")
sync.sync(server)
check("a new version replaces the file", "TWO" in read("O01234.nc") and server.programs["O01234.nc"]["synced"])

open(os.path.join(folder, "HAAS1.nc"), "w").write("saved on the control\n")
server.upload("O5555.nc", "O5555\nM30\n%\n")
sync.sync(server)
check("a second upload", files() == ["HAAS1.nc", "O01234.nc", "O5555.nc"])

open(os.path.join(folder, "O5555.nc"), "a").write("(edited on the control)\n")
sync.sync(server)
check("an edit on the control is kept", "edited" in read("O5555.nc"))

del server.programs["O01234.nc"], server.programs["O5555.nc"]
sync.sync(server)
check("a deleted upload goes, an edited one stays, the control's own stays", files() == ["HAAS1.nc", "O5555.nc"])

os.remove(os.path.join(folder, "O5555.nc"))
server.upload("O7777.nc", "O7777\nM30\n%\n")
sync.sync(server)
os.remove(os.path.join(folder, "O7777.nc"))
sync.sync(server)
check("an upload removed on the control comes back", files() == ["HAAS1.nc", "O7777.nc"])

server.programs["../evil"] = {**server.programs["O7777.nc"], "id": 99, "name": "../evil"}
sync.sync(server)
check("a name with a path is skipped", files() == ["HAAS1.nc", "O7777.nc"] and not os.path.exists(os.path.join(work, "evil")))
check("no temp files left", not any(f.startswith(".") for f in files()))

server.upload("Job 1/Operation 1.nc", "O1\nM30\n%\n")
server.upload("Job 1/Fixture/Operation 2.nc", "O2\nM30\n%\n")
server.upload("Job 2/Operation 1.nc", "O3\nM30\n%\n")
sync.sync(server)
check("writes into folders", "O1" in read("Job 1/Operation 1.nc") and "O2" in read("Job 1/Fixture/Operation 2.nc")
      and "O3" in read("Job 2/Operation 1.nc"))
check("same name in two folders", server.programs["Job 2/Operation 1.nc"]["synced"])
check("no temp files in folders", not any(f.startswith(".") for f in os.listdir(os.path.join(folder, "Job 1"))))

server.folders.add("Empty")
sync.sync(server)
check("an empty folder is made", os.path.isdir(os.path.join(folder, "Empty")))

del server.programs["Job 1/Fixture/Operation 2.nc"]
sync.sync(server)
check("deleting a file keeps its folder", os.listdir(os.path.join(folder, "Job 1", "Fixture")) == [])

open(os.path.join(folder, "Job 2", "HAAS2.nc"), "w").write("saved on the control\n")
for name in [n for n in server.programs if n.startswith(("Job 1/", "Job 2/"))]:
    del server.programs[name]
server.folders -= {"Job 1", "Job 1/Fixture", "Job 2", "Empty"}
sync.sync(server)
check("deleted folders go, one with the control's file stays", "Job 1" not in files() and "Empty" not in files()
      and os.listdir(os.path.join(folder, "Job 2")) == ["HAAS2.nc"])

open(os.path.join(folder, "Job 3"), "w").write("a file named like a folder\n")
server.upload("Job 3/Operation 1.nc", "O4\nM30\n%\n")
server.upload("O8888.nc", "O8888\nM30\n%\n")
sync.sync(server)
check("a file in the way is skipped, the rest still sync", not server.programs["Job 3/Operation 1.nc"]["synced"]
      and server.programs["O8888.nc"]["synced"])

server.upload("Move A/Inner/x.nc", "OX\nM30\n%\n")
sync.sync(server)
p = server.programs.pop("Move A/Inner/x.nc")
server.programs["Move B/Inner/x.nc"] = {**p, "name": "Move B/Inner/x.nc", "synced": False}
server.folders -= {"Move A", "Move A/Inner"}
server.folders |= {"Move B", "Move B/Inner"}
sync.sync(server)
check("a moved folder ends up in its new place only", "OX" in read("Move B/Inner/x.nc") and "Move A" not in files()
      and server.programs["Move B/Inner/x.nc"]["synced"])


def listed():
    return sorted(n + ("/" if r["folder"] else "") for n, r in server.share.items())


def delete(name):
    server.share[name]["deleting"] = True


check("lists what the control saved and the folders it made", listed() == ["HAAS1.nc", "Job 2/", "Job 2/HAAS2.nc", "Job 3"])
open(os.path.join(folder, "O8888.nc"), "a").write("(edited on the control)\n")
sync.sync(server)
check("and an upload it edited", "O8888.nc" in listed() and server.share["O8888.nc"]["size"] == os.path.getsize(os.path.join(folder, "O8888.nc")))
writes = server.writes
sync.sync(server)
check("writes nothing when nothing changed", server.writes == writes)

del server.programs["O8888.nc"]
delete("O8888.nc")
sync.sync(server)
check("an edited upload deleted on the dashboard goes", "O8888.nc" not in files() and "O8888.nc" not in listed())

delete("Job 2/HAAS2.nc")
delete("Job 2")
sync.sync(server)
check("a folder of the control's goes with its file", "Job 2" not in files() and "Job 2/" not in listed())

delete("Job 3")
open(os.path.join(folder, "Job 3"), "a").write("saved again\n")
sync.sync(server)
check("a file saved again after the delete stays", "Job 3" in files() and not server.share["Job 3"]["deleting"])

os.makedirs(os.path.join(folder, "Saved"))
sync.sync(server)
open(os.path.join(folder, "Saved", "HAAS3.nc"), "w").write("saved on the control\n")
delete("Saved")
sync.sync(server)
check("a folder saved into after the delete stays", "Saved" in files() and not server.share["Saved"]["deleting"])

os.remove(os.path.join(folder, "Job 3"))
sync.sync(server)
check("one removed at the control drops off the list", "Job 3" not in listed())

# Probation. The share shows only the session's own and assigned programs.
folder = os.path.join(work, "share2")
os.makedirs(folder)
server = FakeServer()
sync = ProgramSync("zz_test", folder=folder, manifest=os.path.join(work, "programs2.json"))
hidden = sync.hidden


def tree(top):
    return sorted(os.path.relpath(os.path.join(r, n), top).replace(os.sep, "/") + ("/" if n in d else "")
                  for r, d, f in os.walk(top) for n in d + f if not n.startswith("."))


for name in ("Staff/Op 1.nc", "Staff/Op 2.nc", "Mine/part.nc", "top.nc", "Staff2/x.nc"):
    server.upload(name, f"({name})\nM30\n%\n")
server.folders.add("Empty")
ids = {name: p["id"] for name, p in server.programs.items()}
sync.sync(server)
open(os.path.join(folder, "HAAS.nc"), "w").write("saved on the control\n")
open(os.path.join(folder, "Staff", "HAAS2.nc"), "w").write("saved on the control\n")
os.makedirs(os.path.join(folder, "Saved"))
open(os.path.join(folder, "Saved", "HAAS3.nc"), "w").write("saved on the control\n")
sync.sync(server)
everything = tree(folder)
check("no session shows everything", "top.nc" in everything and "Staff/HAAS2.nc" in everything)

sync.session = lambda: "zz_test_1"
server.probation = [ids["Mine/part.nc"], ids["Staff/Op 2.nc"]]
sync.sync(server)
check("a probation session shows its own and assigned files, and folders with none in them",
      tree(folder) == ["Empty/", "Mine/", "Mine/part.nc", "Staff/", "Staff/Op 2.nc"])
check("the rest are hidden", {"top.nc", "Staff/Op 1.nc", "Staff2/x.nc", "HAAS.nc", "Staff/HAAS2.nc", "Saved/HAAS3.nc"}
      <= set(tree(hidden)))
check("what the control saved is still listed", {"HAAS.nc", "Staff/HAAS2.nc", "Saved/HAAS3.nc"} <= set(listed()))

open(os.path.join(folder, "NEW.nc"), "w").write("saved during the session\n")
server.upload("late.nc", "(late)\nM30\n%\n")
ids["late.nc"] = server.programs["late.nc"]["id"]
sync.sync(server)
check("what the control saves during the session stays", "NEW.nc" in tree(folder))
check("an upload during the session goes hidden", "late.nc" not in tree(folder) and "late.nc" in tree(hidden)
      and server.programs["late.nc"]["synced"])

server.probation = server.probation + [ids["top.nc"]]
sync.sync(server)
check("a file assigned during the session shows", "top.nc" in tree(folder) and "top.nc" not in tree(hidden))
del server.programs["Staff/Op 1.nc"]
sync.sync(server)
check("a hidden file deleted on the dashboard goes", "Staff/Op 1.nc" not in tree(hidden))

sync.session = lambda: None
sync.sync(server)
check("everything comes back after the session", tree(folder) == sorted(set(everything) - {"Staff/Op 1.nc"}
                                                                          | {"NEW.nc", "late.nc"})
      and not [n for n in tree(hidden) if not n.endswith("/")] and not os.path.exists(sync.marker))

sync.session = lambda: "zz_test_2"
server.probation = [ids["Mine/part.nc"]]
sync.sync(server)
check("the next student's session hides what the control saved in the last one", "NEW.nc" not in tree(folder))
open(os.path.join(folder, "Mine", "part.nc"), "w").write("edited on the control\n")
open(os.path.join(hidden, "late.nc"), "a").write("(edited)\n")
sync.session = lambda: None
open(os.path.join(folder, "late.nc"), "w").write("saved on the control with the same name\n")
sync.sync(server)
check("a hidden file whose name the control took stays hidden", "late.nc" in tree(hidden)
      and "same name" in read("late.nc"))
check("an edit at the control during the session is kept", "edited on the control" in read("Mine/part.nc"))

os.remove(os.path.join(folder, "late.nc"))
server.probation = None
sync.session = lambda: "zz_test_3"
sync.sync(server)
check("a session not on probation shows everything", "top.nc" in tree(folder) and "late.nc" in tree(folder))

print("all passed" if not fails else f"{fails} failed")
sys.exit(1 if fails else 0)
