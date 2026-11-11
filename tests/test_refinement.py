"""Verify crop-copy detection and preserve held-out rows during manual curation."""

import unittest

import cv2
import numpy as np

from tcm_herb.refine_supplement import apply_training_quality_flags, verified_copy


class RefinementTests(unittest.TestCase):
    @staticmethod
    def features(image):
        points, descriptors = cv2.SIFT_create(nfeatures=600).detectAndCompute(image, None)
        return {"gray": image, "points": np.float32([point.pt for point in points]), "descriptors": descriptors}

    def test_resized_crop_is_a_copy_but_independent_texture_is_not(self):
        rng = np.random.default_rng(17)
        image = cv2.GaussianBlur(rng.integers(0, 256, (320, 320), dtype=np.uint8), (3, 3), 0)
        cropped = cv2.resize(image[32:288, 32:288], (320, 320))
        independent = cv2.GaussianBlur(rng.integers(0, 256, (320, 320), dtype=np.uint8), (3, 3), 0)
        self.assertIsNotNone(verified_copy(self.features(image), self.features(cropped)))
        self.assertIsNone(verified_copy(self.features(image), self.features(independent)))

    def test_shared_heading_does_not_make_different_photos_duplicates(self):
        rng = np.random.default_rng(19)
        left = rng.integers(0, 256, (320, 320), dtype=np.uint8)
        right = rng.integers(0, 256, (320, 320), dtype=np.uint8)
        for image in (left, right):
            image[:70] = 240
            cv2.putText(image, "HERB PHOTO", (5, 45), cv2.FONT_HERSHEY_SIMPLEX, 1.1, 0, 2)
        self.assertIsNone(verified_copy(self.features(left), self.features(right)))

    def test_quality_flags_exclude_training_without_filtering_holdouts(self):
        records = [
            {"id": "train", "path": "train", "relative_path": "train/ad.jpg", "source_split": "train", "split": "train", "group": "a"},
            {"id": "test", "path": "test", "relative_path": "test/ad.jpg", "source_split": "test", "split": "chm_test", "group": "b"},
        ]
        apply_training_quality_flags(records, {"train/ad.jpg": "advertising layout", "test/ad.jpg": "advertising layout"})
        self.assertEqual(records[0]["split"], "excluded_manual_quality")
        self.assertEqual(records[1]["split"], "chm_test")


if __name__ == "__main__":
    unittest.main()
