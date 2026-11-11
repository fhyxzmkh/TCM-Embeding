"""Summarize the three predefined experiments; choose a candidate using validation only."""

import argparse
import csv
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from .evaluate import setup_fonts

EXPERIMENTS = [
    ("baseline", "基线160", "runs/mobilenet_v2_nb_tcm_chm_baseline"),
    ("resolution", "仅分辨率224", "runs/mobilenet_v2_nb_tcm_chm_224"),
    ("supplement", "仅补数据160", "runs/mobilenet_v2_nb_chm100_160_curated"),
]


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def verify_comparison(results):
    """Refuse mixed checkpoints, moved NB records, or unequal evaluation samples."""
    baseline = results[0]
    base_manifest = read_json(Path(baseline["directory"]) / "manifest.json")
    frozen_fields = ("id", "path", "label", "split", "group", "sha256", "pixel_sha256")
    frozen_nb = {record["id"]: tuple(record[key] for key in frozen_fields) for record in base_manifest["records"]}
    reference_samples = {}
    training_keys = ("epochs", "warmup_epochs", "patience", "batch_size", "workers", "backbone_lr", "head_lr",
                     "seed", "architecture", "width_multiplier", "pretrained")
    for result in results:
        directory = Path(result["directory"])
        manifest_path = directory / "manifest.json"
        manifest = read_json(manifest_path)
        if manifest["labels"] != base_manifest["labels"]:
            raise ValueError(f"Label order changed in {directory}")
        records = {record["id"]: record for record in manifest["records"]}
        for record_id, values in frozen_nb.items():
            if record_id not in records or tuple(records[record_id][key] for key in frozen_fields) != values:
                raise ValueError(f"Frozen NB record changed: {directory}/{record_id}")
        config = result["config"]
        if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != config["manifest_sha256"]:
            raise ValueError(f"Training manifest snapshot changed in {directory}")
        if any(config[key] != baseline["config"][key] for key in training_keys):
            raise ValueError(f"Training settings differ beyond the planned factor in {directory}")
        checkpoint_hash = hashlib.sha256((directory / "best.pt").read_bytes()).hexdigest()
        for split in ("test", "external", "chm_valid", "chm_test"):
            metrics = result["metrics"][split]
            if metrics["checkpoint_epoch"] != result["training"]["best_epoch"] or metrics["checkpoint_sha256"] != checkpoint_hash:
                raise ValueError(f"Stale checkpoint evaluation in {directory}/{split}")
            with (directory / f"{split}_predictions.csv").open(encoding="utf-8-sig", newline="") as stream:
                samples = [(row["path"], int(row["true_id"])) for row in csv.DictReader(stream)]
            if len(samples) != metrics["samples"] or len(set(samples)) != len(samples):
                raise ValueError(f"Invalid sample inventory in {directory}/{split}")
            if split in reference_samples and samples != reference_samples[split]:
                raise ValueError(f"Evaluation samples differ in {directory}/{split}")
            reference_samples[split] = samples


def compare(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    results, table = [], []
    for key, name, directory in EXPERIMENTS:
        directory = Path(directory).resolve()
        metrics = read_json(directory / "metrics.json")
        training = read_json(directory / "training_summary.json")
        config = read_json(directory / "config.json")
        for split in ("test", "external", "chm_test"):
            if metrics[split]["checkpoint_epoch"] != training["best_epoch"]:
                raise ValueError(f"Stale evaluation in {directory}/{split}")
        results.append({"key": key, "name": name, "directory": str(directory), "config": config, "training": training, "metrics": metrics})
        table.append({"experiment": key, "image_size": config["image_size"], "best_epoch": training["best_epoch"],
                      "val_macro_f1": training["best_val_macro_f1"],
                      "nb_test_top1": metrics["test"]["top1_accuracy"], "nb_test_macro_f1": metrics["test"]["macro_f1"],
                      "phone_top1": metrics["external"]["top1_accuracy"], "phone_top3": metrics["external"]["top3_accuracy"],
                      "phone_macro_f1": metrics["external"]["macro_f1"],
                      "chm_test_samples": metrics["chm_test"]["samples"], "chm_test_top1": metrics["chm_test"]["top1_accuracy"],
                      "peak_cuda_allocated_mib": training["peak_cuda_allocated_mib"], "training_seconds": training["training_seconds"]})
    verify_comparison(results)
    # This ranking is deliberately independent of the repeatedly observed phone set.
    selected = max(results, key=lambda result: result["training"]["best_val_macro_f1"])
    summary = {"selection_rule": "highest original NB validation macro F1; phone results are diagnostic comparisons, not selection criteria",
               "selected_by_validation": selected["key"], "selected_checkpoint": str(Path(selected["directory"]) / "best.pt"),
               "experiments": results, "table": table}
    (output / "comparison.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output / "comparison.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(table[0]))
        writer.writeheader()
        writer.writerows(table)
    setup_fonts()
    x, width = np.arange(len(results)), 0.25
    figure, axes = plt.subplots(figsize=(10, 5))
    for offset, (field, title, color) in enumerate([
        ("nb_test_top1", "NB网络测试 Top-1", "#527d9a"),
        ("phone_top1", "固定手机对比集 Top-1", "#bf6d45"),
        ("phone_top3", "固定手机对比集 Top-3", "#74956c"),
    ]):
        bars = axes.bar(x + (offset - 1) * width, [row[field] * 100 for row in table], width, label=title, color=color)
        axes.bar_label(bars, fmt="%.2f", fontsize=9, padding=3)
    axes.set(xticks=x, xticklabels=[result["name"] for result in results], ylim=(0, 110), ylabel="准确率（%）",
             title="MobileNetV2：分辨率与公开训练数据补充的独立对照")
    axes.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=3, frameon=False)
    axes.grid(axis="y", alpha=0.15)
    axes.set_axisbelow(True)
    figure.tight_layout()
    figure.savefig(output / "accuracy_comparison.png", dpi=160, bbox_inches="tight")
    plt.close(figure)
    audit = read_json("data/nb_chm100_curated/audit.json")
    by_key = {result["key"]: result for result in results}
    base_phone = by_key["baseline"]["metrics"]["external"]
    resolution_phone = by_key["resolution"]["metrics"]["external"]
    supplement_phone = by_key["supplement"]["metrics"]["external"]
    selected_checkpoint = (Path(selected["directory"]) / "best.pt").as_posix()
    report = [
        "# 第二轮：分辨率与 CHM-100 补充数据对照", "", "日期：2026-10-21。", "",
        f"本次按原NB验证指标选择 **{selected['name']}**。224组在固定手机对比集上的Top-1由{base_phone['top1_accuracy']:.2%}提高到{resolution_phone['top1_accuracy']:.2%}，增加{(resolution_phone['top1_accuracy'] - base_phone['top1_accuracy'])*100:.2f}个百分点、多识别正确{base_phone['errors'] - resolution_phone['errors']}张；加入CHM训练图的160组为{supplement_phone['top1_accuracy']:.2%}，本次未带来整体收益。每组只有一次运行，这不是对公开数据补充普遍无效的结论。", "",
        "## 固定协议", "",
        "三个实验均为 MobileNetV2 1.0、ImageNet 预训练、seed=42、batch=64、两轮分类头预热后全网微调，最多20轮；按同一NB验证集macro F1选最佳权重。仅224组改变输入分辨率；仅补数据组保持160输入，增加146张经过审计的CHM训练图。补数据后每轮更新步数增加，未做严格等更新步数控制。", "",
        "原NB验证515张、内部测试512张、手机图400张保持原图片、标签、分组和划分。手机图已在上一轮用于错误诊断，本轮应称固定对比集，不能当作新的盲测。没有把这些图片放入训练或依据其结果选择权重。", "",
        "## 对照结果", "",
        "| 实验 | 最佳轮次 | 验证 Macro F1 | NB网络 Top-1 | 手机 Top-1 | 手机 Top-3 | 手机 Macro F1 | CHM三类 Top-1（25张） |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result, row in zip(results, table):
        report.append(f"| {result['name']} | {row['best_epoch']} | {row['val_macro_f1']:.2%} | {row['nb_test_top1']:.2%} | {row['phone_top1']:.2%} | {row['phone_top3']:.2%} | {row['phone_macro_f1']:.2%} | {row['chm_test_top1']:.2%} |")
    report += ["", f"推荐权重：[best.pt]({selected_checkpoint})。这表示它在当前验证协议下占优，不等于已经满足实物识别部署要求。", "",
               f"![实验结果]({(output / 'accuracy_comparison.png').as_posix()})", "", "## 数据审计", "",
               "CHM-100解包实测6,556张、100类，全部可读；官方划分为train 5,207、valid 649、test 700。作者表中只与当前20类重合栀子、连翘、枸杞子，合计259张，没有覆盆子、草豆蔻和小茴香。", "",
               "另抽查覆盆子、草豆蔻、小茴香各一组网络／手机原图，可见颜色、取景尺度、数量和背景差异；这种抽样不足以认证所有药材身份，也没有据此自动修改NB标签。", "",
               "使用文件和像素哈希、pHash、SIFT检索／RANSAC几何／图像相关性复查，并人工复核246张候选联系图。两组同一摆放场景不同拍摄角度显式归组；它们不是像素级复制。原图及原标签没有改写。", "",
               f"最终决策：`{json.dumps(audit['candidate_decisions'], ensure_ascii=False)}`。与NB相关的整组27张隔离，另排除6张同组重复、31张明显文字／广告／水印／鲜果形态问题及1张身份待确认图片；训练增加146张（栀子48、连翘43、枸杞子55），NB训练总数由2,357增至2,503。", "",
               "CHM另保留23张valid与25张test，均只覆盖三类、来源仍为网络图片；近似检索不能保证穷尽所有同源图，因此不将它们描述成新的20类实拍验证集。质量筛选只针对补充训练图，没有为提高分数删除评估集中的广告或困难图片。", "",
               "具体审核依据见 `configs/chm100_quality_flags.json`；完整自动与人工决策见 `data/nb_chm100_curated/audit.json`。", "",
               "## 各类别的手机召回率", "",
               "| 类别 | 基线160 | 仅224 | 补数据160 |", "|---|---:|---:|---:|"]
    label_names = {item["name"]: item["name_zh"] for item in read_json(Path(results[0]["directory"]) / "manifest.json")["labels"]}
    for label in ("Rubi Fructus", "Alpinia katsumadai Hayata", "Foeniculi Fructus", "Gardeniae Fructus", "Forsythiae Fructus", "Lycii Fructus"):
        chinese = label_names[label]
        values = [result["metrics"]["external"]["classification_report"][label]["recall"] for result in results]
        report.append(f"| {chinese} | {values[0]:.0%} | {values[1]:.0%} | {values[2]:.0%} |")
    report += ["", "224组的小茴香改善明显，但覆盆子与草豆蔻仍是薄弱类别，栀子还出现下降。当前总体提高不代表每一类都提高；后续应优先补齐薄弱类别的可靠数据，并保留逐类评估。", ""]
    report += ["", "## 运行资源", "", "| 实验 | GPU峰值已分配内存 | 训练秒数 |", "|---|---:|---:|"]
    for result, row in zip(results, table):
        report.append(f"| {result['name']} | {row['peak_cuda_allocated_mib']:.1f} MiB | {row['training_seconds']:.1f} |")
    report += ["", "时间包含首次CUDA算法选择、数据加载等影响，仅记录本机本次运行，不能当成严格的速度基准。GPU训练内存不是E84推理内存。", "",
               "本轮为局部图像复查新增OpenCV 5.0.0.93，使用--no-deps安装；原PyTorch与NumPy版本未改变。自动检查涵盖类别对齐、分组隔离、冻结NB划分、裁剪同源图匹配、共享标题误匹配防护和评估来源校验。", "",
               "这轮只检验两个因素，每组仅运行seed=42一次，没有多种子方差或显著性检验；不把少量三类补充的结果外推到所有药材。所有候选仍是20类闭集分类，尚未量化或在E84上测试。", "",
               "## 使用与后续", "", "先以224模型作为本机演示候选，160基线保留用于后续比较板端精度、内存和耗时。下一阶段需要检查目标工具链的INT8转换兼容性，并用训练图片校准后重新评估。", "",
               "```powershell", "conda activate common",
               f"python -m tcm_herb.predict --checkpoint {Path(selected['directory']).relative_to(Path.cwd()).as_posix()}/best.pt --image '你的药材照片.jpg'",
               "```", "",
               "各运行目录的 `export/mobilenet_v2_fp32.pt2` 为电脑端FP32导出；已验证随机输入与原模型数值一致。推荐模型另有 `export/image_smoke.json`，检查原图与缓存预处理以及真实图片的导出一致性。"]
    report_path = Path("docs/round2-results.md")
    report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps({"selected_by_validation": selected["key"], "table": table}, ensure_ascii=False, indent=2), flush=True)
    print(report_path.resolve(), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="runs/round2_comparison")
    compare(parser.parse_args().output)


if __name__ == "__main__":
    main()
