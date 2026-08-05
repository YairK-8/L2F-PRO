from flask import Blueprint, request, jsonify
from database.db import get_connection
from backend.auth_utils import require_branch
from backend.barcodes import (
    get_catalog_sizes_for_sku_color,
    normalize_barcode as _normalize_catalog_barcode,
    normalize_size_label,
    resolve_barcode_catalog_entry,
)
from backend.realtime import emit_update
from backend.scan_queue import JOB_TYPE_MORNING_SCAN, enqueue_scan_job
from backend.utils import today as _today

missing_floor_bp = Blueprint("missing_floor", __name__, url_prefix="/api/missing-floor")

def _sizes_list(s: str) -> list:
    result = []
    for value in str(s or "").split(","):
        normalized = normalize_size_label(value)
        if normalized:
            result.append(normalized)
    return result


def _sizes_str(lst: list) -> str:
    seen = set()
    ordered = []
    for value in lst or []:
        cleaned = normalize_size_label(value)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        ordered.append(cleaned)
    return ",".join(ordered)


def _order_found_sizes(all_sizes, found_sizes):
    normalized_found = _sizes_list(_sizes_str(found_sizes))
    found_set = set(normalized_found)
    ordered = [size for size in all_sizes if size in found_set]
    for size in normalized_found:
        if size not in ordered:
            ordered.append(size)
    return ordered


def _get_session_catalog_sizes(conn, sku, color, extra_sizes=None):
    return get_catalog_sizes_for_sku_color(
        conn,
        sku,
        color,
        include_sizes=extra_sizes,
    )


def _normalize_session_row(conn, row, persist=False):
    session_data = dict(row)
    stored_sizes = _sizes_list(session_data.get("sizes_all", ""))
    found_sizes = _sizes_list(session_data.get("sizes_found", ""))
    all_sizes = _get_session_catalog_sizes(
        conn,
        session_data["sku"],
        session_data["color"],
        extra_sizes=[*stored_sizes, *found_sizes],
    )
    ordered_found = _order_found_sizes(all_sizes, found_sizes)
    all_sizes_str = _sizes_str(all_sizes)
    found_sizes_str = _sizes_str(ordered_found)

    if persist and session_data.get("id") and (
        session_data.get("sizes_all", "") != all_sizes_str
        or session_data.get("sizes_found", "") != found_sizes_str
    ):
        conn.execute(
            "UPDATE morning_sessions SET sizes_all=?, sizes_found=? WHERE id=?",
            (all_sizes_str, found_sizes_str, session_data["id"]),
        )

    session_data["sizes_all"] = all_sizes
    session_data["sizes_found"] = ordered_found
    return session_data

def _location_for_sku(conn, branch_id, sku):
    row = conn.execute(
        "SELECT location FROM warehouse_locations WHERE branch_id=? AND sku=?",
        (branch_id, sku)
    ).fetchone()
    return row["location"] if row else ""


def _clear_stale_manual_missing(conn, branch_id, current_session_date):
    conn.execute(
        """DELETE FROM missing_floor
           WHERE branch_id=?
             AND status='missing'
             AND source='manual'
             AND manual_session_date IS NOT NULL
             AND manual_session_date < ?""",
        (branch_id, current_session_date)
    )


def _clear_stale_morning_sessions(conn, branch_id, current_session_date):
    # Morning sessions now stay open until a manual clear.
    return


def _resolve_missing_floor_item(conn, branch_id, sku, color, size):
    conn.execute(
        """UPDATE missing_floor
           SET status='resolved', resolved_at=datetime('now','localtime')
           WHERE branch_id=?
             AND sku=?
             AND color=?
             AND size=?
             AND status='missing'""",
        (branch_id, sku, color, size)
    )


def _session_missing_sizes(session_data):
    all_sizes = session_data.get("sizes_all") or []
    found_set = set(session_data.get("sizes_found") or [])
    return [size for size in all_sizes if size not in found_set]


def _approve_session_data(conn, branch_id, session_data, session_date=None):
    missing = _session_missing_sizes(session_data)
    location_hint = _location_for_sku(conn, branch_id, session_data["sku"])
    _clear_stale_manual_missing(conn, branch_id, session_date or _today())

    for size in session_data.get("sizes_found", []):
        _resolve_missing_floor_item(conn, branch_id, session_data["sku"], session_data["color"], size)

    for size in missing:
        exists = conn.execute(
            """SELECT id FROM missing_floor
               WHERE branch_id=? AND sku=? AND color=? AND size=? AND status='missing'""",
            (branch_id, session_data["sku"], session_data["color"], size)
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO missing_floor (branch_id,sku,color,size) VALUES (?,?,?,?)",
                (branch_id, session_data["sku"], session_data["color"], size)
            )

    conn.execute(
        "UPDATE morning_sessions SET approved=1 WHERE id=?",
        (session_data["id"],)
    )
    return {
        "session_id": session_data["id"],
        "sku": session_data["sku"],
        "color": session_data["color"],
        "missing_sizes": missing,
        "location_hint": location_hint,
    }


def _auto_approve_completed_session(conn, branch_id, session_data, session_date=None):
    if _session_missing_sizes(session_data):
        return None
    return _approve_session_data(conn, branch_id, session_data, session_date=session_date)


# ── Morning sessions ──────────────────────────────────────────

@missing_floor_bp.route("/sessions", methods=["GET"])
@require_branch
def get_sessions(branch_id):
    """Return all open (unapproved) morning sessions, with location hint."""
    conn = get_connection()
    try:
        _clear_stale_morning_sessions(conn, branch_id, _today())
        rows = conn.execute(
            """SELECT ms.*, wl.location AS location_hint
               FROM morning_sessions ms
               LEFT JOIN warehouse_locations wl
                 ON wl.branch_id = ms.branch_id AND wl.sku = ms.sku
               WHERE ms.branch_id=? AND ms.approved=0
               ORDER BY ms.sku, ms.color, ms.session_date, ms.created_at, ms.id""",
            (branch_id,)
        ).fetchall()
        result = []
        approved_payloads = []
        for r in rows:
            session_data = _normalize_session_row(conn, r, persist=False)
            approval_payload = _auto_approve_completed_session(conn, branch_id, session_data, r["session_date"])
            if approval_payload:
                approved_payloads.append(approval_payload)
                continue
            session_data["location_hint"] = r["location_hint"] or ""
            result.append(session_data)
        conn.commit()
    finally:
        conn.close()
    for payload in approved_payloads:
        emit_update(branch_id, "tab1_approved", payload)
    return jsonify(result)


@missing_floor_bp.route("/scan", methods=["POST"])
@require_branch
def scan(branch_id):
    data = request.get_json(silent=True) or {}
    barcode = _normalize_catalog_barcode(data.get("barcode", ""))
    if not barcode:
        return jsonify({"error": "missing_barcode"}), 400

    device_id = str(data.get("device_id", "")).strip()
    device_name = str(data.get("device_name", "")).strip()
    if not device_id:
        response, status_code = process_morning_scan_request(branch_id, data)
        return jsonify(response), status_code

    job = enqueue_scan_job(
        branch_id=branch_id,
        device_id=device_id,
        device_name=device_name,
        job_type=JOB_TYPE_MORNING_SCAN,
        payload={"barcode": str(data.get("barcode", ""))},
    )
    return jsonify({"ok": True, **job}), 202


def process_morning_scan_request(branch_id, data):
    """
    Record a morning scan.
    Finds or creates a session for (branch, today, sku, color).
    Marks the scanned size as found.
    """
    barcode_raw = data.get("barcode", "")
    barcode = _normalize_catalog_barcode(barcode_raw)

    if not barcode:
        return {"error": "missing_barcode"}, 400

    conn = get_connection()
    session_data = None
    approval_payload = None
    catalog_created = False
    try:
        _clear_stale_morning_sessions(conn, branch_id, _today())

        meta, catalog_created = resolve_barcode_catalog_entry(
            conn,
            barcode,
            autocreate_structured=True,
        )

        if not meta:
            return {
                "error": "not_found",
                "barcode_received": str(barcode_raw),
                "barcode_normalized": barcode
            }, 404

        sku, color, size = meta["sku"], meta["color"], meta["size"]
        _resolve_missing_floor_item(conn, branch_id, sku, color, size)
        session_data = _upsert_session(conn, branch_id, sku, color, size)
        session_data["location_hint"] = _location_for_sku(conn, branch_id, sku)
        approval_payload = _auto_approve_completed_session(conn, branch_id, session_data)

        conn.commit()
    finally:
        conn.close()

    if approval_payload:
        emit_update(branch_id, "tab1_approved", approval_payload)
        return {
            "ok": True,
            "approved": True,
            "approval": approval_payload,
            "catalog_created": bool(catalog_created),
        }, 200

    emit_update(branch_id, "tab1_update", session_data)
    return {
        "ok": True,
        "session": session_data,
        "approved": False,
        "catalog_created": bool(catalog_created),
    }, 200


@missing_floor_bp.route("/sessions/tick", methods=["POST"])
@require_branch
def tick_size(branch_id):
    """Manually tick/untick a size in a morning session."""
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id")
    size = normalize_size_label(data.get("size", ""))
    found = bool(data.get("found", True))

    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM morning_sessions WHERE id=? AND branch_id=?",
        (session_id, branch_id)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not_found"}), 404

    sizes_found = set(_sizes_list(row["sizes_found"]))
    if found:
        sizes_found.add(size)
        _resolve_missing_floor_item(conn, branch_id, row["sku"], row["color"], size)
    else:
        sizes_found.discard(size)

    stored_sizes = _sizes_list(row["sizes_all"])
    all_sizes = _get_session_catalog_sizes(
        conn,
        row["sku"],
        row["color"],
        extra_sizes=[*stored_sizes, *sizes_found],
    )
    ordered_found = _order_found_sizes(all_sizes, sizes_found)
    all_sizes_str = _sizes_str(all_sizes)
    new_found = _sizes_str(ordered_found)
    conn.execute(
        "UPDATE morning_sessions SET sizes_found=?, sizes_all=? WHERE id=?",
        (new_found, all_sizes_str, session_id)
    )
    session_data = {
        **dict(row),
        "sizes_all": all_sizes,
        "sizes_found": ordered_found,
        "location_hint": _location_for_sku(conn, branch_id, row["sku"]),
    }
    approval_payload = _auto_approve_completed_session(conn, branch_id, session_data, row["session_date"])
    conn.commit()
    conn.close()

    if approval_payload:
        emit_update(branch_id, "tab1_approved", approval_payload)
        return jsonify({"ok": True, "approved": True, "approval": approval_payload})

    emit_update(branch_id, "tab1_update", session_data)
    return jsonify({"ok": True, "approved": False, "session": session_data})


@missing_floor_bp.route("/sessions/<int:session_id>/approve", methods=["POST"])
@require_branch
def approve_session(branch_id, session_id):
    """
    Approve a morning session:
    - sizes NOT found → inserted into missing_floor
    - session marked approved
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM morning_sessions WHERE id=? AND branch_id=?",
        (session_id, branch_id)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not_found"}), 404

    session_data = _normalize_session_row(conn, row, persist=True)
    approval_payload = _approve_session_data(conn, branch_id, session_data, row["session_date"])
    conn.commit()
    conn.close()

    emit_update(branch_id, "tab1_approved", approval_payload)
    return jsonify({
        "ok": True,
        "missing_sizes": approval_payload["missing_sizes"],
        "location_hint": approval_payload["location_hint"],
    })


@missing_floor_bp.route("/manual", methods=["POST"])
@require_branch
def add_manual_missing(branch_id):
    data = request.get_json(silent=True) or {}
    sku = str(data.get("sku", "")).strip()
    color = str(data.get("color", "")).strip()
    size = normalize_size_label(data.get("size", ""))

    if not all([sku, color, size]):
        return jsonify({"error": "missing_fields"}), 400

    conn = get_connection()
    _clear_stale_morning_sessions(conn, branch_id, _today())
    session_data = _upsert_manual_session(conn, branch_id, sku, color, size)
    session_data["location_hint"] = _location_for_sku(conn, branch_id, sku)
    conn.commit()
    conn.close()

    emit_update(branch_id, "tab1_update", session_data)
    return jsonify({"ok": True, "session": session_data}), 201


@missing_floor_bp.route("/sessions/clear", methods=["POST"])
@require_branch
def clear_sessions(branch_id):
    """Clear all morning sessions for the branch (manual reset)."""
    conn = get_connection()
    conn.execute(
        "DELETE FROM morning_sessions WHERE branch_id=?",
        (branch_id,)
    )
    conn.commit()
    conn.close()
    emit_update(branch_id, "tab1_cleared", {})
    return jsonify({"ok": True})


# ── Missing floor results ─────────────────────────────────────

@missing_floor_bp.route("", methods=["GET"])
@require_branch
def list_missing(branch_id):
    conn = get_connection()
    rows = conn.execute(
        """SELECT mf.*, wl.location AS location_hint
           FROM missing_floor mf
           LEFT JOIN warehouse_locations wl
             ON wl.branch_id = mf.branch_id AND wl.sku = mf.sku
           WHERE mf.branch_id=? AND mf.status='missing'
           ORDER BY mf.sku, mf.color, mf.size""",
        (branch_id,)
    ).fetchall()
    conn.close()

    result = []
    for r in rows:
        item = dict(r)
        item["size"] = normalize_size_label(item.get("size", ""))
        item["location_hint"] = r["location_hint"] or ""
        result.append(item)
    return jsonify(result)


@missing_floor_bp.route("/<int:item_id>/resolve", methods=["POST"])
@require_branch
def resolve(branch_id, item_id):
    conn = get_connection()
    conn.execute(
        """UPDATE missing_floor SET status='resolved', resolved_at=datetime('now','localtime')
           WHERE id=? AND branch_id=?""",
        (item_id, branch_id)
    )
    conn.commit()
    changed = conn.total_changes
    conn.close()
    if not changed:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"ok": True})


@missing_floor_bp.route("/clear", methods=["POST"])
@require_branch
def clear_missing(branch_id):
    conn = get_connection()
    conn.execute("DELETE FROM missing_floor WHERE branch_id=?", (branch_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Internal helper ───────────────────────────────────────────

def _upsert_session(conn, branch_id, sku, color, size):
    size = normalize_size_label(size)
    today = _today()
    row = conn.execute(
        """SELECT * FROM morning_sessions
           WHERE branch_id=? AND sku=? AND color=? AND approved=0
           ORDER BY created_at DESC, id DESC
           LIMIT 1""",
        (branch_id, sku, color)
    ).fetchone()

    stored_sizes = _sizes_list(row["sizes_all"]) if row else []
    found_sizes = _sizes_list(row["sizes_found"]) if row else []
    all_sizes_list = _get_session_catalog_sizes(
        conn,
        sku,
        color,
        extra_sizes=[*stored_sizes, *found_sizes, size],
    )
    all_sizes = _sizes_str(all_sizes_list)

    if row:
        found = set(_sizes_list(row["sizes_found"]))
        found.add(size)
        ordered_found = _order_found_sizes(all_sizes_list, found)
        new_found = _sizes_str(ordered_found)
        conn.execute(
            "UPDATE morning_sessions SET sizes_found=?, sizes_all=? WHERE id=?",
            (new_found, all_sizes, row["id"])
        )
        return {
            "id": row["id"], "sku": sku, "color": color,
            "sizes_all": all_sizes_list,
            "sizes_found": ordered_found,
            "approved": 0
        }
    else:
        ordered_found = _order_found_sizes(all_sizes_list, [size])
        cur = conn.execute(
            """INSERT INTO morning_sessions (branch_id,session_date,sku,color,sizes_all,sizes_found)
               VALUES (?,?,?,?,?,?)""",
            (branch_id, today, sku, color, all_sizes, _sizes_str(ordered_found))
        )
        return {
            "id": cur.lastrowid, "sku": sku, "color": color,
            "sizes_all": all_sizes_list,
            "sizes_found": ordered_found,
            "approved": 0
        }


def _upsert_manual_session(conn, branch_id, sku, color, size):
    size = normalize_size_label(size)
    today = _today()
    row = conn.execute(
        """SELECT * FROM morning_sessions
           WHERE branch_id=? AND sku=? AND color=? AND approved=0
           ORDER BY created_at DESC, id DESC
           LIMIT 1""",
        (branch_id, sku, color)
    ).fetchone()

    stored_sizes = _sizes_list(row["sizes_all"]) if row else []
    found_sizes = _sizes_list(row["sizes_found"]) if row else []
    all_sizes_list = _get_session_catalog_sizes(
        conn,
        sku,
        color,
        extra_sizes=[*stored_sizes, *found_sizes, size],
    )
    all_sizes_str = _sizes_str(all_sizes_list)

    if row:
        ordered_found = _order_found_sizes(all_sizes_list, _sizes_list(row["sizes_found"]))
        conn.execute(
            "UPDATE morning_sessions SET sizes_all=?, sizes_found=? WHERE id=?",
            (all_sizes_str, _sizes_str(ordered_found), row["id"])
        )
        return {
            "id": row["id"], "sku": sku, "color": color,
            "sizes_all": all_sizes_list,
            "sizes_found": ordered_found,
            "approved": 0
        }

    cur = conn.execute(
        """INSERT INTO morning_sessions (branch_id,session_date,sku,color,sizes_all,sizes_found)
           VALUES (?,?,?,?,?,?)""",
        (branch_id, today, sku, color, all_sizes_str, "")
    )
    return {
        "id": cur.lastrowid, "sku": sku, "color": color,
        "sizes_all": all_sizes_list,
        "sizes_found": [],
        "approved": 0
    }
