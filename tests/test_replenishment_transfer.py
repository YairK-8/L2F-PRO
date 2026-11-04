import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.missing_floor import _transfer_morning_sessions_to_warehouse  # noqa: E402
from backend.missing_warehouse import _transfer_warehouse_items_to_morning  # noqa: E402
from database.db import CompatConnection  # noqa: E402


class ReplenishmentTransferTests(unittest.TestCase):
    def setUp(self):
        native_conn = sqlite3.connect(":memory:")
        self.conn = CompatConnection(native_conn, "sqlite")
        self.conn.executescript(
            """
            CREATE TABLE morning_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                branch_id INTEGER NOT NULL,
                session_date TEXT NOT NULL DEFAULT (date('now','localtime')),
                sku TEXT NOT NULL,
                color TEXT NOT NULL,
                sizes_all TEXT NOT NULL DEFAULT '',
                sizes_found TEXT NOT NULL DEFAULT '',
                approved INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                last_activity_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                UNIQUE(branch_id, session_date, sku, color)
            );
            CREATE TABLE missing_warehouse (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                branch_id INTEGER NOT NULL,
                sku TEXT NOT NULL,
                color TEXT NOT NULL,
                size TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                scan_history TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                scanned_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                restocked_at TEXT
            );
            CREATE TABLE barcodes (
                barcode TEXT PRIMARY KEY, sku TEXT, color TEXT, size TEXT
            );
            CREATE TABLE barcode_size_scale (code TEXT PRIMARY KEY, size TEXT);
            CREATE TABLE missing_floor (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                branch_id INTEGER, sku TEXT, color TEXT, size TEXT,
                status TEXT DEFAULT 'missing', source TEXT DEFAULT 'session',
                manual_session_date TEXT, created_at TEXT DEFAULT (datetime('now','localtime')),
                resolved_at TEXT
            );
            CREATE UNIQUE INDEX missing_floor_open_unique
                ON missing_floor(branch_id,sku,color,size) WHERE status='missing';
            CREATE TABLE warehouse_locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                branch_id INTEGER, sku TEXT, location TEXT
            );
            """
        )
        self.conn.executemany(
            "INSERT INTO barcode_size_scale(code,size) VALUES (?,?)",
            [("02", "xs"), ("04", "s"), ("06", "m")],
        )

    def tearDown(self):
        self.conn.close()

    def test_morning_to_sales_moves_only_scanned_sizes(self):
        self.conn.executemany(
            """INSERT INTO morning_sessions
                   (branch_id,session_date,sku,color,sizes_all,sizes_found)
               VALUES (1,date('now','localtime'),?,?,?,?)""",
            [
                ("10001", "black", "xs,s,m", "xs,m"),
                ("10002", "white", "xs,s", ""),
            ],
        )

        result = _transfer_morning_sessions_to_warehouse(self.conn, 1)

        moved = self.conn.execute(
            "SELECT sku,size FROM missing_warehouse ORDER BY size"
        ).fetchall()
        remaining = self.conn.execute(
            "SELECT sku FROM morning_sessions"
        ).fetchall()
        self.assertEqual([(row["sku"], row["size"]) for row in moved], [("10001", "m"), ("10001", "xs")])
        self.assertEqual([row["sku"] for row in remaining], ["10002"])
        self.assertEqual(result["variants"], 2)
        self.assertEqual(result["skipped_sessions"], 1)

    def test_sales_to_morning_marks_variants_found_and_clears_source(self):
        self.conn.executemany(
            "INSERT INTO barcodes(barcode,sku,color,size) VALUES (?,?,?,?)",
            [("a", "20001", "blue", "xs"), ("b", "20001", "blue", "s"), ("c", "20001", "blue", "m")],
        )
        self.conn.executemany(
            """INSERT INTO missing_warehouse(branch_id,sku,color,size)
               VALUES (1,'20001','blue',?)""",
            [("xs",), ("s",)],
        )

        result = _transfer_warehouse_items_to_morning(self.conn, 1)

        session = self.conn.execute(
            "SELECT sizes_all,sizes_found,approved FROM morning_sessions"
        ).fetchone()
        pending = self.conn.execute(
            "SELECT COUNT(*) AS count FROM missing_warehouse WHERE status='pending'"
        ).fetchone()["count"]
        self.assertEqual(session["sizes_all"], "xs,s,m")
        self.assertEqual(session["sizes_found"], "xs,s")
        self.assertEqual(session["approved"], 0)
        self.assertEqual(pending, 0)
        self.assertEqual(result["items"], 2)


if __name__ == "__main__":
    unittest.main()
