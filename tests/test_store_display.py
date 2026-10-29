import sqlite3
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.looks import STORE_DISPLAY_TTL_DAYS, _store_display_cutoff  # noqa: E402
from backend.missing_floor import _record_store_display_item  # noqa: E402


class StoreDisplayTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            """CREATE TABLE store_display_items (
                   branch_id INTEGER NOT NULL,
                   sku TEXT NOT NULL,
                   color TEXT NOT NULL,
                   session_date TEXT NOT NULL,
                   last_seen_at TEXT NOT NULL,
                   PRIMARY KEY(branch_id, sku, color)
               )"""
        )

    def tearDown(self):
        self.conn.close()

    def test_new_day_keeps_recent_skus_and_removes_only_expired_rows(self):
        now = datetime.now(ZoneInfo("Asia/Jerusalem"))
        recent = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        expired = (now - timedelta(days=6)).strftime("%Y-%m-%d %H:%M:%S")
        self.conn.executemany(
            "INSERT INTO store_display_items VALUES (?,?,?,?,?)",
            [
                (3, "YESTERDAY", "black", "2026-10-28", recent),
                (3, "EXPIRED", "white", "2026-10-23", expired),
            ],
        )

        _record_store_display_item(self.conn, 3, "TODAY", "blue", "2026-10-29")

        rows = self.conn.execute(
            "SELECT sku FROM store_display_items WHERE branch_id=3 ORDER BY sku"
        ).fetchall()
        self.assertEqual([row[0] for row in rows], ["TODAY", "YESTERDAY"])

    def test_cutoff_is_five_days_old(self):
        cutoff = datetime.strptime(_store_display_cutoff(), "%Y-%m-%d %H:%M:%S")
        local_now = datetime.now(ZoneInfo("Asia/Jerusalem")).replace(tzinfo=None)
        self.assertAlmostEqual(
            (local_now - cutoff).total_seconds(),
            STORE_DISPLAY_TTL_DAYS * 24 * 60 * 60,
            delta=2,
        )


if __name__ == "__main__":
    unittest.main()
