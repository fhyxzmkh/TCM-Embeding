"""Data-contract tests: alias alignment, duplicate leakage and preprocessing."""

import unittest

from PIL import Image

from tcm_herb.data import assign_splits, square_image, validate_manifest
from tcm_herb.labels import label_id


class DataContractTests(unittest.TestCase):
    def test_external_alias_uses_training_label(self):
        self.assertEqual(label_id("doukou"), label_id("Amomi Fructus Rotundus"))
        with self.assertRaises(ValueError):
            label_id("not-a-known-herb")

    def test_duplicate_groups_and_external_priority(self):
        records = []
        for label in range(2):
            for group in range(12):
                for duplicate in range(2):
                    records.append({"label": label, "domain": "web", "group": f"{label}-{group}", "id": f"{label}-{group}-{duplicate}"})
            records.extend([
                {"label": label, "domain": "web", "group": f"external-{label}"},
                {"label": label, "domain": "external", "group": f"external-{label}"},
            ])
        assign_splits(records)
        validate_manifest(records, expected_classes=2)
        self.assertEqual(sum(r["split"] == "excluded_external_overlap" for r in records), 2)
        first = [dict(record) for record in records]
        assign_splits(records)
        self.assertEqual([r["split"] for r in first], [r["split"] for r in records])
        train_group = next(r["group"] for r in records if r["split"] == "train")
        leaked = next(r for r in records if r["group"] == train_group)
        leaked["split"] = "val"
        with self.assertRaises(ValueError):
            validate_manifest(records, expected_classes=2)

    def test_letterbox_preserves_shape_and_color(self):
        canvas = square_image(Image.new("RGB", (24, 12), (200, 20, 10)), size=32)
        self.assertEqual(canvas.size, (32, 32))
        self.assertEqual(canvas.getpixel((0, 0)), (255, 255, 255))
        self.assertEqual(canvas.getpixel((16, 16)), (200, 20, 10))


if __name__ == "__main__":
    unittest.main()
