"""Background synchronization of catalog images from ITAY BRANDS.

The request path only enqueues a SKU in PostgreSQL/SQLite.  A single daemon
worker claims jobs and performs the external requests, so catalog writes stay
fast and concurrent branches cannot create duplicate work.
"""

import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from backend.catalog_sync import reconcile_product_catalog

from database.db import get_connection


SOURCE_ORIGIN = "https://itaybrands.co.il"
SOURCE_HOSTS = {"itaybrands.co.il", "www.itaybrands.co.il"}
IMAGE_HOSTS = {"cdn.shopify.com", "itaybrands.co.il", "www.itaybrands.co.il"}
SYNC_ENABLED = os.environ.get("PRODUCT_IMAGE_SYNC_ENABLED", "1").strip().lower() not in {
    "0", "false", "no",
}
REQUEST_TIMEOUT = float(os.environ.get("PRODUCT_IMAGE_SYNC_TIMEOUT", "15"))
REQUEST_INTERVAL = float(os.environ.get("PRODUCT_IMAGE_SYNC_INTERVAL", "0.6"))
IDLE_INTERVAL = float(os.environ.get("PRODUCT_IMAGE_SYNC_IDLE_INTERVAL", "10"))
MAX_IMAGE_BYTES = int(os.environ.get("PRODUCT_IMAGE_MAX_BYTES", str(4 * 1024 * 1024)))
MAX_COLOR_CODES = int(os.environ.get("PRODUCT_IMAGE_MAX_COLORS", "16"))
DEFAULT_COLOR_CODES = tuple(
    code.strip()
    for code in os.environ.get(
        "PRODUCT_IMAGE_DEFAULT_COLORS",
        "0001,0002,0004,0011,0018,0015,0005,0019,0003,0008,0012,0026,0028,0035,0052,0087",
    ).split(",")
    if re.fullmatch(r"\d{4}", code.strip())
)
IMAGE_DIR = Path(
    os.environ.get(
        "PRODUCT_IMAGE_DIR",
        str(Path(__file__).resolve().parent.parent / "static" / "product_images"),
    )
)
PUBLIC_IMAGE_PREFIX = os.environ.get(
    "PRODUCT_IMAGE_PUBLIC_PREFIX", "/static/product_images"
).rstrip("/")

_worker_lock = threading.Lock()
_worker_thread = None


class ProductNotFound(Exception):
    pass


def _now_sql():
    return "datetime('now','localtime')"


def enqueue_all_missing_images(conn):
    """Queue every shared catalog model that has no saved image."""
    conn.execute(
        f"""INSERT INTO product_image_sync_jobs (sku, status, updated_at)
            SELECT cm.sku, 'pending', {_now_sql()}
              FROM catalog_models cm
             WHERE NOT EXISTS (
                       SELECT 1 FROM product_images pi WHERE pi.sku=cm.sku
                   )
            ON CONFLICT(sku) DO NOTHING"""
    )


def _claim_job():
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT sku FROM product_image_sync_jobs
               WHERE status='pending'
               ORDER BY updated_at, sku LIMIT 1"""
        ).fetchone()
        if not row:
            conn.close()
            return None
        sku = str(row["sku"])
        cursor = conn.execute(
            f"""UPDATE product_image_sync_jobs
                   SET status='syncing', attempts=attempts+1,
                       last_error='', updated_at={_now_sql()}
                 WHERE sku=? AND status='pending'""",
            (sku,),
        )
        conn.commit()
        claimed = cursor.rowcount > 0
        conn.close()
        return sku if claimed else None
    except Exception:
        conn.rollback()
        conn.close()
        raise


def _request_bytes(url, accepted_hosts, accept):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in accepted_hosts:
        raise ValueError("untrusted_source_url")
    request = Request(
        url,
        headers={
            "User-Agent": "L2F-Product-Image-Sync/1.0",
            "Accept": accept,
        },
    )
    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        final_host = (urlsplit(response.geturl()).hostname or "").lower()
        if final_host not in accepted_hosts:
            raise ValueError("untrusted_redirect")
        content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].lower()
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_IMAGE_BYTES:
            raise ValueError("response_too_large")
        payload = response.read(MAX_IMAGE_BYTES + 1)
        if len(payload) > MAX_IMAGE_BYTES:
            raise ValueError("response_too_large")
        return payload, content_type


def _fetch_product(sku, color_code):
    handle = f"i{sku}{color_code}"
    product_url = f"{SOURCE_ORIGIN}/products/{handle}"
    try:
        payload, content_type = _request_bytes(
            product_url + ".js", SOURCE_HOSTS, "application/json"
        )
    except HTTPError as exc:
        if exc.code == 404:
            raise ProductNotFound(handle) from exc
        raise
    if "json" not in content_type and "javascript" not in content_type:
        # Shopify may redirect unpublished/retired handles to an HTML page.
        raise ProductNotFound(handle)
    product = json.loads(payload.decode("utf-8"))
    image_url = str(product.get("featured_image") or "").strip()
    if image_url.startswith("//"):
        image_url = "https:" + image_url
    if not image_url:
        raise ProductNotFound(handle)
    return product, product_url, _resize_shopify_url(image_url)


def _resize_shopify_url(url):
    """Ask Shopify CDN for a warehouse-friendly image instead of the original."""
    parsed = urlsplit(url)
    query = parsed.query
    query = re.sub(r"(^|&)width=\d+(&|$)", lambda m: m.group(1), query).strip("&")
    query = (query + "&" if query else "") + "width=720"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))


def _extension_for(content_type):
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }.get(content_type, "")


def _download_image(image_url, sku, color_code):
    payload, content_type = _request_bytes(image_url, IMAGE_HOSTS, "image/*")
    extension = _extension_for(content_type)
    if not extension:
        raise ValueError("unexpected_image_response")
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{sku}-{color_code}{extension}"
    target = IMAGE_DIR / filename
    temporary = IMAGE_DIR / (filename + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, target)
    return f"{PUBLIC_IMAGE_PREFIX}/{filename}"


def _candidate_colors(conn, sku):
    candidates = []
    seen = set()

    def add(code, color=""):
        code = str(code or "").strip()
        if not re.fullmatch(r"\d{4}", code) or code in seen:
            return
        seen.add(code)
        candidates.append((code, str(color or "").strip()))

    structured = conn.execute(
        """SELECT DISTINCT SUBSTR(barcode,7,4) AS color_code, color
             FROM barcodes
            WHERE sku=? AND LENGTH(barcode)=12
              AND SUBSTR(barcode,2,5)=?
            ORDER BY color_code""",
        (sku, sku),
    ).fetchall()
    for row in structured:
        add(row["color_code"], row["color"])

    mapped = conn.execute(
        """SELECT DISTINCT scale.code AS color_code, barcode.color
             FROM barcodes barcode
             JOIN barcode_color_scale scale
               ON TRIM(scale.color)=TRIM(barcode.color)
            WHERE barcode.sku=?
            ORDER BY scale.code""",
        (sku,),
    ).fetchall()
    for row in mapped:
        add(row["color_code"], row["color"])

    if candidates:
        add("0001", "")
    else:
        for code in DEFAULT_COLOR_CODES:
            add(code, "")
    return candidates[:MAX_COLOR_CODES]


def sync_sku(sku):
    """Synchronize every catalog-known color for one SKU."""
    sku = str(sku or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{3,20}", sku):
        raise ProductNotFound(sku)
    conn = get_connection()
    try:
        candidates = _candidate_colors(conn, sku)
        conn.close()
    except Exception:
        conn.close()
        raise

    imported = []
    for color_code, catalog_color in candidates:
        try:
            product, product_url, image_url = _fetch_product(sku, color_code)
        except ProductNotFound:
            pass
        else:
            image_path = _download_image(image_url, sku, color_code)
            imported.append({
                "color_code": color_code,
                "color": catalog_color,
                "title": str(product.get("title") or "").strip(),
                "image_path": image_path,
                "product_url": product_url,
                "source_image_url": image_url,
                "product": product,
            })
        finally:
            # Rate-limit both successful requests and 404 probes.
            time.sleep(max(0, REQUEST_INTERVAL))

    if not imported:
        raise ProductNotFound(sku)

    conn = get_connection()
    try:
        for index, image in enumerate(imported):
            reconcile_product_catalog(conn, image["product"])
            conn.execute(
                f"""INSERT INTO product_images
                       (sku,color_code,color,title,image_path,product_url,
                        source_image_url,is_primary,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,{_now_sql()})
                   ON CONFLICT(sku,color_code) DO UPDATE SET
                       color=excluded.color,
                       title=excluded.title,
                       image_path=excluded.image_path,
                       product_url=excluded.product_url,
                       source_image_url=excluded.source_image_url,
                       is_primary=excluded.is_primary,
                       updated_at=excluded.updated_at""",
                (
                    sku, image["color_code"], image["color"], image["title"],
                    image["image_path"], image["product_url"],
                    image["source_image_url"], 1 if index == 0 else 0,
                ),
            )
        conn.execute(
            f"""UPDATE product_image_sync_jobs
                   SET status='synced', last_error='', updated_at={_now_sql()}
                 WHERE sku=?""",
            (sku,),
        )
        conn.commit()
    finally:
        conn.close()
    return len(imported)


def _finish_failed_job(sku, status, error):
    conn = get_connection()
    try:
        conn.execute(
            f"""UPDATE product_image_sync_jobs
                   SET status=?, last_error=?, updated_at={_now_sql()}
                 WHERE sku=?""",
            (status, str(error)[:500], sku),
        )
        conn.commit()
    finally:
        conn.close()


def _worker_loop():
    while True:
        try:
            sku = _claim_job()
            if not sku:
                time.sleep(max(1, IDLE_INTERVAL))
                continue
            try:
                sync_sku(sku)
            except ProductNotFound as exc:
                _finish_failed_job(sku, "not_found", exc)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
                _finish_failed_job(sku, "pending", exc)
                time.sleep(max(2, IDLE_INTERVAL))
            time.sleep(max(0, REQUEST_INTERVAL))
        except Exception:
            time.sleep(max(2, IDLE_INTERVAL))


def start_product_image_sync():
    """Seed missing jobs and start one process-local daemon worker."""
    global _worker_thread
    if not SYNC_ENABLED:
        return None
    with _worker_lock:
        if _worker_thread and _worker_thread.is_alive():
            return _worker_thread
        conn = get_connection()
        try:
            enqueue_all_missing_images(conn)
            conn.execute(
                f"""UPDATE product_image_sync_jobs
                       SET status='pending', updated_at={_now_sql()}
                     WHERE status='syncing'"""
            )
            conn.commit()
        finally:
            conn.close()
        _worker_thread = threading.Thread(
            target=_worker_loop,
            name="product-image-sync",
            daemon=True,
        )
        _worker_thread.start()
        return _worker_thread
