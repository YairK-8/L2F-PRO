from flask import Blueprint, request, jsonify
from database.db import get_connection
from backend.auth_utils import require_branch
from backend.realtime import emit_update
from datetime import date

missing_floor_bp = Blueprint("missing_floor", __name__, url_prefix="/api/missing-floor")


def _today():
    return str(date.today())


def _sizes_list(s: str) -> list:
    return [x for x in s.split(",") if x] if s else []


def _sizes_str(lst: list) -> str:
    return ",".join(sorted(set(lst)))


def _normalize_barcode(value: str) -> str:
    value = str(value or "").strip().upper()
    if value.startswith("E"):
        value = value[1:]
    return value


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
    conn.execute(
        """DELETE FROM morning_sessions
           WHERE branch_id=?
             AND session_date < ?""",
        (branch_id, current_session_date)
    )


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


# ── Morning sessions ──────────────────────────────────────────

@missing_floor_bp.route("/sessions", methods=["GET"])
@require_branch
def get_sessions(branch_id):
    """Return all open (unapproved) morning sessions for today, with location hint."""
    conn = get_connection()
    _clear_stale_morning_sessions(conn, branch_id, _today())
    conn.commit()
    rows = conn.execute(
        """SELECT ms.*, wl.location AS location_hint
           FROM morning_sessions ms
           LEFT JOIN warehouse_locations wl
             ON wl.branch_id = ms.branch_id AND wl.sku = ms.sku
           WHERE ms.branch_id=? AND ms.session_date=? AND ms.approved=0
           ORDER BY ms.created_at""",
        (branch_id, _today())
    ).fetchall()
    conn.close()

    result = []
    for r in rows:
        result.append({
            **dict(r),
            "sizes_all": _sizes_list(r["sizes_all"]),
            "sizes_found": _sizes_list(r["sizes_found"]),
            "location_hint": r["location_hint"] or "",
        })
    return jsonify(result)


@missing_floor_bp.route("/scan", methods=["POST"])
@require_branch
def scan(branch_id):
    """
    Record a morning scan.
    Finds or creates a session for (branch, today, sku, color).
    Marks the scanned size as found.
    """
    data = request.get_json(silent=True) or {}
    barcode_raw = data.get("barcode", "")
    barcode = _normalize_barcode(barcode_raw)

    if not barcode:
        return jsonify({"error": "missing_barcode"}), 400

    conn = get_connection()
    _clear_stale_morning_sessions(conn, branch_id, _today())

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
    _resolve_missing_floor_item(conn, branch_id, sku, color, size)
    session_data = _upsert_session(conn, branch_id, sku, color, size)
    session_data["location_hint"] = _location_for_sku(conn, branch_id, sku)

    conn.commit()
    conn.close()

    emit_update(branch_id, "tab1_update", session_data)
    return jsonify({"ok": True, "session": session_data})


@missing_floor_bp.route("/sessions/tick", methods=["POST"])
@require_branch
def tick_size(branch_id):
    """Manually tick/untick a size in a morning session."""
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id")
    size = str(data.get("size", "")).strip()
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

    new_found = _sizes_str(list(sizes_found))
    conn.execute(
        "UPDATE morning_sessions SET sizes_found=? WHERE id=?",
        (new_found, session_id)
    )
    conn.commit()

    session_data = {
        **dict(row),
        "sizes_all": _sizes_list(row["sizes_all"]),
        "sizes_found": _sizes_list(new_found),
        "location_hint": _location_for_sku(conn, branch_id, row["sku"]),
    }
    conn.close()

    emit_update(branch_id, "tab1_update", session_data)
    return jsonify({"ok": True, "session": session_data})


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

    sizes_all = set(_sizes_list(row["sizes_all"]))
    sizes_found = set(_sizes_list(row["sizes_found"]))
    missing = sizes_all - sizes_found
    location_hint = _location_for_sku(conn, branch_id, row["sku"])
    _clear_stale_manual_missing(conn, branch_id, row["session_date"])

    for size in sizes_found:
        _resolve_missing_floor_item(conn, branch_id, row["sku"], row["color"], size)

    for size in missing:
        exists = conn.execute(
            """SELECT id FROM missing_floor
               WHERE branch_id=? AND sku=? AND color=? AND size=? AND status='missing'""",
            (branch_id, row["sku"], row["color"], size)
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO missing_floor (branch_id,sku,color,size) VALUES (?,?,?,?)",
                (branch_id, row["sku"], row["color"], size)
            )

    conn.execute(
        "UPDATE morning_sessions SET approved=1 WHERE id=?", (session_id,)
    )
    conn.commit()
    conn.close()

    emit_update(branch_id, "tab1_approved", {
        "session_id": session_id,
        "sku": row["sku"],
        "color": row["color"],
        "missing_sizes": list(missing),
        "location_hint": location_hint,
    })
    return jsonify({
        "ok": True,
        "missing_sizes": list(missing),
        "location_hint": location_hint,
    })


@missing_floor_bp.route("/manual", methods=["POST"])
@require_branch
def add_manual_missing(branch_id):
    data = request.get_json(silent=True) or {}
    sku = str(data.get("sku", "")).strip()
    color = str(data.get("color", "")).strip()
    size = str(data.get("size", "")).strip().lower()

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
    """Clear all today's morning sessions (manual end-of-day)."""
    conn = get_connection()
    conn.execute(
        "DELETE FROM morning_sessions WHERE branch_id=? AND session_date=?",
        (branch_id, _today())
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
    today = _today()
    row = conn.execute(
        """SELECT * FROM morning_sessions
           WHERE branch_id=? AND session_date=? AND sku=? AND color=?""",
        (branch_id, today, sku, color)
    ).fetchone()

    cat_sizes = conn.execute(
        "SELECT DISTINCT size FROM barcodes WHERE sku=? AND color=? ORDER BY size",
        (sku, color)
    ).fetchall()
    all_sizes = _sizes_str([r["size"] for r in cat_sizes])

    if row:
        found = set(_sizes_list(row["sizes_found"]))
        found.add(size)
        new_found = _sizes_str(list(found))
        conn.execute(
            "UPDATE morning_sessions SET sizes_found=?, sizes_all=? WHERE id=?",
            (new_found, all_sizes, row["id"])
        )
        return {
            "id": row["id"], "sku": sku, "color": color,
            "sizes_all": _sizes_list(all_sizes),
            "sizes_found": _sizes_list(new_found),
            "approved": 0
        }
    else:
        cur = conn.execute(
            """INSERT INTO morning_sessions (branch_id,session_date,sku,color,sizes_all,sizes_found)
               VALUES (?,?,?,?,?,?)""",
            (branch_id, today, sku, color, all_sizes, size)
        )
        return {
            "id": cur.lastrowid, "sku": sku, "color": color,
            "sizes_all": _sizes_list(all_sizes),
            "sizes_found": [size],
            "approved": 0
        }


def _upsert_manual_session(conn, branch_id, sku, color, size):
    today = _today()
    row = conn.execute(
        """SELECT * FROM morning_sessions
           WHERE branch_id=? AND session_date=? AND sku=? AND color=?""",
        (branch_id, today, sku, color)
    ).fetchone()

    cat_sizes = conn.execute(
        "SELECT DISTINCT size FROM barcodes WHERE sku=? AND color=? ORDER BY size",
        (sku, color)
    ).fetchall()
    all_sizes = set(r["size"] for r in cat_sizes)
    all_sizes.add(size)
    all_sizes_str = _sizes_str(list(all_sizes))

    if row:
        found = set(_sizes_list(row["sizes_found"]))
        conn.execute(
            "UPDATE morning_sessions SET sizes_all=? WHERE id=?",
            (all_sizes_str, row["id"])
        )
        return {
            "id": row["id"], "sku": sku, "color": color,
            "sizes_all": _sizes_list(all_sizes_str),
            "sizes_found": _sizes_list(row["sizes_found"]),
            "approved": 0
        }

    cur = conn.execute(
        """INSERT INTO morning_sessions (branch_id,session_date,sku,color,sizes_all,sizes_found)
           VALUES (?,?,?,?,?,?)""",
        (branch_id, today, sku, color, all_sizes_str, "")
    )
    return {
        "id": cur.lastrowid, "sku": sku, "color": color,
        "sizes_all": _sizes_list(all_sizes_str),
        "sizes_found": [],
        "approved": 0
    }
