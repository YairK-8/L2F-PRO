import os
import re
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "l2f.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

BARCODE_COLOR_SCALE_SEED = [
    ("0001", "לבן"),
    ("0002", "אבן"),
    ("0003", "מוקה"),
    ("0004", "שחור"),
    ("0005", "אפור"),
    ("0006", "אפור כהה"),
    ("0007", "בורדו"),
    ("0008", "בז'"),
    ("0009", "ג'ינס"),
    ("0010", "זית בהיר"),
    ("0011", "חום"),
    ("0012", "ירוק"),
    ("0013", "ירוק זית"),
    ("0014", "ירוק כהה"),
    ("0015", "כחול"),
    ("0016", "מנומר"),
    ("0017", "כאמל"),
    ("0018", "שמנת"),
    ("0019", "תכלת"),
    ("0020", "אדום"),
    ("0021", "אפור בהיר"),
    ("0022", "אפור מלאנז'"),
    ("0023", "אפרסק"),
    ("0024", "גינס בהיר"),
    ("0025", "ג'ינס כהה"),
    ("0026", "ורוד"),
    ("0027", "ורוד עתיק"),
    ("0028", "זית"),
    ("0029", "חאקי"),
    ("0030", "חום בהיר"),
    ("0031", "חום כהה"),
    ("0032", "תמרה"),
    ("0033", "חרדל"),
    ("0034", "ירוק בהיר"),
    ("0035", "כחול כהה"),
    ("0036", "כתום"),
    ("0037", "ליים"),
    ("0038", "לילך"),
    ("0039", "מוקה"),
    ("0040", "מנטה"),
    ("0041", "ניוד"),
    ("0042", "סגול"),
    ("0043", "פסים"),
    ("0044", "פסים אבן"),
    ("0045", "פסים שחור לבן"),
    ("0046", "צהוב"),
    ("0049", "צהוב"),
    ("0050", "אבן מלאנז'"),
    ("0051", "כחול בהיר"),
    ("0052", "נייבי"),
    ("0053", "הדפס"),
    ("0054", "אבן מודסס"),
    ("0055", "אפור ווש"),
    ("0056", "אפור כהה ווש"),
    ("0058", "ג'ינס כחול"),
    ("0059", "ג'ינס תכלת"),
    ("0060", "ורוד בהיר"),
    ("0061", "ורוד מודסס"),
    ("0062", "זברה"),
    ("0063", "חום שוקולד"),
    ("0064", "טורקיז"),
    ("0065", "כחול פסים"),
    ("0066", "נוגט"),
    ("0067", "פסים חום"),
    ("0068", "פסים כחול"),
    ("0069", "פסים לבן"),
    ("0070", "פסים שחור"),
    ("0079", "חום ווש"),
    ("0080", "חום דהוי"),
    ("0081", "חום ווש"),
    ("0082", "חום אדום"),
    ("0083", "קאמל כהה"),
    ("0084", "קאמל"),
    ("0086", "כחול ווש"),
    ("0087", "שחור ווש"),
    ("0088", "לבן ווש"),
    ("0089", "מולטי"),
    ("0091", "ירוק בקבוק"),
    ("0095", "ירוק ווש"),
    ("0096", "כחול ווש"),
    ("0097", "כחול כהה ווש"),
    ("0100", "חמרה ווש"),
]

BARCODE_SIZE_SCALE_SEED = [
    ("01", "y"),
    ("02", "xs"),
    ("03", "xs"),
    ("04", "s"),
    ("06", "m"),
    ("07", "m"),
    ("08", "l"),
    ("10", "xl"),
    ("11", "xxl"),
]

SIZE_ALIAS_MAP = {
    "xs-s": "xs",
    "xs s": "xs",
    "xss": "xs",
    "m-l": "m",
    "m l": "m",
    "ml": "m",
}


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _ensure_structured_barcode_scale_tables(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS barcode_color_scale (
            code TEXT PRIMARY KEY,
            color TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS barcode_size_scale (
            code TEXT PRIMARY KEY,
            size TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_barcode_color_scale_color ON barcode_color_scale(color)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_barcode_size_scale_size ON barcode_size_scale(size)")


def _seed_structured_barcode_scales(cur):
    color_count = cur.execute("SELECT COUNT(*) AS count FROM barcode_color_scale").fetchone()["count"]
    if not color_count:
        cur.executemany(
            "INSERT INTO barcode_color_scale (code, color) VALUES (?, ?)",
            BARCODE_COLOR_SCALE_SEED,
        )

    size_count = cur.execute("SELECT COUNT(*) AS count FROM barcode_size_scale").fetchone()["count"]
    if not size_count:
        cur.executemany(
            "INSERT INTO barcode_size_scale (code, size) VALUES (?, ?)",
            BARCODE_SIZE_SCALE_SEED,
        )


def _normalize_size_label(value):
    cleaned = str(value or "").strip().lower()
    if not cleaned:
        return ""
    normalized = re.sub(r"\s*-\s*", "-", cleaned)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    compact = re.sub(r"[^a-z0-9]+", "", normalized)
    if normalized in SIZE_ALIAS_MAP:
        return SIZE_ALIAS_MAP[normalized]
    if compact in SIZE_ALIAS_MAP:
        return SIZE_ALIAS_MAP[compact]
    return normalized


def _normalize_size_csv(value):
    seen = set()
    ordered = []
    for part in str(value or "").split(","):
        normalized = _normalize_size_label(part)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ",".join(ordered)


def _normalize_table_size_column(cur, table_name):
    rows = cur.execute(f"SELECT rowid AS row_id, size FROM {table_name}").fetchall()
    for row in rows:
        current = str(row["size"] or "").strip()
        normalized = _normalize_size_label(current)
        if normalized and normalized != current:
            cur.execute(
                f"UPDATE {table_name} SET size=? WHERE rowid=?",
                (normalized, row["row_id"]),
            )


def _normalize_legacy_size_values(cur):
    for table_name in ("barcodes", "missing_floor", "missing_warehouse", "barcode_size_scale"):
        _normalize_table_size_column(cur, table_name)

    rows = cur.execute(
        "SELECT id, sizes_all, sizes_found FROM morning_sessions"
    ).fetchall()
    for row in rows:
        sizes_all = _normalize_size_csv(row["sizes_all"])
        sizes_found = _normalize_size_csv(row["sizes_found"])
        if sizes_all != str(row["sizes_all"] or "") or sizes_found != str(row["sizes_found"] or ""):
            cur.execute(
                "UPDATE morning_sessions SET sizes_all=?, sizes_found=? WHERE id=?",
                (sizes_all, sizes_found, row["id"]),
            )


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

    cur.execute("CREATE INDEX IF NOT EXISTS idx_blocked_devices_branch ON blocked_devices(branch_id)")
    _ensure_structured_barcode_scale_tables(cur)
    _seed_structured_barcode_scales(cur)
    _normalize_legacy_size_values(cur)

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
