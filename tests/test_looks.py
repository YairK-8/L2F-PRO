import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.looks import (  # noqa: E402
    CatalogItem,
    build_ranked_looks,
    classify_category,
    color_score,
)


class LookEngineTests(unittest.TestCase):
    def test_hebrew_catalog_categories(self):
        self.assertEqual(classify_category("חולצת טי בגזרה רחבה"), "top")
        self.assertEqual(classify_category("גופיית ריב כתפיות דקות"), "top")
        self.assertEqual(classify_category("טי שירט מבד נשפך"), "top")
        self.assertEqual(classify_category("מכנסיים ארוכים מבד נמתח"), "bottom")
        self.assertEqual(classify_category("קרדיגן פסים"), "outerwear")
        self.assertEqual(classify_category("שמלת סאטן"), "dress")
        self.assertEqual(classify_category("חליפה מכופתרת במראה סאטן"), "dress")

    def test_neutral_colors_rank_high(self):
        points, reason = color_score("שחור", "שמנת")
        self.assertGreaterEqual(points, 20)
        self.assertTrue(reason)

    def test_ranked_looks_are_deterministic_and_keep_anchor(self):
        anchor = CatalogItem("100", "שחור", "חולצת טי", "/100.jpg", "")
        candidates = [
            CatalogItem("200", "לבן", "מכנסיים ארוכים", "/200.jpg", ""),
            CatalogItem("201", "כחול", "מכנס ג'ינס", "/201.jpg", ""),
            CatalogItem("300", "אפור", "גקט קלאסי", "/300.jpg", ""),
            CatalogItem("301", "חום", "קרדיגן פסים", "/301.jpg", ""),
        ]
        first = build_ranked_looks(anchor, candidates, "casual")
        second = build_ranked_looks(anchor, list(reversed(candidates)), "casual")
        self.assertEqual(first, second)
        self.assertTrue(first)
        self.assertEqual(first[0]["items"][0]["sku"], anchor.sku)
        self.assertTrue(first[0]["items"][0]["fixed"])
        self.assertEqual(len({item["sku"] for item in first[0]["items"]}), 3)

    def test_dress_can_return_useful_two_piece_look_without_accessories(self):
        anchor = CatalogItem("400", "שחור", "שמלת ערב", "/400.jpg", "")
        candidates = [CatalogItem("401", "לבן", "בלייזר קלאסי", "/401.jpg", "")]
        looks = build_ranked_looks(anchor, candidates, "evening")
        self.assertEqual(len(looks[0]["items"]), 2)


if __name__ == "__main__":
    unittest.main()
