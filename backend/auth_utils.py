"""
backend/auth_utils.py
Decorators for protecting routes.
  - require_branch  : regular branch session
  - require_admin   : super-admin session
"""
from functools import wraps
from flask import session, jsonify


def require_branch(f):
    """Ensures a branch is logged in. Injects branch_id into kwargs."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "branch_id" not in session:
            return jsonify({"error": "not_logged_in"}), 401
        kwargs["branch_id"] = session["branch_id"]
        return f(*args, **kwargs)
    return decorated


def require_branch_or_admin(f):
    """Allows either a branch session or an admin session."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("is_admin"):
            kwargs["branch_id"] = session.get("branch_id")
            return f(*args, **kwargs)
        if "branch_id" not in session:
            return jsonify({"error": "not_logged_in"}), 401
        kwargs["branch_id"] = session["branch_id"]
        return f(*args, **kwargs)
    return decorated


def require_admin(f):
    """Ensures the super-admin is logged in. Returns 403 otherwise."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            return jsonify({"error": "forbidden"}), 403
        return f(*args, **kwargs)
    return decorated
