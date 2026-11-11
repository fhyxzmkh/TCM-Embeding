"""Additional-source evaluation must not silently replace the NB test contract."""

import unittest

from tcm_herb.evaluate import validate_evaluation_lineage


class EvaluationLineageTests(unittest.TestCase):
    def test_original_manifest_is_accepted(self):
        validate_evaluation_lineage({}, "same", "same", ["test", "external"])

    def test_only_new_chm_holdouts_are_allowed_from_a_descendant(self):
        descendant = {"base_manifest_sha256": "original"}
        validate_evaluation_lineage(descendant, "derived", "original", ["chm_valid", "chm_test"])
        with self.assertRaises(ValueError):
            validate_evaluation_lineage(descendant, "derived", "original", ["external"])
        with self.assertRaises(ValueError):
            validate_evaluation_lineage(descendant, "derived", "unrelated", ["chm_test"])


if __name__ == "__main__":
    unittest.main()
