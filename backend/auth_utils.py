"""
backend/auth_utils.py
Decorators for protecting routes.
  - require_branch  : regular branch session
  - require_admin   : super-admin session
"""
from functools import wraps
from flask import session, jsonify, request

from database.db import get_connection


def _check_branch_access(branch_id):
    """Return an API error when a branch is limited to warehouse locations."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT locations_only FROM branches WHERE id=?", (branch_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"error": "not_logged_in"}), 401
    if not row["locations_only"]:
        return None
    location_barcode_lookup = (
        request.method == "GET" and request.endpoint == "barcodes.get_barcode"
    )
    if request.blueprint == "locations" or location_barcode_lookup:
        return None
    return jsonify({"error": "locations_only"}), 403


def require_branch(f):
    """Ensures a branch is logged in. Injects branch_id into kwargs."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "branch_id" not in session:
            return jsonify({"error": "not_logged_in"}), 401
        branch_id = session["branch_id"]
        denied = _check_branch_access(branch_id)
        if denied:
            return denied
        kwargs["branch_id"] = branch_id
        return f(*args, **kwargs)
    return decorated


def require_branch_or_admin(f):
    """Allows either a branch session or an admin session."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("is_admin"):
            kwargs["branch_id"] = None
            return f(*args, **kwargs)
        if "branch_id" not in session:
            return jsonify({"error": "not_logged_in"}), 401
        branch_id = session["branch_id"]
        denied = _check_branch_access(branch_id)
        if denied:
            return denied
        kwargs["branch_id"] = branch_id
        return f(*args, **kwargs)
    return decorated


def require_admin(f):
    """Ensures the super-admin is logged in. Returns 403 otherwise."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin") or not session.get("admin_id"):
            return jsonify({"error": "forbidden"}), 403
        session.permanent = True
        session.modified = True
        return f(*args, **kwargs)
    return decorated
