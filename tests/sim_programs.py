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

print("all passed" if not fails else f"{fails} failed")
sys.exit(1 if fails else 0)
