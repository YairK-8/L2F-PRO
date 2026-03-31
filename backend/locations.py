"""backend/locations.py — Warehouse locations per branch"""
from flask import Blueprint, request, jsonify, Response
from database.db import get_connection
from backend.auth_utils import require_branch
import json

locations_bp = Blueprint("locations", __name__, url_prefix="/api/locations")


@locations_bp.route("", methods=["GET"])
@require_branch
def list_locations(branch_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM warehouse_locations WHERE branch_id=? ORDER BY sku",
        (branch_id,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@locations_bp.route("/search", methods=["GET"])
@require_branch
def search(branch_id):
    sku = request.args.get("sku","").strip()
    if not sku:
        return jsonify({"error": "missing_sku"}), 400
    conn = get_connection()
    row  = conn.execute(
        "SELECT * FROM warehouse_locations WHERE branch_id=? AND sku=?",
        (branch_id, sku)
    ).fetchone()
    conn.close()
    if row:
        return jsonify(dict(row))
    return jsonify({"error": "not_found"}), 404


@locations_bp.route("", methods=["POST"])
@require_branch
def upsert(branch_id):
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
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@locations_bp.route("/<sku>", methods=["DELETE"])
@require_branch
def delete_location(branch_id, sku):
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


@locations_bp.route("/export/json", methods=["GET"])
@require_branch
def export_json(branch_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT sku,location FROM warehouse_locations WHERE branch_id=? ORDER BY sku",
        (branch_id,)
    ).fetchall()
    conn.close()
    return Response(
        json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=locations.json"}
    )


@locations_bp.route("/import/json", methods=["POST"])
@require_branch
def import_json(branch_id):
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no_file"}), 400
    data = json.loads(f.read().decode("utf-8"))
    if not isinstance(data, list):
        return jsonify({"error": "invalid_format"}), 400
    conn = get_connection()
    inserted = skipped = 0
    for row in data:
        sku = str(row.get("sku","")).strip()
        loc = str(row.get("location","") or row.get("loc","")).strip()
        if not sku or not loc:
            skipped += 1; continue
        conn.execute(
            """INSERT INTO warehouse_locations (branch_id,sku,location,updated_at)
               VALUES (?,?,?,datetime('now','localtime'))
               ON CONFLICT(branch_id,sku) DO UPDATE
               SET location=excluded.location, updated_at=excluded.updated_at""",
            (branch_id, sku, loc)
        )
        inserted += 1
    conn.commit()
    conn.close()
    return jsonify({"inserted": inserted, "skipped": skipped})
