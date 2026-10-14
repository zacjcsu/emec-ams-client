"""Copies the CNC programs uploaded on the dashboard's CNC tab into PROGRAM_FOLDER, which Samba shares with the control.

The dashboard's list wins for the files it put there: a new upload replaces the file, and a deleted one goes. Files the
control saved there itself are left alone, and so is one of ours it edited, until the dashboard has a new version.
PROGRAM_MANIFEST remembers which files are ours and what we wrote.
"""
import hashlib
import json
import logging
import os
import threading

import psycopg

from db.server_sync import get_server_connection
from config.constants import PROGRAM_FOLDER, PROGRAM_MANIFEST, PROGRAM_POLL_SECONDS

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


class ProgramSync:
    def __init__(self, machine_id, folder=PROGRAM_FOLDER, manifest=PROGRAM_MANIFEST, poll_seconds=PROGRAM_POLL_SECONDS):
        self.machine_id = machine_id
        self.folder = folder
        self.manifest = manifest
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="programs", daemon=True)

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

    def sync(self, conn):
        ours = self._load()
        rows = conn.execute("SELECT id, name, sha256, synced_at IS NOT NULL AS synced FROM cnc_programs WHERE machine_id = %s",
                            (self.machine_id,)).fetchall()
        listed = set()
        for r in rows:
            name = r["name"]
            if name != os.path.basename(name) or name.startswith("."):
                continue   # the dashboard doesn't allow these
            listed.add(name)
            path = os.path.join(self.folder, name)
            if ours.get(name) != r["sha256"] or not os.path.exists(path):
                content = conn.execute("SELECT content FROM cnc_programs WHERE id = %s AND sha256 = %s",
                                       (r["id"], r["sha256"])).fetchone()
                if not content:
                    continue   # replaced or deleted since; the next round has it
                tmp = os.path.join(self.folder, f".{name}.part")
                with open(tmp, "wb") as f:
                    f.write(content["content"])
                os.replace(tmp, path)
                ours[name] = r["sha256"]
                self._save(ours)
                logger.info(f"[PROGRAMS] Wrote {name}.")
                r["synced"] = False
            if not r["synced"]:
                conn.execute("UPDATE cnc_programs SET synced_at = now() WHERE id = %s AND sha256 = %s",
                             (r["id"], r["sha256"]))
        for name in [n for n in ours if n not in listed]:
            path = os.path.join(self.folder, name)
            if file_sha(path) == ours[name]:
                os.remove(path)
                logger.info(f"[PROGRAMS] Deleted {name}.")
            else:
                logger.info(f"[PROGRAMS] Left {name}. It was deleted on the dashboard but changed here.")
            del ours[name]
            self._save(ours)

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
