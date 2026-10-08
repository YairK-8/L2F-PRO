"""
Tab 2 — "Scanned = Missing from Warehouse" (per branch, FIFO)
Emits realtime updates via SocketIO.
"""
from flask import Blueprint, request, jsonify
from database.db import get_connection, insert_and_get_id
from backend.auth_utils import require_branch
from backend.barcodes import normalize_size_label, normalize_barcode as _normalize_barcode, resolve_barcode_catalog_entry
from backend.product_images import attach_product_image, attach_product_images
from backend.realtime import emit_update
from backend.utils import today as _today

missing_warehouse_bp = Blueprint("missing_warehouse", __name__, url_prefix="/api/missing-warehouse")

def _load_item_with_location(conn, item_id):
    row = conn.execute(
        """SELECT mw.*, wl.location AS location_hint,
                  EXISTS (
                      SELECT 1 FROM missing_floor mf
                      WHERE mf.branch_id=mw.branch_id
                        AND mf.sku=mw.sku AND mf.color=mw.color AND mf.size=mw.size
                        AND mf.status='missing'
                  ) AS is_approved_missing
           FROM missing_warehouse mw
           LEFT JOIN warehouse_locations wl
             ON wl.branch_id = mw.branch_id AND wl.sku = mw.sku
           WHERE mw.id=?""",
        (item_id,)
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["size"] = normalize_size_label(item.get("size", ""))
    item["location_hint"] = row["location_hint"] or ""
    return attach_product_image(conn, item)


def _find_pending_item(conn, branch_id, sku, color, size):
    return conn.execute(
        """SELECT * FROM missing_warehouse
           WHERE branch_id=? AND sku=? AND color=? AND size=? AND status='pending'""",
        (branch_id, sku, color, size)
    ).fetchone()


def _history_list(value: str) -> list[str]:
    return [x for x in str(value or "").split(",") if x]


def _history_str(values: list[str]) -> str:
    return ",".join(values)


def _lock_warehouse_branch(conn, branch_id):
    """Serialize sale-list mutations per branch while the transaction is open."""
    if conn.dialect == "postgres":
        conn.execute("SELECT pg_advisory_xact_lock(?, ?)", (127837, int(branch_id)))


def _clear_stale_pending_missing_warehouse(conn, branch_id):
    conn.execute(
        """DELETE FROM missing_warehouse
           WHERE branch_id=?
             AND status='pending'
             AND scanned_at < ?""",
        (branch_id, _today())
    )


def _ensure_missing_floor_item(conn, branch_id, sku, color, size):
    conn.execute(
        """INSERT INTO missing_floor (branch_id,sku,color,size)
           VALUES (?,?,?,?)
           ON CONFLICT(branch_id,sku,color,size) WHERE status='missing' DO NOTHING""",
        (branch_id, sku, color, size),
    )
    row = conn.execute(
        """SELECT id FROM missing_floor
           WHERE branch_id=? AND sku=? AND color=? AND size=? AND status='missing'""",
        (branch_id, sku, color, size),
    ).fetchone()
    return row["id"]


def _load_missing_floor_item(conn, item_id):
    row = conn.execute(
        """SELECT mf.*, wl.location AS location_hint
           FROM missing_floor mf
           LEFT JOIN warehouse_locations wl
             ON wl.branch_id = mf.branch_id AND wl.sku = mf.sku
           WHERE mf.id=?""",
        (item_id,)
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["size"] = normalize_size_label(item.get("size", ""))
    item["location_hint"] = row["location_hint"] or ""
    return item


@missing_warehouse_bp.route("", methods=["GET"])
@require_branch
def list_pending(branch_id):
    conn = get_connection()
    rows = conn.execute(
        """SELECT mw.*, wl.location AS location_hint,
                  EXISTS (
                      SELECT 1 FROM missing_floor mf
                      WHERE mf.branch_id=mw.branch_id
                        AND mf.sku=mw.sku AND mf.color=mw.color AND mf.size=mw.size
                        AND mf.status='missing'
                  ) AS is_approved_missing
           FROM missing_warehouse mw
           LEFT JOIN warehouse_locations wl
             ON wl.branch_id = mw.branch_id AND wl.sku = mw.sku
           WHERE mw.branch_id=? AND mw.status='pending'
             AND mw.scanned_at >= ?
           ORDER BY mw.scanned_at ASC, mw.id ASC""",
        (branch_id, _today())
    ).fetchall()
    result = []
    for r in rows:
        item = dict(r)
        item["size"] = normalize_size_label(item.get("size", ""))
        item["location_hint"] = r["location_hint"] or ""
        result.append(item)
    result = attach_product_images(conn, result)
    conn.close()
    return jsonify(result)


@missing_warehouse_bp.route("/scan", methods=["POST"])
@require_branch
def scan_sold(branch_id):
    data = request.get_json(silent=True) or {}
    source_device_id = str(data.get("device_id", "")).strip()
    barcode_raw = data.get("barcode", "")
    barcode = _normalize_barcode(barcode_raw)
    conn = get_connection()
    _lock_warehouse_branch(conn, branch_id)
    _clear_stale_pending_missing_warehouse(conn, branch_id)

    if barcode:
        meta, _catalog_created = resolve_barcode_catalog_entry(
            conn,
            barcode,
            autocreate_structured=True,
        )
        if not meta:
            conn.close()
            return jsonify({
                "error": "not_found",
                "barcode_received": str(barcode_raw),
                "barcode_normalized": barcode
            }), 404
        sku, color, size = meta["sku"], meta["color"], normalize_size_label(meta["size"])
    else:
        sku = str(data.get("sku", "")).strip()
        color = str(data.get("color", "")).strip()
        size = normalize_size_label(data.get("size", ""))
        if not all([sku, color, size]):
            conn.close()
            return jsonify({"error": "missing_fields"}), 400

    existing = _find_pending_item(conn, branch_id, sku, color, size)
    now_ts = conn.execute("SELECT datetime('now','localtime') AS ts").fetchone()["ts"]
    if existing:
        item_id = existing["id"]
        already_pending = True
    else:
        item_id = insert_and_get_id(
            conn,
            "INSERT INTO missing_warehouse (branch_id,sku,color,size,quantity,scan_history,scanned_at) VALUES (?,?,?,?,1,?,?)",
            (branch_id, sku, color, size, now_ts, now_ts),
        )
        already_pending = False
    conn.commit()

    item = _load_item_with_location(conn, item_id)
    conn.close()

    if not already_pending:
        item["_source_device_id"] = source_device_id
        emit_update(branch_id, "tab2_new_item", item)
    return jsonify({"ok": True, "item": item, "already_pending": already_pending}), 200 if already_pending else 201


@missing_warehouse_bp.route("/<int:item_id>/restock", methods=["POST"])
@require_branch
def mark_restocked(branch_id, item_id):
    conn = get_connection()
    conn.execute(
        """UPDATE missing_warehouse
           SET status='restocked', restocked_at=datetime('now','localtime')
           WHERE id=? AND branch_id=?""",
        (item_id, branch_id)
    )
    conn.commit()
    changed = conn.total_changes
    conn.close()
    if not changed:
        return jsonify({"error": "not_found"}), 404
    emit_update(branch_id, "tab2_item_restocked", {"id": item_id})
    return jsonify({"ok": True})


@missing_warehouse_bp.route("/<int:item_id>/missing", methods=["POST"])
@require_branch
def mark_missing(branch_id, item_id):
    conn = get_connection()
    row = conn.execute(
        """SELECT * FROM missing_warehouse
           WHERE id=? AND branch_id=? AND status='pending'""",
        (item_id, branch_id)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not_found"}), 404

    floor_item_id = _ensure_missing_floor_item(conn, branch_id, row["sku"], row["color"], row["size"])
    floor_item = _load_missing_floor_item(conn, floor_item_id)
    conn.execute(
        """UPDATE missing_warehouse
           SET status='missing', restocked_at=datetime('now','localtime')
           WHERE id=? AND branch_id=?""",
        (item_id, branch_id)
    )
    conn.commit()
    conn.close()

    emit_update(branch_id, "tab2_item_restocked", {"id": item_id})
    if floor_item:
        emit_update(branch_id, "tab1_floor_missing_added", floor_item)
    return jsonify({"ok": True, "missing_floor_item": floor_item})


@missing_warehouse_bp.route("/undo-last", methods=["POST"])
@require_branch
def undo_last(branch_id):
    conn = get_connection()
    _lock_warehouse_branch(conn, branch_id)
    row = conn.execute(
        """SELECT * FROM missing_warehouse
           WHERE branch_id=? AND status='pending'
           ORDER BY scanned_at DESC, id DESC
           LIMIT 1""",
        (branch_id,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not_found"}), 404

    conn.execute("DELETE FROM missing_warehouse WHERE id=? AND branch_id=?", (row["id"], branch_id))
    conn.commit()
    conn.close()
    emit_update(branch_id, "tab2_item_restocked", {"id": row["id"]})
    return jsonify({
        "ok": True,
        "removed_id": row["id"],
        "undone_item": {
            "sku": row["sku"],
            "color": row["color"],
            "size": row["size"],
            "quantity": 0
        }
    })


@missing_warehouse_bp.route("/clear", methods=["POST"])
@require_branch
def clear_all(branch_id):
    conn = get_connection()
    conn.execute(
        "DELETE FROM missing_warehouse WHERE branch_id=? AND status='pending'",
        (branch_id,)
    )
    conn.commit()
    conn.close()
    emit_update(branch_id, "tab2_cleared", {})
    return jsonify({"ok": True})
