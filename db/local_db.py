import sqlite3
import logging
from config.constants import LOCAL_DB_PATH, STATUS_NEUTRAL
from create_local_db import create_local_db
from utils.timeutil import utc_now_str
from db import access_rule

logger = logging.getLogger("local_db")


class LocalDB:
    def __init__(self):
        create_local_db()

        self.conn = sqlite3.connect(LOCAL_DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()

    def get_machine(self, machine_id):
        self.cursor.execute("SELECT * FROM Machine WHERE machine_id = ?", (machine_id,))
        return self.cursor.fetchone()

    def insert_machine_if_missing(self, machine_id, machine_name, machine_type):
        self.cursor.execute("SELECT * FROM Machine WHERE machine_id = ?", (machine_id,))
        if not self.cursor.fetchone():
            self.cursor.execute("""
                INSERT INTO Machine (machine_id, machine_name, machine_type, machine_status)
                VALUES (?, ?, ?, ?)
            """, (machine_id, machine_name, machine_type, STATUS_NEUTRAL))
            self.conn.commit()

    def update_machine_status(self, machine_id, status):
        """Plain write, no guard: SessionManager decides (from the live lockout state, not this cache)
        whether a status push should be redirected to 'maintenance' instead of clobbering it."""
        self.cursor.execute("UPDATE Machine SET machine_status = ? WHERE machine_id = ?", (status, machine_id))
        self.conn.commit()

    def update_machine_ip(self, machine_id, device_ip):
        self.cursor.execute("UPDATE Machine SET device_ip = ? WHERE machine_id = ?", (device_ip, machine_id))
        self.conn.commit()

    def update_machine_device(self, machine_id, device_id):
        self.cursor.execute("UPDATE Machine SET device_id = ? WHERE machine_id = ?", (device_id, machine_id))
        self.conn.commit()

    def update_machine_heartbeat(self, machine_id):
        now = utc_now_str()
        self.cursor.execute("UPDATE Machine SET last_heartbeat = ? WHERE machine_id = ?", (now, machine_id))
        self.conn.commit()

    def get_setting(self, key, default=None):
        self.cursor.execute("SELECT value FROM System_Settings WHERE setting = ?", (key,))
        row = self.cursor.fetchone()
        return row["value"] if row else default

    def get_user(self, csu_id):
        self.cursor.execute("SELECT * FROM Users WHERE csu_id = ?", (csu_id,))
        return self.cursor.fetchone()

    def get_last_user(self):
        """Name (or CSU ID if the user has no name) of whoever last used this machine, or None."""
        self.cursor.execute("SELECT csu_id, name FROM Last_Session LIMIT 1")
        row = self.cursor.fetchone()
        if not row:
            return None
        return row["name"] or row["csu_id"]

    def access_decision(self, csu_id, machine_id, at=None):
        """(allowed, reason, via) for this user on this machine's category, from the local cache.
        Mirrors the server's access_decision_machine(); see db/access_rule.py."""
        machine = self.get_machine(machine_id)
        if not machine or not machine["machine_type"]:
            return False, "unknown_machine", None
        machine_type = machine["machine_type"]
        c = self.cursor

        c.execute("SELECT disabled_at, disabled_line1, disabled_line2 FROM Users WHERE csu_id = ?", (csu_id,))
        user_row = c.fetchone()
        user_exists = user_row is not None
        disabled_message = None
        if user_row and user_row["disabled_at"]:
            disabled_message = "\n".join(
                filter(None, [user_row["disabled_line1"] or "User disabled", user_row["disabled_line2"]]))
        c.execute(
            "SELECT 1 FROM User_Groups ug JOIN Groups g ON g.group_name = ug.group_name "
            "WHERE ug.csu_id = ? AND g.enabled = 0", (csu_id,))
        in_disabled_group = c.fetchone() is not None
        c.execute(
            "SELECT 1 FROM Category_Permissions WHERE csu_id = ? AND machine_type = ?",
            (csu_id, machine_type))
        has_permission = c.fetchone() is not None

        tz = self.get_setting("lab_timezone", access_rule.DEFAULT_TZ)
        local = access_rule.local_now(tz, at)
        lab_open = access_rule.parse_time(self.get_setting("lab_open_time"))
        lab_close = access_rule.parse_time(self.get_setting("lab_close_time"))
        lab_days = access_rule.parse_days(self.get_setting("lab_days", ""))

        c.execute(
            "SELECT w.level_name, w.days, w.start_time, w.end_time FROM User_Access ua "
            "JOIN Access_Levels l ON l.level_name = ua.level_name "
            "JOIN Level_Windows w ON w.level_name = ua.level_name "
            "WHERE ua.csu_id = ? AND l.enabled = 1", (csu_id,))
        windows = []
        for name, days, start, end in c.fetchall():
            start_t, end_t = access_rule.parse_time(start), access_rule.parse_time(end)
            if start_t is not None and end_t is not None:
                windows.append((name, access_rule.parse_days(days), start_t, end_t))

        return access_rule.decide(
            user_exists=user_exists, user_disabled_message=disabled_message, in_disabled_group=in_disabled_group,
            has_permission=has_permission, lab_open=lab_open, lab_close=lab_close,
            lab_days=lab_days, level_windows=windows, local=local)

    def access_request_exists(self, csu_id, machine_id):
        self.cursor.execute(
            "SELECT 1 FROM Access_Requests WHERE csu_id = ? AND machine_id = ? AND status = 'under review'",
            (csu_id, machine_id)
        )
        return self.cursor.fetchone() is not None

    def insert_access_request(self, csu_id, machine_id, uid_fallback):
        now = utc_now_str()
        self.cursor.execute("SELECT uid FROM Users WHERE csu_id = ?", (csu_id,))
        user = self.cursor.fetchone()
        uid = user["uid"] if user else uid_fallback

        self.cursor.execute("SELECT machine_type FROM Machine WHERE machine_id = ?", (machine_id,))
        machine = self.cursor.fetchone()
        machine_type = machine["machine_type"] if machine else None

        self.cursor.execute("""
            INSERT INTO Access_Requests (
                uid, csu_id, machine_id, machine_type,
                status, requested_on
            ) VALUES (?, ?, ?, ?, 'under review', ?)
        """, (uid, csu_id, machine_id, machine_type, now))
        self.conn.commit()

    def mark_user_active(self, csu_id):
        now = utc_now_str()
        self.cursor.execute(
            "UPDATE Users SET is_active = 1, last_used = ? WHERE csu_id = ?", (now, csu_id,)
        )
        self.conn.commit()

    def mark_user_inactive(self, csu_id):
        now = utc_now_str()
        self.cursor.execute(
            "UPDATE Users SET is_active = 0, last_used = ? WHERE csu_id = ?", (now, csu_id,)
        )
        self.conn.commit()

    def insert_session(self, session_id, csu_id, machine_id, card_uid=None):
        now = utc_now_str()
        self.cursor.execute("SELECT machine_type FROM Machine WHERE machine_id = ?", (machine_id,))
        result = self.cursor.fetchone()
        machine_type = result["machine_type"] if result and result["machine_type"] else "Unknown"

        self.cursor.execute(
            "INSERT INTO Machine_Usage (session_id, csu_id, machine_id, machine_type, start_time, card_uid) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, csu_id, machine_id, machine_type, now, card_uid)
        )
        self.conn.commit()

    def get_open_sessions(self):
        """Sessions with no end time: normally only the one running now; after a crash, orphans."""
        self.cursor.execute("SELECT session_id, csu_id, start_time FROM Machine_Usage WHERE end_time IS NULL")
        return self.cursor.fetchall()

    def close_session_at(self, session_id, end_time):
        self.cursor.execute("""
            UPDATE Machine_Usage
            SET end_time = ?,
                duration = MAX(0, (strftime('%s', ?) - strftime('%s', start_time)) / 60)
            WHERE session_id = ?
        """, (end_time, end_time, session_id))
        self.conn.commit()

    def end_session(self, session_id):
        now = utc_now_str()
        self.cursor.execute("""
            UPDATE Machine_Usage
            SET end_time = ?,
                duration = ((strftime('%s',?) - strftime('%s', start_time)) / 60)
            WHERE session_id = ?
        """, (now, now, session_id,))
        self.conn.commit()

    def ensure_user_uid(self, csu_id, uid):
        self.cursor.execute("SELECT uid FROM Users WHERE csu_id = ?", (csu_id,))
        result = self.cursor.fetchone()
        if result and (result["uid"] is None or result["uid"].strip() == ""):
            self.cursor.execute("UPDATE Users SET uid = ? WHERE csu_id = ?", (str(uid), csu_id))
            self.conn.commit()
            return True
        return False
