import sqlite3
import os
import logging
from config.constants import LOCAL_DB_PATH

logger = logging.getLogger("create_local_db")

schema = """
-- USERS
CREATE TABLE IF NOT EXISTS Users (
    csu_id TEXT PRIMARY KEY,
    uid TEXT,
    name TEXT,
    last_used TEXT,
    is_active INTEGER DEFAULT 0,
    disabled_at TEXT,
    disabled_line1 TEXT,
    disabled_line2 TEXT
);

-- USER ACCESS 
CREATE TABLE IF NOT EXISTS User_Access (
    csu_id TEXT,
    level_name TEXT,
    added_at TEXT,
    PRIMARY KEY (csu_id, level_name)
);

-- ACCESS LEVELS (grant access during their windows)
CREATE TABLE IF NOT EXISTS Access_Levels (
    level_name TEXT PRIMARY KEY,
    enabled INTEGER DEFAULT 1
);

-- LEVEL WINDOWS (days = comma-separated ISO weekdays of the start day, 1=Mon..7=Sun; times HH:MM:SS)
CREATE TABLE IF NOT EXISTS Level_Windows (
    window_id INTEGER PRIMARY KEY,
    level_name TEXT,
    days TEXT,
    start_time TEXT,
    end_time TEXT
);

-- GROUPS (a disabled group blocks every member)
CREATE TABLE IF NOT EXISTS Groups (
    group_name TEXT PRIMARY KEY,
    enabled INTEGER DEFAULT 1
);

-- USER GROUPS
CREATE TABLE IF NOT EXISTS User_Groups (
    csu_id TEXT,
    group_name TEXT,
    added_at TEXT,
    PRIMARY KEY (csu_id, group_name)
);

-- MACHINE
CREATE TABLE IF NOT EXISTS Machine (
    machine_id TEXT PRIMARY KEY,
    machine_type TEXT,
    machine_name TEXT,
    machine_status TEXT DEFAULT 'offline',
    device_ip TEXT,
    last_heartbeat TEXT,
    device_id TEXT
);

-- CATEGORY PERMISSIONS (permissions are per machine category = Machine.machine_type)
CREATE TABLE IF NOT EXISTS Category_Permissions (
    csu_id TEXT,
    machine_type TEXT,
    PRIMARY KEY (csu_id, machine_type)
);

-- ACCESS REQUESTS
CREATE TABLE IF NOT EXISTS Access_Requests (
    request_id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT,
    csu_id TEXT,
    machine_id TEXT,
    machine_type TEXT,
    requested_on TEXT,
    status TEXT DEFAULT 'under review',
    reviewed_by TEXT,
    reviewed_at TEXT
);

-- MACHINE USAGE
CREATE TABLE IF NOT EXISTS Machine_Usage (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    csu_id TEXT,
    machine_id TEXT,
    machine_type TEXT,
    start_time TEXT,
    end_time TEXT,
    duration INTEGER,
    card_uid TEXT
);

-- LAST SESSION on this machine (one row, refreshed by the sync; drives the idle "Last Used" screen)
CREATE TABLE IF NOT EXISTS Last_Session (
    session_id TEXT PRIMARY KEY,
    csu_id TEXT,
    name TEXT,
    end_time TEXT
);

-- SYSTEM SETTINGS
CREATE TABLE IF NOT EXISTS System_Settings (
    setting TEXT PRIMARY KEY,
    value TEXT,
    description TEXT,
    last_updated TEXT
);
"""

def create_local_db():
    """Create missing tables. Safe to run every start: Machine_Usage and Access_Requests are never dropped."""
    os.makedirs(os.path.dirname(LOCAL_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(LOCAL_DB_PATH)
    conn.executescript(schema)
    conn.close()
    logger.info(f"Local DB ready at {LOCAL_DB_PATH}")
