"""Train MobileNetV2 using only the web-domain training and validation splits."""

import argparse
import csv
import hashlib
import json
import math
import os
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import torch
from sklearn.metrics import accuracy_score, f1_score
from torch.utils.data import DataLoader

from .data import validate_manifest
from .model import HerbDataset, build_model, preprocessing_metadata


def seed_worker(worker_id):
    seed = torch.initial_seed() % (2**32)
    np.random.seed(seed)
    random.seed(seed)


def validate(model, loader, device, classes):
    model.eval()
    targets, predictions = [], []
    loss_sum = 0.0
    with torch.inference_mode():
        for images, labels, _ in loader:
            images, labels = images.to(device), labels.to(device)
            logits = model(images)
            loss_sum += torch.nn.functional.cross_entropy(logits, labels, reduction="sum").item()
            targets.extend(labels.cpu().tolist())
            predictions.extend(logits.argmax(1).cpu().tolist())
    return {
        "loss": loss_sum / len(targets), "accuracy": accuracy_score(targets, predictions),
        "macro_f1": f1_score(targets, predictions, labels=list(range(classes)), average="macro", zero_division=0),
    }


def train(args):
    torch.set_num_threads(min(8, os.cpu_count() or 1))
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)
    device = torch.device("cuda" if args.device == "auto" and torch.cuda.is_available() else "cpu" if args.device == "auto" else args.device)
    if device.type == "cuda":
        torch.backends.cudnn.benchmark = True
    manifest_path = Path(args.manifest).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_manifest(manifest["records"], len(manifest["labels"]))
    timestamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
    output = Path(args.output or f"runs/mobilenet_v2_nb_tcm_chm_{timestamp}").resolve()
    if (output / "history.csv").exists():
        raise FileExistsError(f"Run already exists: {output}; choose a fresh output directory.")
    output.mkdir(parents=True, exist_ok=True)
    # Copy the exact split contract into the run; cache paths continue to resolve from the original manifest.
    (output / "manifest.json").write_bytes(manifest_path.read_bytes())
    train_data = HerbDataset(manifest_path, "train", args.image_size, training=True)
    val_data = HerbDataset(manifest_path, "val", args.image_size)
    generator = torch.Generator().manual_seed(args.seed)
    train_loader = DataLoader(train_data, batch_size=args.batch_size, shuffle=True, num_workers=args.workers,
                              pin_memory=device.type == "cuda", persistent_workers=args.workers > 0,
                              worker_init_fn=seed_worker, generator=generator)
    val_loader = DataLoader(val_data, batch_size=args.batch_size, shuffle=False, num_workers=0)
    print(f"RUN {output}\nDEVICE {device} | TRAIN {len(train_data)} | VAL {len(val_data)}", flush=True)
    print("Loading official ImageNet-pretrained MobileNetV2 width 1.0 ...", flush=True)
    model = build_model(len(manifest["labels"]), pretrained=True).to(device)
    optimizer = torch.optim.AdamW([
        {"params": model.features.parameters(), "lr": args.backbone_lr},
        {"params": model.classifier.parameters(), "lr": args.head_lr},
    ], weight_decay=1e-4)
    criterion = torch.nn.CrossEntropyLoss(label_smoothing=0.05)
    scaler = torch.amp.GradScaler("cuda", enabled=device.type == "cuda")
    config = vars(args) | {
        "device_used": str(device), "gpu": torch.cuda.get_device_name(device) if device.type == "cuda" else None,
        "torch_version": str(torch.__version__), "numpy_version": np.__version__,
        "architecture": "mobilenet_v2", "width_multiplier": 1.0,
        "pretrained": "torchvision MobileNet_V2_Weights.DEFAULT (IMAGENET1K_V2)",
        "checkpoint_selection": "validation macro F1; Dataset 2 never evaluated during training",
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "preprocessing": preprocessing_metadata(args.image_size, manifest["cache_size"]),
        "determinism": "seeded; cuDNN benchmark enabled, bitwise GPU reproducibility not guaranteed",
    }
    (output / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    history, best_f1, best_epoch = [], -1.0, 0
    started = time.perf_counter()
    if device.type == "cuda":
        torch.cuda.reset_peak_memory_stats(device)
    for epoch in range(1, args.epochs + 1):
        epoch_start = time.perf_counter()
        warming = epoch <= args.warmup_epochs
        for parameter in model.features.parameters():
            parameter.requires_grad_(not warming)
        model.train()
        if warming:
            model.features.eval()
        progress = max(0.0, (epoch - args.warmup_epochs - 1) / max(1, args.epochs - args.warmup_epochs))
        decay = 0.1 + 0.9 * (1 + math.cos(math.pi * progress)) / 2
        optimizer.param_groups[0]["lr"] = args.backbone_lr * decay
        optimizer.param_groups[1]["lr"] = args.head_lr * decay
        loss_sum, correct, seen = 0.0, 0, 0
        print(f"EPOCH {epoch}/{args.epochs} {'head warmup' if warming else 'full fine-tuning'}", flush=True)
        for step, (images, labels, _) in enumerate(train_loader, start=1):
            images = images.to(device, non_blocking=True)
            labels = labels.to(device, non_blocking=True)
            optimizer.zero_grad(set_to_none=True)
            with torch.autocast(device_type=device.type, dtype=torch.float16, enabled=device.type == "cuda"):
                logits = model(images)
                loss = criterion(logits, labels)
            if not torch.isfinite(loss):
                raise FloatingPointError(f"Non-finite training loss at epoch {epoch}, step {step}")
            scaler.scale(loss).backward()
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            scaler.step(optimizer)
            scaler.update()
            loss_sum += loss.item() * labels.size(0)
            correct += (logits.argmax(1) == labels).sum().item()
            seen += labels.size(0)
            if step == 1 or step % 20 == 0:
                print(f"  step {step}/{len(train_loader)} loss={loss_sum/seen:.4f} accuracy={correct/seen:.3f}", flush=True)
        metrics = validate(model, val_loader, device, len(manifest["labels"]))
        row = {"epoch": epoch, "train_loss": loss_sum / seen, "train_accuracy": correct / seen,
               "val_loss": metrics["loss"], "val_accuracy": metrics["accuracy"], "val_macro_f1": metrics["macro_f1"],
               "seconds": time.perf_counter() - epoch_start, "backbone_lr": optimizer.param_groups[0]["lr"]}
        history.append(row)
        with (output / "history.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(row))
            writer.writeheader()
            writer.writerows(history)
        print(f"  VAL accuracy={metrics['accuracy']:.4f} macro_f1={metrics['macro_f1']:.4f} time={row['seconds']:.1f}s", flush=True)
        if metrics["macro_f1"] > best_f1:
            best_f1, best_epoch = float(metrics["macro_f1"]), epoch
            checkpoint = {"architecture": "mobilenet_v2", "width_multiplier": 1.0,
                          "image_size": args.image_size, "cache_size": manifest["cache_size"],
                          "labels": manifest["labels"], "epoch": epoch, "val_macro_f1": best_f1,
                          "manifest_sha256": config["manifest_sha256"],
                          "state_dict": {name: tensor.detach().cpu() for name, tensor in model.state_dict().items()}}
            torch.save(checkpoint, output / "best.pt")
            print(f"  BEST epoch={best_epoch}", flush=True)
        if epoch >= args.warmup_epochs + args.patience and epoch - best_epoch >= args.patience:
            print("Early stopping based on validation macro F1.", flush=True)
            break
    summary = {"best_epoch": best_epoch, "best_val_macro_f1": best_f1, "epochs_completed": len(history),
               "training_seconds": time.perf_counter() - started,
               "peak_cuda_allocated_mib": torch.cuda.max_memory_allocated(device) / 1024**2 if device.type == "cuda" else None,
               "parameters": sum(parameter.numel() for parameter in model.parameters())}
    (output / "training_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    print(f"CHECKPOINT {output / 'best.pt'}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="data/nb_tcm_chm/manifest.json")
    parser.add_argument("--output")
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--warmup-epochs", type=int, default=2)
    parser.add_argument("--patience", type=int, default=6)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--image-size", type=int, default=160)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--backbone-lr", type=float, default=1e-4)
    parser.add_argument("--head-lr", type=float, default=1e-3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    args = parser.parse_args()
    if args.epochs <= args.warmup_epochs or args.batch_size < 1 or args.workers < 0 or args.image_size < 32:
        parser.error("Require epochs > warmup-epochs, positive batch-size, workers >= 0, image-size >= 32.")
    train(args)


if __name__ == "__main__":
    main()
