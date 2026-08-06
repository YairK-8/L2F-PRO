"""
backend/barcodes.py
Global barcode catalog (shared across all branches).
Includes filtered import: only import SKUs that exist in branch's warehouse_locations.

Barcode normalization:
- remove scanner/control noise
- trim whitespace
- uppercase
- strip common scanner symbology prefixes
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

from flask import Blueprint, Response, jsonify, request

from backend.auth_utils import require_admin, require_branch, require_branch_or_admin
from database.db import IntegrityError, get_connection


barcodes_bp = Blueprint("barcodes", __name__, url_prefix="/api/barcodes")

STRUCTURED_BARCODE_RE = re.compile(r"^([A-Z])(\d{5})(\d{4})(\d{2})$")
STRUCTURED_BARCODE_BODY_RE = re.compile(r"^(\d{5})(\d{4})(\d{2})$")
SCANNER_SYMBOLOGY_PREFIX_RE = re.compile(r"^\][A-Z0-9][0-9]")


def _clean_barcode_input(value):
    v = str(value or "").upper()
    v = v.replace("\u200b", "").replace("\ufeff", "")
    v = re.sub(r"[\x00-\x20]+", "", v)
    while SCANNER_SYMBOLOGY_PREFIX_RE.match(v):
        v = v[3:]
    v = re.sub(r"^[^A-Z0-9]+", "", v)
    v = re.sub(r"[^A-Z0-9]+$", "", v)
    return v


def parse_structured_barcode(value):
    normalized = _clean_barcode_input(value)
    match = STRUCTURED_BARCODE_RE.fullmatch(normalized)
    if not match:
        return None
    return {
        "missing_prefix": False,
        "prefix": match.group(1),
        "barcode": normalized,
        "sku": match.group(2),
        "color_code": match.group(3),
        "size_code": match.group(4),
    }


def parse_structured_barcode_body(value):
    normalized = _clean_barcode_input(value)
    match = STRUCTURED_BARCODE_BODY_RE.fullmatch(normalized)
    if not match:
        return None
    return {
        "missing_prefix": True,
        "prefix": "",
        "barcode": normalized,
        "sku": match.group(1),
        "color_code": match.group(2),
        "size_code": match.group(3),
    }


def normalize_barcode(value):
    """Normalize scanner/input barcode while preserving new structured barcodes."""
    v = _clean_barcode_input(value)
    if v.startswith("E") and not parse_structured_barcode(v):
        v = v[1:]
    return v


def find_barcode_catalog_entry(conn, barcode):
    normalized = normalize_barcode(barcode)
    if not normalized:
        return None, normalized

    row = conn.execute(
        "SELECT * FROM barcodes WHERE barcode=?",
        (normalized,),
    ).fetchone()
    if row:
        return dict(row), normalized

    parsed_without_prefix = parse_structured_barcode_body(normalized)
    if not parsed_without_prefix:
        return None, normalized

    rows = conn.execute(
        """
        SELECT * FROM barcodes
        WHERE LENGTH(barcode)=12
          AND SUBSTR(barcode, 2)=?
        ORDER BY barcode
        """,
        (parsed_without_prefix["barcode"],),
    ).fetchall()
    if len(rows) == 1:
        return dict(rows[0]), rows[0]["barcode"]

    return None, normalized


def normalize_scale_code(value, expected_length):
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits or len(digits) > expected_length:
        return ""
    padded = digits.zfill(expected_length)
    return padded if len(padded) == expected_length else ""


def normalize_size_label(value):
    return str(value or "").strip().lower()


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


def _clean_size_values(values):
    seen = set()
    result = []
    for value in values or []:
        cleaned = normalize_size_label(value)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def get_barcode_scale_sizes(conn):
    rows = conn.execute(
        "SELECT size FROM barcode_size_scale ORDER BY code"
    ).fetchall()
    return _clean_size_values([row["size"] for row in rows])


def order_sizes_by_scale(values, scale_sizes):
    cleaned = _clean_size_values(values)
    if not scale_sizes:
        return cleaned
    order_map = {size: index for index, size in enumerate(scale_sizes)}
    return sorted(cleaned, key=lambda size: (order_map.get(size, len(order_map)), size))


def get_catalog_sizes_for_sku_color(conn, sku, color, include_sizes=None):
    rows = conn.execute(
        "SELECT DISTINCT size FROM barcodes WHERE sku=? AND color=? ORDER BY size",
        (sku, color),
    ).fetchall()
    barcode_sizes = _clean_size_values([row["size"] for row in rows])
    scale_sizes = get_barcode_scale_sizes(conn)

    merged_sizes = list(barcode_sizes)

    if include_sizes is not None:
        if isinstance(include_sizes, (list, tuple, set)):
            merged_sizes.extend(include_sizes)
        else:
            merged_sizes.append(include_sizes)

    return order_sizes_by_scale(merged_sizes, scale_sizes)


def resolve_barcode_catalog_entry(conn, barcode, autocreate_structured=False):
    row, normalized = find_barcode_catalog_entry(conn, barcode)
    if row:
        return row, False

    if not autocreate_structured:
        return None, False

    parsed = parse_structured_barcode(normalized)
    parsed_without_prefix = None
    if not parsed:
        parsed_without_prefix = parse_structured_barcode_body(normalized)
        if not parsed_without_prefix:
            return None, False
        parsed = parsed_without_prefix

    color_row = conn.execute(
        "SELECT color FROM barcode_color_scale WHERE code=?",
        (parsed["color_code"],),
    ).fetchone()
    size_row = conn.execute(
        "SELECT size FROM barcode_size_scale WHERE code=?",
        (parsed["size_code"],),
    ).fetchone()
    if not color_row or not size_row:
        return None, False

    if parsed.get("missing_prefix"):
        return {
            "barcode": normalized,
            "sku": parsed["sku"],
            "color": str(color_row["color"]).strip(),
            "size": normalize_size_label(size_row["size"]),
        }, False

    before = conn.total_changes
    conn.execute(
        """
        INSERT INTO barcodes (barcode, sku, color, size)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(barcode) DO NOTHING
        """,
        (
            parsed["barcode"],
            parsed["sku"],
            str(color_row["color"]).strip(),
            normalize_size_label(size_row["size"]),
        ),
    )
    created = conn.total_changes > before

    row = conn.execute(
        "SELECT * FROM barcodes WHERE barcode=?",
        (parsed["barcode"],),
    ).fetchone()
    if not row:
        return None, created

    return dict(row), created


def learn_structured_scale_mappings(conn, barcode, color="", size=""):
    parsed = parse_structured_barcode(barcode)
    if not parsed:
        return {"color_added": False, "size_added": False}

    learned = {"color_added": False, "size_added": False}
    color_value = str(color or "").strip()
    size_value = normalize_size_label(size)

    if color_value:
        before = conn.total_changes
        conn.execute(
            """
            INSERT INTO barcode_color_scale (code, color, updated_at)
            VALUES (?, ?, datetime('now','localtime'))
            ON CONFLICT(code) DO NOTHING
            """,
            (parsed["color_code"], color_value),
        )
        learned["color_added"] = conn.total_changes > before

    if size_value:
        before = conn.total_changes
        conn.execute(
            """
            INSERT INTO barcode_size_scale (code, size, updated_at)
            VALUES (?, ?, datetime('now','localtime'))
            ON CONFLICT(code) DO NOTHING
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
    rows = get_catalog_sizes_for_sku_color(conn, sku, color)
    conn.close()
    return jsonify(rows)


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
    except IntegrityError:
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
    except IntegrityError:
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
    size = normalize_size_label(data.get("size", ""))
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
    except IntegrityError:
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
    size = normalize_size_label(data.get("size", ""))
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
    except IntegrityError:
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
    conn = get_connection()
    row, _normalized = find_barcode_catalog_entry(conn, barcode)
    conn.close()
    if not row:
        return jsonify({"error": "not_found"}), 404
    return jsonify(row)


# Branch write: add single barcode

@barcodes_bp.route("", methods=["POST"])
@require_branch_or_admin
def add_barcode(branch_id):
    data = request.get_json(silent=True) or {}
    barcode = normalize_barcode(data.get("barcode", ""))
    sku = str(data.get("sku", "")).strip()
    color = str(data.get("color", "")).strip()
    size = normalize_size_label(data.get("size", ""))
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
    size = normalize_size_label(data.get("size", ""))
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
        size = normalize_size_label(row.get("size", ""))

        if not all([barcode, sku, color, size]):
            skipped_error += 1
            continue

        if sku not in allowed_skus:
            skipped_no_location += 1
            continue

        try:
            conn.execute(
                """INSERT INTO barcodes (barcode,sku,color,size)
                   VALUES (?,?,?,?)
                   ON CONFLICT(barcode) DO UPDATE
                   SET sku=excluded.sku, color=excluded.color, size=excluded.size""",
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
        size = normalize_size_label(row.get("size", ""))

        if not all([barcode, sku, color, size]):
            skipped_error += 1
            continue

        try:
            conn.execute(
                """INSERT INTO barcodes (barcode,sku,color,size)
                   VALUES (?,?,?,?)
                   ON CONFLICT(barcode) DO UPDATE
                   SET sku=excluded.sku, color=excluded.color, size=excluded.size""",
                (barcode, sku, color, size),
            )
            learn_structured_scale_mappings(conn, barcode, color=color, size=size)
            inserted += 1
        except Exception:
            skipped_error += 1

    conn.commit()
    conn.close()
    return jsonify({"inserted": inserted, "skipped_error": skipped_error})
