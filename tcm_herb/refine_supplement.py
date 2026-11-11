"""Check crop duplicates with SIFT geometry and pixel correlation before supplementing."""

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .data import UnionFind
from .supplement import assert_base_preserved, select_groups


def verified_copy(left, right):
    """A local-feature vote alone is insufficient: verify geometry and appearance."""
    if left["descriptors"] is None or right["descriptors"] is None or min(len(left["descriptors"]), len(right["descriptors"])) < 12:
        return None
    pairs = cv2.BFMatcher(cv2.NORM_L2).knnMatch(left["descriptors"], right["descriptors"], k=2)
    good = [one for one, two in pairs if one.distance < 0.70 * two.distance]
    if len(good) < 12:
        return None
    source = np.float32([left["points"][match.queryIdx] for match in good])
    target = np.float32([right["points"][match.trainIdx] for match in good])
    transform, mask = cv2.findHomography(source, target, cv2.RANSAC, 3.0)
    if transform is None or mask is None:
        return None
    inliers = mask.ravel().astype(bool)
    count = int(inliers.sum())
    if count < 12 or count / len(good) < 0.75:
        return None
    for points, gray in ((source[inliers], left["gray"]), (target[inliers], right["gray"])):
        coverage = float(np.ptp(points[:, 0]) * np.ptp(points[:, 1]) / gray.size)
        if coverage < 0.06:
            return None
    height, width = right["gray"].shape
    warped = cv2.warpPerspective(left["gray"], transform, (width, height))
    valid = cv2.warpPerspective(np.full(left["gray"].shape, 255, dtype=np.uint8), transform, (width, height))
    valid = cv2.erode(valid, np.ones((3, 3), dtype=np.uint8)) == 255
    valid &= (warped < 245) | (right["gray"] < 245)
    if int(valid.sum()) < 5000:
        return None
    a, b = warped[valid].astype(np.float32), right["gray"][valid].astype(np.float32)
    if min(float(a.std()), float(b.std())) < 8:
        return None
    correlation = float(np.corrcoef(a, b)[0, 1])
    if not np.isfinite(correlation) or correlation < 0.92:
        return None
    return {"inliers": count, "good_matches": len(good), "correlation": correlation}


def apply_training_quality_flags(records, flags):
    groups = defaultdict(list)
    for record in records:
        record["quality_flag"] = flags.get(record["relative_path"])
        groups[record["group"]].append(record)
    for members in groups.values():
        selected = [record for record in members if record["split"] == "train"]
        if not selected:
            continue
        chosen = selected[0]
        uncertain = any("uncertain identity" in (record["quality_flag"] or "") for record in members)
        if uncertain:
            for record in members:
                record["split"] = "excluded_manual_uncertain"
        elif chosen["quality_flag"]:
            alternatives = [record for record in members if record["source_split"] == "train" and not record["quality_flag"]]
            chosen["split"] = "excluded_manual_quality"
            if alternatives:
                sorted(alternatives, key=lambda record: record["path"])[0]["split"] = "train"
    return records


def refine(args):
    cv2.setNumThreads(4)
    source_path = Path(args.input).resolve()
    manifest = json.loads(source_path.read_text(encoding="utf-8"))
    audit = json.loads((source_path.parent / "audit.json").read_text(encoding="utf-8"))
    flag_path = Path(args.quality_flags).resolve()
    flag_document = json.loads(flag_path.read_text(encoding="utf-8"))
    nb = [dict(record) for record in manifest["records"] if record["domain"] != "chm100"]
    candidates = [dict(record) for record in manifest["records"] if record["domain"] == "chm100"]
    target_ids = {record["label"] for record in candidates}
    references = [record for record in nb if record["label"] in target_ids] + candidates
    sift, features, indexed = cv2.SIFT_create(nfeatures=600), {}, []
    for position, record in enumerate(references):
        with Image.open(record["cache"]) as image:
            gray = np.asarray(image.convert("L"))
        keypoints, descriptors = sift.detectAndCompute(gray, None)
        features[record["id"]] = {"gray": gray, "points": np.float32([point.pt for point in keypoints]), "descriptors": descriptors}
        if descriptors is not None and len(descriptors) >= 12:
            indexed.append(record)
        if (position + 1) % 200 == 0:
            print(f"SIFT_FEATURES {position+1}/{len(references)}", flush=True)
    matcher = cv2.FlannBasedMatcher({"algorithm": 1, "trees": 4}, {"checks": 64})
    matcher.add([features[record["id"]]["descriptors"] for record in indexed])
    matcher.train()
    union = UnionFind(len(candidates))
    candidate_indices = {record["id"]: index for index, record in enumerate(candidates)}
    existing_groups = defaultdict(list)
    for index, record in enumerate(candidates):
        existing_groups[record["group"]].append(index)
    for indices in existing_groups.values():
        for index in indices[1:]:
            union.union(indices[0], index)
    relative_indices = {record["relative_path"]: index for index, record in enumerate(candidates)}
    for related in flag_document.get("related_scene_groups", []):
        if not all(path in relative_indices for path in related):
            raise ValueError(f"Manual scene group contains an unknown source path: {related}")
        for path in related[1:]:
            union.union(relative_indices[related[0]], relative_indices[path])
    nb_matches = defaultdict(list, audit["nb_overlap_references"])
    checked, verified = set(), []
    for position, record in enumerate(candidates):
        feature = features[record["id"]]
        if feature["descriptors"] is None or len(feature["descriptors"]) < 12:
            continue
        votes = Counter()
        for neighbors in matcher.knnMatch(feature["descriptors"], k=4):
            for neighbor in neighbors:
                other = indexed[neighbor.imgIdx]
                if other["id"] != record["id"] and neighbor.distance < 250:
                    votes[neighbor.imgIdx] += 1
        for target_index, count in votes.most_common(20):
            if count < 8:
                continue
            other = indexed[target_index]
            pair = tuple(sorted((record["id"], other["id"])))
            if pair in checked:
                continue
            checked.add(pair)
            result = verified_copy(feature, features[other["id"]])
            if result is None:
                continue
            verified.append({"left": record["path"], "right": other["path"], "right_domain": other["domain"], "right_split": other["split"], **result})
            if record["label"] != other["label"]:
                record["label_conflict"] = True
            if other["domain"] == "chm100":
                union.union(candidate_indices[record["id"]], candidate_indices[other["id"]])
            else:
                nb_matches[record["id"]].append({"path": other["path"], "split": other["split"], "label": other["label"], "kind": "sift_geometry_correlation", **result})
        if (position + 1) % 50 == 0:
            print(f"SIFT_CHECK {position+1}/{len(candidates)} verified_pairs={len(verified)}", flush=True)
    for index, record in enumerate(candidates):
        record["group"] = candidates[union.find(index)]["id"]
    select_groups(candidates, nb_matches)
    apply_training_quality_flags(candidates, flag_document["flags"])
    records = nb + candidates
    assert_base_preserved(nb, records)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest.update({"records": records, "parent_manifest_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                     "curation": {"quality_flags_sha256": hashlib.sha256(flag_path.read_bytes()).hexdigest(),
                                  "opencv_version": cv2.__version__, "method": "SIFT candidate retrieval + ratio test + RANSAC + warped grayscale correlation >=0.92; manually reviewed supplemental training quality flags"}})
    summary = {"parent_audit": str(source_path.parent / "audit.json"), "verified_feature_pairs": verified,
               "nb_overlap_images": len(nb_matches), "nb_overlap_references": dict(nb_matches),
               "candidate_decisions": dict(Counter(record["split"] for record in candidates)),
               "per_class": {label["name_zh"]: dict(Counter(record["split"] for record in candidates if record["label"] == label["id"])) for label in manifest["labels"] if label["id"] in target_ids},
               "manual_quality_policy": flag_document, "base_splits_preserved": True,
               "limits": "Approximate feature retrieval is not exhaustive deduplication; CHM holdouts cover only 3 classes and contain web images, not a new 20-class camera test."}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "audit.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: summary[key] for key in ("nb_overlap_images", "candidate_decisions", "per_class")}, ensure_ascii=False, indent=2), flush=True)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="data/nb_chm100_aug/manifest.json")
    parser.add_argument("--quality-flags", default="configs/chm100_quality_flags.json")
    parser.add_argument("--output", default="data/nb_chm100_curated")
    refine(parser.parse_args())


if __name__ == "__main__":
    main()
