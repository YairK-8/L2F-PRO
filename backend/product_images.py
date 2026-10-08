"""Shared product-image lookup helpers."""


def _normalize(value):
    return str(value or "").strip().casefold()


def _public_image(row):
    if not row:
        return None
    return {
        "image_url": row["image_path"],
        "product_url": row["product_url"] or "",
        "image_title": row["title"] or "",
        "image_color": row["color"] or "",
    }


def attach_product_images(conn, items):
    """Attach one exact-color image, or a model-level primary fallback, per item."""
    rows = [dict(item) for item in (items or [])]
    skus = sorted({str(item.get("sku") or "").strip() for item in rows if item.get("sku")})
    if not skus:
        return rows

    placeholders = ",".join("?" for _ in skus)
    image_rows = conn.execute(
        f"""SELECT sku, color_code, color, title, image_path, product_url,
                   source_image_url, is_primary
            FROM product_images
            WHERE sku IN ({placeholders})
            ORDER BY sku, is_primary DESC, color_code""",
        tuple(skus),
    ).fetchall()

    by_sku = {}
    for image_row in image_rows:
        by_sku.setdefault(str(image_row["sku"]), []).append(image_row)

    for item in rows:
        candidates = by_sku.get(str(item.get("sku") or "").strip(), [])
        requested_color = _normalize(item.get("color"))
        exact = next(
            (candidate for candidate in candidates if requested_color and _normalize(candidate["color"]) == requested_color),
            None,
        )
        selected = exact or (candidates[0] if candidates else None)
        item.update(_public_image(selected) or {
            "image_url": "",
            "product_url": "",
            "image_title": "",
            "image_color": "",
        })
        item["image_is_exact"] = bool(exact)
    return rows


def attach_product_image(conn, item):
    rows = attach_product_images(conn, [item])
    return rows[0] if rows else dict(item or {})
