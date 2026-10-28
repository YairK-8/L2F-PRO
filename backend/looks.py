"""Deterministic, read-only outfit recommendations from the existing catalog."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from flask import Blueprint, jsonify, request

from backend.auth_utils import require_branch_or_admin
from database.db import get_connection


looks_bp = Blueprint("looks", __name__, url_prefix="/api/looks")


def _enabled():
    return os.environ.get("LOOK_BUILDER_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _text(value):
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


CATEGORY_TERMS = {
    "dress": ("שמל", "אוברול", "חליפ", "סרפן", "jumpsuit", "dress"),
    "bottom": (
        "מכנס", "ג'ינס", "גינס", "חצאית", "שורט", "ברמודה", "טייץ",
        "pants", "jeans", "skirt", "shorts",
    ),
    "outerwear": (
        "ג'קט", "גקט", "ז'קט", "קרדיגן", "עליונית", "בלייזר", "מעיל",
        "jacket", "cardigan", "blazer", "coat",
    ),
    "accessory": (
        "תיק", "חגורה", "כובע", "צעיף", "נעל", "סנדל", "מגף",
        "bag", "belt", "hat", "scarf", "shoe", "boot",
    ),
    "top": (
        "חולצ", "טי-שרט", "טי שרט", "טי-שירט", "טי שירט", "טישרט", "טישירט",
        "סוודר", "סריג", "פולו", "גופי", "טוניקה", "טופ", "סטרפלס", "מכופתרת",
        "קפוצ'ון", "סווטשירט", "סווטשרט", "shirt", "sweater", "top",
        "hoodie", "sweatshirt",
    ),
}

STYLE_TERMS = {
    "work": ("מחויט", "מכופתר", "בלייזר", "קלאסי", "פיקה", "צווארון"),
    "evening": ("סאטן", "תחרה", "קטיפה", "פאייט", "מבריק", "ערב"),
    "casual": ("ג'ינס", "גינס", "סריג", "טי", "סלאב", "קרופ", "פשתן", "בייסיק"),
}

NEUTRAL_COLORS = (
    "שחור", "לבן", "שמנת", "אבן", "בז", "ניוד", "אפור", "מוקה", "חום",
    "black", "white", "cream", "beige", "grey", "gray", "brown",
)

COLOR_FAMILIES = {
    "black": ("שחור", "black"),
    "white": ("לבן", "שמנת", "אבן", "ניוד", "בז", "white", "cream", "beige"),
    "gray": ("אפור", "כסוף", "gray", "grey", "silver"),
    "brown": ("חום", "מוקה", "קאמל", "כאמל", "נוגט", "brown", "camel"),
    "blue": ("כחול", "תכלת", "נייבי", "ג'ינס", "גינס", "blue", "navy", "denim"),
    "green": ("ירוק", "זית", "חאקי", "מנטה", "green", "olive", "khaki"),
    "red": ("אדום", "בורדו", "חמרה", "red", "burgundy"),
    "pink": ("ורוד", "אפרסק", "pink", "peach"),
    "purple": ("סגול", "לילך", "purple", "lilac"),
    "yellow": ("צהוב", "חרדל", "כתום", "ליים", "yellow", "orange"),
}

COMPLEMENTARY = {
    frozenset(("blue", "brown")),
    frozenset(("blue", "yellow")),
    frozenset(("green", "brown")),
    frozenset(("green", "pink")),
    frozenset(("red", "blue")),
    frozenset(("purple", "yellow")),
}

SLOT_PLAN = {
    "top": ("bottom", "outerwear"),
    "bottom": ("top", "outerwear"),
    "outerwear": ("top", "bottom"),
    "dress": ("outerwear", "accessory"),
    "accessory": ("top", "bottom"),
}

CATEGORY_LABELS = {
    "top": "חלק עליון",
    "bottom": "חלק תחתון",
    "outerwear": "שכבה עליונה",
    "dress": "שמלה / אוברול",
    "accessory": "אביזר",
    "unknown": "פריט",
}


def classify_category(title):
    normalized = _text(title)
    for category in ("dress", "bottom", "outerwear", "accessory", "top"):
        if any(term in normalized for term in CATEGORY_TERMS[category]):
            return category
    return "unknown"


def classify_styles(title):
    normalized = _text(title)
    return {
        style for style, terms in STYLE_TERMS.items()
        if any(term in normalized for term in terms)
    } or {"casual"}


def color_family(color):
    normalized = _text(color)
    for family, terms in COLOR_FAMILIES.items():
        if any(term in normalized for term in terms):
            return family
    return "other"


def color_score(left, right):
    left_text, right_text = _text(left), _text(right)
    left_family, right_family = color_family(left), color_family(right)
    if left_family == right_family and left_family != "other":
        return 24, "צבעוניות אחידה"
    if any(term in left_text for term in NEUTRAL_COLORS) or any(
        term in right_text for term in NEUTRAL_COLORS
    ):
        return 22, "שילוב עם גוון ניטרלי"
    if frozenset((left_family, right_family)) in COMPLEMENTARY:
        return 20, "צבעים משלימים"
    if "other" not in (left_family, right_family):
        return 13, "ניגוד צבעים מאוזן"
    return 9, "התאמת צבע בסיסית"


@dataclass(frozen=True)
class CatalogItem:
    sku: str
    color: str
    title: str
    image_url: str
    product_url: str
    barcode: str = ""

    @property
    def category(self):
        return classify_category(self.title)

    @property
    def styles(self):
        return classify_styles(self.title)

    def public(self, *, compatibility_score=None, reason=""):
        data = {
            "sku": self.sku,
            "color": self.color,
            "title": self.title,
            "image_url": self.image_url,
            "product_url": self.product_url,
            "barcode": self.barcode,
            "category": self.category,
            "category_label": CATEGORY_LABELS.get(self.category, "פריט"),
        }
        if compatibility_score is not None:
            data["compatibility_score"] = compatibility_score
        if reason:
            data["reason"] = reason
        return data


def _catalog_item(row):
    return CatalogItem(
        sku=str(row["sku"] or "").strip(),
        color=str(row["color"] or "").strip(),
        title=str(row["title"] or "").strip(),
        image_url=str(row["image_path"] or "").strip(),
        product_url=str(row["product_url"] or "").strip(),
        barcode=str(row.get("barcode") or "").strip(),
    )


def score_candidate(anchor, candidate, target_category, occasion):
    if candidate.category != target_category:
        return None
    score = 48
    color_points, color_reason = color_score(anchor.color, candidate.color)
    score += color_points
    reasons = [color_reason]
    requested_style = occasion if occasion in STYLE_TERMS else "casual"
    if requested_style in candidate.styles:
        score += 18
        reasons.append({"casual": "סגנון יומיומי", "work": "מתאים לעבודה", "evening": "מתאים לערב"}[requested_style])
    elif anchor.styles & candidate.styles:
        score += 10
        reasons.append("שפה סגנונית משותפת")
    else:
        score += 4
    if candidate.image_url:
        score += 5
    if candidate.title:
        score += 3
    return min(score, 98), " · ".join(reasons[:2])


def build_ranked_looks(anchor, candidates, occasion="casual", limit=3):
    slots = SLOT_PLAN.get(anchor.category)
    if not slots:
        return []
    ranked_by_slot = []
    for slot in slots:
        ranked = []
        for candidate in candidates:
            if candidate.sku == anchor.sku or candidate.category == "unknown":
                continue
            result = score_candidate(anchor, candidate, slot, occasion)
            if result is None:
                continue
            score, reason = result
            ranked.append((score, candidate.sku, candidate.color, reason, candidate))
        ranked.sort(key=lambda value: (-value[0], value[1], value[2]))
        ranked_by_slot.append(ranked)
    if any(not ranked for ranked in ranked_by_slot):
        # An apparel-only catalog may not contain accessories. A useful two-piece
        # recommendation is better than fabricating or returning a random product.
        ranked_by_slot = [ranked for ranked in ranked_by_slot if ranked]
    if not ranked_by_slot:
        return []

    looks = []
    seen_pairs = set()
    max_depth = max(limit * 4, 12)
    for shift in range(max_depth):
        first = ranked_by_slot[0][shift % len(ranked_by_slot[0])]
        second_pool = (
            [entry for entry in ranked_by_slot[1] if entry[4].sku != first[4].sku]
            if len(ranked_by_slot) > 1 else []
        )
        second = second_pool[(shift * 2) % len(second_pool)] if second_pool else None
        pair = (first[4].sku, second[4].sku if second else "")
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        selected_entries = [first] + ([second] if second else [])
        item_scores = tuple(entry[0] for entry in selected_entries)
        overall = max(70, min(96, round(sum(item_scores) / len(item_scores))))
        alternatives = []
        for slot_index, selected in enumerate(selected_entries):
            pool = ranked_by_slot[slot_index]
            options = [
                entry[4].public(compatibility_score=entry[0], reason=entry[3])
                for entry in pool
                if entry[4].sku != selected[4].sku
            ][:5]
            alternatives.append(options)
        looks.append({
            "score": overall,
            "reason": "; ".join(entry[3] for entry in selected_entries),
            "items": [
                {**anchor.public(), "fixed": True, "alternatives": []},
            ] + [
                {**entry[4].public(compatibility_score=entry[0], reason=entry[3]), "fixed": False, "alternatives": alternatives[index]}
                for index, entry in enumerate(selected_entries)
            ],
        })
        if len(looks) >= limit:
            break
    return looks


def _feature_guard():
    if _enabled():
        return None
    return jsonify({"error": "feature_disabled"}), 404


@looks_bp.route("/config", methods=["GET"])
@require_branch_or_admin
def config(branch_id):
    return jsonify({"enabled": _enabled(), "engine": "deterministic-v2-store-display"})


@looks_bp.route("/search", methods=["GET"])
@require_branch_or_admin
def search(branch_id):
    blocked = _feature_guard()
    if blocked:
        return blocked
    query = _text(request.args.get("q"))
    if len(query) < 2:
        return jsonify({"items": []})
    like = f"%{query}%"
    exact = query
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT pi.sku, pi.color, pi.title, pi.image_path, pi.product_url,
                      MIN(COALESCE(b.barcode,'')) AS barcode
                 FROM product_images pi
                 LEFT JOIN barcodes b
                   ON b.sku=pi.sku AND LOWER(b.color)=LOWER(pi.color)
                WHERE LOWER(pi.sku) LIKE ?
                   OR LOWER(pi.title) LIKE ?
                   OR LOWER(pi.color) LIKE ?
                   OR EXISTS (
                       SELECT 1 FROM barcodes bx
                        WHERE bx.sku=pi.sku
                          AND LOWER(bx.color)=LOWER(pi.color)
                          AND LOWER(bx.barcode) LIKE ?
                   )
                GROUP BY pi.sku, pi.color, pi.title, pi.image_path, pi.product_url, pi.is_primary
                ORDER BY CASE WHEN EXISTS (
                             SELECT 1 FROM barcodes exact_barcode
                              WHERE exact_barcode.sku=pi.sku
                                AND LOWER(exact_barcode.color)=LOWER(pi.color)
                                AND LOWER(exact_barcode.barcode)=?
                         ) THEN 0 ELSE 1 END,
                         CASE WHEN LOWER(pi.sku)=? THEN 0 ELSE 1 END,
                         pi.is_primary DESC, pi.sku, pi.color
                LIMIT 24""",
            (like, like, like, like, exact, exact),
        ).fetchall()
    finally:
        conn.close()
    items = [_catalog_item(row).public() for row in rows]
    return jsonify({"items": items})


@looks_bp.route("/recommend", methods=["GET"])
@require_branch_or_admin
def recommend(branch_id):
    blocked = _feature_guard()
    if blocked:
        return blocked
    sku = str(request.args.get("sku") or "").strip()
    color = str(request.args.get("color") or "").strip()
    occasion = str(request.args.get("occasion") or "casual").strip().lower()
    if not sku:
        return jsonify({"error": "missing_sku"}), 400
    conn = get_connection()
    try:
        anchor_row = conn.execute(
            """SELECT pi.sku, pi.color, pi.title, pi.image_path, pi.product_url,
                      MIN(COALESCE(b.barcode,'')) AS barcode
                 FROM product_images pi
                 LEFT JOIN barcodes b
                   ON b.sku=pi.sku AND LOWER(b.color)=LOWER(pi.color)
                WHERE pi.sku=? AND (?='' OR LOWER(pi.color)=LOWER(?))
                GROUP BY pi.sku, pi.color, pi.title, pi.image_path, pi.product_url, pi.is_primary
                ORDER BY CASE WHEN LOWER(pi.color)=LOWER(?) THEN 0 ELSE 1 END,
                         pi.is_primary DESC
                LIMIT 1""",
            (sku, color, color, color),
        ).fetchone()
        if not anchor_row:
            return jsonify({"error": "product_not_found"}), 404
        department_rows = conn.execute(
            "SELECT department FROM product_departments WHERE sku=? ORDER BY department",
            (sku,),
        ).fetchall()
        departments = [row["department"] for row in department_rows]
        if not departments:
            return jsonify({
                "anchor": _catalog_item(anchor_row).public(),
                "occasion": occasion,
                "engine": "deterministic-v2-store-display",
                "looks": [],
                "message": "לא ניתן לזהות בבטחה אם הפריט שייך למחלקת גברים או נשים.",
            })
        if branch_id is None:
            return jsonify({"error": "branch_required"}), 400
        department_placeholders = ",".join("?" for _ in departments)
        candidate_rows = conn.execute(
            f"""SELECT pi.sku, pi.color, pi.title, pi.image_path, pi.product_url,
                      MIN(COALESCE(b.barcode,'')) AS barcode
                 FROM product_images pi
                 JOIN store_display_items display
                   ON display.branch_id=? AND display.sku=pi.sku
                  AND LOWER(display.color)=LOWER(pi.color)
                 LEFT JOIN barcodes b
                   ON b.sku=pi.sku AND LOWER(b.color)=LOWER(pi.color)
                WHERE pi.sku<>? AND pi.title<>'' AND pi.image_path<>''
                  AND EXISTS (
                      SELECT 1 FROM product_departments pd
                       WHERE pd.sku=pi.sku
                         AND pd.department IN ({department_placeholders})
                  )
                GROUP BY pi.sku, pi.color, pi.title, pi.image_path, pi.product_url, pi.is_primary
                ORDER BY pi.sku, pi.color""",
            (branch_id, sku, *departments),
        ).fetchall()
    finally:
        conn.close()
    anchor = _catalog_item(anchor_row)
    candidates = [_catalog_item(row) for row in candidate_rows]
    looks = build_ranked_looks(anchor, candidates, occasion=occasion, limit=3)
    return jsonify({
        "anchor": anchor.public(),
        "occasion": occasion,
        "engine": "deterministic-v2-store-display",
        "departments": departments,
        "store_display_only": True,
        "looks": looks,
        "message": "" if looks else "אין כרגע מספיק פריטים מתאימים שנסרקו בחנות הבוקר.",
    })
