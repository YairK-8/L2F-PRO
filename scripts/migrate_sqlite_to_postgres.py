import os
import sqlite3
import sys
from pathlib import Path

import psycopg2


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SQLITE_PATH = ROOT / "database" / "l2f.db"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TABLE_PLANS = [
    {
        "table": "branches",
        "columns": ["id", "name", "password", "store_id", "last_login", "is_blocked", "created_at"],
        "conflict": "(id)",
        "update": ["name", "password", "store_id", "last_login", "is_blocked", "created_at"],
    },
    {
        "table": "admins",
        "columns": ["id", "username", "password", "created_at"],
        "conflict": "(id)",
        "update": ["username", "password", "created_at"],
    },
    {
        "table": "barcodes",
        "columns": ["id", "barcode", "sku", "color", "size", "created_at"],
        "conflict": "(barcode)",
        "update": ["sku", "color", "size", "created_at"],
    },
    {
        "table": "barcode_color_scale",
        "columns": ["code", "color", "created_at", "updated_at"],
        "conflict": "(code)",
        "update": ["color", "created_at", "updated_at"],
    },
    {
        "table": "barcode_size_scale",
        "columns": ["code", "size", "created_at", "updated_at"],
        "conflict": "(code)",
        "update": ["size", "created_at", "updated_at"],
    },
    {
        "table": "morning_sessions",
        "columns": [
            "id",
            "branch_id",
            "session_date",
            "sku",
            "color",
            "sizes_all",
            "sizes_found",
            "approved",
            "created_at",
        ],
        "conflict": "(branch_id, session_date, sku, color)",
        "update": ["sizes_all", "sizes_found", "approved", "created_at"],
    },
    {
        "table": "missing_floor",
        "columns": [
            "id",
            "branch_id",
            "sku",
            "color",
            "size",
            "status",
            "source",
            "manual_session_date",
            "created_at",
            "resolved_at",
        ],
        "conflict": "(id)",
        "update": [
            "branch_id",
            "sku",
            "color",
            "size",
            "status",
            "source",
            "manual_session_date",
            "created_at",
            "resolved_at",
        ],
    },
    {
        "table": "missing_warehouse",
        "columns": [
            "id",
            "branch_id",
            "sku",
            "color",
            "size",
            "quantity",
            "scan_history",
            "status",
            "scanned_at",
            "restocked_at",
        ],
        "conflict": "(id)",
        "update": [
            "branch_id",
            "sku",
            "color",
            "size",
            "quantity",
            "scan_history",
            "status",
            "scanned_at",
            "restocked_at",
        ],
    },
    {
        "table": "warehouse_locations",
        "columns": ["id", "branch_id", "sku", "location", "updated_at"],
        "conflict": "(branch_id, sku)",
        "update": ["location", "updated_at"],
    },
    {
        "table": "blocked_devices",
        "columns": ["id", "branch_id", "device_id", "device_name", "blocked_at"],
        "conflict": "(branch_id, device_id)",
        "update": ["device_name", "blocked_at"],
    },
]

SEQUENCE_TABLES = [
    "branches",
    "admins",
    "barcodes",
    "morning_sessions",
    "missing_floor",
    "missing_warehouse",
    "warehouse_locations",
    "blocked_devices",
]

SQLITE_FALLBACKS = {
    ("branches", "store_id"): "",
    ("branches", "last_login"): None,
    ("branches", "is_blocked"): 0,
    ("missing_floor", "source"): "session",
    ("missing_floor", "manual_session_date"): None,
    ("missing_warehouse", "quantity"): 1,
    ("missing_warehouse", "scan_history"): "",
}


def _quote_ident(name):
    return '"' + str(name).replace('"', '""') + '"'


def _build_upsert_sql(plan):
    table = _quote_ident(plan["table"])
    columns = ", ".join(_quote_ident(column) for column in plan["columns"])
    placeholders = ", ".join(["%s"] * len(plan["columns"]))
    updates = ", ".join(
        f'{_quote_ident(column)}=EXCLUDED.{_quote_ident(column)}'
        for column in plan["update"]
    )
    return (
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT {plan['conflict']} DO UPDATE SET {updates}"
    )


def _table_exists_sqlite(conn, table_name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return bool(row)


def _load_sqlite_rows(conn, table_name, columns):
    if not _table_exists_sqlite(conn, table_name):
        return []
    existing_columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({_quote_ident(table_name)})").fetchall()
    }
    select_columns = [column for column in columns if column in existing_columns]
    rows = conn.execute(
        f"SELECT {', '.join(_quote_ident(column) for column in select_columns)} FROM {_quote_ident(table_name)}"
    ).fetchall()
    result = []
    for row in rows:
        result.append(
            tuple(
                row[column] if column in existing_columns else SQLITE_FALLBACKS.get((table_name, column))
                for column in columns
            )
        )
    return result


def _reset_sequences(pg_conn):
    with pg_conn.cursor() as cur:
        for table_name in SEQUENCE_TABLES:
            cur.execute(
                f"""
                SELECT setval(
                    pg_get_serial_sequence(%s, 'id'),
                    COALESCE((SELECT MAX(id) FROM {_quote_ident(table_name)}), 1),
                    EXISTS (SELECT 1 FROM {_quote_ident(table_name)})
                )
                """,
                (f"public.{table_name}",),
            )


def main():
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL is required.", file=sys.stderr)
        sys.exit(1)
    if database_url.startswith("postgres://"):
        database_url = "postgresql://" + database_url[len("postgres://"):]

    sqlite_path = Path(os.environ.get("SQLITE_PATH", str(DEFAULT_SQLITE_PATH))).expanduser()
    if not sqlite_path.exists():
        print(f"SQLite source not found: {sqlite_path}", file=sys.stderr)
        sys.exit(1)

    from database.db import init_db  # import after env validation

    init_db()

    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row
    pg_conn = psycopg2.connect(database_url)
    pg_conn.autocommit = False

    try:
        with pg_conn.cursor() as pg_cur:
            for plan in TABLE_PLANS:
                rows = _load_sqlite_rows(sqlite_conn, plan["table"], plan["columns"])
                if not rows:
                    print(f"Skipping {plan['table']} (no rows)")
                    continue
                pg_cur.executemany(_build_upsert_sql(plan), rows)
                print(f"Copied {len(rows)} rows into {plan['table']}")

        _reset_sequences(pg_conn)
        pg_conn.commit()
        print("Migration completed successfully.")
        print(f"Source SQLite file was kept untouched: {sqlite_path}")
    except Exception:
        pg_conn.rollback()
        raise
    finally:
        sqlite_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    main()
