"""
backend/barcodes.py
Global barcode catalog (shared across all branches).
Includes filtered import: only import SKUs that exist in branch's warehouse_locations.

Barcode normalization:
- trim whitespace
- uppercase
- if barcode starts with 'E' and is not a structured new-format barcode, remove the leading E

Structured new-format barcode:
- 1 letter prefix
- 5 digits SKU
- 4 digits color code
- 2 digits size code

Permissions:
- GET    /api/barcodes                    -> require_branch_or_admin
- GET    /api/barcodes/colors             -> require_branch_or_admin
- GET    /api/barcodes/sizes              -> require_branch_or_admin
- GET    /api/barcodes/scale              -> require_branch_or_admin
- GET    /api/barcodes/<barcode>          -> require_branch_or_admin
- POST   /api/barcodes                    -> require_branch_or_admin
- PUT    /api/barcodes/<barcode>          -> require_admin
- DELETE /api/barcodes/<barcode>          -> require_admin
- POST   /api/barcodes/scale/colors       -> require_admin
- PUT    /api/barcodes/scale/colors/<code> -> require_admin
- DELETE /api/barcodes/scale/colors/<code> -> require_admin
- POST   /api/barcodes/scale/sizes        -> require_admin
- PUT    /api/barcodes/scale/sizes/<code> -> require_admin
- DELETE /api/barcodes/scale/sizes/<code> -> require_admin
- GET    /api/barcodes/export/csv         -> require_admin
- POST   /api/barcodes/import             -> require_branch
- POST   /api/barcodes/import-admin       -> require_admin
"""

import csv
import io
import re
import sqlite3

from flask import Blueprint, Response, jsonify, request

from backend.auth_utils import require_admin, require_branch, require_branch_or_admin
from database.db import get_connection


barcodes_bp = Blueprint("barcodes", __name__, url_prefix="/api/barcodes")

STRUCTURED_BARCODE_RE = re.compile(r"^([A-Z])(\d{5})(\d{4})(\d{2})$")


def parse_structured_barcode(value):
    normalized = str(value or "").strip().upper().replace(" ", "")
    match = STRUCTURED_BARCODE_RE.fullmatch(normalized)
    if not match:
        return None
    return {
        "prefix": match.group(1),
        "barcode": normalized,
        "sku": match.group(2),
        "color_code": match.group(3),
        "size_code": match.group(4),
    }


def normalize_barcode(value):
    """Normalize scanner/input barcode while preserving new structured barcodes."""
    v = str(value or "").strip().upper()
    if v.startswith("E") and not parse_structured_barcode(v):
        v = v[1:]
    return v


def normalize_scale_code(value, expected_length):
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits or len(digits) > expected_length:
        return ""
    padded = digits.zfill(expected_length)
    return padded if len(padded) == expected_length else ""


def fetch_barcode_scale(conn):
    colors = conn.execute(
        "SELECT code, color FROM barcode_color_scale ORDER BY code"
    ).fetchall()
    sizes = conn.execute(
        "SELECT code, size FROM barcode_size_scale ORDER BY code"
    ).fetchall()
    return {
        "colors": [dict(r) for r in colors],
        "sizes": [dict(r) for r in sizes],
    }


def learn_structured_scale_mappings(conn, barcode, color="", size=""):
    parsed = parse_structured_barcode(barcode)
    if not parsed:
        return {"color_added": False, "size_added": False}

    learned = {"color_added": False, "size_added": False}
    color_value = str(color or "").strip()
    size_value = str(size or "").strip().lower()

    if color_value:
        before = conn.total_changes
        conn.execute(
            """
            INSERT OR IGNORE INTO barcode_color_scale (code, color, updated_at)
            VALUES (?, ?, datetime('now','localtime'))
            """,
            (parsed["color_code"], color_value),
        )
        learned["color_added"] = conn.total_changes > before

    if size_value:
        before = conn.total_changes
        conn.execute(
            """
            INSERT OR IGNORE INTO barcode_size_scale (code, size, updated_at)
            VALUES (?, ?, datetime('now','localtime'))
            """,
            (parsed["size_code"], size_value),
        )
        learned["size_added"] = conn.total_changes > before

    return learned


# Read endpoints

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


@barcodes_bp.route("/scale", methods=["GET"])
@require_branch_or_admin
def get_barcode_scale(branch_id):
    conn = get_connection()
    data = fetch_barcode_scale(conn)
    conn.close()
    return jsonify(data)


@barcodes_bp.route("/scale/colors", methods=["POST"])
@require_admin
def create_barcode_color_scale_entry():
    data = request.get_json(silent=True) or {}
    code = normalize_scale_code(data.get("code", ""), 4)
    color = str(data.get("color", "")).strip()
    if not code or not color:
        return jsonify({"error": "missing_fields"}), 400

    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO barcode_color_scale (code, color, updated_at)
            VALUES (?, ?, datetime('now','localtime'))
            """,
            (code, color),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "code_exists"}), 409
    conn.close()
    return jsonify({"ok": True, "code": code, "color": color}), 201


@barcodes_bp.route("/scale/colors/<code>", methods=["PUT"])
@require_admin
def update_barcode_color_scale_entry(code):
    original_code = normalize_scale_code(code, 4)
    data = request.get_json(silent=True) or {}
    next_code = normalize_scale_code(data.get("code", original_code), 4)
    color = str(data.get("color", "")).strip()
    if not original_code or not next_code or not color:
        return jsonify({"error": "missing_fields"}), 400

    conn = get_connection()
    existing = conn.execute(
        "SELECT code FROM barcode_color_scale WHERE code=?",
        (original_code,),
    ).fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "not_found"}), 404

    try:
        conn.execute(
            """
            UPDATE barcode_color_scale
               SET code=?, color=?, updated_at=datetime('now','localtime')
             WHERE code=?
            """,
            (next_code, color, original_code),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "code_exists"}), 409
    conn.close()
    return jsonify({"ok": True, "code": next_code, "color": color})


@barcodes_bp.route("/scale/colors/<code>", methods=["DELETE"])
@require_admin
def delete_barcode_color_scale_entry(code):
    normalized_code = normalize_scale_code(code, 4)
    if not normalized_code:
        return jsonify({"error": "invalid_code"}), 400

    conn = get_connection()
    conn.execute("DELETE FROM barcode_color_scale WHERE code=?", (normalized_code,))
    conn.commit()
    deleted = conn.total_changes
    conn.close()
    if not deleted:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"ok": True})


@barcodes_bp.route("/scale/sizes", methods=["POST"])
@require_admin
def create_barcode_size_scale_entry():
    data = request.get_json(silent=True) or {}
    code = normalize_scale_code(data.get("code", ""), 2)
    size = str(data.get("size", "")).strip().lower()
    if not code or not size:
        return jsonify({"error": "missing_fields"}), 400

    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO barcode_size_scale (code, size, updated_at)
            VALUES (?, ?, datetime('now','localtime'))
            """,
            (code, size),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "code_exists"}), 409
    conn.close()
    return jsonify({"ok": True, "code": code, "size": size}), 201


@barcodes_bp.route("/scale/sizes/<code>", methods=["PUT"])
@require_admin
def update_barcode_size_scale_entry(code):
    original_code = normalize_scale_code(code, 2)
    data = request.get_json(silent=True) or {}
    next_code = normalize_scale_code(data.get("code", original_code), 2)
    size = str(data.get("size", "")).strip().lower()
    if not original_code or not next_code or not size:
        return jsonify({"error": "missing_fields"}), 400

    conn = get_connection()
    existing = conn.execute(
        "SELECT code FROM barcode_size_scale WHERE code=?",
        (original_code,),
    ).fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "not_found"}), 404

    try:
        conn.execute(
            """
            UPDATE barcode_size_scale
               SET code=?, size=?, updated_at=datetime('now','localtime')
             WHERE code=?
            """,
            (next_code, size, original_code),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "code_exists"}), 409
    conn.close()
    return jsonify({"ok": True, "code": next_code, "size": size})


@barcodes_bp.route("/scale/sizes/<code>", methods=["DELETE"])
@require_admin
def delete_barcode_size_scale_entry(code):
    normalized_code = normalize_scale_code(code, 2)
    if not normalized_code:
        return jsonify({"error": "invalid_code"}), 400

    conn = get_connection()
    conn.execute("DELETE FROM barcode_size_scale WHERE code=?", (normalized_code,))
    conn.commit()
    deleted = conn.total_changes
    conn.close()
    if not deleted:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"ok": True})


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


# Branch write: add single barcode

@barcodes_bp.route("", methods=["POST"])
@require_branch_or_admin
def add_barcode(branch_id):
    data = request.get_json(silent=True) or {}
    barcode = normalize_barcode(data.get("barcode", ""))
    sku = str(data.get("sku", "")).strip()
    color = str(data.get("color", "")).strip()
    size = str(data.get("size", "")).strip().lower()
    if not all([barcode, sku, color, size]):
        return jsonify({"error": "missing_fields"}), 400

    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT 1 FROM barcodes WHERE barcode=?",
            (barcode,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE barcodes SET sku=?, color=?, size=? WHERE barcode=?",
                (sku, color, size, barcode),
            )
            action = "updated"
        else:
            conn.execute(
                "INSERT INTO barcodes (barcode,sku,color,size) VALUES (?,?,?,?)",
                (barcode, sku, color, size),
            )
            action = "created"
        scale_updates = learn_structured_scale_mappings(conn, barcode, color=color, size=size)
        conn.commit()
    except Exception as exc:
        conn.close()
        return jsonify({"error": str(exc)}), 409
    conn.close()
    return (
        jsonify({"ok": True, "action": action, "scale_updates": scale_updates}),
        201 if action == "created" else 200,
    )


# Admin-only write endpoints

@barcodes_bp.route("/<barcode>", methods=["PUT"])
@require_admin
def update_barcode(barcode):
    barcode = normalize_barcode(barcode)
    data = request.get_json(silent=True) or {}
    sku = str(data.get("sku", "")).strip()
    color = str(data.get("color", "")).strip()
    size = str(data.get("size", "")).strip().lower()
    if not all([sku, color, size]):
        return jsonify({"error": "missing_fields"}), 400

    conn = get_connection()
    existing = conn.execute(
        "SELECT 1 FROM barcodes WHERE barcode=?",
        (barcode,),
    ).fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "not_found"}), 404

    conn.execute(
        "UPDATE barcodes SET sku=?, color=?, size=? WHERE barcode=?",
        (sku, color, size, barcode),
    )
    scale_updates = learn_structured_scale_mappings(conn, barcode, color=color, size=size)
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "scale_updates": scale_updates})


@barcodes_bp.route("/<barcode>", methods=["DELETE"])
@require_admin
def delete_barcode(barcode):
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
    conn = get_connection()
    rows = conn.execute(
        "SELECT barcode,sku,color,size FROM barcodes ORDER BY sku,color,size"
    ).fetchall()
    conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["barcode", "sku", "color", "size"])
    for row in rows:
        writer.writerow([row["barcode"], row["sku"], row["color"], row["size"]])

    return Response(
        "\uFEFF" + buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=barcodes.csv"},
    )


# Import endpoints

@barcodes_bp.route("/import", methods=["POST"])
@require_branch
def import_csv(branch_id):
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
            learn_structured_scale_mappings(conn, barcode, color=color, size=size)
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
            learn_structured_scale_mappings(conn, barcode, color=color, size=size)
            inserted += 1
        except Exception:
            skipped_error += 1

    conn.commit()
    conn.close()
    return jsonify({"inserted": inserted, "skipped_error": skipped_error})
