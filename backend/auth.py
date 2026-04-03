"""backend/auth.py â€” Branch login / register / logout"""
from flask import Blueprint, request, jsonify, session
from database.db import get_connection
from backend.realtime import socketio
from werkzeug.security import generate_password_hash, check_password_hash

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")


def _clear_branch_session():
    session.pop("branch_id", None)
    session.pop("branch_name", None)
    session.modified = True


def _set_branch_session(branch_id, branch_name):
    _clear_branch_session()
    session["branch_id"] = branch_id
    session["branch_name"] = branch_name
    session.modified = True


@auth_bp.route("/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    store_id = str(data.get("store_id", "")).strip()
    pw   = str(data.get("password", "")).strip()
    if not name or not store_id or not pw:
        return jsonify({"error": "missing_fields"}), 400
    if len(pw) < 4:
        return jsonify({"error": "password_too_short"}), 400
    hashed = generate_password_hash(pw)
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO branches (name, store_id, password) VALUES (?,?,?)", (name, store_id, hashed)
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, name, store_id FROM branches WHERE name=?", (name,)
        ).fetchone()
        _set_branch_session(row["id"], row["name"])
    except Exception:
        conn.close()
        return jsonify({"error": "name_taken"}), 409
    conn.close()
    return jsonify({"ok": True, "branch": {"id": row["id"], "name": row["name"], "store_id": row["store_id"]}}), 201


@auth_bp.route("/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    pw   = str(data.get("password", "")).strip()
    conn = get_connection()
    row  = conn.execute(
        "SELECT id, name, password, is_blocked FROM branches WHERE name=?", (name,)
    ).fetchone()
    if not row or not check_password_hash(row["password"], pw):
        conn.close()
        return jsonify({"error": "invalid_credentials"}), 401
    if row["is_blocked"]:
        conn.close()
        return jsonify({"error": "branch_blocked"}), 403
    # Record last login timestamp
    conn.execute(
        "UPDATE branches SET last_login=datetime('now','localtime') WHERE id=?",
        (row["id"],)
    )
    conn.commit()
    conn.close()
    _set_branch_session(row["id"], row["name"])
    return jsonify({"ok": True, "branch": {"id": row["id"], "name": row["name"]}})


@auth_bp.route("/logout", methods=["POST"])
def logout():
    _clear_branch_session()
    return jsonify({"ok": True})


@auth_bp.route("/me", methods=["GET"])
def me():
    if "branch_id" not in session:
        return jsonify({"error": "not_logged_in"}), 401
    conn = get_connection()
    row = conn.execute(
        "SELECT name, is_blocked FROM branches WHERE id=?",
        (session["branch_id"],)
    ).fetchone()
    conn.close()
    if not row:
        _clear_branch_session()
        return jsonify({"error": "not_logged_in"}), 401
    if row["is_blocked"]:
        _clear_branch_session()
        return jsonify({"error": "branch_blocked"}), 403
    return jsonify({
        "branch_id":   session["branch_id"],
        "branch_name": row["name"],
    })
