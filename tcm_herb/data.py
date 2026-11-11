"""Audit raw data and create reproducible, duplicate-grouped manifests."""

import argparse
import hashlib
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
from scipy.fft import dctn

from .labels import CLASS_NAMES, label_id, label_metadata

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
CACHE_SIZE = 320


def square_image(image, size=CACHE_SIZE):
    """EXIF-correct RGB, aspect-preserving resize, centered white letterbox."""
    image = ImageOps.exif_transpose(image).convert("RGB")
    return ImageOps.pad(
        image, (size, size), method=Image.Resampling.BILINEAR, color=(255, 255, 255)
    )


def perceptual_hash(image):
    gray = np.asarray(image.convert("L").resize((32, 32)), dtype=np.float32)
    frequencies = dctn(gray, type=2, norm="ortho")[:8, :8].flatten()[1:]
    return sum(int(bit) << index for index, bit in enumerate(frequencies > np.median(frequencies)))


class UnionFind:
    def __init__(self, count):
        self.parents = list(range(count))

    def find(self, value):
        while self.parents[value] != value:
            self.parents[value] = self.parents[self.parents[value]]
            value = self.parents[value]
        return value

    def union(self, left, right):
        left, right = self.find(left), self.find(right)
        self.parents[max(left, right)] = min(left, right)


def assign_splits(records, seed=42):
    """Dataset 2 has priority; all related Dataset 1 images are excluded."""
    groups = defaultdict(list)
    for record in records:
        groups[record["group"]].append(record)
    by_class = defaultdict(list)
    for members in groups.values():
        labels = {record["label"] for record in members}
        if len(labels) != 1:
            raise ValueError("Duplicate images have conflicting labels; inspect the audit report.")
        if any(record["domain"] == "external" for record in members):
            for record in members:
                record["split"] = "external" if record["domain"] == "external" else "excluded_external_overlap"
        else:
            by_class[members[0]["label"]].append(members)
    rng = random.Random(seed)
    for label, class_groups in sorted(by_class.items()):
        if len(class_groups) < 3:
            raise ValueError(f"Class {label} has fewer than 3 independent groups.")
        rng.shuffle(class_groups)
        # Allocate larger groups first; random order breaks equal-size ties.
        class_groups.sort(key=len, reverse=True)
        total = sum(map(len, class_groups))
        targets = {"train": total * 0.7, "val": total * 0.15, "test": total * 0.15}
        allocated = Counter()
        for index, members in enumerate(class_groups):
            remaining = len(class_groups) - index
            empty = [split for split in targets if allocated[split] == 0]
            choices = empty if remaining == len(empty) else list(targets)
            split = max(choices, key=lambda name: (targets[name] - allocated[name]) / targets[name])
            for record in members:
                record["split"] = split
            allocated[split] += len(members)
    return records


def validate_manifest(records, expected_classes=20):
    split_groups = defaultdict(set)
    for record in records:
        if record["split"].startswith("excluded"):
            continue
        split_groups[record["split"]].add(record["group"])
        if record["domain"] == "external" and record["split"] != "external":
            raise ValueError("Dataset 2 entered development data.")
    names = list(split_groups)
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            if split_groups[left] & split_groups[right]:
                raise ValueError(f"Duplicate group leaked across {left} and {right}.")
    for split in ("train", "val", "test", "external"):
        labels = {r["label"] for r in records if r["split"] == split}
        if labels != set(range(expected_classes)):
            raise ValueError(f"Missing labels in {split}: {labels}")


def prepare(root, output, seed=42):
    root, output = Path(root).resolve(), Path(output).resolve()
    if not (root / "Dataset 1").is_dir() or not (root / "Dataset 2").is_dir():
        raise FileNotFoundError(f"Expected Dataset 1 and Dataset 2 under {root}")
    output.mkdir(parents=True, exist_ok=True)
    cache = output / "cache"
    cache.mkdir(exist_ok=True)
    records, thumbnails, errors = [], [], []
    for domain, directory in (("web", "Dataset 1"), ("external", "Dataset 2")):
        folders = sorted(path for path in (root / directory).iterdir() if path.is_dir())
        if {label_id(path.name) for path in folders} != set(range(len(CLASS_NAMES))):
            raise ValueError(f"Incomplete or duplicated labels in {directory}")
        for folder in folders:
            for path in sorted(folder.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                    continue
                relative = path.relative_to(root).as_posix()
                sample_id = hashlib.sha256(relative.encode()).hexdigest()[:20]
                try:
                    file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                    with Image.open(path) as image:
                        image.load()
                        width, height = image.size
                        image = square_image(image)
                    pixel_hash = hashlib.sha256(image.tobytes()).hexdigest()
                    thumbnail = np.asarray(image.resize((32, 32)), dtype=np.float32)
                    # A changed raw file must never silently reuse an older resized image.
                    cache_name = f"{sample_id}_{file_hash[:12]}.png"
                    target = cache / cache_name
                    if not target.exists():
                        image.save(target, compress_level=1)
                    records.append({
                        "id": sample_id, "path": relative, "cache": f"cache/{cache_name}",
                        "label": label_id(folder.name), "folder": folder.name, "domain": domain,
                        "width": width, "height": height, "sha256": file_hash,
                        "pixel_sha256": pixel_hash, "phash": f"{perceptual_hash(image):016x}",
                    })
                    thumbnails.append(thumbnail)
                    if len(records) % 200 == 0:
                        print(f"AUDIT {len(records)} readable images", flush=True)
                except (OSError, ValueError, Image.DecompressionBombError) as error:
                    errors.append({"path": relative, "error": str(error)})
    if not records:
        raise ValueError("No readable images found.")
    union = UnionFind(len(records))
    exact_seen, pixel_seen, by_class = {}, {}, defaultdict(list)
    conflicts, near_pairs = [], []
    for index, record in enumerate(records):
        for key, seen in (("sha256", exact_seen), ("pixel_sha256", pixel_seen)):
            previous = seen.get(record[key])
            if previous is not None:
                if records[previous]["label"] != record["label"]:
                    conflicts.append([records[previous]["path"], record["path"]])
                union.union(index, previous)
            seen[record[key]] = index
        by_class[record["label"]].append(index)
    for indices in by_class.values():
        for offset, left in enumerate(indices):
            left_hash = int(records[left]["phash"], 16)
            for right in indices[offset + 1 :]:
                distance = (left_hash ^ int(records[right]["phash"], 16)).bit_count()
                if distance <= 4:
                    rms = float(np.sqrt(np.mean((thumbnails[left] - thumbnails[right]) ** 2)))
                    if rms <= 8.0:
                        union.union(left, right)
                        if records[left]["pixel_sha256"] != records[right]["pixel_sha256"]:
                            near_pairs.append({"left": records[left]["path"], "right": records[right]["path"], "phash_distance": distance, "thumbnail_rms": round(rms, 3)})
    for index, record in enumerate(records):
        record["group"] = records[union.find(index)]["id"]
    audit = {
        "root": str(root), "seed": seed, "cache_size": CACHE_SIZE,
        "images": len(records), "corrupt": errors,
        "domain_counts": dict(Counter(r["domain"] for r in records)),
        "exact_file_duplicate_excess": len(records) - len(exact_seen),
        "exact_preprocessed_pixel_duplicate_excess": len(records) - len(pixel_seen),
        "independent_groups": len({r["group"] for r in records}),
        "near_duplicate_rule": "same label; pHash distance <= 4 and 32x32 RGB RMS <= 8/255; conservative grouping, not proof of common source",
        "near_duplicate_pairs": near_pairs,
        "conflicting_duplicates": conflicts,
        "labels": label_metadata(),
    }
    audit_path = output / "audit.json"
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    if errors or conflicts:
        raise ValueError(f"Found {len(errors)} corrupt images and {len(conflicts)} label conflicts. Inspect {audit_path}")
    assign_splits(records, seed)
    validate_manifest(records)
    audit["split_counts"] = dict(Counter(r["split"] for r in records))
    audit["class_split_counts"] = {
        name: dict(Counter(r["split"] for r in records if r["label"] == index))
        for index, name in enumerate(CLASS_NAMES)
    }
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {"root": str(root), "seed": seed, "cache_size": CACHE_SIZE, "labels": label_metadata(), "records": records}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: audit[key] for key in ("images", "domain_counts", "exact_file_duplicate_excess", "independent_groups", "split_counts")}, indent=2), flush=True)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="assets/NB-TCM-CHM")
    parser.add_argument("--output", default="data/nb_tcm_chm")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    prepare(args.root, args.output, args.seed)


if __name__ == "__main__":
    main()
