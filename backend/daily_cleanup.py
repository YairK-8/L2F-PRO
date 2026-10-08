"""Daily database cleanup jobs for branch operational data."""

import os
import calendar
import threading
import time
from datetime import datetime, time as clock_time, timedelta
from zoneinfo import ZoneInfo

from database.db import get_connection
from backend.realtime import emit_update


TIMEZONE = ZoneInfo(os.environ.get("DAILY_RESET_TIMEZONE", "Asia/Jerusalem"))
RESET_HOUR = int(os.environ.get("MISSING_FLOOR_RESET_HOUR", "6"))
RESET_ENABLED = os.environ.get("DAILY_MISSING_FLOOR_RESET_ENABLED", "1").strip().lower() not in {
    "0", "false", "no",
}
LOCATION_INACTIVITY_MONTHS = max(1, int(os.environ.get("LOCATION_INACTIVITY_MONTHS", "3")))

_worker_lock = threading.Lock()
_worker_thread = None
_cleanup_guard_lock = threading.Lock()
_last_cleanup_check = 0.0


def current_missing_floor_cutoff(now=None):
    """Return the beginning of the current 06:00-to-06:00 workday."""
    current = now.astimezone(TIMEZONE) if now else datetime.now(TIMEZONE)
    cutoff = datetime.combine(current.date(), clock_time(hour=RESET_HOUR), TIMEZONE)
    if current < cutoff:
        cutoff -= timedelta(days=1)
    return cutoff


def _subtract_calendar_months(value, months):
    month_index = value.year * 12 + (value.month - 1) - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def clear_expired_operational_data(now=None, force=False):
    """Delete expired daily work data from all operational lists.

    The current workday runs from RESET_HOUR to RESET_HOUR. Catalog,
    authentication and synchronization tables are deliberately not touched.
    Warehouse locations are retained only while searched within the configured
    calendar-month retention window.
    """
    global _last_cleanup_check
    with _cleanup_guard_lock:
        monotonic_now = time.monotonic()
        if not force and monotonic_now - _last_cleanup_check < 30:
            return []
        cutoff_dt = current_missing_floor_cutoff(now)
        cutoff = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")
        session_cutoff = cutoff_dt.strftime("%Y-%m-%d")
        location_cutoff = _subtract_calendar_months(
            cutoff_dt, LOCATION_INACTIVITY_MONTHS
        ).strftime("%Y-%m-%d %H:%M:%S")
        conn = get_connection()
        try:
            if conn.dialect == "postgres":
                conn.execute("SELECT pg_advisory_xact_lock(?, ?)", (127836, 600))

            floor_branches = {
                int(row["branch_id"])
                for row in conn.execute(
                    "SELECT DISTINCT branch_id FROM missing_floor WHERE created_at < ?",
                    (cutoff,),
                ).fetchall()
            }
            warehouse_branches = {
                int(row["branch_id"])
                for row in conn.execute(
                    "SELECT DISTINCT branch_id FROM missing_warehouse WHERE scanned_at < ?",
                    (cutoff,),
                ).fetchall()
            }
            morning_branches = {
                int(row["branch_id"])
                for row in conn.execute(
                    """SELECT DISTINCT branch_id FROM morning_sessions
                       WHERE approved=1 AND session_date < ?""",
                    (session_cutoff,),
                ).fetchall()
            }
            location_branches = {
                int(row["branch_id"])
                for row in conn.execute(
                    """SELECT DISTINCT branch_id FROM warehouse_locations
                       WHERE last_searched_at < ?""",
                    (location_cutoff,),
                ).fetchall()
            }

            # Match the per-branch mutation locks used by the scan endpoints.
            if conn.dialect == "postgres":
                for branch_id in sorted(floor_branches | morning_branches):
                    conn.execute("SELECT pg_advisory_xact_lock(?, ?)", (127836, branch_id))
                for branch_id in sorted(warehouse_branches):
                    conn.execute("SELECT pg_advisory_xact_lock(?, ?)", (127837, branch_id))
                for branch_id in sorted(location_branches):
                    conn.execute("SELECT pg_advisory_xact_lock(?, ?)", (127838, branch_id))

            floor_deleted = conn.execute(
                "DELETE FROM missing_floor WHERE created_at < ?", (cutoff,)
            ).rowcount
            warehouse_deleted = conn.execute(
                "DELETE FROM missing_warehouse WHERE scanned_at < ?", (cutoff,)
            ).rowcount
            morning_deleted = conn.execute(
                """DELETE FROM morning_sessions
                   WHERE approved=1 AND session_date < ?""",
                (session_cutoff,),
            ).rowcount
            locations_deleted = conn.execute(
                "DELETE FROM warehouse_locations WHERE last_searched_at < ?",
                (location_cutoff,),
            ).rowcount
            conn.commit()
            _last_cleanup_check = monotonic_now
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    for branch_id in floor_branches:
        emit_update(branch_id, "tab1_floor_missing_cleared", {"cutoff": cutoff})
    for branch_id in morning_branches:
        emit_update(branch_id, "tab1_cleared", {"cutoff": cutoff})
    for branch_id in warehouse_branches:
        emit_update(branch_id, "tab2_cleared", {"cutoff": cutoff})
    for branch_id in location_branches:
        emit_update(
            branch_id,
            "locations_pruned",
            {"cutoff": location_cutoff, "months": LOCATION_INACTIVITY_MONTHS},
        )
    return {
        "cutoff": cutoff,
        "missing_floor": max(0, floor_deleted),
        "missing_warehouse": max(0, warehouse_deleted),
        "morning_sessions": max(0, morning_deleted),
        "warehouse_locations": max(0, locations_deleted),
        "location_cutoff": location_cutoff,
    }


def clear_expired_missing_floor(now=None, force=False):
    """Backward-compatible entry point used by the missing-floor API."""
    return clear_expired_operational_data(now=now, force=force)


def _seconds_until_next_reset(now=None):
    current = now.astimezone(TIMEZONE) if now else datetime.now(TIMEZONE)
    next_reset = datetime.combine(current.date(), clock_time(hour=RESET_HOUR), TIMEZONE)
    if next_reset <= current:
        next_reset += timedelta(days=1)
    return max(1.0, (next_reset - current).total_seconds())


def _worker():
    try:
        result = clear_expired_operational_data(force=True)
        print(f"Daily operational cleanup completed at startup: {result}")
    except Exception as exc:
        print(f"Daily missing-floor cleanup failed at startup: {exc}")

    while True:
        threading.Event().wait(_seconds_until_next_reset())
        try:
            result = clear_expired_operational_data(force=True)
            print(f"Daily operational cleanup completed: {result}")
        except Exception as exc:
            print(f"Daily missing-floor cleanup failed: {exc}")


def start_daily_cleanup():
    global _worker_thread
    if not RESET_ENABLED:
        return None
    with _worker_lock:
        if _worker_thread and _worker_thread.is_alive():
            return _worker_thread
        _worker_thread = threading.Thread(
            target=_worker,
            name="l2f-daily-cleanup",
            daemon=True,
        )
        _worker_thread.start()
        return _worker_thread
