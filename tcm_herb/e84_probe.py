"""Stage the fixed-sample CM55 probe or compare captured UART results."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
from statistics import median
import zlib

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "firmware/e84_static_probe"
PACK = ROOT / "runs/e84_u55_224/static_validation"
BSP_COMMIT = "b135be8a28127e276a04b70d27950c3ba07b917a"
BSP_PROJECT = "Edgi_Talk_M55_DEEPCRAFT_Deploy_Vision"
PROBE_PROJECT = "TCM_Static_Probe"
PROBE_BATCH_PROJECT = "TCM_Static_Probe_20_RAM"
MODEL = "models/e84_shared_sram_vela.tflite"
DISABLE = ("RT_USING_LCD", "RT_USING_CHERRYUSB", "RT_CHERRYUSB_HOST",
           "RT_CHERRYUSB_HOST_DWC2_INFINEON", "RT_CHERRYUSB_HOST_MSC",
           "RT_CHERRYUSB_HOST_VIDEO", "BSP_USING_LCD")
PLACEMENT = {"FLASH": (0x60580000, 0x00800000), "SRAM": (0x26060000, 0x00310000)}


def sha256(blob):
    return hashlib.sha256(blob).hexdigest()


def load_assets(pack, sample_count=1):
    checks = json.loads((pack / "checksums.json").read_text(encoding="utf-8"))

    def checked(relative):
        content = (pack / relative).read_bytes()
        if sha256(content) != checks[relative]:
            raise ValueError(f"Changed validation asset: {relative}")
        return content

    manifest = json.loads(checked("manifest.json"))
    contract = json.loads(checked("pack_contract.json"))
    audit = json.loads(checked("models/compile_audit.json"))
    target = json.loads(checked("models/target_contract.json"))
    model = checked(MODEL)
    if (target["bsp_commit"] != BSP_COMMIT or audit["mode"] != "Shared_Sram" or
            audit["compiled"]["sha256"] != sha256(model) or
            contract["compiled_model_sha256"] != sha256(model) or
            model[4:8] != b"TFL3"):
        raise ValueError("Unexpected BSP or compiled-model contract.")
    for name, shape, size in (("input", [1, 224, 224, 3], 150528), ("output", [1, 20], 20)):
        tensor = manifest[name]
        if (tensor["dtype"], tensor["shape"], tensor["bytes"]) != ("int8", shape, size):
            raise ValueError(f"Unexpected {name} contract.")
    if sample_count not in (1, 20) or len(manifest["samples"]) != 20:
        raise ValueError("Expected the 20-class static validation pack.")
    inputs, expected = [], []
    for index, entry in enumerate(manifest["samples"][:sample_count]):
        name = f"sample_{index:02d}"
        paths = (f"inputs/{name}.bin", f"expected/{name}.bin")
        if (entry["sample"], entry["input_file"], entry["expected_file"]) != (name, *paths):
            raise ValueError(f"Validation sample order or filenames changed: {name}")
        source, reference = map(checked, paths)
        if len(source) != 150528 or len(reference) != 20:
            raise ValueError(f"Invalid tensor length: {name}")
        inputs.append(source)
        expected.append(reference)
    return manifest, target, model, b"".join(inputs), b"".join(expected)


def asset_header(manifest, model, inputs, sample_count):
    lines = ["#pragma once", f'#define TCM_MODEL_SHA256 "{sha256(model)}"',
             f'#define TCM_INPUTS_SHA256 "{sha256(inputs)}"',
             f"#define TCM_INPUTS_CRC32 0x{zlib.crc32(inputs):08x}u",
             f"#define TCM_MODEL_BYTES {len(model)}u", f"#define TCM_SAMPLE_COUNT {sample_count}u"]
    for name in ("input", "output"):
        tensor = manifest[name]
        prefix = "TCM_" + name.upper()
        lines += [f"#define {prefix}_BYTES {tensor['bytes']}u",
                  f"#define {prefix}_SCALE {float(tensor['scale']):.17e}f",
                  f"#define {prefix}_ZERO_POINT ({tensor['zero_point']})"]
    for name, (address, size) in PLACEMENT.items():
        lines += [f"#define TCM_{name}_BASE 0x{address:08x}u", f"#define TCM_{name}_SIZE 0x{size:08x}u"]
    return "\n".join(lines) + "\n"


def stage(sdk_root, pack=PACK, *, all_samples=False):
    original = sdk_root / "projects" / BSP_PROJECT
    destination = sdk_root / "projects" / (PROBE_BATCH_PROJECT if all_samples else PROBE_PROJECT)
    if destination.exists():
        raise FileExistsError(f"Already staged; will not overwrite: {destination}")
    sample_count = 20 if all_samples else 1
    manifest, target, model, inputs, _ = load_assets(pack, sample_count)
    linker = original / "board/linker_scripts/link.ld"
    linker_bytes = linker.read_bytes().replace(b"\r\n", b"\n")
    if sha256(linker_bytes) != target["linker_sha256"]:
        raise ValueError("Expected the pinned vision example linker script.")
    sconstruct = (original / "SConstruct").read_text(encoding="utf-8")
    sconstruct, count = re.subn(r"(?m)^ +objs\.extend\(SConscript\('(\.\./\.\./)?libraries/Common/deepcraft_ai/SConscript'[^\n]*\n", "", sconstruct)
    if count != 2:
        raise ValueError("Unexpected vision example SConstruct; cannot isolate the old model.")
    config = (original / "rtconfig.h").read_text(encoding="utf-8")
    for macro in DISABLE:
        config = re.sub(rf"(?m)^#define {macro}\b[^\n]*\n", "", config)
    config, count = re.subn(r"(?m)^#define RT_MAIN_THREAD_STACK_SIZE +2048$",
                            "#define RT_MAIN_THREAD_STACK_SIZE 8192", config)
    if count != 1:
        raise ValueError("Unexpected vision example stack setting.")
    dot_config = (original / ".config").read_text(encoding="utf-8")
    for macro in DISABLE:
        dot_config = re.sub(rf"(?m)^CONFIG_{macro}=y$", f"# CONFIG_{macro} is not set", dot_config)
    dot_config = re.sub(r"(?m)^CONFIG_RT_MAIN_THREAD_STACK_SIZE=2048$",
                        "CONFIG_RT_MAIN_THREAD_STACK_SIZE=8192", dot_config)

    shutil.copytree(original, destination, ignore=shutil.ignore_patterns(
        "build", "Debug", ".sconsign*", "figures", ".project", ".cproject", ".settings",
        "uvc_ai_app.c", "uvc_ai_app.h"))
    (destination / "SConstruct").write_text(sconstruct, encoding="utf-8")
    (destination / "rtconfig.h").write_text(config, encoding="utf-8")
    (destination / ".config").write_text(dot_config, encoding="utf-8")
    applications = destination / "applications"
    shutil.copyfile(SOURCE / "main.c", applications / "main.c")
    probe = applications / "tcm_static_probe"
    probe.mkdir()
    for name in ("probe.cpp", "SConscript", "assets.S"):
        shutil.copyfile(SOURCE / name, probe / name)
    (probe / "probe_assets.h").write_text(asset_header(manifest, model, inputs, sample_count), encoding="ascii")
    (probe / "model.tflite").write_bytes(model)
    (probe / "samples.bin").write_bytes(inputs)
    return destination


def compare_record(record, entry, model_hash_expected, expected):
    model_hash, _, ticks, hz, used, total, encoded = record
    ticks, hz, used, total = map(int, (ticks, hz, used, total))
    if model_hash != model_hash_expected or hz < 1 or not (0 < used <= total):
        raise ValueError("Unexpected model hash, timer rate or arena report.")
    signed = lambda data: [byte if byte < 128 else byte - 256 for byte in data]
    reference, actual = signed(expected), signed(bytes.fromhex(encoded))
    reference_top1 = max(range(len(reference)), key=reference.__getitem__)
    actual_top1 = max(range(len(actual)), key=actual.__getitem__)
    delta = [abs(a - b) for a, b in zip(actual, reference)]
    return {"sample": entry["sample"], "model_sha256": model_hash,
            "true_name": entry["true_name"], "true_id": entry["true_id"],
            "reference_top1": reference_top1, "actual_top1": actual_top1,
            "top1_matches": reference_top1 == actual_top1,
            "different_elements": sum(d != 0 for d in delta), "max_abs_lsb": max(delta),
            "ticks": ticks, "tick_hz": hz, "invoke_ms_by_ticks": 1000 * ticks / hz,
            "arena_used_bytes": used, "arena_total_bytes": total,
            "reference_int8": reference, "actual_int8": actual}


def compare_log(log, pack=PACK, *, all_samples=False):
    sample_count = 20 if all_samples else 1
    manifest, _, model, inputs, expected = load_assets(pack, sample_count)
    # The RT-Thread shell can print its prompt on the same line as the probe.
    matches = list(re.finditer(r"(?m)^(?:msh [^>\r\n]*>[ \t]*)?TCM_PROBE v=1 model=([0-9a-f]{64}) sample=(\d{2}) "
                               r"ticks=(\d+) hz=(\d+) arena_used=(\d+) arena_total=(\d+) "
                               r"logits=([0-9a-f]{40})[ \t\r]*$", log))
    if ([int(row[2]) for row in matches] != list(range(sample_count)) or
            "TCM_PROBE_ERROR " in log):
        raise ValueError(f"Expected {sample_count} complete, ordered probe results without errors; found {len(matches)}.")
    model_hash, inputs_hash = sha256(model), sha256(inputs)
    if all_samples:
        start = f"TCM_PROBE_START samples={sample_count} model={model_hash} inputs={inputs_hash}"
        done = f"TCM_PROBE_DONE samples={sample_count} inputs={inputs_hash}"
        if (log.count(start) != 1 or log.count(done) != 1 or
                not log.index(start) < matches[0].start() <= matches[-1].end() <= log.index(done)):
            raise ValueError("Expected matching batch start and completion markers.")
    records = [compare_record(row.groups(), entry, model_hash, expected[i * 20:(i + 1) * 20])
               for i, (row, entry) in enumerate(zip(matches, manifest["samples"]))]
    if not all_samples:
        return records[0]
    latency = [row["invoke_ms_by_ticks"] for row in records]
    return {"sample_count": len(records), "model_sha256": model_hash, "inputs_sha256": inputs_hash,
            "top1_matches_cpu": sum(row["top1_matches"] for row in records),
            "board_top1_correct": sum(row["actual_top1"] == row["true_id"] for row in records),
            "cpu_top1_correct": sum(row["reference_top1"] == row["true_id"] for row in records),
            "byte_exact_samples": sum(row["different_elements"] == 0 for row in records),
            "max_abs_lsb": max(row["max_abs_lsb"] for row in records),
            "invoke_ms": {"min": min(latency), "median": median(latency), "max": max(latency)},
            "arena_peak_used_bytes": max(row["arena_used_bytes"] for row in records), "samples": records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, default=PACK)
    sub = parser.add_subparsers(dest="action", required=True)
    stage_parser = sub.add_parser("stage")
    stage_parser.add_argument("--sdk-root", type=Path, required=True)
    stage_parser.add_argument("--all-samples", action="store_true")
    compare_parser = sub.add_parser("compare-log")
    compare_parser.add_argument("--log", type=Path, required=True)
    compare_parser.add_argument("--all-samples", action="store_true")
    args = parser.parse_args()
    if args.action == "stage":
        print(stage(args.sdk_root.resolve(), args.pack, all_samples=args.all_samples))
    else:
        result = compare_log(args.log.read_text(encoding="utf-8-sig"), args.pack, all_samples=args.all_samples)
        print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
