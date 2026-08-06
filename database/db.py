import os
import re
import sqlite3
from pathlib import Path

try:
    import psycopg2
    from psycopg2 import pool as psycopg2_pool
except ImportError:  # pragma: no cover - dependency may be absent until install
    psycopg2 = None
    psycopg2_pool = None

DB_PATH = os.path.join(os.path.dirname(__file__), "l2f.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")
POSTGRES_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.postgres.sql")
DATABASE_TIMEZONE = os.environ.get("DATABASE_TIMEZONE", "Asia/Jerusalem")
SQLITE_TIMEOUT_SECONDS = int(os.environ.get("SQLITE_TIMEOUT_SECONDS", "30"))
POSTGRES_POOL_MIN_CONN = int(os.environ.get("POSTGRES_POOL_MIN_CONN", "1"))
POSTGRES_POOL_MAX_CONN = int(os.environ.get("POSTGRES_POOL_MAX_CONN", "12"))

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
    ("03", "xs-s"),
    ("04", "s"),
    ("06", "m"),
    ("07", "m-l"),
    ("08", "l"),
    ("10", "xl"),
    ("11", "xxl"),
]
STRUCTURED_BARCODE_RE = re.compile(r"^([A-Z])(\d{5})(\d{4})(\d{2})$")

_postgres_pool = None


if psycopg2 is not None:
    IntegrityError = (sqlite3.IntegrityError, psycopg2.IntegrityError)
else:
    IntegrityError = (sqlite3.IntegrityError,)


class CompatRow(dict):
    def __init__(self, columns, values):
        super().__init__(zip(columns, values))
        self._values = list(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class CompatCursor:
    def __init__(self, connection, native_cursor):
        self._connection = connection
        self._native_cursor = native_cursor

    def execute(self, sql, params=None):
        rendered_sql = _translate_sql(sql, self._connection.dialect)
        parameters = tuple(params or ())
        self._native_cursor.execute(rendered_sql, parameters)
        self._connection._track_changes(rendered_sql, getattr(self._native_cursor, "rowcount", -1))
        return self

    def executemany(self, sql, seq_of_params):
        rendered_sql = _translate_sql(sql, self._connection.dialect)
        self._native_cursor.executemany(rendered_sql, list(seq_of_params))
        self._connection._track_changes(rendered_sql, getattr(self._native_cursor, "rowcount", -1))
        return self

    def fetchone(self):
        row = self._native_cursor.fetchone()
        return _row_to_compat(row, self._native_cursor.description)

    def fetchall(self):
        rows = self._native_cursor.fetchall()
        return [_row_to_compat(row, self._native_cursor.description) for row in rows]

    def close(self):
        self._native_cursor.close()

    @property
    def rowcount(self):
        return getattr(self._native_cursor, "rowcount", -1)

    @property
    def lastrowid(self):
        return getattr(self._native_cursor, "lastrowid", None)


class CompatConnection:
    def __init__(self, native_connection, dialect, release_callback=None):
        self._native_connection = native_connection
        self._release_callback = release_callback
        self.dialect = dialect
        self._total_changes = 0

    def cursor(self):
        return CompatCursor(self, self._native_connection.cursor())

    def execute(self, sql, params=None):
        cursor = self.cursor()
        cursor.execute(sql, params)
        return cursor

    def executemany(self, sql, seq_of_params):
        cursor = self.cursor()
        cursor.executemany(sql, seq_of_params)
        return cursor

    def executescript(self, script):
        if self.dialect == "sqlite":
            self._native_connection.executescript(script)
            return

        cursor = self.cursor()
        for statement in _split_sql_script(script):
            cursor.execute(statement)

    def commit(self):
        self._native_connection.commit()

    def rollback(self):
        self._native_connection.rollback()

    def close(self):
        if self._release_callback:
            try:
                self._native_connection.rollback()
            except Exception:
                pass
            self._release_callback(self._native_connection)
            self._release_callback = None
            return
        self._native_connection.close()

    def _track_changes(self, sql, rowcount):
        if rowcount is None or rowcount < 0:
            return
        if re.match(r"^\s*(INSERT|UPDATE|DELETE)\b", str(sql), flags=re.IGNORECASE):
            self._total_changes += rowcount

    @property
    def total_changes(self):
        if self.dialect == "sqlite":
            return self._native_connection.total_changes
        return self._total_changes


def get_db_backend():
    return "postgres" if _database_url() else "sqlite"


def quote_identifier(name):
    normalized = str(name or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", normalized):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return '"' + normalized.replace('"', '""') + '"'


def list_table_names(conn):
    if conn.dialect == "postgres":
        rows = conn.execute(
            """
            SELECT table_name AS name
            FROM information_schema.tables
            WHERE table_schema='public' AND table_type='BASE TABLE'
            ORDER BY table_name
            """
        ).fetchall()
        return [row["name"] for row in rows]

    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type='table'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    return [row["name"] for row in rows]


def table_exists(conn, table_name):
    if conn.dialect == "postgres":
        row = conn.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema='public' AND table_type='BASE TABLE' AND table_name=?
            LIMIT 1
            """,
            (table_name,),
        ).fetchone()
        return bool(row)

    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type='table'
          AND name=?
          AND name NOT LIKE 'sqlite_%'
        LIMIT 1
        """,
        (table_name,),
    ).fetchone()
    return bool(row)


def get_table_columns(conn, table_name):
    if conn.dialect == "postgres":
        return conn.execute(
            """
            SELECT
                cols.ordinal_position - 1 AS cid,
                cols.column_name AS name,
                cols.data_type AS type,
                (cols.is_nullable = 'NO') AS notnull,
                cols.column_default AS dflt_value,
                EXISTS (
                    SELECT 1
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                    WHERE tc.table_schema='public'
                      AND tc.table_name = cols.table_name
                      AND tc.constraint_type='PRIMARY KEY'
                      AND kcu.column_name = cols.column_name
                ) AS pk
            FROM information_schema.columns cols
            WHERE cols.table_schema='public' AND cols.table_name=?
            ORDER BY cols.ordinal_position
            """,
            (table_name,),
        ).fetchall()

    escaped_name = str(table_name).replace("'", "''")
    return conn.execute(f"PRAGMA table_info('{escaped_name}')").fetchall()


def insert_and_get_id(conn, sql, params=None):
    parameters = tuple(params or ())
    if conn.dialect == "postgres":
        cursor = conn.execute(_append_sql_suffix(sql, " RETURNING id"), parameters)
        row = cursor.fetchone()
        return row["id"] if row else None

    cursor = conn.execute(sql, parameters)
    return cursor.lastrowid


def get_connection():
    if get_db_backend() == "postgres":
        if psycopg2 is None or psycopg2_pool is None:
            raise RuntimeError("PostgreSQL support is not installed. Run pip install -r requirements.txt.")

        native = _get_postgres_pool().getconn()
        native.autocommit = False
        return CompatConnection(
            native,
            "postgres",
            release_callback=_get_postgres_pool().putconn,
        )

    native = sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT_SECONDS, check_same_thread=False)
    native.row_factory = sqlite3.Row
    native.execute("PRAGMA foreign_keys=ON;")
    native.execute("PRAGMA busy_timeout=30000;")
    native.execute("PRAGMA journal_mode=WAL;")
    native.execute("PRAGMA synchronous=NORMAL;")
    return CompatConnection(native, "sqlite")


def _database_url():
    raw = os.environ.get("DATABASE_URL", "").strip()
    if not raw:
        return ""
    if raw.startswith("postgres://"):
        return "postgresql://" + raw[len("postgres://"):]
    return raw


def _get_postgres_pool():
    global _postgres_pool
    if _postgres_pool is not None:
        return _postgres_pool

    database_url = _database_url()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required for PostgreSQL mode.")

    _postgres_pool = psycopg2_pool.ThreadedConnectionPool(
        POSTGRES_POOL_MIN_CONN,
        POSTGRES_POOL_MAX_CONN,
        database_url,
    )
    return _postgres_pool


def _local_datetime_sql(dialect):
    if dialect == "postgres":
        return f"to_char(timezone('{DATABASE_TIMEZONE}', now()), 'YYYY-MM-DD HH24:MI:SS')"
    return "datetime('now','localtime')"


def _local_date_sql(dialect):
    if dialect == "postgres":
        return f"to_char(timezone('{DATABASE_TIMEZONE}', now()), 'YYYY-MM-DD')"
    return "date('now','localtime')"


def _translate_sql(sql, dialect):
    if dialect != "postgres":
        return sql

    translated = str(sql)
    translated = re.sub(
        r"datetime\(\s*'now'\s*,\s*'localtime'\s*\)",
        _local_datetime_sql("postgres"),
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"date\(\s*'now'\s*,\s*'localtime'\s*\)",
        _local_date_sql("postgres"),
        translated,
        flags=re.IGNORECASE,
    )

    if re.match(r"^\s*INSERT\s+OR\s+IGNORE\b", translated, flags=re.IGNORECASE):
        translated = re.sub(
            r"^\s*INSERT\s+OR\s+IGNORE\b",
            "INSERT",
            translated,
            count=1,
            flags=re.IGNORECASE,
        )
        translated = _append_sql_suffix(translated, " ON CONFLICT DO NOTHING")

    return _replace_qmark_placeholders(translated)


def _replace_qmark_placeholders(sql):
    result = []
    in_single = False
    in_double = False
    idx = 0
    while idx < len(sql):
        char = sql[idx]
        next_char = sql[idx + 1] if idx + 1 < len(sql) else ""

        if char == "'" and not in_double:
            result.append(char)
            if in_single and next_char == "'":
                result.append(next_char)
                idx += 2
                continue
            in_single = not in_single
            idx += 1
            continue

        if char == '"' and not in_single:
            result.append(char)
            if in_double and next_char == '"':
                result.append(next_char)
                idx += 2
                continue
            in_double = not in_double
            idx += 1
            continue

        if char == "?" and not in_single and not in_double:
            result.append("%s")
        else:
            result.append(char)
        idx += 1

    return "".join(result)


def _append_sql_suffix(sql, suffix):
    base = str(sql).rstrip()
    if base.endswith(";"):
        return base[:-1] + suffix + ";"
    return base + suffix


def _split_sql_script(script):
    statements = []
    chunk = []
    in_single = False
    in_double = False
    for char in script:
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double

        if char == ";" and not in_single and not in_double:
            statement = "".join(chunk).strip()
            if statement:
                statements.append(statement)
            chunk = []
            continue

        chunk.append(char)

    tail = "".join(chunk).strip()
    if tail:
        statements.append(tail)
    return statements


def _row_to_compat(row, description):
    if row is None:
        return None
    if isinstance(row, CompatRow):
        return row
    if isinstance(row, sqlite3.Row):
        columns = row.keys()
        values = [row[column] for column in columns]
        return CompatRow(columns, values)
    if isinstance(row, dict):
        return CompatRow(row.keys(), row.values())
    columns = [column[0] for column in (description or [])]
    return CompatRow(columns, row)


def _ensure_structured_barcode_scale_tables(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS barcode_color_scale (
            code TEXT PRIMARY KEY,
            color TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS barcode_size_scale (
            code TEXT PRIMARY KEY,
            size TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_barcode_color_scale_color ON barcode_color_scale(color)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_barcode_size_scale_size ON barcode_size_scale(size)")


def _seed_structured_barcode_scales(conn):
    color_count = conn.execute("SELECT COUNT(*) AS count FROM barcode_color_scale").fetchone()["count"]
    if not color_count:
        conn.executemany(
            "INSERT INTO barcode_color_scale (code, color) VALUES (?, ?)",
            BARCODE_COLOR_SCALE_SEED,
        )

    size_count = conn.execute("SELECT COUNT(*) AS count FROM barcode_size_scale").fetchone()["count"]
    if not size_count:
        conn.executemany(
            "INSERT INTO barcode_size_scale (code, size) VALUES (?, ?)",
            BARCODE_SIZE_SCALE_SEED,
        )


def _restore_legacy_size_scale(conn):
    conn.execute(
        "UPDATE barcode_size_scale SET size='xs-s', updated_at=datetime('now','localtime') WHERE code='03' AND size<>'xs-s'"
    )
    conn.execute(
        "UPDATE barcode_size_scale SET size='m-l', updated_at=datetime('now','localtime') WHERE code='07' AND size<>'m-l'"
    )


def _restore_structured_barcode_sizes(conn):
    rows = conn.execute("SELECT id, barcode, size FROM barcodes").fetchall()
    for row in rows:
        barcode = str(row["barcode"] or "").strip().upper()
        match = STRUCTURED_BARCODE_RE.fullmatch(barcode)
        if not match:
            continue
        expected_size = {
            "03": "xs-s",
            "07": "m-l",
        }.get(match.group(4))
        if not expected_size:
            continue
        current_size = str(row["size"] or "").strip().lower()
        if current_size != expected_size:
            conn.execute(
                "UPDATE barcodes SET size=? WHERE id=?",
                (expected_size, row["id"]),
            )


def _migrate_sqlite():
    conn = get_connection()
    try:
        try:
            conn.execute("ALTER TABLE branches ADD COLUMN store_id TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("ALTER TABLE branches ADD COLUMN last_login TEXT")
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("ALTER TABLE branches ADD COLUMN is_blocked INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("ALTER TABLE missing_floor ADD COLUMN source TEXT NOT NULL DEFAULT 'session'")
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("ALTER TABLE missing_floor ADD COLUMN manual_session_date TEXT")
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("ALTER TABLE missing_warehouse ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1")
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("ALTER TABLE missing_warehouse ADD COLUMN scan_history TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass

        conn.execute("CREATE INDEX IF NOT EXISTS idx_blocked_devices_branch ON blocked_devices(branch_id)")
        _ensure_structured_barcode_scale_tables(conn)
        _seed_structured_barcode_scales(conn)
        _restore_legacy_size_scale(conn)
        _restore_structured_barcode_sizes(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate_postgres():
    conn = get_connection()
    try:
        conn.execute("ALTER TABLE branches ADD COLUMN IF NOT EXISTS store_id TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE branches ADD COLUMN IF NOT EXISTS last_login TEXT")
        conn.execute("ALTER TABLE branches ADD COLUMN IF NOT EXISTS is_blocked INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE missing_floor ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'session'")
        conn.execute("ALTER TABLE missing_floor ADD COLUMN IF NOT EXISTS manual_session_date TEXT")
        conn.execute("ALTER TABLE missing_warehouse ADD COLUMN IF NOT EXISTS quantity INTEGER NOT NULL DEFAULT 1")
        conn.execute("ALTER TABLE missing_warehouse ADD COLUMN IF NOT EXISTS scan_history TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_blocked_devices_branch ON blocked_devices(branch_id)")
        _ensure_structured_barcode_scale_tables(conn)
        _seed_structured_barcode_scales(conn)
        _restore_legacy_size_scale(conn)
        _restore_structured_barcode_sizes(conn)
        conn.commit()
    finally:
        conn.close()


def _initialize_from_schema(schema_path):
    conn = get_connection()
    try:
        schema = Path(schema_path).read_text(encoding="utf-8")
        conn.executescript(schema)
        conn.commit()
    finally:
        conn.close()


def init_db():
    if get_db_backend() == "postgres":
        _initialize_from_schema(POSTGRES_SCHEMA_PATH)
        _migrate_postgres()
        return

    _initialize_from_schema(SCHEMA_PATH)
    _migrate_sqlite()
