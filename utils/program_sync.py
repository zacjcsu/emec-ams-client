"""Copies the CNC programs uploaded in the dashboard's machine popup into PROGRAM_FOLDER, which the control reads
through Samba or the USB drive (utils/usb_drive.py). A program's name can have folders in it, like "Job 1/Operation
1.nc". Folders made on the dashboard are made here too, even empty ones, and one deleted there goes here unless the
control saved files in it.

The dashboard's list wins for the files it put there: a new upload replaces the file, and a deleted one goes. Files the
control saved there itself are left alone, and so is one of ours it edited, until the dashboard has a new version.
PROGRAM_MANIFEST remembers which files are ours and what we wrote.

Those other files and folders go in cnc_share_files, so the dashboard can list them. One deleted there goes here too,
unless the control saved it again since.

While someone on probation has a session open, the share holds only the programs they may use: their own uploads and
those staff shared with them, from the server's visible_programs(). The rest wait in the hidden folder beside the share,
with what the control had saved before the session. What the control saves during the session stays. A folder with
none of their files in it is hidden too, unless no upload is in it. Everything comes back once the session ends.
"""
import hashlib
import json
import logging
import os
import threading

import psycopg

from db.server_sync import get_server_connection
from config.constants import PROGRAM_FOLDER, PROGRAM_MANIFEST, PROGRAM_POLL_SECONDS, READER_FITTED

logger = logging.getLogger("programs")


def file_sha(path):
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(1024 * 1024):
                digest.update(chunk)
    except FileNotFoundError:
        return None
    return digest.hexdigest()


def allowed(name):
    """The dashboard doesn't allow the others."""
    return all(p and not p.startswith(".") for p in name.split("/"))


class ProgramSync:
    def __init__(self, machine_id, folder=PROGRAM_FOLDER, manifest=PROGRAM_MANIFEST, poll_seconds=PROGRAM_POLL_SECONDS,
                 reader=READER_FITTED):
        self.machine_id = machine_id
        self.reader = reader
        self.folder = folder
        folder = os.path.abspath(folder)
        self.hidden = os.path.join(os.path.dirname(folder), f".{os.path.basename(folder)}-hidden")
        self.marker = os.path.join(self.hidden, ".session")   # whose session hid the control's files
        self.manifest = manifest
        self.folder_manifest = os.path.splitext(manifest)[0] + "-folders.json"   # the folders we made
        self.poll_seconds = poll_seconds
        self.session = lambda: None   # the CSU ID of the open session, or None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="programs", daemon=True)
        self._failed = set()   # names already logged as not writable
        self._shas = {}        # path: (mtime_ns, size, sha256), so an unchanged file isn't read every round

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _load(self):
        try:
            with open(self.manifest) as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return {}

    def _save(self, ours):
        tmp = self.manifest + ".tmp"
        with open(tmp, "w") as f:
            json.dump(ours, f)
        os.replace(tmp, self.manifest)

    def _load_folders(self):
        try:
            with open(self.folder_manifest) as f:
                return set(json.load(f))
        except (FileNotFoundError, ValueError):
            return set()

    def _save_folders(self, made):
        tmp = self.folder_manifest + ".tmp"
        with open(tmp, "w") as f:
            json.dump(sorted(made), f)
        os.replace(tmp, self.folder_manifest)

    def _not_writable(self, name, e):
        """Say, the control saved a file where this needs a folder."""
        if name not in self._failed:
            logger.warning(f"[PROGRAMS] Cannot write {name}: {e}")
            self._failed.add(name)

    def _sha(self, path, st):
        known = self._shas.get(path)
        if known and known[:2] == (st.st_mtime_ns, st.st_size):
            return known[2]
        sha = file_sha(path)
        self._shas[path] = (st.st_mtime_ns, st.st_size, sha)
        return sha

    def _path(self, name, shown=True):
        return os.path.join(self.folder if shown else self.hidden, *name.split("/"))

    def _found(self, name):
        """Where the file or folder is, in the share or else the hidden folder."""
        path = self._path(name)
        return path if os.path.lexists(path) else self._path(name, False)

    def _put(self, name, shown):
        """Moves a file into the share or out of it. False if something with its name is already there."""
        src, dst = self._path(name, not shown), self._path(name, shown)
        if not os.path.isfile(src) or os.path.islink(src):
            return True
        if os.path.lexists(dst):
            return False
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(src, dst)
        return True

    def _shown(self, conn):
        """(CSU ID, the ids of the programs they may use) while someone on probation has a session open, else None.
        The server keeps the flash drive from exactly those sessions."""
        csu_id = self.session()
        if not csu_id:
            return None
        args = (self.machine_id, str(csu_id), self.reader)
        if conn.execute("SELECT stick_allowed(%s, %s, %s) AS ok", args).fetchone()["ok"]:
            return None
        ids = conn.execute("SELECT visible_programs(%s, %s, %s) AS ids", args).fetchone()["ids"]
        return str(csu_id), set(ids)

    def _hidden_for(self):
        try:
            with open(self.marker) as f:
                return f.read()
        except FileNotFoundError:
            return None

    def _hide_control(self, ours, folders):
        """Moves what the control saved out of the share. Its own folders go too once empty."""
        for root, dirs, names in os.walk(self.folder, topdown=False):
            rel = os.path.relpath(root, self.folder)
            prefix = "" if rel == "." else rel.replace(os.sep, "/") + "/"
            for n in names:
                if not n.startswith(".") and prefix + n not in ours:
                    self._put(prefix + n, False)
            for d in dirs:
                path = os.path.join(root, d)
                if prefix + d not in folders and not os.path.islink(path) and not d.startswith("."):
                    try:
                        os.rmdir(path)
                        os.makedirs(self._path(prefix + d, False), exist_ok=True)
                    except OSError:
                        pass

    def _restore(self):
        """Puts everything hidden back in the share."""
        if not os.path.isdir(self.hidden):
            return
        for root, dirs, names in os.walk(self.hidden, topdown=False):
            rel = os.path.relpath(root, self.hidden)
            prefix = "" if rel == "." else rel.replace(os.sep, "/") + "/"
            for n in names:
                if n.startswith("."):
                    continue
                if not self._put(prefix + n, True) and prefix + n not in self._failed:
                    logger.warning(f"[PROGRAMS] Left {prefix + n} hidden. The control saved a file with its name.")
                    self._failed.add(prefix + n)
            for d in dirs:
                os.makedirs(self._path(prefix + d), exist_ok=True)
                try:
                    os.rmdir(os.path.join(root, d))
                except OSError:
                    pass
        try:
            os.remove(self.marker)
        except FileNotFoundError:
            pass

    def _delete_requested(self, conn):
        """What was deleted on the dashboard. Files first, then the deepest folders."""
        rows = conn.execute("SELECT name, folder, sha256 FROM cnc_share_files WHERE machine_id = %s AND delete_requested_at IS NOT NULL",
                            (self.machine_id,)).fetchall()
        for r in sorted(rows, key=lambda r: (r["folder"], -r["name"].count("/"))):
            name = r["name"]
            try:
                if not allowed(name):
                    raise OSError("not a name the dashboard gives")
                path = self._found(name)
                if r["folder"]:
                    os.rmdir(path)
                elif file_sha(path) == r["sha256"]:
                    os.remove(path)
                else:
                    raise OSError("it was saved again")
                logger.info(f"[PROGRAMS] Deleted {name} from the control's files.")
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.info(f"[PROGRAMS] Left {name}: {e}")
                conn.execute("UPDATE cnc_share_files SET delete_requested_at = NULL WHERE machine_id = %s AND name = %s",
                             (self.machine_id, name))

    def _report(self, conn, known):
        """Lists in cnc_share_files each file and folder here or hidden that the dashboard doesn't have as it is.
        `known` maps our files to what we wrote, and our folders to None."""
        found = {}
        for top in (self.folder, self.hidden):
            for root, dirs, names in os.walk(top):
                rel = os.path.relpath(root, top)
                prefix = "" if rel == "." else rel.replace(os.sep, "/") + "/"
                dirs[:] = [d for d in dirs if not d.startswith(".") and not os.path.islink(os.path.join(root, d))]
                for d in dirs:
                    if prefix + d not in known:
                        found.setdefault(prefix + d, (True, None, None, None))
                for n in names:
                    path = os.path.join(root, n)
                    if n.startswith(".") or os.path.islink(path) or prefix + n in found:
                        continue
                    try:
                        st = os.stat(path)
                        sha = self._sha(path, st)
                    except OSError:
                        continue
                    if sha and known.get(prefix + n) != sha:
                        found[prefix + n] = (False, st.st_size, sha, st.st_mtime)
        listed = {r["name"]: (r["folder"], r["size"], r["sha256"]) for r in conn.execute(
            "SELECT name, folder, size, sha256 FROM cnc_share_files WHERE machine_id = %s", (self.machine_id,)).fetchall()}
        for name, (folder, size, sha, mtime) in found.items():
            if listed.get(name) == (folder, size, sha):
                continue
            conn.execute("""
                INSERT INTO cnc_share_files (machine_id, name, folder, size, sha256, modified_at)
                VALUES (%s, %s, %s, %s, %s, to_timestamp(%s))
                ON CONFLICT (machine_id, name) DO UPDATE SET folder = EXCLUDED.folder, size = EXCLUDED.size,
                    sha256 = EXCLUDED.sha256, modified_at = EXCLUDED.modified_at, delete_requested_at = NULL""",
                (self.machine_id, name, folder, size, sha, mtime))
        gone = [n for n in listed if n not in found]
        if gone:
            conn.execute("DELETE FROM cnc_share_files WHERE machine_id = %s AND name = ANY(%s)", (self.machine_id, gone))
        self._shas = {p: v for p, v in self._shas.items() if os.path.exists(p)}

    def sync(self, conn):
        probation = self._shown(conn)
        made = self._load_folders()
        folders = sorted(r["path"] for r in conn.execute("SELECT path FROM cnc_program_folders WHERE machine_id = %s",
                                                         (self.machine_id,)).fetchall() if allowed(r["path"]))
        rows = [r for r in conn.execute("SELECT id, name, sha256, synced_at IS NOT NULL AS synced FROM cnc_programs WHERE machine_id = %s",
                                        (self.machine_id,)).fetchall() if allowed(r["name"])]
        shown = {r["name"] for r in rows if not probation or r["id"] in probation[1]}

        def folder_shown(path):
            inside = [r["name"] for r in rows if r["name"].startswith(path + "/")]
            return not probation or not inside or any(n in shown for n in inside)

        for path in folders:
            full = self._path(path, folder_shown(path))
            if path not in made or not os.path.isdir(full):
                try:
                    os.makedirs(full, exist_ok=True)
                except OSError as e:
                    self._not_writable(path, e)
                    continue
                if path not in made:
                    made.add(path)
                    self._save_folders(made)
                    logger.info(f"[PROGRAMS] Made folder {path}.")
        ours = self._load()
        listed = set()
        for r in rows:
            name = r["name"]
            listed.add(name)
            if not self._put(name, name in shown):
                self._not_writable(name, OSError("the control saved a file with its name"))
            path = self._path(name, name in shown)
            if ours.get(name) != r["sha256"] or not os.path.isfile(path):
                content = conn.execute("SELECT content FROM cnc_programs WHERE id = %s AND sha256 = %s",
                                       (r["id"], r["sha256"])).fetchone()
                if not content:
                    continue   # replaced or deleted since; the next round has it
                folder, base = os.path.split(path)
                tmp = os.path.join(folder, f".{base}.part")
                try:
                    os.makedirs(folder, exist_ok=True)
                    with open(tmp, "wb") as f:
                        f.write(content["content"])
                    os.replace(tmp, path)
                except OSError as e:
                    self._not_writable(name, e)
                    continue
                self._failed.discard(name)
                ours[name] = r["sha256"]
                self._save(ours)
                logger.info(f"[PROGRAMS] Wrote {name}.")
                r["synced"] = False
            if not r["synced"]:
                conn.execute("UPDATE cnc_programs SET synced_at = now() WHERE id = %s AND sha256 = %s",
                             (r["id"], r["sha256"]))
        for name in [n for n in ours if n not in listed]:
            path = self._found(name)
            if file_sha(path) == ours[name]:
                os.remove(path)
                logger.info(f"[PROGRAMS] Deleted {name}.")
            else:
                logger.info(f"[PROGRAMS] Left {name}. It was deleted on the dashboard but changed here.")
            del ours[name]
            self._save(ours)
        if not probation:
            self._restore()
        elif self._hidden_for() != probation[0]:
            self._hide_control(ours, set(folders))
            os.makedirs(self.hidden, exist_ok=True)
            with open(self.marker, "w") as f:
                f.write(probation[0])
            logger.info(f"[PROGRAMS] Showing only {len(shown)} of {len(rows)} programs for this session.")
        self._delete_requested(conn)
        for path in sorted(made - set(folders), key=lambda p: -p.count("/")):   # the ones inside first
            left = False
            for full in (self._path(path), self._path(path, False)):
                try:
                    os.rmdir(full)
                except FileNotFoundError:
                    pass
                except OSError:
                    left = True
            logger.info(f"[PROGRAMS] Left folder {path}. It was deleted on the dashboard but has files saved here." if left
                        else f"[PROGRAMS] Deleted folder {path}.")
            made.discard(path)
            self._save_folders(made)
        # A folder moved in or out is left empty where it was.
        for path in sorted(folders, key=lambda p: -p.count("/")):
            try:
                os.rmdir(self._path(path, not folder_shown(path)))
            except OSError:
                pass
        above = {"/".join(n.split("/")[:i]) for n in ours for i in range(1, n.count("/") + 1)}
        self._report(conn, {**dict.fromkeys(above | set(folders)), **ours})

    def _run(self):
        conn = None
        failing = False
        while not self._stop.is_set():
            try:
                if conn is None or conn.closed:
                    conn = get_server_connection(timeout=3)
                    conn.autocommit = True
                self.sync(conn)
                if failing:
                    logger.info("[PROGRAMS] Syncing again.")
                    failing = False
            except Exception as e:
                # The heartbeat already reports the server being out of reach.
                if not failing:
                    (logger.info if isinstance(e, psycopg.OperationalError) else logger.warning)(
                        f"[PROGRAMS] Cannot sync programs: {e}")
                    failing = True
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                conn = None
            self._stop.wait(self.poll_seconds)
