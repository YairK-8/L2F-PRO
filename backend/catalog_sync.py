"""Daily reconciliation with the public ITAY BRANDS product catalog.

The website is authoritative for variants it currently publishes: missing rows
are inserted and existing SKU/color/size and EAN mappings are corrected. Local
rows which are absent from the website are retained for legacy/internal use.
"""

import json
import os
import re
import threading
import time
from datetime import datetime, time as clock_time, timedelta
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from backend.barcodes import ensure_catalog_model, normalize_barcode, normalize_size_label
from database.db import get_connection


SOURCE = "itay_brands"
SOURCE_ORIGIN = "https://itaybrands.co.il"
TIMEZONE = ZoneInfo(os.environ.get("DAILY_RESET_TIMEZONE", "Asia/Jerusalem"))
SYNC_ENABLED = os.environ.get("CATALOG_SYNC_ENABLED", "1").strip().lower() not in {
    "0", "false", "no",
}
SYNC_HOUR = int(os.environ.get("CATALOG_SYNC_HOUR", "3"))
STARTUP_DELAY = float(os.environ.get("CATALOG_SYNC_STARTUP_DELAY", "20"))
REQUEST_TIMEOUT = float(os.environ.get("CATALOG_SYNC_TIMEOUT", "30"))
PAGE_SIZE = min(250, max(1, int(os.environ.get("CATALOG_SYNC_PAGE_SIZE", "250"))))
MAX_PAGES = max(1, int(os.environ.get("CATALOG_SYNC_MAX_PAGES", "20")))
MAX_RESPONSE_BYTES = int(os.environ.get("CATALOG_SYNC_MAX_RESPONSE_BYTES", str(8 * 1024 * 1024)))
EAN_REQUEST_INTERVAL = float(os.environ.get("CATALOG_EAN_REQUEST_INTERVAL", "0.6"))
HANDLE_RE = re.compile(r"^i([a-z0-9]+)(\d{4})$", re.IGNORECASE)
SIZE_CODE_RE = re.compile(r"\d{2}$")
EAN_RE = re.compile(r"\d{8,14}$")

_worker_lock = threading.Lock()
_sync_lock = threading.Lock()
_worker_thread = None
_ean_thread = None


def _now_sql():
    return "datetime('now','localtime')"


def _set_state(status, *, error="", stats=None, completed=False):
    stats = stats or {}
    conn = get_connection()
    try:
        conn.execute(
            f"""INSERT INTO external_catalog_sync_state
                    (source,status,last_started,last_completed,last_error,
                     products_seen,variants_seen,barcodes_added,aliases_added)
                VALUES (?,?,{_now_sql()},?,?, ?,?,?,?)
                ON CONFLICT(source) DO UPDATE SET
                    status=excluded.status,
                    last_started=CASE WHEN excluded.status='syncing' THEN excluded.last_started ELSE external_catalog_sync_state.last_started END,
                    last_completed=CASE WHEN ?=1 THEN {_now_sql()} ELSE external_catalog_sync_state.last_completed END,
                    last_error=excluded.last_error,
                    products_seen=excluded.products_seen,
                    variants_seen=excluded.variants_seen,
                    barcodes_added=excluded.barcodes_added,
                    aliases_added=excluded.aliases_added""",
            (
                SOURCE, status, "" if completed else "", error[:500],
                int(stats.get("products_seen", 0)), int(stats.get("variants_seen", 0)),
                int(stats.get("barcodes_added", 0)), int(stats.get("aliases_added", 0)),
                1 if completed else 0,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _fetch_page(page):
    query = urlencode({"limit": PAGE_SIZE, "page": page})
    request = Request(
        f"{SOURCE_ORIGIN}/products.json?{query}",
        headers={
            "User-Agent": "L2F-Catalog-Sync/1.0",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        final_url = response.geturl()
        if not final_url.startswith(SOURCE_ORIGIN + "/"):
            raise ValueError("untrusted_catalog_redirect")
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ValueError("catalog_response_too_large")
    parsed = json.loads(payload.decode("utf-8"))
    products = parsed.get("products", [])
    if not isinstance(products, list):
        raise ValueError("invalid_catalog_response")
    return products


def _fetch_product(handle):
    if not HANDLE_RE.fullmatch(str(handle or "")):
        raise ValueError("invalid_product_handle")
    request = Request(
        f"{SOURCE_ORIGIN}/products/{handle}.js",
        headers={"User-Agent": "L2F-Catalog-Sync/1.0", "Accept": "application/json"},
    )
    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        if not response.geturl().startswith(SOURCE_ORIGIN + "/"):
            raise ValueError("untrusted_product_redirect")
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ValueError("product_response_too_large")
    product = json.loads(payload.decode("utf-8"))
    if not isinstance(product, dict):
        raise ValueError("invalid_product_response")
    return product


def _variant_details(product, variant, color_map):
    handle = str(product.get("handle") or "").strip()
    match = HANDLE_RE.fullmatch(handle)
    if not match:
        return None
    sku, color_code = match.groups()
    canonical = normalize_barcode(variant.get("sku") or "")
    if not canonical or not canonical.upper().startswith(handle.upper()):
        return None
    suffix = canonical[len(handle):]
    if not SIZE_CODE_RE.fullmatch(suffix):
        return None
    size = normalize_size_label(
        variant.get("option1") or variant.get("title") or ""
    )
    if not size or size == "default title":
        return None
    alias = normalize_barcode(variant.get("barcode") or "")
    if not EAN_RE.fullmatch(alias):
        alias = ""
    return {
        "sku": sku,
        "color_code": color_code,
        "color": color_map.get(color_code, color_code),
        "size": size,
        "canonical": canonical,
        "alias": alias,
    }


def _import_page(products, stats):
    conn = get_connection()
    try:
        color_rows = conn.execute(
            "SELECT code,color FROM barcode_color_scale"
        ).fetchall()
        color_map = {str(row["code"]): str(row["color"]) for row in color_rows}
        for product in products:
            handle = str(product.get("handle") or "").strip()
            if HANDLE_RE.fullmatch(handle):
                conn.execute(
                    """INSERT INTO external_catalog_products (handle,status,updated_at)
                       VALUES (?, 'pending', datetime('now','localtime'))
                       ON CONFLICT(handle) DO UPDATE SET
                           status=CASE
                               WHEN external_catalog_products.status='syncing' THEN 'syncing'
                               ELSE 'pending'
                           END,
                           last_error=CASE
                               WHEN external_catalog_products.status='syncing' THEN external_catalog_products.last_error
                               ELSE ''
                           END,
                           updated_at=CASE
                               WHEN external_catalog_products.status='syncing' THEN external_catalog_products.updated_at
                               ELSE excluded.updated_at
                           END""",
                    (handle,),
                )
            variants = product.get("variants") or []
            if not isinstance(variants, list):
                continue
            product_model_created = set()
            product_new_barcode = set()
            for variant in variants:
                details = _variant_details(product, variant, color_map)
                if not details:
                    continue
                stats["variants_seen"] += 1
                sku = details["sku"]
                if sku not in product_model_created:
                    ensure_catalog_model(conn, sku, requeue_not_found=False)
                    product_model_created.add(sku)
                cursor = conn.execute(
                    """INSERT INTO barcodes (barcode,sku,color,size)
                       VALUES (?,?,?,?)
                       ON CONFLICT(barcode) DO UPDATE SET
                           sku=excluded.sku,
                           color=excluded.color,
                           size=excluded.size
                       WHERE barcodes.sku<>excluded.sku
                          OR barcodes.color<>excluded.color
                          OR barcodes.size<>excluded.size""",
                    (
                        details["canonical"], sku,
                        details["color"], details["size"],
                    ),
                )
                if cursor.rowcount and cursor.rowcount > 0:
                    stats["barcodes_added"] += cursor.rowcount
                    product_new_barcode.add(sku)
                if details["alias"] and details["alias"] != details["canonical"]:
                    alias_cursor = conn.execute(
                        """INSERT INTO barcode_aliases (alias,canonical_barcode,source)
                           VALUES (?,?,?)
                           ON CONFLICT(alias) DO UPDATE SET
                               canonical_barcode=excluded.canonical_barcode,
                               source=excluded.source
                           WHERE barcode_aliases.canonical_barcode<>excluded.canonical_barcode
                              OR barcode_aliases.source<>excluded.source""",
                        (details["alias"], details["canonical"], SOURCE),
                    )
                    if alias_cursor.rowcount and alias_cursor.rowcount > 0:
                        stats["aliases_added"] += alias_cursor.rowcount
            for sku in product_new_barcode:
                ensure_catalog_model(conn, sku, requeue_not_found=True)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sync_catalog():
    """Import all currently public products and return synchronization stats."""
    if not _sync_lock.acquire(blocking=False):
        return {"status": "already_running"}
    stats = {
        "products_seen": 0,
        "variants_seen": 0,
        "barcodes_added": 0,
        "aliases_added": 0,
    }
    try:
        _set_state("syncing", stats=stats)
        for page in range(1, MAX_PAGES + 1):
            products = _fetch_page(page)
            if not products:
                break
            stats["products_seen"] += len(products)
            _import_page(products, stats)
            if len(products) < PAGE_SIZE:
                break
        _set_state("completed", stats=stats, completed=True)
        print(f"ITAY BRANDS catalog sync completed: {stats}")
        return {"status": "completed", **stats}
    except Exception as exc:
        _set_state("failed", error=str(exc), stats=stats, completed=True)
        print(f"ITAY BRANDS catalog sync failed: {exc}")
        raise
    finally:
        _sync_lock.release()


def _seconds_until_next_sync(now=None):
    current = now.astimezone(TIMEZONE) if now else datetime.now(TIMEZONE)
    next_run = datetime.combine(current.date(), clock_time(hour=SYNC_HOUR), TIMEZONE)
    if next_run <= current:
        next_run += timedelta(days=1)
    return max(1.0, (next_run - current).total_seconds())


def _claim_ean_product():
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT handle FROM external_catalog_products
                WHERE status='pending'
                ORDER BY updated_at,handle LIMIT 1"""
        ).fetchone()
        if not row:
            conn.close()
            return ""
        handle = str(row["handle"])
        cursor = conn.execute(
            f"""UPDATE external_catalog_products
                   SET status='syncing',attempts=attempts+1,last_error='',updated_at={_now_sql()}
                 WHERE handle=? AND status='pending'""",
            (handle,),
        )
        conn.commit()
        claimed = cursor.rowcount > 0
        conn.close()
        return handle if claimed else ""
    except Exception:
        conn.rollback()
        conn.close()
        raise


def _finish_ean_product(handle, status, error=""):
    conn = get_connection()
    try:
        conn.execute(
            f"""UPDATE external_catalog_products
                   SET status=?,last_error=?,updated_at={_now_sql()}
                 WHERE handle=?""",
            (status, str(error)[:500], handle),
        )
        conn.commit()
    finally:
        conn.close()


def reconcile_product_catalog(conn, product):
    """Apply the source site's SKU/color/size truth for every product variant."""
    color_rows = conn.execute(
        "SELECT code,color FROM barcode_color_scale"
    ).fetchall()
    color_map = {str(row["code"]): str(row["color"]) for row in color_rows}
    result = {"variants": 0, "barcodes_updated": 0, "aliases_updated": 0}
    for variant in product.get("variants") or []:
        details = _variant_details(product, variant, color_map)
        if not details:
            continue
        result["variants"] += 1
        ensure_catalog_model(conn, details["sku"], requeue_not_found=False)
        cursor = conn.execute(
            """INSERT INTO barcodes (barcode,sku,color,size)
               VALUES (?,?,?,?)
               ON CONFLICT(barcode) DO UPDATE SET
                   sku=excluded.sku,
                   color=excluded.color,
                   size=excluded.size
               WHERE barcodes.sku<>excluded.sku
                  OR barcodes.color<>excluded.color
                  OR barcodes.size<>excluded.size""",
            (
                details["canonical"], details["sku"],
                details["color"], details["size"],
            ),
        )
        if cursor.rowcount and cursor.rowcount > 0:
            result["barcodes_updated"] += cursor.rowcount
        alias = details["alias"]
        if not alias or alias == details["canonical"]:
            continue
        alias_cursor = conn.execute(
            """INSERT INTO barcode_aliases (alias,canonical_barcode,source)
               VALUES (?,?,?) ON CONFLICT(alias) DO UPDATE SET
                   canonical_barcode=excluded.canonical_barcode,
                   source=excluded.source
               WHERE barcode_aliases.canonical_barcode<>excluded.canonical_barcode
                  OR barcode_aliases.source<>excluded.source""",
            (alias, details["canonical"], SOURCE),
        )
        if alias_cursor.rowcount and alias_cursor.rowcount > 0:
            result["aliases_updated"] += alias_cursor.rowcount
    return result


def _sync_product_eans(handle):
    product = _fetch_product(handle)
    conn = get_connection()
    try:
        result = reconcile_product_catalog(conn, product)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _finish_ean_product(handle, "synced")
    return result["aliases_updated"]


def _ean_worker():
    conn = get_connection()
    try:
        conn.execute(
            f"""UPDATE external_catalog_products
                   SET status='pending',updated_at={_now_sql()}
                 WHERE status='syncing'"""
        )
        conn.commit()
    finally:
        conn.close()
    while True:
        try:
            handle = _claim_ean_product()
            if not handle:
                threading.Event().wait(10)
                continue
            try:
                _sync_product_eans(handle)
            except HTTPError as exc:
                _finish_ean_product(handle, "not_found" if exc.code == 404 else "pending", exc)
            except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
                _finish_ean_product(handle, "pending", exc)
                threading.Event().wait(5)
            time.sleep(max(0, EAN_REQUEST_INTERVAL))
        except Exception:
            threading.Event().wait(5)


def _worker():
    if STARTUP_DELAY > 0:
        threading.Event().wait(STARTUP_DELAY)
    try:
        sync_catalog()
    except Exception:
        pass
    while True:
        threading.Event().wait(_seconds_until_next_sync())
        try:
            sync_catalog()
        except Exception:
            pass


def start_catalog_sync():
    global _worker_thread, _ean_thread
    if not SYNC_ENABLED:
        return None
    with _worker_lock:
        if _worker_thread and _worker_thread.is_alive():
            return _worker_thread
        _worker_thread = threading.Thread(
            target=_worker,
            name="itay-catalog-sync",
            daemon=True,
        )
        _worker_thread.start()
        _ean_thread = threading.Thread(
            target=_ean_worker,
            name="itay-ean-sync",
            daemon=True,
        )
        _ean_thread.start()
        return _worker_thread
