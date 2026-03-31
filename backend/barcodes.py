"""
backend/barcodes.py
Global barcode catalog (shared across all branches).
Includes filtered import: only import SKUs that exist in branch's warehouse_locations.

Barcode normalization:
- trim whitespace
- uppercase
- if barcode starts with 'E', remove the leading E

This allows scanned barcodes like E12345 to match stored barcodes like 12345.

Permissions:
- GET  /api/barcodes             → require_branch_or_admin
- GET  /api/barcodes/colors      → require_branch_or_admin
- GET  /api/barcodes/sizes       → require_branch_or_admin
- GET  /api/barcodes/<barcode>   → require_branch_or_admin
- POST /api/barcodes             → require_branch_or_admin
- PUT  /api/barcodes/<barcode>   → require_admin   (admin only)
- DELETE /api/barcodes/<barcode> → require_admin   (admin only)
- GET  /api/barcodes/export/csv  → require_admin
- POST /api/barcodes/import      → require_branch  (filtered by branch locations)
- POST /api/barcodes/import-admin → require_admin
"""

from flask import Blueprint, request, jsonify, Response
import csv
import io

try:
    from database.db import get_connection
except ImportError:
    from ..database.db import get_connection

try:
    from backend.auth_utils import require_branch, require_branch_or_admin, require_admin
except ImportError:
    from .auth_utils import require_branch, require_branch_or_admin, require_admin


barcodes_bp = Blueprint("barcodes", __name__, url_prefix="/api/barcodes")


def normalize_barcode(value):
    """Normalize scanner/input barcode so E123 and 123 are treated as the same barcode."""
    v = str(value or "").strip().upper()
    if v.startswith("E"):
        v = v[1:]
    return v


# ── Read endpoints (require branch login) ────────────────────

@barcodes_bp.route("", methods=["GET"])
@require_branch_or_admin
def list_barcodes(branch_id):
    sku = request.args.get("sku", "").strip()
    conn = get_connection()
    if sku:
        rows = conn.execute(
            "SELECT * FROM barcodes WHERE sku=? ORDER BY sku,color,size", (sku,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM barcodes ORDER BY sku,color,size"
        ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@barcodes_bp.route("/colors", methods=["GET"])
@require_branch_or_admin
def colors_for_sku(branch_id):
    """Return distinct colors for a given SKU. Used for autocomplete."""
    sku = request.args.get("sku", "").strip()
    if not sku:
        return jsonify([])
    conn = get_connection()
    rows = conn.execute(
        "SELECT DISTINCT color FROM barcodes WHERE sku=? ORDER BY color", (sku,)
    ).fetchall()
    conn.close()
    return jsonify([r["color"] for r in rows])


@barcodes_bp.route("/sizes", methods=["GET"])
@require_branch_or_admin
def sizes_for_sku_color(branch_id):
    """Return distinct sizes for a given SKU+color."""
    sku = request.args.get("sku", "").strip()
    color = request.args.get("color", "").strip()
    if not sku or not color:
        return jsonify([])
    conn = get_connection()
    rows = conn.execute(
        "SELECT DISTINCT size FROM barcodes WHERE sku=? AND color=? ORDER BY size",
        (sku, color),
    ).fetchall()
    conn.close()
    return jsonify([r["size"] for r in rows])


@barcodes_bp.route("/<barcode>", methods=["GET"])
@require_branch_or_admin
def get_barcode(branch_id, barcode):
    barcode = normalize_barcode(barcode)
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM barcodes WHERE barcode=?",
        (barcode,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not_found"}), 404
    return jsonify(dict(row))


# ── Branch write: add single barcode ─────────────────────────

@barcodes_bp.route("", methods=["POST"])
@require_branch_or_admin
def add_barcode(branch_id):
    """Branch or admin can add a single barcode manually."""
    data = request.get_json(silent=True) or {}
    barcode = normalize_barcode(data.get("barcode", ""))
    sku = str(data.get("sku", "")).strip()
    color = str(data.get("color", "")).strip()
    size = str(data.get("size", "")).strip().lower()
    if not all([barcode, sku, color, size]):
        return jsonify({"error": "missing_fields"}), 400

    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO barcodes (barcode,sku,color,size) VALUES (?,?,?,?)",
            (barcode, sku, color, size),
        )
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 409
    conn.close()
    return jsonify({"ok": True}), 201


# ── Admin-only write endpoints ────────────────────────────────

@barcodes_bp.route("/<barcode>", methods=["PUT"])
@require_admin
def update_barcode(barcode):
    """Update an existing barcode entry. Admin only."""
    barcode = normalize_barcode(barcode)
    data = request.get_json(silent=True) or {}
    sku = str(data.get("sku", "")).strip()
    color = str(data.get("color", "")).strip()
    size = str(data.get("size", "")).strip().lower()
    if not all([sku, color, size]):
        return jsonify({"error": "missing_fields"}), 400

    conn = get_connection()
    conn.execute(
        "UPDATE barcodes SET sku=?, color=?, size=? WHERE barcode=?",
        (sku, color, size, barcode),
    )
    conn.commit()
    changed = conn.total_changes
    conn.close()
    if not changed:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"ok": True})


@barcodes_bp.route("/<barcode>", methods=["DELETE"])
@require_admin
def delete_barcode(barcode):
    """Delete a barcode. Admin only."""
    barcode = normalize_barcode(barcode)
    conn = get_connection()
    conn.execute("DELETE FROM barcodes WHERE barcode=?", (barcode,))
    conn.commit()
    deleted = conn.total_changes
    conn.close()
    if not deleted:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"ok": True})


@barcodes_bp.route("/export/csv", methods=["GET"])
@require_admin
def export_csv():
    """Export full catalog as CSV. Admin only."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT barcode,sku,color,size FROM barcodes ORDER BY sku,color,size"
    ).fetchall()
    conn.close()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["barcode", "sku", "color", "size"])
    for r in rows:
        w.writerow([r["barcode"], r["sku"], r["color"], r["size"]])

    return Response(
        "\uFEFF" + buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=barcodes.csv"},
    )


# ── Import endpoints ──────────────────────────────────────────

@barcodes_bp.route("/import", methods=["POST"])
@require_branch
def import_csv(branch_id):
    """
    Import CSV filtered by branch's warehouse_locations.
    Only SKUs that exist in the branch's locations are imported.
    Body: multipart file upload.
    """
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no_file"}), 400

    conn = get_connection()

    loc_rows = conn.execute(
        "SELECT DISTINCT sku FROM warehouse_locations WHERE branch_id=?",
        (branch_id,),
    ).fetchall()
    allowed_skus = {r["sku"] for r in loc_rows}

    text = f.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    inserted = skipped_no_location = skipped_error = 0

    for row in reader:
        barcode = normalize_barcode(row.get("barcode", ""))
        sku = str(row.get("sku", "")).strip()
        color = str(row.get("color", "")).strip()
        size = str(row.get("size", "")).strip().lower()

        if not all([barcode, sku, color, size]):
            skipped_error += 1
            continue

        if sku not in allowed_skus:
            skipped_no_location += 1
            continue

        try:
            conn.execute(
                "INSERT OR REPLACE INTO barcodes (barcode,sku,color,size) VALUES (?,?,?,?)",
                (barcode, sku, color, size),
            )
            inserted += 1
        except Exception:
            skipped_error += 1

    conn.commit()
    conn.close()
    return jsonify(
        {
            "inserted": inserted,
            "skipped_no_location": skipped_no_location,
            "skipped_error": skipped_error,
        }
    )


@barcodes_bp.route("/import-admin", methods=["POST"])
@require_admin
def import_csv_admin():
    """Import full CSV without location filter. Admin only."""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no_file"}), 400

    text = f.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    conn = get_connection()
    inserted = skipped_error = 0

    for row in reader:
        barcode = normalize_barcode(row.get("barcode", ""))
        sku = str(row.get("sku", "")).strip()
        color = str(row.get("color", "")).strip()
        size = str(row.get("size", "")).strip().lower()

        if not all([barcode, sku, color, size]):
            skipped_error += 1
            continue

        try:
            conn.execute(
                "INSERT OR REPLACE INTO barcodes (barcode,sku,color,size) VALUES (?,?,?,?)",
                (barcode, sku, color, size),
            )
            inserted += 1
        except Exception:
            skipped_error += 1

    conn.commit()
    conn.close()
    return jsonify({"inserted": inserted, "skipped_error": skipped_error})
