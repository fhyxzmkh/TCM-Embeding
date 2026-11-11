"""Export a fixed-shape FP32 .pt2 graph and verify numerical parity on CPU."""

import argparse
import json
from pathlib import Path

import torch

from .model import load_checkpoint, preprocessing_metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    torch.set_num_threads(4)
    model, checkpoint = load_checkpoint(args.checkpoint)
    output = Path(args.output or Path(args.checkpoint).parent / "export")
    output.mkdir(parents=True, exist_ok=True)
    torch.manual_seed(42)
    sample = torch.randn(1, 3, checkpoint["image_size"], checkpoint["image_size"])
    print("Exporting static batch-1 FP32 graph ...", flush=True)
    program = torch.export.export(model, (sample,))
    artifact = output / "mobilenet_v2_fp32.pt2"
    torch.export.save(program, artifact)
    restored = torch.export.load(artifact).module()
    differences = []
    with torch.inference_mode():
        for _ in range(3):
            sample = torch.randn_like(sample)
            expected, actual = model(sample), restored(sample)
            torch.testing.assert_close(actual, expected, atol=1e-5, rtol=1e-4)
            differences.append(float((actual - expected).abs().max()))
    metadata = {"architecture": checkpoint["architecture"], "labels": checkpoint["labels"],
                "input_shape": list(sample.shape), "preprocessing": preprocessing_metadata(checkpoint["image_size"], checkpoint["cache_size"]),
                "precision": "FP32", "max_absolute_parity_error": max(differences),
                "status": "PC-exported .pt2; not INT8, not NPU-compiled, not measured on E84",
                "checkpoint_epoch": checkpoint["epoch"], "bytes": artifact.stat().st_size}
    (output / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: metadata[key] for key in ("precision", "max_absolute_parity_error", "status", "bytes")}, indent=2), flush=True)
    print(artifact.resolve(), flush=True)


if __name__ == "__main__":
    main()
