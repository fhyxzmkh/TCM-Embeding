"""A supplement must not silently move held-out images back into training."""

import unittest

from tcm_herb.supplement import assert_base_preserved, read_class_table, select_groups


class SupplementContractTests(unittest.TestCase):
    def record(self, ident, source_split, group="shared"):
        return {"id": ident, "path": ident, "label": 8, "source_split": source_split, "group": group}

    def test_nb_overlap_excludes_entire_related_group(self):
        records = [self.record("a", "train"), self.record("b", "valid"), self.record("c", "test")]
        select_groups(records, {"b": [{"split": "val"}]})
        self.assertEqual({record["split"] for record in records}, {"excluded_nb_overlap"})

    def test_source_test_has_priority_over_training_and_validation_duplicates(self):
        records = [self.record("a", "train"), self.record("b", "valid"), self.record("c", "test"), self.record("d", "train", "independent")]
        select_groups(records, {})
        self.assertEqual({record["id"]: record["split"] for record in records},
                         {"a": "excluded_chm_duplicate_group", "b": "excluded_chm_duplicate_group", "c": "chm_test", "d": "train"})

    def test_conflicting_labels_are_quarantined(self):
        records = [self.record("a", "train"), self.record("b", "train")]
        records[1]["label"] = 7
        select_groups(records, {})
        self.assertTrue(all(record["split"] == "excluded_label_conflict" for record in records))

    def test_frozen_nb_labels_and_splits_cannot_change(self):
        base = [
            {"id": f"{label}-{split}", "path": f"{label}/{split}", "label": label, "split": split,
             "group": f"{label}-{split}", "domain": "external" if split == "external" else "web",
             "sha256": str(label), "pixel_sha256": str(label)}
            for label in range(20) for split in ("train", "val", "test", "external")
        ]
        merged = [dict(record) for record in base]
        assert_base_preserved(base, merged)
        merged[0]["split"] = "test"
        with self.assertRaises(ValueError):
            assert_base_preserved(base, merged)


if __name__ == "__main__":
    unittest.main()
