"""Sync between the local SQLite cache and the dashboard's PostgreSQL database.

All timestamps written to the server are naive UTC strings, as the server stores them.
"""
import sqlite3
import logging
from contextlib import closing
import psycopg
from psycopg.rows import dict_row
from config.constants import DB_ENV, LOCAL_DB_PATH, MACHINE_ID

logger = logging.getLogger("server_sync")


def _local():
    """A connection to the local cache, closed when the `with` block ends."""
    conn = sqlite3.connect(LOCAL_DB_PATH)
    conn.row_factory = sqlite3.Row
    return closing(conn)


def get_server_connection(timeout=10):
    return psycopg.connect(
        host=DB_ENV["host"],
        port=DB_ENV["port"],
        user=DB_ENV["user"],
        password=DB_ENV["password"],
        dbname=DB_ENV["database"],
        sslmode=DB_ENV["sslmode"],
        connect_timeout=timeout,
        row_factory=dict_row,
    )


# Local table -> SELECT run on the server. Aliases must match the local column names
# (create_local_db.py). Timestamps, times and arrays are cast to text, booleans to int, because
# SQLite stores neither natively.
PULL_QUERIES = {
    "Users": (
        "SELECT csu_id, uid, name, last_used::text AS last_used, is_active::int AS is_active, "
        "disabled_at::text AS disabled_at, disabled_line1, disabled_line2 FROM users",
        (),
    ),
    "Groups": (
        "SELECT group_name, enabled::int AS enabled FROM groups",
        (),
    ),
    "User_Groups": (
        "SELECT csu_id, group_name FROM user_groups",
        (),
    ),
    "User_Access": (
        "SELECT csu_id, level_name FROM user_access",
        (),
    ),
    "Access_Levels": (
        "SELECT level_name, enabled::int AS enabled FROM access_levels",
        (),
    ),
    "Level_Windows": (
        "SELECT window_id, level_name, array_to_string(days, ',') AS days, "
        "to_char(start_time, 'HH24:MI:SS') AS start_time, to_char(end_time, 'HH24:MI:SS') AS end_time "
        "FROM level_windows",
        (),
    ),
    "Category_Permissions": (
        "SELECT csu_id, machine_type FROM category_permissions",
        (),
    ),
    "Access_Requests": (
        "SELECT request_id, uid, csu_id, machine_id, machine_type, requested_on::text AS requested_on, "
        "status, reviewed_by, reviewed_at::text AS reviewed_at "
        "FROM access_requests WHERE status = 'under review' AND machine_id = %s",
        (MACHINE_ID,),
    ),
    "Last_Session": (
        "SELECT mu.session_id, mu.csu_id, u.name, mu.end_time::text AS end_time "
        "FROM machine_usage mu LEFT JOIN users u ON u.csu_id = mu.csu_id "
        "WHERE mu.machine_id = %s ORDER BY mu.start_time DESC LIMIT 1",
        (MACHINE_ID,),
    ),
    "System_Settings": (
        "SELECT setting, value, description, last_updated::text AS last_updated FROM system_settings",
        (),
    ),
    "Machine": (
        "SELECT machine_id, machine_type, machine_name, machine_status, device_ip, "
        "last_heartbeat::text AS last_heartbeat, device_id FROM machine",
        (),
    ),
}


def sync_local_from_server():
    """Replace the local cache tables with the server's. All or nothing: raises on any failure so
    the caller never runs on a half-updated permission set."""
    pulled = {}
    conn_pg = get_server_connection()
    try:
        # One snapshot, so the tables are consistent with each other.
        conn_pg.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        with conn_pg.cursor() as cur:
            for table, (query, params) in PULL_QUERIES.items():
                cur.execute(query, params)
                pulled[table] = cur.fetchall()
        conn_pg.rollback()
    finally:
        conn_pg.close()

    with _local() as conn_local, conn_local:  # one transaction: commit on success, roll back on error
        for table, rows in pulled.items():
            conn_local.execute(f"DELETE FROM {table}")
            if rows:
                keys = list(rows[0].keys())
                placeholders = ", ".join(["?"] * len(keys))
                conn_local.executemany(
                    f"INSERT INTO {table} ({', '.join(keys)}) VALUES ({placeholders})",
                    [tuple(r[k] for k in keys) for r in rows],
                )
            logger.info(f"[SYNC] Pulled {len(rows)} rows from server -> {table}")


def remote_access_decision(csu_id, machine_id):
    """(allowed, reason, via) straight from the server's access_decision_machine(), so a change made
    on the dashboard applies to the very next scan. Returns None if the server can't be reached;
    the caller then falls back to the local cache."""
    try:
        with get_server_connection(timeout=3) as conn:
            row = conn.execute(
                "SELECT allowed, reason, via FROM access_decision_machine(%s, %s)",
                (str(csu_id), machine_id),
            ).fetchone()
        return row["allowed"], row["reason"], row["via"]
    except Exception as e:
        logger.warning(f"[SYNC] Live access check failed, using local cache: {e}")
        return None


_SESSION_COLS = ("session_id, csu_id, machine_id, machine_type, start_time, end_time, duration, card_uid")


def _upsert_session(cur_pg, row):
    """Write a machine_usage row without depending on which unique key the table has: update by session_id,
    insert if nothing matched. Used for the open row at session start and again for the closed row at the end.

    The UPDATE only touches end_time and duration, because that is all the Pi's database role may update
    (it may INSERT a full row, but not rewrite one). Everything else is fixed when the row is inserted.
    """
    session_id, csu_id, machine_id, machine_type, start_time, end_time, duration, card_uid = row
    cur_pg.execute(
        "UPDATE machine_usage SET end_time = %s, duration = %s WHERE session_id = %s",
        (end_time, duration, session_id),
    )
    if cur_pg.rowcount == 0:
        cur_pg.execute(
            f"INSERT INTO machine_usage ({_SESSION_COLS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (session_id, csu_id, machine_id, machine_type, start_time, end_time, duration, card_uid),
        )


def _local_session_row(session_id):
    with _local() as conn_local:
        row = conn_local.execute(f"SELECT {_SESSION_COLS} FROM Machine_Usage WHERE session_id = ?", (session_id,)).fetchone()
    return tuple(row) if row else None


def push_session_start(session_id):
    """Write the session to the server the moment it starts, with end_time and duration empty, so the dashboard can
    show who is on a machine and for how long. The local row stays: sync_session_to_server() fills in the end later.
    If the server cannot be reached the end-of-session sync inserts the whole row instead."""
    try:
        row = _local_session_row(session_id)
        if not row:
            return
        with get_server_connection(timeout=3) as conn:
            with conn.cursor() as cur_pg:
                _upsert_session(cur_pg, row)
        logger.info(f"[SYNC] Session {session_id} started on the server (open row).")
    except Exception as e:
        logger.error(f"[SYNC] Session start push failed (the end-of-session sync will write it): {e}")


def sync_session_to_server(session_id):
    try:
        row = _local_session_row(session_id)
        if not row:
            return
        with get_server_connection() as conn:
            with conn.cursor() as cur_pg:
                _upsert_session(cur_pg, row)

        with _local() as conn_local, conn_local:
            conn_local.execute("DELETE FROM Machine_Usage WHERE session_id = ?", (session_id,))
        logger.info(f"[SYNC] Session {session_id} synced and removed locally.")
    except Exception as e:
        logger.error(f"[SYNC] Session sync failed: {e}")


def fetch_last_heartbeat(machine_id):
    """machine.last_heartbeat (naive UTC) for this machine, or None if the server is unreachable or it has none."""
    try:
        with get_server_connection(timeout=3) as conn:
            row = conn.execute("SELECT last_heartbeat FROM machine WHERE machine_id = %s", (machine_id,)).fetchone()
        return row["last_heartbeat"] if row else None
    except Exception as e:
        logger.warning(f"[SYNC] Could not read the last heartbeat: {e}")
        return None


def push_machine_status(db, machine_id):
    machine = db.get_machine(machine_id)
    if not machine:
        logger.warning(f"[SYNC] Machine {machine_id} not found locally.")
        return

    try:
        device_ip = machine["device_ip"]
        device_id = machine["device_id"]
        with get_server_connection() as conn:
            with conn.cursor() as cur:
                # The dashboard owns name and category, so an existing row only gets status fields.
                cur.execute(
                    "UPDATE machine SET machine_status = %s, last_heartbeat = %s, device_ip = %s, device_id = %s "
                    "WHERE machine_id = %s",
                    (machine["machine_status"], machine["last_heartbeat"], device_ip, device_id, machine_id),
                )
                if cur.rowcount == 0:
                    # First run of a new Pi. machine_type must already exist as a category on the dashboard.
                    cur.execute(
                        "INSERT INTO machine (machine_id, machine_name, machine_type, device_ip, machine_status, last_heartbeat, device_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (machine["machine_id"], machine["machine_name"], machine["machine_type"],
                         device_ip, machine["machine_status"], machine["last_heartbeat"], device_id),
                    )
        logger.info(f"[SYNC] Machine status pushed for {machine_id}")
    except Exception as e:
        logger.error(f"[SYNC] Machine status push failed: {e}")


def push_user_status(db, csu_id):
    user = db.get_user(csu_id)
    if not user:
        logger.warning(f"[SYNC] User {csu_id} not found locally.")
        return

    try:
        with get_server_connection() as conn:
            conn.execute(
                "UPDATE users SET last_used = %s WHERE csu_id = %s",
                (user["last_used"], str(csu_id)),
            )
        logger.info(f"[SYNC] User status pushed for {csu_id}")
    except Exception as e:
        logger.error(f"[SYNC] User status push failed: {e}")


def push_user_update(csu_id):
    """Send the card UID recorded locally for this user."""
    try:
        with _local() as conn_local:
            row = conn_local.execute("SELECT uid FROM Users WHERE csu_id = ?", (csu_id,)).fetchone()
        if not row:
            logger.warning(f"[SYNC] No local user found with CSU ID {csu_id}")
            return
        with get_server_connection() as conn:
            conn.execute("UPDATE users SET uid = %s WHERE csu_id = %s", (row["uid"], str(csu_id)))
        logger.info(f"[SYNC] UID pushed for {csu_id}")
    except Exception as e:
        logger.error(f"[SYNC] Failed to push user update for {csu_id}: {e}")


def push_access_requests():
    """Send this machine's locally raised requests to the server. The server assigns request_id
    (local ids would collide across Pis), and a request already under review is not duplicated."""
    try:
        with _local() as conn_local:
            requests = conn_local.execute(
                "SELECT uid, csu_id, machine_id, machine_type, requested_on FROM Access_Requests "
                "WHERE status = 'under review' AND machine_id = ?", (MACHINE_ID,)).fetchall()
        if not requests:
            return

        with get_server_connection() as conn:
            with conn.cursor() as cur_pg:
                for uid, csu_id, machine_id, machine_type, requested_on in requests:
                    cur_pg.execute(
                        "INSERT INTO access_requests (uid, csu_id, machine_id, machine_type, requested_on, status) "
                        "SELECT %(uid)s::varchar, %(csu)s::varchar, %(mid)s::varchar, %(mt)s::varchar, %(ts)s::timestamp, 'under review' "
                        "WHERE NOT EXISTS (SELECT 1 FROM access_requests WHERE csu_id = %(csu)s::varchar "
                        "AND machine_id = %(mid)s::varchar AND status = 'under review')",
                        {"uid": uid, "csu": csu_id, "mid": machine_id, "mt": machine_type, "ts": requested_on},
                    )
        logger.info("[SYNC] Access requests synced to server")
    except Exception as e:
        logger.error(f"[SYNC] Access request sync failed: {e}")


# ---------------------------------------------------------------- temporary cards
# The Pi never reads card_issues (it holds keys and secrets). It calls these SECURITY DEFINER functions
# (migrations 005 and 006, contract in the dashboard repo's db/PI_ACCESS_CHECK.md). Each returns None when
# the server cannot be reached, so callers can tell "offline" from "no".

def _with_conn(conn, fn):
    if conn is not None:
        return fn(conn)
    with get_server_connection(timeout=3) as c:
        return fn(c)


def temp_card_lookup(uid_hex, conn=None):
    """dict(ok, reason, issue_id, sector, sector_key) or None if the server is unreachable."""
    try:
        row = _with_conn(conn, lambda c: c.execute(
            "SELECT ok, reason, issue_id, sector, sector_key FROM temp_card_lookup(%s::text)", (uid_hex,)).fetchone())
        return dict(row) if row else None
    except Exception as e:
        logger.warning(f"[TEMP] temp_card_lookup failed: {e}")
        return None


def temp_card_verify(uid_hex, secret, conn=None):
    """dict(allowed, reason, csu_id, issue_id) or None if the server is unreachable."""
    try:
        row = _with_conn(conn, lambda c: c.execute(
            "SELECT allowed, reason, csu_id, issue_id FROM temp_card_verify(%s::text, %s::bytea)",
            (uid_hex, bytes(secret))).fetchone())
        return dict(row) if row else None
    except Exception as e:
        logger.warning(f"[TEMP] temp_card_verify failed: {e}")
        return None


def temp_card_maintenance_bypass(uid_hex, machine_id, conn=None):
    """True if this temp card may run this machine while it's in maintenance. None if the server is unreachable."""
    try:
        return bool(_with_conn(conn, lambda c: c.execute(
            "SELECT temp_card_maintenance_bypass(%s::text, %s::text) AS ok", (uid_hex, machine_id)).fetchone()["ok"]))
    except Exception as e:
        logger.warning(f"[TEMP] temp_card_maintenance_bypass failed: {e}")
        return None


def report_card_present(machine_id, uid_hex, blank, held=False, conn=None):
    _with_conn(conn, lambda c: c.execute("SELECT report_card_present(%s::text, %s::text, %s::boolean, %s::boolean)",
                                         (machine_id, uid_hex, bool(blank), bool(held))))


def report_power_on(machine_id, at, relay_on, conn=None):
    _with_conn(conn, lambda c: c.execute("SELECT report_power_on(%s::text, %s::timestamptz, %s::boolean)",
                                         (machine_id, at, bool(relay_on))))


def report_power_off(machine_id, at, conn=None):
    _with_conn(conn, lambda c: c.execute("SELECT report_power_off(%s::text, %s::timestamptz)", (machine_id, at)))


def report_card_removed(machine_id, conn=None):
    _with_conn(conn, lambda c: c.execute("SELECT report_card_removed(%s::text)", (machine_id,)))


def report_scan(machine_id, uid_hex, csu_id, reason, age_seconds, conn=None):
    """A card this reader refused (migration 025). age_seconds is how long ago it was refused."""
    _with_conn(conn, lambda c: c.execute(
        "SELECT report_scan(%s::text, %s::text, %s::text, %s::text, %s::float8)",
        (machine_id, uid_hex, None if csu_id is None else str(csu_id), reason, float(age_seconds))))


def temp_card_claim_job(machine_id, conn=None):
    """The programming job aimed at this machine, as a dict, or None when there is nothing to do."""
    row = _with_conn(conn, lambda c: c.execute(
        "SELECT issue_id, card_uid, sector, sector_key, secret, previous_keys, duration_seconds "
        "FROM temp_card_claim_job(%s::text)", (machine_id,)).fetchone())
    return dict(row) if row else None


def temp_card_finish(issue_id, ok, detail=None, conn=None):
    """Report the outcome of a job; returns the server's answer ('active', 'failed', 'not_writing') or None."""
    try:
        row = _with_conn(conn, lambda c: c.execute(
            "SELECT temp_card_finish(%s::bigint, %s::boolean, %s::text) AS r",
            (issue_id, bool(ok), (detail or None) and str(detail)[:300])).fetchone())
        return row["r"] if row else None
    except Exception as e:
        logger.error(f"[TEMP] temp_card_finish failed: {e}")
        return None


# Cardless access (dashboard migration 043): staff start a session from the dashboard, with no card.

def cardless_claim(machine_id, conn=None):
    """The id of a cardless request for this machine, now claimed, or None."""
    row = _with_conn(conn, lambda c: c.execute("SELECT cardless_claim(%s::text) AS id", (machine_id,)).fetchone())
    return row["id"] if row else None


def cardless_begin(cardless_id):
    """dict(ok, session_id, csu_id, name, seconds) as the session starts, or None if the server is unreachable.
    seconds is None for no time limit."""
    try:
        row = _with_conn(None, lambda c: c.execute(
            "SELECT ok, session_id, csu_id, name, seconds FROM cardless_begin(%s::bigint)", (cardless_id,)).fetchone())
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"[CARDLESS] cardless_begin failed: {e}")
        return None


def cardless_check(cardless_id, conn=None):
    """'running', 'time_up' or 'stop'."""
    row = _with_conn(conn, lambda c: c.execute("SELECT cardless_check(%s::bigint) AS s", (cardless_id,)).fetchone())
    return row["s"] if row else "stop"


def cardless_finish(cardless_id, reason):
    try:
        _with_conn(None, lambda c: c.execute("SELECT cardless_finish(%s::bigint, %s::text)", (cardless_id, reason)))
    except Exception as e:
        # The dashboard also ends it once the usage row is closed.
        logger.error(f"[CARDLESS] cardless_finish failed: {e}")
