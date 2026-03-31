import sqlite3, os

DB_PATH = os.path.join(os.path.dirname(__file__), "l2f.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _migrate():
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("ALTER TABLE branches ADD COLUMN store_id TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute("ALTER TABLE branches ADD COLUMN last_login TEXT")
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute("ALTER TABLE branches ADD COLUMN is_blocked INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute("ALTER TABLE missing_floor ADD COLUMN source TEXT NOT NULL DEFAULT 'session'")
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute("ALTER TABLE missing_floor ADD COLUMN manual_session_date TEXT")
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute("ALTER TABLE missing_warehouse ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1")
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute("ALTER TABLE missing_warehouse ADD COLUMN scan_history TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass

    cur.execute(
        """CREATE TABLE IF NOT EXISTS blocked_devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            branch_id INTEGER NOT NULL REFERENCES branches(id),
            device_id TEXT NOT NULL,
            device_name TEXT NOT NULL DEFAULT '',
            blocked_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            UNIQUE(branch_id, device_id)
        )"""
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_blocked_devices_branch ON blocked_devices(branch_id)")

    conn.commit()
    conn.close()


def init_db():
    conn = get_connection()
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema = f.read()
    conn.executescript(schema)
    conn.commit()
    conn.close()
    _migrate()
