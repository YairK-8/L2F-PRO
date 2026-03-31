"""app.py — L2F v4 with Super Admin"""
import os, secrets, time
from datetime import timedelta
from flask import Flask, g, request, send_from_directory
from database.db import init_db
from backend.realtime import (
    ensure_realtime_background_tasks,
    record_error_event,
    record_request_metric,
    socketio,
)
from backend.auth import auth_bp
from backend.admin import admin_bp
from backend.barcodes import barcodes_bp
from backend.missing_floor import missing_floor_bp
from backend.missing_warehouse import missing_warehouse_bp
from backend.locations import locations_bp

app = Flask(__name__, static_folder="static", template_folder="templates")

_env_secret = os.environ.get("SECRET_KEY", "").strip()
app.secret_key = _env_secret if _env_secret else secrets.token_hex(32)

app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)

for bp in [auth_bp, admin_bp, barcodes_bp, missing_floor_bp,
           missing_warehouse_bp, locations_bp]:
    app.register_blueprint(bp)

socketio.init_app(app)
ensure_realtime_background_tasks()


@app.before_request
def _mark_request_start():
    g._request_started_at = time.perf_counter()


@app.after_request
def _capture_request_metrics(response):
    started = getattr(g, "_request_started_at", None)
    if started is not None:
        duration_ms = (time.perf_counter() - started) * 1000
        record_request_metric(request.path, response.status_code, duration_ms)
    return response


@app.teardown_request
def _capture_request_errors(exc):
    if exc is not None:
        record_error_event("request_exception", f"{request.path}: {exc}", "http")


@app.route("/")
def index():
    return send_from_directory("templates", "index.html")


@app.route("/admin")
def admin_panel():
    return send_from_directory("templates", "admin.html")


@app.route("/health")
def health():
    return {"status": "ok", "version": "4.0"}


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    print(f"\n🚀 L2F v4 → http://localhost:{port}")
    print(f"   Admin panel → http://localhost:{port}/admin\n")
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
