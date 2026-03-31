"""
Tab 2 — "Scanned = Missing from Warehouse" (per branch, FIFO)
Emits realtime updates via SocketIO.
"""
from flask import Blueprint, request, jsonify
from database.db import get_connection
from backend.auth_utils import require_branch
from backend.realtime import emit_update
from datetime import date

missing_warehouse_bp = Blueprint("missing_warehouse", __name__, url_prefix="/api/missing-warehouse")


def _today():
    return str(date.today())


def _normalize_barcode(value: str) -> str:
    value = str(value or "").strip().upper()
    if value.startswith("E"):
        value = value[1:]
    return value


def _load_item_with_location(conn, item_id):
    row = conn.execute(
        """SELECT mw.*, wl.location AS location_hint
           FROM missing_warehouse mw
           LEFT JOIN warehouse_locations wl
             ON wl.branch_id = mw.branch_id AND wl.sku = mw.sku
           WHERE mw.id=?""",
        (item_id,)
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["location_hint"] = row["location_hint"] or ""
    return item


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


def _clear_stale_pending_missing_warehouse(conn, branch_id):
    conn.execute(
        """DELETE FROM missing_warehouse
           WHERE branch_id=?
             AND status='pending'
             AND substr(scanned_at, 1, 10) < ?""",
        (branch_id, _today())
    )


def _ensure_missing_floor_item(conn, branch_id, sku, color, size):
    exists = conn.execute(
        """SELECT id FROM missing_floor
           WHERE branch_id=? AND sku=? AND color=? AND size=? AND status='missing'""",
        (branch_id, sku, color, size)
    ).fetchone()
    if exists:
        return exists["id"]

    cur = conn.execute(
        "INSERT INTO missing_floor (branch_id,sku,color,size) VALUES (?,?,?,?)",
        (branch_id, sku, color, size)
    )
    return cur.lastrowid


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
    item["location_hint"] = row["location_hint"] or ""
    return item


@missing_warehouse_bp.route("", methods=["GET"])
@require_branch
def list_pending(branch_id):
    conn = get_connection()
    _clear_stale_pending_missing_warehouse(conn, branch_id)
    conn.commit()
    rows = conn.execute(
        """SELECT mw.*, wl.location AS location_hint
           FROM missing_warehouse mw
           LEFT JOIN warehouse_locations wl
             ON wl.branch_id = mw.branch_id AND wl.sku = mw.sku
           WHERE mw.branch_id=? AND mw.status='pending'
           ORDER BY mw.scanned_at ASC, mw.id ASC""",
        (branch_id,)
    ).fetchall()
    conn.close()

    result = []
    for r in rows:
        item = dict(r)
        item["location_hint"] = r["location_hint"] or ""
        result.append(item)
    return jsonify(result)


@missing_warehouse_bp.route("/scan", methods=["POST"])
@require_branch
def scan_sold(branch_id):
    data = request.get_json(silent=True) or {}
    barcode_raw = data.get("barcode", "")
    barcode = _normalize_barcode(barcode_raw)
    conn = get_connection()
    _clear_stale_pending_missing_warehouse(conn, branch_id)

    if barcode:
        meta = conn.execute(
            "SELECT sku,color,size FROM barcodes WHERE barcode=?",
            (barcode,)
        ).fetchone()
        if not meta:
            conn.close()
            return jsonify({
                "error": "not_found",
                "barcode_received": str(barcode_raw),
                "barcode_normalized": barcode
            }), 404
        sku, color, size = meta["sku"], meta["color"], meta["size"]
    else:
        sku = str(data.get("sku", "")).strip()
        color = str(data.get("color", "")).strip()
        size = str(data.get("size", "")).strip().lower()
        if not all([sku, color, size]):
            conn.close()
            return jsonify({"error": "missing_fields"}), 400

    existing = _find_pending_item(conn, branch_id, sku, color, size)
    now_ts = conn.execute("SELECT datetime('now','localtime') AS ts").fetchone()["ts"]
    if existing:
        history = _history_list(existing["scan_history"])
        history.append(now_ts)
        conn.execute(
            "UPDATE missing_warehouse SET quantity=quantity+1, scanned_at=?, scan_history=? WHERE id=?",
            (now_ts, _history_str(history), existing["id"])
        )
        item_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO missing_warehouse (branch_id,sku,color,size,quantity,scan_history,scanned_at) VALUES (?,?,?,?,1,?,?)",
            (branch_id, sku, color, size, now_ts, now_ts)
        )
        item_id = cur.lastrowid
    conn.commit()

    item = _load_item_with_location(conn, item_id)
    conn.close()

    emit_update(branch_id, "tab2_new_item", item)
    return jsonify({"ok": True, "item": item}), 201


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

    history = _history_list(row["scan_history"])
    if history:
        history.pop()

    new_quantity = max(0, int(row["quantity"] or 1) - 1)
    if new_quantity <= 0:
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

    new_scanned_at = history[-1] if history else row["scanned_at"]
    conn.execute(
        "UPDATE missing_warehouse SET quantity=?, scan_history=?, scanned_at=? WHERE id=? AND branch_id=?",
        (new_quantity, _history_str(history), new_scanned_at, row["id"], branch_id)
    )
    conn.commit()
    item = _load_item_with_location(conn, row["id"])
    conn.close()
    emit_update(branch_id, "tab2_new_item", item)
    return jsonify({
        "ok": True,
        "item": item,
        "undone_item": {
            "sku": row["sku"],
            "color": row["color"],
            "size": row["size"],
            "quantity": new_quantity
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
