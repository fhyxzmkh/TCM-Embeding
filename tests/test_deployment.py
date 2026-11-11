import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import numpy as np

from tcm_herb.compile_u55 import parse_memory_regions, sram_placement
from tcm_herb.validation_pack import archive_pack, check_hashes, choose_samples, compare_output, sha256, verify


class DeploymentContractTests(unittest.TestCase):
    def test_linker_regions_and_sram_residency_include_constants(self):
        regions = parse_memory_regions("""
        MEMORY {
          m55_nvm : ORIGIN = 0x60580000, LENGTH = 0x00800000
          m55_data_secondary : ORIGIN = 0x26060000, LENGTH = 0x00310000
        }
        """)
        self.assertEqual(regions["m55_data_secondary"]["length"], 3211264)
        self.assertEqual(regions["m55_nvm"]["origin"], 0x60580000)
        shared = sram_placement("Shared_Sram", 1509584, 2258640, 150548, 3211264)
        self.assertEqual(shared["with_separate_external_io_bytes"], 1660132)
        self.assertEqual(shared["required_sram_constants_bytes"], 0)
        self.assertTrue(shared["npu_buffers_fit_region_before_other_usage"])
        sram = sram_placement("Sram_Only", 1505280, 2258000, 150548, 3211264)
        self.assertEqual(sram["scratch_and_required_constants_bytes"], 3763280)
        self.assertFalse(sram["npu_buffers_fit_region_before_other_usage"])
        with self.assertRaises(ValueError):
            sram_placement("typo", 0, 0, 0, 3211264)

    def test_sample_selection_is_balanced_val_only_and_order_independent(self):
        records = []
        for label in range(3):
            for index in range(5):
                records.append(dict(id=f"{label}-{index}", label=label, split="val", group=f"g-{label}-{index}"))
            records.append(dict(id=f"train-{label}", label=label, split="train", group="train"))
        selected = choose_samples(records, 3)
        self.assertEqual([row["id"] for row in selected], [row["id"] for row in choose_samples(records[::-1], 3)])
        self.assertEqual([row["label"] for row in selected], [0, 1, 2])
        self.assertTrue(all(row["split"] == "val" for row in selected))
        self.assertEqual(len({row["group"] for row in selected}), 3)
        # The selector does not require or inspect any prediction fields.
        with self.assertRaises(ValueError):
            choose_samples([row for row in records if row["split"] == "train"], 3)
        with self.assertRaises(ValueError):
            choose_samples([dict(id=str(i), label=i, split="val", group="duplicate") for i in range(2)], 2)

    def test_output_error_does_not_overflow_int8_and_ties_use_first_class(self):
        expected = np.array([-128, 127, 127], np.int8)
        actual = np.array([127, -128, 127], np.int8)
        result = compare_output(expected, actual)
        self.assertEqual(result["max_abs_lsb"], 255)
        self.assertEqual(result["different_elements"], 2)
        self.assertEqual(result["reference_top1"], 1)
        self.assertEqual(result["actual_top1"], 0)
        self.assertFalse(result["top1_matches"])
        self.assertTrue(compare_output(expected, expected.copy())["exact"])
        with self.assertRaises(ValueError):
            compare_output(expected, actual.astype(np.uint8))
        with self.assertRaises(ValueError):
            compare_output(expected, actual.reshape(1, 3))

    def test_pack_hashes_reject_changed_files_and_escape_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "pack"
            root.mkdir()
            payload = root / "input.bin"
            payload.write_bytes(b"\x80\x00\x7f")
            (root / "checksums.json").write_text(json.dumps({"input.bin": sha256(payload)}))
            self.assertEqual(check_hashes(root), 1)
            payload.write_bytes(b"changed")
            with self.assertRaises(ValueError):
                check_hashes(root)
            outside = root.parent / "outside.bin"
            outside.write_bytes(b"outside")
            (root / "checksums.json").write_text(json.dumps({"../outside.bin": sha256(outside)}))
            with self.assertRaises(ValueError):
                check_hashes(root)

    def test_finalized_archive_can_be_read_and_matches_source_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "pack"
            (root / "inputs").mkdir(parents=True)
            payload = bytes(range(256)) * 600
            (root / "inputs/sample_00.bin").write_bytes(payload)
            archive = archive_pack(root)
            with zipfile.ZipFile(archive, "r") as reader:
                self.assertIsNone(reader.testzip())
                self.assertEqual(reader.read("pack/inputs/sample_00.bin"), payload)

    def test_verifier_reports_difference_and_rejects_wrong_output_length(self):
        with tempfile.TemporaryDirectory() as directory:
            root, outputs = Path(directory) / "pack", Path(directory) / "outputs"
            (root / "expected").mkdir(parents=True)
            outputs.mkdir()
            expected = root / "expected/sample_00.bin"
            expected.write_bytes(np.array([-128, 127, 5], np.int8).tobytes())
            manifest = {"output": {"bytes": 3}, "samples": [{"sample": "sample_00", "true_id": 1,
                         "expected_file": "expected/sample_00.bin"}]}
            (root / "manifest.json").write_text(json.dumps(manifest))
            (root / "checksums.json").write_text(json.dumps({
                "expected/sample_00.bin": sha256(expected), "manifest.json": sha256(root / "manifest.json")}))
            actual = outputs / "sample_00.bin"
            actual.write_bytes(np.array([127, -128, 5], np.int8).tobytes())
            result = verify(root, outputs)
            self.assertEqual(result["samples"], 1)
            self.assertEqual(result["exact_samples"], 0)
            self.assertEqual(result["top1_matches"], 0)
            self.assertEqual(result["max_abs_lsb"], 255)
            actual.write_bytes(b"\x00")
            with self.assertRaises(ValueError):
                verify(root, outputs)


if __name__ == "__main__":
    unittest.main()
