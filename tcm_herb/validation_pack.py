"""Build portable static inputs and host references; compare later board outputs.

The golden outputs come from the uncompiled INT8 model on the PC. This module
does not execute an Ethos-U model or claim that the board has been validated.
"""

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import sys
import zipfile

import numpy as np


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def choose_samples(records, classes, seed=42):
    rng, selected = random.Random(seed), []
    for label in range(classes):
        candidates = sorted((row for row in records if row["split"] == "val" and row["label"] == label), key=lambda row: str(row["id"]))
        if not candidates:
            raise ValueError(f"No validation image for class {label}")
        # No prediction or correctness information is used for selection.
        selected.append(candidates[rng.randrange(len(candidates))])
    if len({row["group"] for row in selected}) != classes:
        raise ValueError("Validation pack contains duplicate groups.")
    return selected


def compare_output(expected, actual):
    expected, actual = np.asarray(expected), np.asarray(actual)
    if expected.dtype != np.int8 or actual.dtype != np.int8 or expected.shape != actual.shape or expected.ndim != 1:
        raise ValueError("Outputs must be equal-length one-dimensional INT8 arrays.")
    # Cast before subtraction: int8 subtraction would overflow at +/-128.
    delta = actual.astype(np.int16) - expected.astype(np.int16)
    return {"exact": bool(np.array_equal(actual, expected)), "different_elements": int(np.count_nonzero(delta)),
            "max_abs_lsb": int(np.abs(delta).max()), "mean_abs_lsb": float(np.abs(delta).mean()),
            "reference_top1": int(expected.argmax()), "actual_top1": int(actual.argmax()),
            "top1_matches": bool(expected.argmax() == actual.argmax())}


def check_hashes(root):
    root = Path(root).resolve()
    checksums = read_json(root / "checksums.json")
    for relative, checksum in checksums.items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file() or sha256(path) != checksum:
            raise ValueError(f"Missing, changed, or unsafe pack file: {relative}")
    return len(checksums)


def archive_pack(root):
    root = Path(root)
    archive = root.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zip_file:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                zip_file.write(path, arcname=root.name + "/" + path.relative_to(root).as_posix())
    # Finalize and flush before opening a separate reader for CRCs.
    with zipfile.ZipFile(archive, "r") as zip_file:
        if zip_file.testzip() is not None:
            raise ValueError("ZIP CRC check failed.")
    return archive


def write_c_headers(root, input_bytes, samples, labels):
    include = root / "include"
    include.mkdir(exist_ok=True)
    count, classes = len(samples), len(labels)
    lines = ["/* Host CPU reference data; not measured NPU results. */", "#ifndef TCM_GOLDEN_OUTPUTS_H", "#define TCM_GOLDEN_OUTPUTS_H",
             "#include <stdint.h>", "#include <stddef.h>", f"#define TCM_SAMPLE_COUNT {count}", f"#define TCM_CLASS_COUNT {classes}",
             f"#define TCM_INPUT_BYTES {input_bytes}u", f"#define TCM_OUTPUT_BYTES {classes}u",
             "static const int8_t tcm_expected_output[TCM_SAMPLE_COUNT][TCM_CLASS_COUNT] = {"]
    lines += ["  {" + ", ".join(str(value) for value in sample["expected_output_int8"]) + "}," for sample in samples]
    lines += ["};", "static const uint8_t tcm_true_label[TCM_SAMPLE_COUNT] = {" + ", ".join(str(sample["true_id"]) for sample in samples) + "};",
              "typedef struct { int reference_top1; int actual_top1; int max_abs_lsb; size_t different_elements; } tcm_output_diff;",
              "static inline int tcm_argmax(const int8_t *values) {",
              "  int best = 0; for (int i = 1; i < TCM_CLASS_COUNT; ++i) if (values[i] > values[best]) best = i; return best;", "}",
              "static inline tcm_output_diff tcm_compare_output(size_t sample, const int8_t *actual) {",
              "  tcm_output_diff result = { -1, -1, 0, 0 };",
              "  if (sample >= TCM_SAMPLE_COUNT || actual == NULL) return result;",
              "  result.reference_top1 = tcm_argmax(tcm_expected_output[sample]); result.actual_top1 = tcm_argmax(actual);",
              "  for (int i = 0; i < TCM_CLASS_COUNT; ++i) {",
              "    int delta = (int)actual[i] - (int)tcm_expected_output[sample][i]; if (delta < 0) delta = -delta;",
              "    if (delta != 0) ++result.different_elements; if (delta > result.max_abs_lsb) result.max_abs_lsb = delta;", "  }", "  return result;", "}", "#endif", ""]
    (include / "golden_outputs.h").write_text("\n".join(lines), encoding="utf-8")
    first = np.fromfile(root / samples[0]["input_file"], dtype=np.int8)
    lines = ["/* First sample only. memcpy these bytes into the interpreter's INT8 input. */", "#ifndef TCM_FIRST_INPUT_H", "#define TCM_FIRST_INPUT_H", "#include <stdint.h>",
             "#if defined(__GNUC__)", "#define TCM_ALIGN16 __attribute__((aligned(16)))", "#else", "#define TCM_ALIGN16", "#endif",
             f"static const int8_t tcm_first_input[{input_bytes}] TCM_ALIGN16 = {{"]
    lines += ["  " + ", ".join(str(int(value)) for value in first[start:start+24]) + "," for start in range(0, len(first), 24)]
    lines += ["};", "#undef TCM_ALIGN16", "#endif", ""]
    (include / "first_input.h").write_text("\n".join(lines), encoding="utf-8")


def build(compile_dir, manifest_path, output, seed=42, resume=False):
    from PIL import Image
    import torch
    from .data import validate_manifest
    from .lite_model import LitePredictor, quantize_input
    from .quantize import load_input

    torch.set_num_threads(4)
    compile_dir, manifest_path, output = Path(compile_dir).resolve(), Path(manifest_path).resolve(), Path(output).resolve()
    summary = read_json(compile_dir / "compile_summary.json")
    target = summary["contract"]
    quant_dir = Path(target["input_model"]).parent
    conversion = read_json(quant_dir / "conversion.json")
    quant_config = read_json(quant_dir / "config.json")
    manifest = read_json(manifest_path)
    if sha256(manifest_path) != quant_config["manifest_sha256"] or manifest["labels"] != conversion["labels"]:
        raise ValueError("Validation source manifest or label order changed.")
    validate_manifest(manifest["records"], len(manifest["labels"]))
    selected = choose_samples(manifest["records"], len(manifest["labels"]), seed)
    calibration_groups = {row["group"] for row in read_json(quant_dir / "calibration.json")["records"]}
    if any(row["group"] in calibration_groups for row in selected):
        raise ValueError("Pack images overlap calibration groups.")
    compiled = next(row for row in summary["results"] if row["mode"] == "Shared_Sram")
    source_model, compiled_model = Path(target["input_model"]), Path(compiled["compiled_path"])
    if sha256(source_model) != target["input_sha256"] or sha256(compiled_model) != compiled["compiled"]["sha256"]:
        raise ValueError("A frozen source or compiled model changed.")
    contract = {"source_model_sha256": sha256(source_model), "compiled_model_sha256": sha256(compiled_model),
                "validation_manifest_sha256": sha256(manifest_path), "prior_val_logits_sha256": sha256(quant_dir / "val_logits.npz"),
                "compile_target_contract_sha256": sha256(compile_dir / "target_contract.json"),
                "seed": seed, "sample_ids": [row["id"] for row in selected], "reference_engine": "uncompiled INT8 TFLite native CPU, no default delegates",
                "board_or_fvp_executed": False}
    if output.exists() and any(output.iterdir()):
        if not resume or not (output / "pack_contract.json").exists() or read_json(output / "pack_contract.json") != contract:
            raise ValueError("Existing pack differs or requires --resume; use a new output directory.")
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "pack_contract.json", contract)
    for directory in ("inputs", "expected", "previews", "models"):
        (output / directory).mkdir(exist_ok=True)
    shutil.copyfile(source_model, output / "models/pc_int8.tflite")
    shutil.copyfile(compiled_model, output / "models/e84_shared_sram_vela.tflite")
    shutil.copyfile(compile_dir / "target_contract.json", output / "models/target_contract.json")
    shutil.copyfile(compile_dir / "Shared_Sram/audit.json", output / "models/compile_audit.json")
    shutil.copyfile(compile_dir / "e84_u55_analysis.ini", output / "models/e84_u55_analysis.ini")
    shutil.copyfile(compile_dir / "arm_vela_5_2_0.ini", output / "models/arm_vela_5_2_0.ini")
    for name in ("reference_sources.json", "vision_example_link.ld", "ARM_VELA_LICENSE.txt"):
        shutil.copyfile(compile_dir / name, output / "models" / name)
    write_json(output / "labels.json", manifest["labels"])
    predictor = LitePredictor(source_model)
    input_scale, input_zero_point = predictor.input["quantization"]
    output_scale, output_zero_point = predictor.output["quantization"]
    input_bytes, output_bytes = int(np.prod(predictor.input["shape"])), int(np.prod(predictor.output["shape"]))
    cases, expected_rows = [], []
    with np.load(quant_dir / "val_logits.npz", allow_pickle=False) as previous:
        index_by_path = {path: index for index, path in enumerate(previous["paths"].tolist())}
        for case_index, record in enumerate(selected):
            inputs = load_input(record, manifest_path, 224)
            quantized = quantize_input(inputs, input_scale, input_zero_point)
            name = f"sample_{case_index:02d}"
            input_file, output_file = f"inputs/{name}.bin", f"expected/{name}.bin"
            (output / input_file).write_bytes(quantized.tobytes(order="C"))
            # Replay the bytes read from disk, rather than a separate float input.
            disk_input = np.fromfile(output / input_file, dtype=np.int8).reshape(predictor.input["shape"])
            np.testing.assert_array_equal(quantized, disk_input)
            predictor.interpreter.set_tensor(predictor.input["index"], disk_input)
            predictor.interpreter.invoke()
            raw_output = predictor.interpreter.get_tensor(predictor.output["index"])[0]
            old_index = index_by_path[record["path"]]
            if int(previous["targets"][old_index]) != record["label"]:
                raise ValueError("Ground-truth label changed since the previous validation.")
            np.testing.assert_array_equal(raw_output, previous["raw_int8"][old_index])
            (output / output_file).write_bytes(raw_output.tobytes())
            if (output / input_file).stat().st_size != input_bytes or (output / output_file).stat().st_size != output_bytes:
                raise ValueError("Invalid binary tensor length.")
            with Image.open(manifest_path.parent / record["cache"]) as image:
                image.convert("RGB").resize((224, 224), Image.Resampling.BILINEAR).save(output / f"previews/{name}.png")
            predicted = int(raw_output.argmax())
            # Stable descending order keeps tied Top-3 consistent with argmax.
            top = np.argsort(-raw_output.astype(np.int16), kind="stable")[:3].tolist()
            case = {"sample": name, "source_id": record["id"], "source_path": record["path"], "source_group": record["group"],
                    "source_sha256": record["sha256"], "source_split": "val", "true_id": record["label"],
                    "true_name": manifest["labels"][record["label"]]["name_zh"], "reference_top1": predicted,
                    "reference_name": manifest["labels"][predicted]["name_zh"], "reference_correct": predicted == record["label"],
                    "reference_top3": top, "expected_output_int8": raw_output.tolist(),
                    "expected_logits": ((raw_output.astype(np.float32)-output_zero_point)*output_scale).tolist(),
                    "input_file": input_file, "expected_file": output_file, "preview_file": f"previews/{name}.png",
                    "input_sha256": sha256(output / input_file), "expected_sha256": sha256(output / output_file)}
            cases.append(case)
            expected_rows.append(raw_output)
            print(f"Packed {name}: true={record['label']} reference={predicted}", flush=True)
    manifest_output = {"format": "TCM static validation pack v1", "hardware_executed": False,
                       "sample_count": len(cases), "seed": seed, "selection": "one seeded val image per class, independent of predictions",
                       "input": {"shape": predictor.input["shape"].tolist(), "dtype": "int8", "layout": "NHWC", "bytes": input_bytes,
                                 "scale": input_scale, "zero_point": input_zero_point},
                       "output": {"shape": predictor.output["shape"].tolist(), "dtype": "int8", "bytes": output_bytes,
                                  "scale": output_scale, "zero_point": output_zero_point},
                       "preprocessing": conversion["preprocessing"], "model_paths": {"pc_reference": "models/pc_int8.tflite", "e84_shared_sram": "models/e84_shared_sram_vela.tflite"},
                       "samples": cases}
    write_json(output / "manifest.json", manifest_output)
    np.savez_compressed(output / "golden_outputs.npz", outputs=np.stack(expected_rows), true_ids=np.array([case["true_id"] for case in cases]))
    with (output / "samples.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        fields = ("sample", "true_id", "true_name", "reference_top1", "reference_name", "reference_correct", "input_file", "expected_file")
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: case[field] for field in fields} for case in cases)
    write_c_headers(output, input_bytes, cases, manifest["labels"])
    (output / "README.md").write_text(
        "# 中药材静态联调包\n\n"
        "这些输出是电脑端未编译INT8 TFLite参考值，不是E84/FVP实测。每类一张固定验证图，选择不看预测是否正确；不能用20张联调样例替代完整精度评估。\n\n"
        "1. 上板加载 `models/e84_shared_sram_vela.tflite`，注册ethos-u自定义算子，核对U55-128、COP1、驱动及Flash/SoCMEM放置。`models/pc_int8.tflite`仅供电脑参考。\n"
        "2. 第一张可用 `include/first_input.h`，其余从 `inputs/sample_XX.bin`逐张读取。将150528字节原样复制到INT8 NHWC输入，不再归一化、减128、转uint8或交换通道。\n"
        "3. 每次推理保存20字节原始INT8输出到同名 `sample_XX.bin`。不要保存softmax浮点结果代替它。`include/golden_outputs.h`提供CPU参考与差值统计。\n"
        "4. 回到本项目根目录执行：\n\n"
        f"```powershell\npython -m tcm_herb.validation_pack verify --pack '{output.as_posix()}' --outputs '板端输出目录' --sample 0\n```\n\n"
        "不指定--sample时核对全部20张。工具报告逐字节、Top-1和最大LSB差，没有预设硬件舍入误差保证。NPU近似并列输出可能改变argmax，应结合实际差值核查。\n\n"
        "模型常量留在可访问Flash，scratch与解释器arena按最终固件map分配到SoCMEM。原BSP的`.cy_ml_model_data`会复制模型到RAM，不能直接照搬；原示例320×320 uint8检测接口也需要改为224×224 int8分类接口。包内没有可直接烧录的固件。\n\n"
        "只有第0张输入被嵌入C头文件；不要把20张输入同时加载到RAM。CPU参考Top-1与真实药材类别分别见samples.csv，不应把错误的CPU预测改成真实类别后冒充参考。\n\n"
        "来源：用户提供的NB-TCM-CHM，CC BY 4.0；[数据集](https://data.mendeley.com/datasets/2kjmzjyrmd/2)，[论文](https://doi.org/10.1016/j.dib.2024.110405)。模型与数据来源哈希见pack_contract.json及manifest.json。\n",
        encoding="utf-8")
    checksums = {path.relative_to(output).as_posix(): sha256(path) for path in sorted(output.rglob("*")) if path.is_file() and path.name != "checksums.json"}
    write_json(output / "checksums.json", checksums)
    checked = check_hashes(output)
    archive = archive_pack(output)
    result = {"samples": len(cases), "host_binary_roundtrips_exact": len(cases), "matches_previous_int8_validation_outputs": True,
              "hash_checked_files": checked, "archive": str(archive), "archive_sha256": sha256(archive),
              "archive_bytes": archive.stat().st_size, "hardware_executed": False}
    write_json(compile_dir / "validation_pack_summary.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


def verify(pack, outputs, sample=None):
    pack, outputs = Path(pack).resolve(), Path(outputs).resolve()
    checked = check_hashes(pack)
    manifest = read_json(pack / "manifest.json")
    selected = list(range(len(manifest["samples"]))) if sample is None else [sample]
    if any(index < 0 or index >= len(manifest["samples"]) for index in selected):
        raise ValueError("Sample index is outside the pack.")
    comparisons = []
    for index in selected:
        case = manifest["samples"][index]
        expected = np.fromfile(pack / case["expected_file"], dtype=np.int8)
        path = outputs / (case["sample"] + ".bin")
        actual = np.fromfile(path, dtype=np.int8)
        if len(expected) != manifest["output"]["bytes"] or len(actual) != len(expected):
            raise ValueError(f"Expected {manifest['output']['bytes']} output bytes: {path}")
        comparisons.append({"sample": case["sample"], "true_id": case["true_id"], **compare_output(expected, actual)})
    return {"hash_checked_files": checked, "samples": len(comparisons), "exact_samples": sum(row["exact"] for row in comparisons),
            "top1_matches": sum(row["top1_matches"] for row in comparisons), "max_abs_lsb": max(row["max_abs_lsb"] for row in comparisons),
            "source": "files supplied via --outputs; hardware execution provenance is not verified by this tool", "comparisons": comparisons}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    build_parser = commands.add_parser("build")
    build_parser.add_argument("--compile-dir", default="runs/e84_u55_224")
    build_parser.add_argument("--manifest", default="data/nb_tcm_chm/manifest.json")
    build_parser.add_argument("--output", default="runs/e84_u55_224/static_validation")
    build_parser.add_argument("--seed", type=int, default=42)
    build_parser.add_argument("--resume", action="store_true")
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--pack", default="runs/e84_u55_224/static_validation")
    verify_parser.add_argument("--outputs", required=True)
    verify_parser.add_argument("--sample", type=int)
    verify_parser.add_argument("--report")
    args = parser.parse_args()
    if args.action == "build":
        for name, value in {"TF_CPP_MIN_LOG_LEVEL": "2", "TF_ENABLE_ONEDNN_OPTS": "0", "CUDA_VISIBLE_DEVICES": "-1",
                            "TF_NUM_INTRAOP_THREADS": "4", "TF_NUM_INTEROP_THREADS": "1"}.items():
            os.environ.setdefault(name, value)
        build(args.compile_dir, args.manifest, args.output, args.seed, args.resume)
    else:
        result = verify(args.pack, args.outputs, args.sample)
        if args.report:
            write_json(args.report, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
