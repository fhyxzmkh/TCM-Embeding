"""Create full-integer TFLite MobileNetV2 and compare fixed desktop evaluations."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import random
import sys
import time

import numpy as np
from PIL import Image
import torch
from torch.utils.data import DataLoader
from sklearn.metrics import accuracy_score, classification_report, f1_score, top_k_accuracy_score

from .data import validate_manifest
from .lite_model import LitePredictor, build_tf_function, inspect_model
from .model import HerbDataset, image_transform, load_checkpoint, preprocessing_metadata

DEFAULT_CHECKPOINT = "runs/mobilenet_v2_nb_tcm_chm_224/best.pt"
MODEL_FILES = {"lite_fp32": "mobilenet_v2_224_fp32.tflite", "int8": "mobilenet_v2_224_int8.tflite"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def select_calibration(records, classes, per_class=20, seed=42):
    """Balanced train-only sampling with at most one member of each group."""
    if per_class < 1:
        raise ValueError("Calibration quota must be positive.")
    groups = defaultdict(lambda: defaultdict(list))
    heldout_groups = {record["group"] for record in records if record["split"] in {"val", "test", "external"}}
    for record in records:
        if record["split"] == "train":
            if record["group"] in heldout_groups:
                raise ValueError("A training group overlaps a held-out split.")
            groups[record["label"]][record["group"]].append(record)
    rng, selected = random.Random(seed), []
    for label in range(classes):
        candidates = sorted(groups[label], key=str)
        if len(candidates) < per_class:
            raise ValueError(f"Class {label} has fewer than {per_class} training groups.")
        rng.shuffle(candidates)
        selected += [min(groups[label][group], key=lambda record: str(record["id"])) for group in candidates[:per_class]]
    if len({record["group"] for record in selected}) != len(selected):
        raise ValueError("A calibration group occurs more than once, possibly under conflicting labels.")
    return selected


def load_input(record, manifest_path, image_size):
    with Image.open(manifest_path.parent / record["cache"]) as image:
        tensor = image_transform(image_size)(image.convert("RGB"))
    return np.ascontiguousarray(tensor.numpy().transpose(1, 2, 0)[None])


def setup(output, checkpoint_path, manifest_path, checkpoint, per_class, seed, resume):
    manifest = read_json(manifest_path)
    validate_manifest(manifest["records"], len(manifest["labels"]))
    if checkpoint["image_size"] != 224 or checkpoint["labels"] != manifest["labels"]:
        raise ValueError("This experiment requires the fixed 224 model and identical label order.")
    manifest_hash = sha256(manifest_path)
    if manifest_hash != checkpoint["manifest_sha256"]:
        raise ValueError("Manifest differs from the checkpoint's training contract.")
    config = {"checkpoint": str(checkpoint_path), "checkpoint_sha256": sha256(checkpoint_path),
              "checkpoint_epoch": checkpoint["epoch"], "manifest": str(manifest_path), "manifest_sha256": manifest_hash,
              "image_size": 224, "per_class": per_class, "seed": seed,
              "recipe": "static PTQ, per-channel weights, full INT8 input/activations/output; training-only calibration",
              "mapping": "exact torchvision inference graph with eval Conv-BN folding; explicit symmetric padding; NHWC",
              "validation_top1_loss_budget": 0.02}
    selected = select_calibration(manifest["records"], len(manifest["labels"]), per_class, seed)
    if output.exists():
        if not resume:
            raise FileExistsError(f"Use a fresh output directory or --resume for this fixed contract: {output}")
        if read_json(output / "config.json") != config or sha256(output / "manifest.json") != manifest_hash:
            raise ValueError("Resume contract does not match the frozen experiment.")
        previous = read_json(output / "calibration.json")
        if [row["id"] for row in previous["records"]] != [row["id"] for row in selected]:
            raise ValueError("Calibration selection changed.")
    else:
        output.mkdir(parents=True)
        write_json(output / "config.json", config)
        (output / "manifest.json").write_bytes(manifest_path.read_bytes())
        write_json(output / "labels.json", manifest["labels"])
        write_json(output / "calibration.json", {
            "source_split": "train", "samples": len(selected), "unique_groups": len(selected), "seed": seed,
            "manifest_sha256": manifest_hash, "per_class": dict(Counter(row["label"] for row in selected)),
            "records": selected,
        })
    return manifest, selected, config


def convert(model, checkpoint, manifest_path, selected, output):
    import tensorflow as tf

    print("Mapping the frozen torchvision inference graph into TensorFlow constants ...", flush=True)
    function, mapping = build_tf_function(model, checkpoint["image_size"])
    write_json(output / "weight_mapping.json", mapping)
    converter = tf.lite.TFLiteConverter.from_concrete_functions([function])
    converter.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS]
    (output / MODEL_FILES["lite_fp32"]).write_bytes(converter.convert())
    floating = LitePredictor(output / MODEL_FILES["lite_fp32"])
    checks = []
    # One calibration sample per class, before exposing any evaluation outcomes.
    for label in range(len(checkpoint["labels"])):
        record = next(row for row in selected if row["label"] == label)
        inputs = load_input(record, manifest_path, checkpoint["image_size"])
        with torch.inference_mode():
            expected = model(torch.from_numpy(inputs.transpose(0, 3, 1, 2).copy())).numpy()
        tf_output, lite_output = function(tf.constant(inputs)).numpy(), floating(inputs)[0]
        np.testing.assert_allclose(tf_output, expected, atol=5e-4, rtol=1e-4)
        np.testing.assert_allclose(lite_output, expected, atol=5e-4, rtol=1e-4)
        checks.append({"id": record["id"], "label": label, "tf_max_error": float(np.max(np.abs(tf_output - expected))),
                       "tflite_max_error": float(np.max(np.abs(lite_output - expected)))})
    write_json(output / "conversion_smoke.json", checks)
    print(f"FP32 parity passed on {len(checks)} training samples; calibrating {len(selected)} train images ...", flush=True)

    def representative_data():
        for index, record in enumerate(selected):
            if index % 50 == 0:
                print(f"  calibration {index}/{len(selected)}", flush=True)
            yield [load_input(record, manifest_path, checkpoint["image_size"])]

    converter = tf.lite.TFLiteConverter.from_concrete_functions([function])
    converter.optimizations = [tf.lite.Optimize.DEFAULT]
    converter.representative_dataset = representative_data
    converter.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
    converter.inference_input_type, converter.inference_output_type = tf.int8, tf.int8
    (output / MODEL_FILES["int8"]).write_bytes(converter.convert())
    artifacts = {key: inspect_model(output / name, require_int8=key == "int8") for key, name in MODEL_FILES.items()}
    conversion = {
        "date": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "tool_versions": {package: metadata.version(package) for package in ["tensorflow-cpu", "torch", "torchvision", "numpy", "Pillow"]},
        "python": sys.executable, "artifacts": artifacts, "labels": checkpoint["labels"],
        "preprocessing": {**preprocessing_metadata(checkpoint["image_size"], checkpoint["cache_size"]), "layout": "NHWC"},
        "quantization_formula": "q = clip(round(normalized_RGB / input_scale) + input_zero_point, -128, 127); logits = (q_out - output_zero_point) * output_scale",
        "rounding": "numpy.rint (round-to-nearest, ties-to-even), clip before int8 cast",
        "calibration_sha256": sha256(output / "calibration.json"),
        "source_checkpoint_sha256": read_json(output / "config.json")["checkpoint_sha256"],
        "limitations": "Desktop TFLite CPU only. Not DEEPCRAFT/Vela compiled, no U55 offload, arena memory or E84 latency measured.",
    }
    write_json(output / "conversion.json", conversion)
    print(json.dumps({"conversion": "passed", "artifacts": {key: {"bytes": value["bytes"], "tensors": value["tensor_types"]} for key, value in artifacts.items()}}, indent=2), flush=True)
    return conversion


def summarize_logits(targets, logits, labels):
    classes = list(range(len(labels)))
    predicted = logits.argmax(1)
    return {"samples": len(targets), "top1_accuracy": accuracy_score(targets, predicted),
            "top3_accuracy": top_k_accuracy_score(targets, logits, k=3, labels=classes),
            "macro_f1": f1_score(targets, predicted, labels=classes, average="macro", zero_division=0),
            "errors": int((predicted != targets).sum()),
            "classification_report": classification_report(targets, predicted, labels=classes,
                target_names=[label["name"] for label in labels], output_dict=True, zero_division=0)}


def evaluate(model, checkpoint, manifest_path, output, splits, conversion):
    from .evaluate import plot_results, write_csv

    config = read_json(output / "config.json")
    if config["checkpoint_sha256"] != conversion["source_checkpoint_sha256"]:
        raise ValueError("Converted model/checkpoint lineage changed.")
    for key, name in MODEL_FILES.items():
        if sha256(output / name) != conversion["artifacts"][key]["sha256"]:
            raise ValueError("A converted model changed since inspection.")
    if sha256(output / "calibration.json") != conversion["calibration_sha256"]:
        raise ValueError("Calibration inventory changed.")
    predictors = {key: LitePredictor(output / name) for key, name in MODEL_FILES.items()}
    metrics_path = output / "metrics.json"
    all_metrics = read_json(metrics_path) if metrics_path.exists() else {}
    for split in splits:
        print(f"EVALUATE {split}: frozen PyTorch FP32 / TFLite FP32 / TFLite INT8", flush=True)
        data = HerbDataset(manifest_path, split, checkpoint["image_size"])
        started = time.perf_counter()
        with torch.inference_mode():
            original = np.concatenate([model(images).numpy() for images, _, _ in DataLoader(data, batch_size=32, shuffle=False, num_workers=0)])
        outputs, raw_int8, clipped, total_pixels = defaultdict(list), [], 0, 0
        for index in range(len(data)):
            tensor, _, _ = data[index]
            inputs = np.ascontiguousarray(tensor.numpy().transpose(1, 2, 0)[None])
            for key, predictor in predictors.items():
                actual, raw = predictor(inputs)
                outputs[key].append(actual[0])
                if key == "int8":
                    raw_int8.append(raw[0])
                    scale, zero_point = predictor.input["quantization"]
                    rounded = np.rint(inputs / scale) + zero_point
                    clipped += int(np.count_nonzero((rounded < -128) | (rounded > 127)))
                    total_pixels += inputs.size
            if (index + 1) % 100 == 0 or index + 1 == len(data):
                print(f"  {split}: {index+1}/{len(data)}", flush=True)
        logits = {"pytorch_fp32": original, **{key: np.stack(value) for key, value in outputs.items()}}
        np.testing.assert_allclose(logits["lite_fp32"], original, atol=5e-4, rtol=1e-4)
        targets = np.array([record["label"] for record in data.records])
        metrics = {key: summarize_logits(targets, value, checkpoint["labels"]) for key, value in logits.items()}
        metrics["comparison"] = {
            "fp32_conversion_max_absolute_logit_error": float(np.max(np.abs(logits["lite_fp32"] - original))),
            "fp32_conversion_top1_disagreements": int(np.count_nonzero(logits["lite_fp32"].argmax(1) != original.argmax(1))),
            "int8_top1_disagreements": int(np.count_nonzero(logits["int8"].argmax(1) != original.argmax(1))),
            "top1_loss_percentage_points": 100 * (metrics["pytorch_fp32"]["top1_accuracy"] - metrics["int8"]["top1_accuracy"]),
            "macro_f1_loss_percentage_points": 100 * (metrics["pytorch_fp32"]["macro_f1"] - metrics["int8"]["macro_f1"]),
            "input_clipped_values": clipped, "input_values": total_pixels,
            "evaluation_manifest_sha256": sha256(manifest_path), "source_checkpoint_sha256": config["checkpoint_sha256"],
            "evaluation_seconds": time.perf_counter() - started,
        }
        np.savez_compressed(output / f"{split}_logits.npz", targets=targets, paths=np.array([r["path"] for r in data.records]),
                            raw_int8=np.stack(raw_int8), **logits)
        rows = []
        for index, record in enumerate(data.records):
            top = np.argsort(logits["int8"][index], kind="stable")[-3:][::-1]
            row = {"path": record["path"], "true_id": int(targets[index]), "true_name": checkpoint["labels"][targets[index]]["name_zh"]}
            for key, value in logits.items():
                prediction = int(value[index].argmax())
                row[key + "_id"] = prediction
                row[key + "_correct"] = bool(prediction == targets[index])
            row["int8_top3"] = " | ".join(checkpoint["labels"][label]["name_zh"] for label in top)
            rows.append(row)
        write_csv(output / f"{split}_predictions.csv", rows)
        write_csv(output / f"{split}_per_class.csv", [
            {"id": label["id"], "name_zh": label["name_zh"],
             **{f"{key}_{field}": metrics[key]["classification_report"][label["name"]][field]
                for key in logits for field in ("precision", "recall", "f1-score", "support")}}
            for label in checkpoint["labels"]
        ])
        shifted = logits["int8"] - logits["int8"].max(1, keepdims=True)
        probabilities = np.exp(shifted) / np.exp(shifted).sum(1, keepdims=True)
        plot_results(output, f"int8_{split}", data, targets, logits["int8"].argmax(1), probabilities, checkpoint["labels"])
        all_metrics[split] = metrics
        write_json(metrics_path, all_metrics)
        print(json.dumps({"split": split, **{key: {field: value[field] for field in ("top1_accuracy", "top3_accuracy", "macro_f1")} for key, value in metrics.items() if key != "comparison"}}, indent=2), flush=True)
    return all_metrics


def verify_outputs(output, config, conversion, metrics):
    """Check that the report refers to the frozen artifacts and actual saved outputs."""
    if sha256(config["checkpoint"]) != config["checkpoint_sha256"] or sha256(output / "manifest.json") != config["manifest_sha256"]:
        raise ValueError("Source checkpoint or manifest changed.")
    if sha256(output / "calibration.json") != conversion["calibration_sha256"]:
        raise ValueError("Calibration metadata changed after conversion.")
    for key, filename in MODEL_FILES.items():
        if sha256(output / filename) != conversion["artifacts"][key]["sha256"]:
            raise ValueError("A converted model changed after evaluation.")
    manifest, calibration = read_json(output / "manifest.json"), read_json(output / "calibration.json")
    source_records = {record["id"]: record for record in manifest["records"]}
    if any(source_records.get(row["id"]) != row or row["split"] != "train" for row in calibration["records"]):
        raise ValueError("Calibration inventory is not an unchanged subset of training data.")
    if len({row["group"] for row in calibration["records"]}) != calibration["samples"]:
        raise ValueError("Calibration groups are not unique.")
    output_spec = conversion["artifacts"]["int8"]["interfaces"][0]["outputs"][0]
    scale, zero_point = output_spec["scales"][0], output_spec["zero_points"][0]
    checks = {}
    prior = read_json(Path(config["checkpoint"]).parent / "metrics.json")
    for split in ("val", "test", "external"):
        value = metrics[split]
        if value["comparison"]["evaluation_manifest_sha256"] != config["manifest_sha256"] or value["comparison"]["source_checkpoint_sha256"] != config["checkpoint_sha256"]:
            raise ValueError("Evaluation source lineage changed.")
        expected = [row for row in manifest["records"] if row["split"] == split]
        with np.load(output / f"{split}_logits.npz", allow_pickle=False) as arrays:
            if arrays["paths"].tolist() != [row["path"] for row in expected]:
                raise ValueError("Evaluation image order changed.")
            np.testing.assert_array_equal(arrays["targets"], [row["label"] for row in expected])
            if arrays["raw_int8"].dtype != np.int8:
                raise ValueError("Saved outputs are not native INT8 values.")
            np.testing.assert_array_equal(arrays["int8"], (arrays["raw_int8"].astype(np.float32) - zero_point) * scale)
            for key in ("pytorch_fp32", "lite_fp32", "int8"):
                recomputed = summarize_logits(arrays["targets"], arrays[key], manifest["labels"])
                for field in ("samples", "top1_accuracy", "top3_accuracy", "macro_f1", "errors"):
                    if recomputed[field] != value[key][field]:
                        raise ValueError(f"Metric/logit mismatch: {split}/{key}/{field}")
            original_correct = arrays["pytorch_fp32"].argmax(1) == arrays["targets"]
            integer_correct = arrays["int8"].argmax(1) == arrays["targets"]
            checks[split] = {"samples": len(expected), "int8_fixed_errors": int(np.count_nonzero(~original_correct & integer_correct)),
                             "int8_introduced_errors": int(np.count_nonzero(original_correct & ~integer_correct))}
        if split in prior:
            checks[split]["reproduces_previous_fp32_metrics"] = all(
                value["pytorch_fp32"][field] == prior[split][field] for field in ("top1_accuracy", "top3_accuracy", "macro_f1"))
    result = {"source_and_model_hashes_verified": True, "calibration_train_only_unique_groups": True,
              "saved_outputs_and_metrics_verified": True, "checks": checks}
    write_json(output / "consistency.json", result)
    return result


def report(output, config, conversion):
    metrics = read_json(output / "metrics.json")
    consistency = verify_outputs(output, config, conversion, metrics)
    calibration = read_json(output / "calibration.json")
    fp32, int8 = conversion["artifacts"]["lite_fp32"], conversion["artifacts"]["int8"]
    passed = metrics["val"]["comparison"]["top1_loss_percentage_points"] <= config["validation_top1_loss_budget"] * 100
    summary = {"validation_within_2pp_target": passed, "fp32_tflite_bytes": fp32["bytes"], "int8_tflite_bytes": int8["bytes"],
               "file_size_ratio": fp32["bytes"] / int8["bytes"], "full_integer_verified": int8["full_integer_verified"],
               "config": config, "metrics": metrics, "consistency": consistency, "status": conversion["limitations"]}
    write_json(output / "summary.json", summary)
    lines = ["# 224 模型：全 INT8 量化与电脑端精度对照", "", "日期：2026-10-21（Asia/Taipei）。", "",
             f"验证集Top-1下降不超过2个百分点的工程目标：**{'达到' if passed else '未达到'}**。模型文件从同格式FP32的{fp32['bytes']/1048576:.2f} MiB缩至INT8的{int8['bytes']/1048576:.2f} MiB（{summary['file_size_ratio']:.2f}倍）。", "",
             "## 固定对照", "", "| 数据划分 | 张数 | 原PyTorch FP32 Top-1 | TFLite FP32 Top-1 | INT8 Top-1 | INT8 Top-3 | INT8 Macro F1 | Top-1下降（百分点） |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    titles = {"val": "NB验证", "test": "NB网络测试", "external": "固定手机对比集"}
    for split in ("val", "test", "external"):
        value = metrics[split]
        lines.append(f"| {titles[split]} | {value['int8']['samples']} | {value['pytorch_fp32']['top1_accuracy']:.2%} | {value['lite_fp32']['top1_accuracy']:.2%} | {value['int8']['top1_accuracy']:.2%} | {value['int8']['top3_accuracy']:.2%} | {value['int8']['macro_f1']:.2%} | {value['comparison']['top1_loss_percentage_points']:+.2f} |")
    max_error = max(value["comparison"]["fp32_conversion_max_absolute_logit_error"] for value in metrics.values())
    disagreements = sum(value["comparison"]["fp32_conversion_top1_disagreements"] for value in metrics.values())
    lines += ["", f"已逐图检查转换前后FP32 logits：按atol=5e-4、rtol=1e-4通过，最大绝对差{max_error:.8f}，Top-1不同{disagreements}张。表中FP32来自本轮在CPU重新评估，三者使用完全相同的图片、标签顺序和预处理。负的“下降”表示准确率提高。", ""]
    boundary_path = output / "backend_boundary_check.json"
    affected = [split for split in ("test", "external") if not consistency["checks"][split].get("reproduces_previous_fp32_metrics", True)]
    if affected and boundary_path.exists():
        boundary = read_json(boundary_path)
        prior = read_json(Path(config["checkpoint"]).parent / "metrics.json")
        descriptions = [f"{titles[split]}CPU {metrics[split]['pytorch_fp32']['top1_accuracy']:.2%}／前轮GPU {prior[split]['top1_accuracy']:.2%}" for split in affected]
        if not all(row["modes"]["gpu_default"]["prediction"] == row["prior_gpu_id"] and row["modes"]["gpu_without_tf32"]["prediction"] == row["current_cpu_id"] for row in boundary["checks"]):
            raise ValueError("The claimed backend explanation is not supported by the diagnostic outputs.")
        strict_error = max(row["modes"]["gpu_without_tf32"]["max_error_vs_cpu"] for row in boundary["checks"])
        lines += [f"本轮CPU FP32与前轮GPU存在少量边界预测差异：{'；'.join(descriptions)}。已逐一复现{len(boundary['checks'])}张差异样本：GPU默认允许cuDNN TF32时复现旧预测，关闭TF32后与CPU预测一致，严格GPU与CPU logits最大差{strict_error:.8f}。权重、图片及标签均未变化。量化精度变化按本轮相同CPU参考计算；复核细节见 `backend_boundary_check.json`。", ""]
    lines += ["手机图片在前轮已用于错误诊断，此处仅是固定对比集；没有用于校准或量化参数选择。每种精度只运行本次固定方案，没有依据手机成绩反复尝试配方。", "",
              "## 校准与转换", "",
              f"校准仅使用原NB训练集{calibration['samples']}张，每类{config['per_class']}张、每个重复组至多一张，seed={config['seed']}。验证／测试／手机图片不参与校准。抽样清单、原图哈希和分组保存在 `calibration.json`，原始数据清单与权重保持不变。", "",
              "本机未发现DEEPCRAFT转换器、Docker/Podman或可用WSL发行版。使用项目内 `.venv-int8`，继承common的训练依赖，只在该虚拟环境安装TensorFlow CPU；没有升级common的PyTorch或NumPy。具体版本见 `conversion.json`。", "",
              "转换采用现有torchvision MobileNetV2权重的显式图映射：Conv-BN按推理公式折叠，卷积核布局转为NHWC，保持原始对称padding、ReLU6、残差相加、全局平均池化与20类线性头。没有改用Keras预训练权重或重新训练。转换先通过20张训练图的FP32检查，再做400张训练图校准。", "",
              "量化使用TFLite静态PTQ，启用TFLITE_BUILTINS_INT8并强制INT8输入和输出。检查FlatBuffer全部子图：无浮点张量、无Flex／Custom算子；卷积、深度卷积和全连接权重均为INT8，偏置／形状辅助张量可为INT32。详细算子及版本、逐通道权重scale数量在 `conversion.json`。", "",
              "输入仍需按原模型做RGB、方向校正、白边填充320、缩放224与ImageNet归一化，随后转为NHWC，再按模型的scale与zero_point量化；输出先反量化为logits。量化式为 `clip(round(x/scale)+zero_point,-128,127)`，禁止直接强制转int8导致溢出。", "",
              "## 手机图各类召回率", "", "| 类别 | FP32 | INT8 |", "|---|---:|---:|"]
    for label in conversion["labels"]:
        left = metrics["external"]["pytorch_fp32"]["classification_report"][label["name"]]["recall"]
        right = metrics["external"]["int8"]["classification_report"][label["name"]]["recall"]
        lines.append(f"| {label['name_zh']} | {left:.0%} | {right:.0%} |")
    lines += ["", "## 文件与边界", "",
              f"- [INT8模型]({(output / MODEL_FILES['int8']).as_posix()})", f"- [同格式FP32模型]({(output / MODEL_FILES['lite_fp32']).as_posix()})",
              f"- [完整指标与来源]({(output / 'summary.json').as_posix()})", "",
              "`*_predictions.csv`、`*_per_class.csv`、`*_logits.npz`保存逐图结果、逐类对照及原始输出；`int8_*`图片保存混淆矩阵与错误示例。", "",
              "本次使用电脑端TFLite原生CPU内核、关闭默认delegate。INT8输出的有限分辨率可能产生分数并列；Top-1使用首个最大值，Top-3沿用sklearn高类别索引优先的并列规则。分数仍不是校准后的置信度，未知药材拒识尚未实现。", "",
              "模型文件大小不是板端运行内存。本次没有进行DEEPCRAFT/Vela编译，未测U55算子覆盖、tensor arena、板端耗时；不能据电脑结果宣称已能在E84 NPU运行。", "",
              "转换参考：[Google全整数PTQ](https://developers.google.com/edge/litert/conversion/tensorflow/quantization/post_training_integer_quant)、[Infineon支持格式](https://developer.imagimob.com/deepcraft-model-converter/additional-resources/supported-model-formats)、[Infineon容器要求](https://developer.imagimob.com/deepcraft-model-converter/additional-resources/container-setup)。"]
    Path("docs/int8-results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({key: summary[key] for key in ("validation_within_2pp_target", "fp32_tflite_bytes", "int8_tflite_bytes", "file_size_ratio", "full_integer_verified")}, indent=2), flush=True)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for name, value in {"TF_CPP_MIN_LOG_LEVEL": "2", "TF_ENABLE_ONEDNN_OPTS": "0", "CUDA_VISIBLE_DEVICES": "-1",
                        "TF_NUM_INTRAOP_THREADS": "4", "TF_NUM_INTEROP_THREADS": "1"}.items():
        os.environ.setdefault(name, value)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", default=DEFAULT_CHECKPOINT)
    parser.add_argument("--manifest", default="data/nb_tcm_chm/manifest.json")
    parser.add_argument("--output", default="runs/mobilenet_v2_nb_tcm_chm_224_int8")
    parser.add_argument("--per-class", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--stage", choices=("all", "convert", "evaluate", "report"), default="all")
    parser.add_argument("--splits", nargs="+", choices=("val", "test", "external"), default=["val", "test", "external"])
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    torch.set_num_threads(4)
    checkpoint_path, manifest_path, output = Path(args.checkpoint).resolve(), Path(args.manifest).resolve(), Path(args.output).resolve()
    model, checkpoint = load_checkpoint(checkpoint_path)
    _, selected, config = setup(output, checkpoint_path, manifest_path, checkpoint, args.per_class, args.seed, args.resume)
    if args.stage in {"all", "convert"}:
        convert(model, checkpoint, manifest_path, selected, output)
    if args.stage in {"all", "evaluate"}:
        evaluate(model, checkpoint, manifest_path, output, args.splits, read_json(output / "conversion.json"))
    if args.stage in {"all", "report"}:
        report(output, config, read_json(output / "conversion.json"))


if __name__ == "__main__":
    main()
