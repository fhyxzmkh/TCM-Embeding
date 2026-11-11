"""Evaluate a fixed checkpoint and write predictions, metrics and diagnostic plots."""

import argparse
import csv
import hashlib
import json
import os
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
import torch
from PIL import Image
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, f1_score, top_k_accuracy_score
from torch.utils.data import DataLoader

from .model import HerbDataset, load_checkpoint


def validate_evaluation_lineage(manifest, manifest_hash, checkpoint_hash, splits):
    if manifest_hash == checkpoint_hash:
        return
    # Existing checkpoints may evaluate only the new CHM holdouts from an audited
    # descendant manifest; the frozen NB test contract cannot be swapped this way.
    if manifest.get("base_manifest_sha256") == checkpoint_hash and set(splits).issubset({"chm_valid", "chm_test"}):
        return
    raise ValueError("Evaluation manifest differs from training without an allowed CHM holdout lineage.")


def write_csv(path, rows):
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def setup_fonts():
    font = Path("C:/Windows/Fonts/msyh.ttc")
    if font.exists():
        font_manager.fontManager.addfont(str(font))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(font)).get_name()
    plt.rcParams["axes.unicode_minus"] = False


def plot_results(output, split, data, targets, predictions, probabilities, labels):
    setup_fonts()
    names = [label["name_zh"] for label in labels]
    matrix = confusion_matrix(targets, predictions, labels=list(range(len(labels))))
    figure, axes = plt.subplots(figsize=(13, 11))
    image = axes.imshow(matrix, cmap="Blues")
    for row in range(len(labels)):
        for column in range(len(labels)):
            if matrix[row, column]:
                axes.text(column, row, str(matrix[row, column]), ha="center", va="center",
                          color="white" if matrix[row, column] > matrix.max()/2 else "black", fontsize=8)
    axes.set(xticks=range(len(labels)), yticks=range(len(labels)), xticklabels=names, yticklabels=names,
             xlabel="预测类别", ylabel="真实类别", title=f"MobileNetV2 · {split} · Top-1 {accuracy_score(targets, predictions):.2%}")
    plt.setp(axes.get_xticklabels(), rotation=65, ha="right", fontsize=9)
    figure.colorbar(image, ax=axes, shrink=0.7)
    figure.tight_layout()
    figure.savefig(output / f"{split}_confusion_matrix.png", dpi=150)
    plt.close(figure)
    wrong = np.flatnonzero(targets != predictions)
    if len(wrong):
        # Highest-scoring errors are useful diagnostics; this does not change training or thresholds.
        wrong = sorted(wrong, key=lambda index: probabilities[index].max(), reverse=True)[:12]
        figure, axes = plt.subplots(3, 4, figsize=(13, 10))
        for axes_item in axes.flat:
            axes_item.axis("off")
        for axes_item, index in zip(axes.flat, wrong):
            with Image.open(data.manifest_path.parent / data.records[index]["cache"]) as image:
                axes_item.imshow(image)
            axes_item.set_title(f"真：{names[targets[index]]}\n预测：{names[predictions[index]]} ({probabilities[index].max():.1%})", fontsize=10)
        figure.suptitle(f"{split} · 错误样例（分数未经置信度校准）")
        figure.tight_layout()
        figure.savefig(output / f"{split}_errors.png", dpi=130)
        plt.close(figure)


def evaluate(checkpoint_path, manifest_path, output, splits, batch_size=64, device_name="auto"):
    torch.set_num_threads(min(8, os.cpu_count() or 1))
    device = torch.device("cuda" if device_name == "auto" and torch.cuda.is_available() else "cpu" if device_name == "auto" else device_name)
    model, checkpoint = load_checkpoint(checkpoint_path, device)
    manifest_path, output = Path(manifest_path).resolve(), Path(output).resolve()
    manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_evaluation_lineage(manifest, manifest_hash, checkpoint["manifest_sha256"], splits)
    output.mkdir(parents=True, exist_ok=True)
    metrics_path = output / "metrics.json"
    all_metrics = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.exists() else {}
    for split in splits:
        data = HerbDataset(manifest_path, split, checkpoint["image_size"])
        if data.manifest["labels"] != checkpoint["labels"]:
            raise ValueError("Checkpoint/manifest label order mismatch.")
        loader = DataLoader(data, batch_size=batch_size, shuffle=False, num_workers=0)
        probabilities, targets = [], []
        started = time.perf_counter()
        with torch.inference_mode():
            for images, labels, _ in loader:
                probabilities.append(model(images.to(device)).softmax(1).cpu().numpy())
                targets.extend(labels.tolist())
        probabilities, targets = np.concatenate(probabilities), np.array(targets)
        predictions = probabilities.argmax(1)
        classes = len(checkpoint["labels"])
        report = classification_report(targets, predictions, labels=list(range(classes)), target_names=[label["name"] for label in checkpoint["labels"]], output_dict=True, zero_division=0)
        active_labels = np.unique(targets).tolist()
        metrics = {"samples": len(data), "top1_accuracy": accuracy_score(targets, predictions),
                   "top3_accuracy": top_k_accuracy_score(targets, probabilities, k=3, labels=list(range(classes))),
                   "macro_f1": f1_score(targets, predictions, labels=active_labels, average="macro", zero_division=0),
                   "macro_f1_label_ids": active_labels, "output_classes": classes,
                   "evaluation_manifest_sha256": manifest_hash,
                   "errors": int((targets != predictions).sum()), "evaluation_seconds": time.perf_counter() - started,
                   "checkpoint_epoch": checkpoint["epoch"], "checkpoint_sha256": hashlib.sha256(Path(checkpoint_path).read_bytes()).hexdigest(),
                   "classification_report": report}
        all_metrics[split] = metrics
        rows = []
        for index, record in enumerate(data.records):
            top = probabilities[index].argsort()[-3:][::-1]
            rows.append({"path": record["path"], "domain": record["domain"], "true_id": int(targets[index]), "true_name": checkpoint["labels"][targets[index]]["name_zh"],
                         "predicted_id": int(predictions[index]), "predicted_name": checkpoint["labels"][predictions[index]]["name_zh"],
                         "correct": bool(targets[index] == predictions[index]), "top1_score": float(probabilities[index, top[0]]),
                         "top3_names": " | ".join(checkpoint["labels"][item]["name_zh"] for item in top)})
        write_csv(output / f"{split}_predictions.csv", rows)
        write_csv(output / f"{split}_per_class.csv", [
            {"id": label["id"], "name": label["name"], "name_zh": label["name_zh"], **report[label["name"]]}
            for label in checkpoint["labels"]
        ])
        metrics_path.write_text(json.dumps(all_metrics, ensure_ascii=False, indent=2), encoding="utf-8")
        plot_results(output, split, data, targets, predictions, probabilities, checkpoint["labels"])
        print(json.dumps({"split": split, **{key: metrics[key] for key in ("samples", "top1_accuracy", "top3_accuracy", "macro_f1", "errors")}}, indent=2), flush=True)
    history_path = output / "history.csv"
    if history_path.exists():
        with history_path.open(encoding="utf-8") as stream:
            history = list(csv.DictReader(stream))
        figure, axes = plt.subplots(1, 2, figsize=(11, 4))
        epochs = [int(row["epoch"]) for row in history]
        for key in ("train_loss", "val_loss"):
            axes[0].plot(epochs, [float(row[key]) for row in history], label=key)
        for key in ("train_accuracy", "val_accuracy", "val_macro_f1"):
            axes[1].plot(epochs, [float(row[key]) for row in history], label=key)
        for axis in axes:
            axis.legend()
            axis.set_xlabel("Epoch")
            axis.grid(alpha=0.2)
        figure.tight_layout()
        figure.savefig(output / "training_curves.png", dpi=150)
        plt.close(figure)
    return all_metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--manifest", default="data/nb_tcm_chm/manifest.json")
    parser.add_argument("--output")
    parser.add_argument("--splits", nargs="+", choices=("val", "test", "external", "chm_valid", "chm_test"), default=["test", "external"])
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    args = parser.parse_args()
    evaluate(args.checkpoint, args.manifest, args.output or Path(args.checkpoint).parent, args.splits, args.batch_size, args.device)


if __name__ == "__main__":
    main()
