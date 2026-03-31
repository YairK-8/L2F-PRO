"""
backend/admin.py
Super-Admin API:
  POST /api/admin/login          — admin login
  POST /api/admin/logout         — admin logout
  GET  /api/admin/me             — check session
  GET  /api/admin/branches       — list all branches
  POST /api/admin/branches       — create branch
  PUT  /api/admin/branches/<id>  — edit branch (name, store_id)
  POST /api/admin/branches/<id>/reset-password
  DELETE /api/admin/branches/<id> — hard delete with full cascade
  POST /api/admin/setup          — first-time admin account creation (only if no admin exists)
"""
from flask import Blueprint, request, jsonify, session
from database.db import get_connection
from backend.auth_utils import require_admin
from backend.realtime import (
    disconnect_branch_devices,
    disconnect_single_device,
    get_active_device_counts,
    get_branch_active_devices,
    get_health_snapshot,
    get_total_active_devices,
    get_total_active_sockets,
)
from werkzeug.security import generate_password_hash, check_password_hash

admin_bp = Blueprint("admin", __name__, url_prefix="/api/admin")


# ── Session check ─────────────────────────────────────────────

@admin_bp.route("/me", methods=["GET"])
def admin_me():
    if not session.get("is_admin"):
        return jsonify({"error": "not_logged_in"}), 401
    return jsonify({
        "ok": True,
        "admin_id": session.get("admin_id"),
        "username": session.get("admin_username"),
    })


# ── First-time setup ─────────────────────────────────────────

@admin_bp.route("/setup-check", methods=["GET"])
def setup_check():
    """Returns whether an admin account exists yet. Used by frontend to decide which screen to show."""
    conn = get_connection()
    exists = conn.execute("SELECT id FROM admins LIMIT 1").fetchone()
    conn.close()
    return jsonify({"needs_setup": not bool(exists)})


@admin_bp.route("/setup", methods=["POST"])
def setup():
    """
    Create the first admin account.
    Only works if NO admin exists yet — completely locked after first use.
    """
    conn = get_connection()
    existing = conn.execute("SELECT id FROM admins LIMIT 1").fetchone()
    if existing:
        conn.close()
        return jsonify({"error": "admin_already_exists"}), 409

    data     = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    pw       = str(data.get("password", "")).strip()

    if not username or len(pw) < 6:
        conn.close()
        return jsonify({"error": "invalid_input"}), 400

    conn.execute(
        "INSERT INTO admins (username, password) VALUES (?,?)",
        (username, generate_password_hash(pw))
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True}), 201


# ── Login / Logout ────────────────────────────────────────────

@admin_bp.route("/login", methods=["POST"])
def admin_login():
    data     = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    pw       = str(data.get("password", "")).strip()

    conn = get_connection()
    row  = conn.execute(
        "SELECT id, username, password FROM admins WHERE username=?", (username,)
    ).fetchone()
    conn.close()

    if not row or not check_password_hash(row["password"], pw):
        return jsonify({"error": "invalid_credentials"}), 401

    # Clear any branch session and set admin session
    session.clear()
    session.permanent = True
    session["is_admin"]       = True
    session["admin_id"]       = row["id"]
    session["admin_username"] = row["username"]
    return jsonify({"ok": True, "username": row["username"]})


@admin_bp.route("/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return jsonify({"ok": True})


# ── Branch management ─────────────────────────────────────────

@admin_bp.route("/branches", methods=["GET"])
@require_admin
def list_branches():
    """List all branches with stats."""
    q    = request.args.get("q", "").strip().lower()
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, name, store_id, created_at, last_login, is_blocked FROM branches ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    active_counts = get_active_device_counts()

    branches = []
    for r in rows:
        b = dict(r)
        # Apply search filter server-side
        if q and q not in b["name"].lower() \
              and q not in (b["store_id"] or "").lower():
            continue
        b.pop("password", None)  # never expose hash
        b["active_devices"] = active_counts.get(b["id"], 0)
        branches.append(b)
    return jsonify(branches)


@admin_bp.route("/overview", methods=["GET"])
@require_admin
def get_overview():
    conn = get_connection()
    total_branches = conn.execute("SELECT COUNT(*) FROM branches").fetchone()[0]
    conn.close()
    health = get_health_snapshot()
    return jsonify({
        "total_branches": total_branches,
        "active_devices": get_total_active_devices(),
        "active_sockets": get_total_active_sockets(),
        "connected_branches": len(get_active_device_counts()),
        "health_score": health["health_score"],
        "health_status": health["health_status"],
        "avg_request_ms": health["avg_request_ms"],
        "scans_last_window": health["scans_last_window"],
        "errors_last_window": health["errors_last_window"],
    })


@admin_bp.route("/system-health", methods=["GET"])
@require_admin
def get_system_health():
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, name, store_id FROM branches ORDER BY name"
    ).fetchall()
    conn.close()
    branch_map = {row["id"]: dict(row) for row in rows}
    health = get_health_snapshot()
    for branch in health["branches"]:
        meta = branch_map.get(branch["branch_id"], {})
        branch["name"] = meta.get("name", f"סניף {branch['branch_id']}")
        branch["store_id"] = meta.get("store_id") or ""
    return jsonify(health)


@admin_bp.route("/branches/<int:branch_id>/devices", methods=["GET"])
@require_admin
def get_branch_devices(branch_id):
    conn = get_connection()
    blocked_rows = conn.execute(
        "SELECT id, device_id, device_name, blocked_at FROM blocked_devices WHERE branch_id=? ORDER BY blocked_at DESC",
        (branch_id,)
    ).fetchall()
    conn.close()
    active = get_branch_active_devices(branch_id)
    return jsonify({
        "branch_id": branch_id,
        "active_devices": active["active_devices"],
        "active_sockets": active["active_sockets"],
        "devices": active["devices"],
        "blocked_devices": [dict(row) for row in blocked_rows],
    })


@admin_bp.route("/branches/<int:branch_id>/disconnect-all", methods=["POST"])
@require_admin
def admin_disconnect_all_devices(branch_id):
    disconnected = disconnect_branch_devices(branch_id)
    return jsonify({"ok": True, "disconnected": disconnected})


@admin_bp.route("/branches/<int:branch_id>/block", methods=["POST"])
@require_admin
def admin_block_branch(branch_id):
    conn = get_connection()
    conn.execute("UPDATE branches SET is_blocked=1 WHERE id=?", (branch_id,))
    conn.commit()
    changed = conn.total_changes
    row = conn.execute("SELECT id, name, is_blocked FROM branches WHERE id=?", (branch_id,)).fetchone()
    conn.close()
    if not changed or not row:
        return jsonify({"error": "not_found"}), 404
    disconnected = disconnect_branch_devices(branch_id, "branch_blocked")
    return jsonify({"ok": True, "branch": dict(row), "disconnected": disconnected})


@admin_bp.route("/branches/<int:branch_id>/unblock", methods=["POST"])
@require_admin
def admin_unblock_branch(branch_id):
    conn = get_connection()
    conn.execute("UPDATE branches SET is_blocked=0 WHERE id=?", (branch_id,))
    conn.commit()
    changed = conn.total_changes
    row = conn.execute("SELECT id, name, is_blocked FROM branches WHERE id=?", (branch_id,)).fetchone()
    conn.close()
    if not changed or not row:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"ok": True, "branch": dict(row)})


@admin_bp.route("/branches/<int:branch_id>/devices/<path:device_id>/disconnect", methods=["POST"])
@require_admin
def admin_disconnect_device(branch_id, device_id):
    disconnected = disconnect_single_device(branch_id, device_id)
    if not disconnected:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"ok": True, "disconnected": disconnected})


@admin_bp.route("/branches/<int:branch_id>/devices/<path:device_id>/block", methods=["POST"])
@require_admin
def admin_block_device(branch_id, device_id):
    data = request.get_json(silent=True) or {}
    device_name = str(data.get("device_name", "")).strip()
    conn = get_connection()
    conn.execute(
        """INSERT INTO blocked_devices (branch_id, device_id, device_name)
           VALUES (?,?,?)
           ON CONFLICT(branch_id, device_id) DO UPDATE
           SET device_name=excluded.device_name, blocked_at=datetime('now','localtime')""",
        (branch_id, device_id, device_name)
    )
    conn.commit()
    row = conn.execute(
        "SELECT id, device_id, device_name, blocked_at FROM blocked_devices WHERE branch_id=? AND device_id=?",
        (branch_id, device_id)
    ).fetchone()
    conn.close()
    disconnect_single_device(branch_id, device_id, "admin_blocked_device")
    return jsonify({"ok": True, "blocked_device": dict(row) if row else None})


@admin_bp.route("/branches/<int:branch_id>/devices/<path:device_id>/unblock", methods=["POST"])
@require_admin
def admin_unblock_device(branch_id, device_id):
    conn = get_connection()
    conn.execute(
        "DELETE FROM blocked_devices WHERE branch_id=? AND device_id=?",
        (branch_id, device_id)
    )
    conn.commit()
    deleted = conn.total_changes
    conn.close()
    if not deleted:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"ok": True})


@admin_bp.route("/branches/<int:branch_id>/devices/<path:device_id>/delete", methods=["POST"])
@require_admin
def admin_delete_device(branch_id, device_id):
    conn = get_connection()
    conn.execute(
        "DELETE FROM blocked_devices WHERE branch_id=? AND device_id=?",
        (branch_id, device_id)
    )
    conn.commit()
    removed_blocks = conn.total_changes
    conn.close()

    disconnected = disconnect_single_device(branch_id, device_id, "admin_deleted_device")
    if not disconnected and not removed_blocks:
        return jsonify({"error": "not_found"}), 404

    return jsonify({
        "ok": True,
        "deleted": True,
        "disconnected": disconnected,
        "removed_blocks": removed_blocks,
    })


@admin_bp.route("/admins", methods=["GET"])
@require_admin
def list_admins():
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, username, created_at FROM admins ORDER BY created_at, id"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@admin_bp.route("/admins/<int:admin_id>", methods=["PUT"])
@require_admin
def update_admin(admin_id):
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    if not username:
        return jsonify({"error": "username_required"}), 400

    conn = get_connection()
    try:
        conn.execute("UPDATE admins SET username=? WHERE id=?", (username, admin_id))
        conn.commit()
    except Exception:
        conn.close()
        return jsonify({"error": "username_taken"}), 409

    row = conn.execute(
        "SELECT id, username, created_at FROM admins WHERE id=?",
        (admin_id,)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not_found"}), 404

    if session.get("admin_id") == admin_id:
        session["admin_username"] = row["username"]
    return jsonify({"ok": True, "admin": dict(row)})


@admin_bp.route("/admins/<int:admin_id>/reset-password", methods=["POST"])
@require_admin
def update_admin_password(admin_id):
    data = request.get_json(silent=True) or {}
    pw = str(data.get("password", "")).strip()
    if len(pw) < 6:
        return jsonify({"error": "password_too_short"}), 400

    conn = get_connection()
    conn.execute(
        "UPDATE admins SET password=? WHERE id=?",
        (generate_password_hash(pw), admin_id)
    )
    conn.commit()
    changed = conn.total_changes
    conn.close()
    if not changed:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"ok": True})


@admin_bp.route("/branches", methods=["POST"])
@require_admin
def create_branch():
    """Create a new branch."""
    data     = request.get_json(silent=True) or {}
    name     = str(data.get("name", "")).strip()
    store_id = str(data.get("store_id", "")).strip()
    pw       = str(data.get("password", "")).strip()

    if not name or not store_id or not pw:
        return jsonify({"error": "missing_fields"}), 400
    if len(pw) < 4:
        return jsonify({"error": "password_too_short"}), 400

    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO branches (name, store_id, password) VALUES (?,?,?)",
            (name, store_id, generate_password_hash(pw))
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, name, store_id, created_at FROM branches WHERE name=?", (name,)
        ).fetchone()
    except Exception:
        conn.close()
        return jsonify({"error": "name_taken"}), 409
    conn.close()
    return jsonify({"ok": True, "branch": dict(row)}), 201


@admin_bp.route("/branches/<int:branch_id>", methods=["PUT"])
@require_admin
def edit_branch(branch_id):
    """Edit branch name and/or store_id."""
    data     = request.get_json(silent=True) or {}
    name     = str(data.get("name", "")).strip()
    store_id = str(data.get("store_id", "")).strip()

    if not name:
        return jsonify({"error": "name_required"}), 400
    if not store_id:
        return jsonify({"error": "store_id_required"}), 400

    conn = get_connection()
    try:
        conn.execute(
            "UPDATE branches SET name=?, store_id=? WHERE id=?",
            (name, store_id, branch_id)
        )
        conn.commit()
    except Exception:
        conn.close()
        return jsonify({"error": "name_taken"}), 409
    if conn.total_changes == 0:
        conn.close()
        return jsonify({"error": "not_found"}), 404
    conn.close()
    return jsonify({"ok": True})


@admin_bp.route("/branches/<int:branch_id>/reset-password", methods=["POST"])
@require_admin
def reset_password(branch_id):
    """Set a new password for a branch. Old password stops working immediately."""
    data = request.get_json(silent=True) or {}
    pw   = str(data.get("password", "")).strip()
    if len(pw) < 4:
        return jsonify({"error": "password_too_short"}), 400

    conn = get_connection()
    conn.execute(
        "UPDATE branches SET password=? WHERE id=?",
        (generate_password_hash(pw), branch_id)
    )
    conn.commit()
    changed = conn.total_changes
    conn.close()
    if not changed:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"ok": True})


@admin_bp.route("/branches/<int:branch_id>", methods=["DELETE"])
@require_admin
def delete_branch(branch_id):
    """
    Hard delete: removes the branch and ALL related data (cascade).
    Requires confirmation token in body: { "confirm": "<branch_name>" }
    """
    data    = request.get_json(silent=True) or {}
    confirm = str(data.get("confirm", "")).strip()

    conn = get_connection()
    row  = conn.execute(
        "SELECT name FROM branches WHERE id=?", (branch_id,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not_found"}), 404

    if confirm != row["name"]:
        conn.close()
        return jsonify({"error": "confirmation_mismatch"}), 400

    # Full cascade delete — order matters (foreign keys)
    tables = [
        "blocked_devices",
        "morning_sessions",
        "missing_floor",
        "missing_warehouse",
        "warehouse_locations",
    ]
    for table in tables:
        conn.execute(f"DELETE FROM {table} WHERE branch_id=?", (branch_id,))

    conn.execute("DELETE FROM branches WHERE id=?", (branch_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "deleted": row["name"]})


# ── Branch detail (for future expansion) ─────────────────────

@admin_bp.route("/branches/<int:branch_id>", methods=["GET"])
@require_admin
def get_branch(branch_id):
    """Get a single branch with basic stats."""
    conn = get_connection()
    row  = conn.execute(
        "SELECT id, name, store_id, created_at, last_login FROM branches WHERE id=?",
        (branch_id,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not_found"}), 404

    b = dict(row)

    # Count related data
    b["stats"] = {
        "missing_floor":     conn.execute("SELECT COUNT(*) FROM missing_floor WHERE branch_id=? AND status='missing'", (branch_id,)).fetchone()[0],
        "missing_warehouse": conn.execute("SELECT COUNT(*) FROM missing_warehouse WHERE branch_id=? AND status='pending'", (branch_id,)).fetchone()[0],
        "locations":         conn.execute("SELECT COUNT(*) FROM warehouse_locations WHERE branch_id=?", (branch_id,)).fetchone()[0],
    }
    conn.close()
    return jsonify(b)


# ══════════════════════════════════════════════════════════════
# BRANCH DATA — Admin view/edit (all endpoints require admin)
# ══════════════════════════════════════════════════════════════

# ── Tab 1: Missing Floor ──────────────────────────────────────

@admin_bp.route("/branches/<int:branch_id>/missing-floor", methods=["GET"])
@require_admin
def admin_get_missing_floor(branch_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM missing_floor WHERE branch_id=? AND status='missing' ORDER BY sku,color,size",
        (branch_id,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@admin_bp.route("/branches/<int:branch_id>/missing-floor/<int:item_id>/resolve", methods=["POST"])
@require_admin
def admin_resolve_floor(branch_id, item_id):
    conn = get_connection()
    conn.execute(
        "UPDATE missing_floor SET status='resolved', resolved_at=datetime('now','localtime') WHERE id=? AND branch_id=?",
        (item_id, branch_id)
    )
    conn.commit()
    changed = conn.total_changes
    conn.close()
    if not changed:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"ok": True})

@admin_bp.route("/branches/<int:branch_id>/missing-floor", methods=["POST"])
@require_admin
def admin_add_missing_floor(branch_id):
    data  = request.get_json(silent=True) or {}
    sku   = str(data.get("sku","")).strip()
    color = str(data.get("color","")).strip()
    size  = str(data.get("size","")).strip().lower()
    if not all([sku, color, size]):
        return jsonify({"error": "missing_fields"}), 400
    conn = get_connection()
    conn.execute(
        "INSERT INTO missing_floor (branch_id,sku,color,size) VALUES (?,?,?,?)",
        (branch_id, sku, color, size)
    )
    conn.commit(); conn.close()
    return jsonify({"ok": True}), 201

@admin_bp.route("/branches/<int:branch_id>/missing-floor/clear", methods=["POST"])
@require_admin
def admin_clear_missing_floor(branch_id):
    conn = get_connection()
    conn.execute("DELETE FROM missing_floor WHERE branch_id=?", (branch_id,))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

# ── Tab 2: Missing Warehouse ──────────────────────────────────

@admin_bp.route("/branches/<int:branch_id>/missing-warehouse", methods=["GET"])
@require_admin
def admin_get_missing_wh(branch_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM missing_warehouse WHERE branch_id=? AND status='pending' ORDER BY scanned_at ASC",
        (branch_id,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@admin_bp.route("/branches/<int:branch_id>/missing-warehouse/<int:item_id>/restock", methods=["POST"])
@require_admin
def admin_restock_wh(branch_id, item_id):
    conn = get_connection()
    conn.execute(
        "UPDATE missing_warehouse SET status='restocked', restocked_at=datetime('now','localtime') WHERE id=? AND branch_id=?",
        (item_id, branch_id)
    )
    conn.commit()
    changed = conn.total_changes
    conn.close()
    if not changed:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"ok": True})

@admin_bp.route("/branches/<int:branch_id>/missing-warehouse", methods=["POST"])
@require_admin
def admin_add_missing_wh(branch_id):
    data  = request.get_json(silent=True) or {}
    sku   = str(data.get("sku","")).strip()
    color = str(data.get("color","")).strip()
    size  = str(data.get("size","")).strip().lower()
    if not all([sku, color, size]):
        return jsonify({"error": "missing_fields"}), 400
    conn = get_connection()
    conn.execute(
        "INSERT INTO missing_warehouse (branch_id,sku,color,size) VALUES (?,?,?,?)",
        (branch_id, sku, color, size)
    )
    conn.commit(); conn.close()
    return jsonify({"ok": True}), 201

@admin_bp.route("/branches/<int:branch_id>/missing-warehouse/clear", methods=["POST"])
@require_admin
def admin_clear_missing_wh(branch_id):
    conn = get_connection()
    conn.execute("DELETE FROM missing_warehouse WHERE branch_id=? AND status='pending'", (branch_id,))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

# ── Tab 3: Locations ──────────────────────────────────────────

@admin_bp.route("/branches/<int:branch_id>/locations", methods=["GET"])
@require_admin
def admin_get_locations(branch_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM warehouse_locations WHERE branch_id=? ORDER BY sku",
        (branch_id,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@admin_bp.route("/branches/<int:branch_id>/locations", methods=["POST"])
@require_admin
def admin_upsert_location(branch_id):
    data = request.get_json(silent=True) or {}
    sku  = str(data.get("sku","")).strip()
    loc  = str(data.get("location","")).strip()
    if not sku or not loc:
        return jsonify({"error": "missing_fields"}), 400
    conn = get_connection()
    conn.execute(
        """INSERT INTO warehouse_locations (branch_id,sku,location,updated_at)
           VALUES (?,?,?,datetime('now','localtime'))
           ON CONFLICT(branch_id,sku) DO UPDATE
           SET location=excluded.location, updated_at=excluded.updated_at""",
        (branch_id, sku, loc)
    )
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@admin_bp.route("/branches/<int:branch_id>/locations/<sku>", methods=["DELETE"])
@require_admin
def admin_delete_location(branch_id, sku):
    conn = get_connection()
    conn.execute(
        "DELETE FROM warehouse_locations WHERE branch_id=? AND sku=?",
        (branch_id, sku)
    )
    conn.commit()
    deleted = conn.total_changes
    conn.close()
    if not deleted:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"ok": True})

# ── Tab 4: Morning Sessions ───────────────────────────────────

@admin_bp.route("/branches/<int:branch_id>/sessions", methods=["GET"])
@require_admin
def admin_get_sessions(branch_id):
    from datetime import date
    today = str(date.today())
    conn  = get_connection()
    rows  = conn.execute(
        "SELECT * FROM morning_sessions WHERE branch_id=? AND session_date=? ORDER BY created_at",
        (branch_id, today)
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["sizes_all"]   = [x for x in d["sizes_all"].split(",")   if x]
        d["sizes_found"] = [x for x in d["sizes_found"].split(",") if x]
        result.append(d)
    return jsonify(result)
