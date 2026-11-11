import unittest

import numpy as np
import torch

from tcm_herb.lite_model import folded_weights, quantize_input
from tcm_herb.quantize import select_calibration


class QuantizationContractTests(unittest.TestCase):
    def test_calibration_is_balanced_grouped_train_only_and_order_independent(self):
        records = []
        for label in range(2):
            for index in range(4):
                records.append(dict(id=f"{label}-{index}", label=label, split="train", group=f"{label}-{index}"))
            records += [dict(id=f"{label}-copy", label=label, split="train", group=f"{label}-0"),
                        dict(id=f"{label}-test", label=label, split="test", group=f"heldout-{label}")]
        first = select_calibration(records, classes=2, per_class=3)
        second = select_calibration(records[::-1], classes=2, per_class=3)
        self.assertEqual([row["id"] for row in first], [row["id"] for row in second])
        self.assertEqual([row["label"] for row in first], [0, 0, 0, 1, 1, 1])
        self.assertEqual(len({row["group"] for row in first}), 6)
        self.assertTrue(all(row["split"] == "train" for row in first))

    def test_calibration_rejects_leaked_or_insufficient_groups(self):
        records = [dict(id="a", label=0, split="train", group="shared"),
                   dict(id="b", label=0, split="val", group="shared")]
        with self.assertRaises(ValueError):
            select_calibration(records, 1, 1)
        with self.assertRaises(ValueError):
            select_calibration(records[:1], 1, 2)

    def test_quantization_clips_before_int8_cast_and_round_trips(self):
        values = np.array([-1000, -1.25, 0, 1.25, 1000], np.float32)
        result = quantize_input(values, scale=0.25, zero_point=-3)
        np.testing.assert_array_equal(result, [-128, -8, -3, 2, 127])
        np.testing.assert_allclose((result[1:4].astype(np.float32)+3)*0.25, values[1:4])
        for scale in (0, -1, np.nan):
            with self.assertRaises(ValueError):
                quantize_input(values, scale, 0)

    def test_folded_conv_matches_inference_batch_norm(self):
        generator = torch.Generator().manual_seed(7)
        for groups in (1, 3):
            conv = torch.nn.Conv2d(3, 3, 3, stride=2, padding=1, groups=groups, bias=False).eval()
            norm = torch.nn.BatchNorm2d(3, eps=1e-5).eval()
            with torch.no_grad():
                norm.running_mean.copy_(torch.rand(3, generator=generator))
                norm.running_var.copy_(torch.rand(3, generator=generator) + 0.2)
                norm.weight.copy_(torch.rand(3, generator=generator))
                norm.bias.copy_(torch.rand(3, generator=generator))
            inputs = torch.randn(2, 3, 10, 10, generator=generator)
            weight, bias = folded_weights(conv, norm)
            with torch.inference_mode():
                expected = norm(conv(inputs))
                actual = torch.nn.functional.conv2d(inputs, torch.from_numpy(weight), torch.from_numpy(bias), stride=2, padding=1, groups=groups)
            torch.testing.assert_close(actual, expected, atol=1e-6, rtol=1e-5)


if __name__ == "__main__":
    unittest.main()
