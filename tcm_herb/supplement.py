"""Audit CHM-100, match explicit class names, and preserve all NB evaluation splits."""

import argparse
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from zipfile import ZipFile

import numpy as np
from PIL import Image

from .data import IMAGE_SUFFIXES, UnionFind, perceptual_hash, square_image, validate_manifest


def read_class_table(path):
    namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows = []
    with ZipFile(path) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = ["".join(item.itertext()) for item in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall("s:si", namespace)]
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        for row in sheet.findall(".//s:row", namespace):
            values = []
            for cell in row.findall("s:c", namespace):
                value = cell.find("s:v", namespace)
                if cell.attrib.get("t") == "s":
                    values.append(shared[int(value.text)])
                elif cell.attrib.get("t") == "inlineStr":
                    values.append("".join(cell.find("s:is", namespace).itertext()))
                else:
                    values.append(value.text if value is not None else "")
            if values and (match := re.match(r"^(\d+)\s+(.+)$", values[0])):
                rows.append({"source_id": int(match[1]), "name_zh": match[2].strip(), "name": values[1].strip(), "medicinal_part": values[2].strip()})
    if {row["source_id"] for row in rows} != set(range(1, 101)):
        raise ValueError("CHM-100 class table must explicitly enumerate IDs 1..100.")
    return rows


def select_groups(records, nb_matches):
    """Exclude every CHM group touching NB; keep official holdouts before training."""
    groups = defaultdict(list)
    for record in records:
        groups[record["group"]].append(record)
    for members in groups.values():
        if any(record.get("label_conflict") for record in members) or len({record["label"] for record in members}) != 1:
            reason = "excluded_label_conflict"
        elif any(nb_matches.get(record["id"]) for record in members):
            reason = "excluded_nb_overlap"
        else:
            reason = None
        if reason:
            for record in members:
                record["split"] = reason
            continue
        priority = {"test": 0, "valid": 1, "train": 2}
        ordered = sorted(members, key=lambda record: (priority[record["source_split"]], record["path"]))
        chosen = ordered[0]
        chosen["split"] = {"test": "chm_test", "valid": "chm_valid", "train": "train"}[chosen["source_split"]]
        for record in ordered[1:]:
            record["split"] = "excluded_chm_duplicate_group"
    return records


def assert_base_preserved(base, merged):
    by_id = {record["id"]: record for record in merged}
    for original in base:
        current = by_id.get(original["id"])
        if current is None or any(current[key] != original[key] for key in ("path", "label", "split", "group", "sha256", "pixel_sha256")):
            raise ValueError("The frozen NB data contract changed.")
    validate_manifest(merged)


def prepare(args):
    base_path = Path(args.base_manifest).resolve()
    base = json.loads(base_path.read_text(encoding="utf-8"))
    root, output = Path(args.root).resolve(), Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    cache = output / "cache"
    cache.mkdir(exist_ok=True)
    table = read_class_table(root.parent / "CHM-100_Data_Description.xlsx")
    source_table = {row["source_id"]: row for row in table}
    target_names = {label["name_zh"]: label["id"] for label in base["labels"]}
    matched = {row["source_id"]: target_names[row["name_zh"]] for row in table if row["name_zh"] in target_names}
    inventory, candidates, thumbnails, failures = [], [], [], []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        relative = path.relative_to(root).as_posix()
        parts = Path(relative).parts
        if len(parts) != 3 or parts[0] not in {"train", "valid", "test"} or not parts[1].isdigit():
            raise ValueError(f"Unexpected CHM path: {relative}")
        source_id = int(parts[1])
        if source_id not in source_table:
            raise ValueError(f"Undocumented CHM label: {source_id}")
        record = {"id": "chm_" + hashlib.sha256(relative.encode()).hexdigest()[:20],
                  "path": str(path), "relative_path": relative, "source_id": source_id,
                  "source_name": source_table[source_id]["name_zh"], "source_split": parts[0], "domain": "chm100"}
        try:
            record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            with Image.open(path) as original:
                original.load()
                record["width"], record["height"] = original.size
                image = square_image(original, base["cache_size"])
            record["pixel_sha256"] = hashlib.sha256(image.tobytes()).hexdigest()
            record["phash"] = f"{perceptual_hash(image):016x}"
            if source_id in matched:
                record["label"] = matched[source_id]
                target = cache / f"{record['id']}_{record['sha256'][:12]}.png"
                if not target.exists():
                    image.save(target, compress_level=1)
                record["cache"] = str(target)
                candidates.append(record)
                thumbnails.append(np.asarray(image.resize((32, 32)), dtype=np.float32))
            inventory.append(record)
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            failures.append({"path": str(path), "error": str(error)})
        if (len(inventory) + len(failures)) % 500 == 0:
            print(f"CHM_AUDIT {len(inventory)} readable, {len(failures)} unreadable", flush=True)
    # Exact duplicate checks cover all 100 source classes, including contradictory labels.
    source_hashes = defaultdict(list)
    for record in inventory:
        for field in ("sha256", "pixel_sha256"):
            source_hashes[(field, record[field])].append(record)
    source_conflicts = []
    for related in source_hashes.values():
        if len({record["source_id"] for record in related}) > 1:
            source_conflicts.append([record["relative_path"] for record in related])
            for record in related:
                record["label_conflict"] = True
    union = UnionFind(len(candidates))
    near_pairs = []
    for left, left_record in enumerate(candidates):
        for right in range(left + 1, len(candidates)):
            right_record = candidates[right]
            exact = any(left_record[key] == right_record[key] for key in ("sha256", "pixel_sha256"))
            distance = (int(left_record["phash"], 16) ^ int(right_record["phash"], 16)).bit_count()
            similar = distance <= 4 and float(np.sqrt(np.mean((thumbnails[left] - thumbnails[right]) ** 2))) <= 8.0
            if exact or similar:
                union.union(left, right)
                near_pairs.append({"left": left_record["relative_path"], "right": right_record["relative_path"], "exact": exact, "phash_distance": distance})
    for index, record in enumerate(candidates):
        record["group"] = candidates[union.find(index)]["id"]
    nb_hashes = defaultdict(list)
    nb_thumbnails, nb_phashes = [], []
    for record in base["records"]:
        for field in ("sha256", "pixel_sha256"):
            nb_hashes[(field, record[field])].append(record)
        with Image.open(base_path.parent / record["cache"]) as image:
            nb_thumbnails.append(np.asarray(image.resize((32, 32)), dtype=np.float32))
        nb_phashes.append(int(record["phash"], 16))
    nb_matches = defaultdict(list)
    for index, record in enumerate(candidates):
        references = {}
        for field in ("sha256", "pixel_sha256"):
            for reference in nb_hashes.get((field, record[field]), []):
                references[reference["id"]] = {"path": reference["path"], "split": reference["split"], "label": reference["label"], "kind": field}
        candidate_hash = int(record["phash"], 16)
        for position, reference in enumerate(base["records"]):
            distance = (candidate_hash ^ nb_phashes[position]).bit_count()
            if distance <= 4:
                rms = float(np.sqrt(np.mean((thumbnails[index] - nb_thumbnails[position]) ** 2)))
                if rms <= 8.0:
                    references[reference["id"]] = {"path": reference["path"], "split": reference["split"], "label": reference["label"], "kind": "near", "phash_distance": distance, "rms": rms}
        if references:
            nb_matches[record["id"]] = list(references.values())
            if any(reference["label"] != record["label"] for reference in references.values()):
                record["label_conflict"] = True
    select_groups(candidates, nb_matches)
    base_records = [dict(record, cache=str((base_path.parent / record["cache"]).resolve())) for record in base["records"]]
    merged = base_records + candidates
    assert_base_preserved(base["records"], merged)
    provenance = {"dataset": "CHM-100 v1.0 (user-provided local archive)",
                  "source_statement_sha256": hashlib.sha256((root.parent / "CHM-100_Data_Statement.md").read_bytes()).hexdigest(),
                  "class_table_sha256": hashlib.sha256((root.parent / "CHM-100_Data_Description.xlsx").read_bytes()).hexdigest(),
                  "scope": "academic research; cite the original MPM-Net paper"}
    summary = {"root": str(root), "provenance": provenance, "images": len(inventory), "unreadable": failures,
               "class_count": len({record["source_id"] for record in inventory}),
               "official_split_counts": dict(Counter(record["source_split"] for record in inventory)),
               "matched_classes": [dict(source_table[source_id], target_id=target) for source_id, target in matched.items()],
               "matched_image_count": len(candidates), "candidate_decisions": dict(Counter(record["split"] for record in candidates)),
               "source_conflicting_duplicates": source_conflicts, "within_matched_duplicate_pairs": near_pairs,
               "nb_overlap_images": len(nb_matches), "nb_overlap_references": dict(nb_matches),
               "per_matched_class": {source_table[source_id]["name_zh"]: dict(Counter(record["split"] for record in candidates if record["source_id"] == source_id)) for source_id in matched},
               "base_splits_preserved": True,
               "limits": "Conservative pHash/RMS grouping is not exhaustive source-image deduplication; label names are matched from the author table, not pharmacognostic verification."}
    manifest = {"root": base["root"], "seed": base["seed"], "cache_size": base["cache_size"], "labels": base["labels"],
                "base_manifest_sha256": hashlib.sha256(base_path.read_bytes()).hexdigest(), "source_roots": {"nb": base["root"], "chm100": str(root)},
                "supplement_provenance": provenance, "records": merged}
    (output / "inventory.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "audit.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: summary[key] for key in ("images", "class_count", "official_split_counts", "matched_classes", "matched_image_count", "candidate_decisions", "nb_overlap_images", "per_matched_class")}, ensure_ascii=False, indent=2), flush=True)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-manifest", default="data/nb_tcm_chm/manifest.json")
    parser.add_argument("--root", default="assets/MPM-Net_Data/dataset_split_train_test-100")
    parser.add_argument("--output", default="data/nb_chm100_aug")
    args = parser.parse_args()
    prepare(args)


if __name__ == "__main__":
    main()
