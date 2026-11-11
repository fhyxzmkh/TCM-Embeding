"""Compile the frozen INT8 model for U55-128 and audit offline resources.

This uses an explicitly named Arm reference cost model at 400 MHz, not the
unavailable vendor PS84_M55_U55_400MHz configuration. No hardware is accessed.
"""

import argparse
from collections import Counter
import csv
import hashlib
from importlib import metadata
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import numpy as np

QUANT_RUN = Path("runs/mobilenet_v2_nb_tcm_chm_224_int8")
PROFILE = Path("configs/e84_u55_analysis.ini")
REFERENCE = Path("data/e84_reference")
LINKER = REFERENCE / "projects/Edgi_Talk_M55_DEEPCRAFT_Deploy_Vision/board/linker_scripts/link.ld"
SYSTEM = "E84_U55_400MHz_Arm_Reference"
MODES = ("Shared_Sram", "Sram_Only")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_memory_regions(text):
    pattern = r"^\s*(\w+)\s*:\s*ORIGIN\s*=\s*(0x[0-9a-fA-F]+)\s*,\s*LENGTH\s*=\s*(0x[0-9a-fA-F]+)"
    return {name: {"origin": int(origin, 16), "length": int(length, 16), "origin_hex": origin}
            for name, origin, length in re.findall(pattern, text, re.MULTILINE)}


def inspect_flatbuffer(path):
    # Use Vela's schema bindings so this audit does not depend on TensorFlow.
    from ethosu.vela.tflite.Model import Model
    from ethosu.vela.tflite.BuiltinOperator import BuiltinOperator
    from ethosu.vela.tflite.TensorType import TensorType

    blob = Path(path).read_bytes()
    model = Model.GetRootAsModel(blob, 0)
    names = {value: key for key, value in vars(BuiltinOperator).items() if isinstance(value, int)}
    types = {value: key for key, value in vars(TensorType).items() if isinstance(value, int)}
    graphs = []
    for graph_index in range(model.SubgraphsLength()):
        graph = model.Subgraphs(graph_index)
        tensors, operations = [], []
        for index in range(graph.TensorsLength()):
            tensor = graph.Tensors(index)
            quant = tensor.Quantization()
            buffer = model.Buffers(tensor.Buffer())
            values = buffer.DataAsNumpy() if buffer.DataLength() else np.array([], np.uint8)
            tensors.append({"index": index, "name": tensor.Name().decode(), "shape": tensor.ShapeAsNumpy().tolist(),
                            "dtype": types[tensor.Type()], "buffer_bytes": buffer.DataLength(),
                            "buffer_prefix_hex": values[:4].tobytes().hex(),
                            "scales": quant.ScaleAsNumpy().tolist() if quant is not None and quant.ScaleLength() else [],
                            "zero_points": quant.ZeroPointAsNumpy().tolist() if quant is not None and quant.ZeroPointLength() else []})
        for index in range(graph.OperatorsLength()):
            operation = graph.Operators(index)
            code = model.OperatorCodes(operation.OpcodeIndex())
            operations.append({"builtin": names[max(code.BuiltinCode(), code.DeprecatedBuiltinCode())],
                               "custom": code.CustomCode().decode() if code.CustomCode() else None,
                               "version": code.Version()})
        graphs.append({"inputs": [tensors[graph.Inputs(index)] for index in range(graph.InputsLength())],
                       "outputs": [tensors[graph.Outputs(index)] for index in range(graph.OutputsLength())],
                       "tensors": tensors, "operators": operations})
    return {"bytes": len(blob), "sha256": digest(path), "subgraphs": graphs}


def io_contract(info):
    return [{kind: [{key: tensor[key] for key in ("shape", "dtype", "scales", "zero_points")} for tensor in graph[kind]]
             for kind in ("inputs", "outputs")} for graph in info["subgraphs"]]


def sram_placement(mode, scratch_bytes, readonly_bytes, io_bytes, data_region_bytes):
    if mode not in MODES:
        raise ValueError(f"Unknown memory mode: {mode}")
    required_constants = readonly_bytes if mode == "Sram_Only" else 0
    minimum = scratch_bytes + required_constants
    return {"required_sram_constants_bytes": required_constants,
            "scratch_and_required_constants_bytes": minimum,
            "with_separate_external_io_bytes": minimum + io_bytes,
            "npu_buffers_fit_region_before_other_usage": minimum <= data_region_bytes}


def command(model, mode, directory):
    return [sys.executable, "-m", "ethosu.vela", str(model),
            "--accelerator-config", "ethos-u55-128", "--config", "Arm/vela.ini", "--config", str(PROFILE.resolve()),
            "--system-config", SYSTEM, "--memory-mode", mode, "--optimise", "Performance", "--cop-format", "COP1",
            "--output-dir", str(directory.resolve()), "--show-cpu-operations", "--verbose-config", "--verbose-performance", "--enable-debug-db"]


def compile_mode(model, output, mode):
    directory = output / mode
    directory.mkdir(exist_ok=True)
    args = command(model, mode, directory)
    command_path = directory / "command.json"
    compiled = directory / (model.stem + "_vela.tflite")
    if command_path.exists():
        if load_json(command_path) != args:
            raise ValueError(f"Compiler options changed; use a new output directory: {directory}")
        if not compiled.exists() or not (directory / "compiler.log").exists():
            raise ValueError(f"Incomplete previous compile in {directory}; use a new output directory.")
        print(f"Reusing {mode}; validating all output artifacts below.", flush=True)
    else:
        save_json(command_path, args)
        print(f"Compiling {mode} ...", flush=True)
        process = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        (directory / "compiler.log").write_text(process.stdout, encoding="utf-8")
        save_json(directory / "exit_status.json", {"returncode": process.returncode})
        if process.returncode != 0:
            raise RuntimeError(f"Vela failed; see {directory / 'compiler.log'}")
    return directory, compiled


def analyze_mode(model, output, mode, source_info, regions):
    directory, compiled = compile_mode(model, output, mode)
    summaries = list(directory.glob("*_summary_*.csv"))
    layers = list(directory.glob("*_per-layer.csv"))
    if len(summaries) != 1 or len(layers) != 1:
        raise ValueError("Expected exactly one summary and one operator report.")
    with summaries[0].open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != 1:
        raise ValueError("Expected one network in the compiler summary.")
    summary = rows[0]
    if summary["accelerator_configuration"] != "Ethos_U55_128" or summary["memory_mode"] != mode or float(summary["core_clock"]) != 400e6:
        raise ValueError("Compiler target differs from the E84 analysis contract.")
    with layers[0].open(encoding="utf-8-sig", newline="") as stream:
        operations = list(csv.DictReader(stream))
    compiled_info = inspect_flatbuffer(compiled)
    if io_contract(compiled_info) != io_contract(source_info):
        raise ValueError("Compiler changed external tensor shape, type or quantization parameters.")
    graph = compiled_info["subgraphs"][0]
    custom = [operation for operation in graph["operators"] if operation["custom"]]
    if any(operation["custom"] != "ethos-u" or operation["version"] != 1 for operation in custom) or not custom:
        raise ValueError("Unexpected custom operator ABI.")
    streams = [tensor for tensor in graph["tensors"] if tensor["name"] == "ethos_u_command_stream"]
    if not streams or any(tensor["buffer_prefix_hex"] != b"COP1".hex() for tensor in streams):
        raise ValueError("Expected COP1 payload for the board's existing 0.16 driver.")
    buffers = {tensor["name"]: int(np.prod(tensor["shape"])) for tensor in graph["tensors"] if tensor["dtype"] in {"INT8", "UINT8"}}
    # Vela's report is in KiB. Do not confuse this with TFLM's complete arena.
    sram_bytes = round(float(summary["sram_memory_used"]) * 1024)
    flash_bytes = round(float(summary["off_chip_flash_memory_used"]) * 1024)
    io_bytes = sum(int(np.prod(tensor["shape"])) for kind in ("inputs", "outputs") for tensor in graph[kind])
    scratch_bytes = sum(size for name, size in buffers.items() if name.startswith("scratch"))
    readonly_bytes = sum(tensor["buffer_bytes"] for tensor in graph["tensors"] if tensor["name"] == "read_only")
    targets = Counter(row["Target"] for row in operations)
    compiled_builtins = [operation for operation in graph["operators"] if not operation["custom"]]
    result = {"mode": mode, "compiled_path": str(compiled), "compiled": compiled_info,
              "native_operator_targets": dict(targets), "native_operator_count": len(operations),
              "remaining_builtin_operators": compiled_builtins, "io_contract_preserved": True,
              "cop1_verified": True, "scratch_tensor_bytes": scratch_bytes, "external_io_bytes": io_bytes,
              "separate_scratch_and_io_bytes": scratch_bytes + io_bytes,
              "readonly_buffer_bytes": readonly_bytes,
              "sram_placement": sram_placement(mode, scratch_bytes, readonly_bytes, io_bytes, regions["m55_data_secondary"]["length"]),
              "compiler_sram_bytes": sram_bytes, "compiler_flash_bytes": flash_bytes,
              "reference_cost_model_only": {"cycles": int(summary["cycles_total"]), "seconds": float(summary["inference_time"]),
                                              "warning": "Arm reference memory parameters; NOT measured Edgi-Talk latency or FPS."},
              "raw_summary": summary}
    save_json(directory / "audit.json", result)
    print(json.dumps({key: result[key] for key in ("mode", "native_operator_targets", "scratch_tensor_bytes", "compiler_sram_bytes", "compiler_flash_bytes")}), flush=True)
    return result


def write_report(output, contract, results, regions):
    shared = results[0]
    lines = ["# E84 U55-128：224 INT8 离线编译与静态验证准备", "", "日期：2026-10-22（Asia/Taipei）。", "",
             "224 INT8模型已完成两种内存布局的离线编译。优先使用Shared_Sram：权重／常量保持在外部Flash，scratch放SoCMEM。原INT8模型不改写。尚未连接开发板、生成完整固件或测量板端精度／耗时。", "",
             "## 编译结果", "", "| 布局 | 编译文件 MiB | scratch MiB | 必需驻留SRAM的只读常量 MiB | 两者下界 MiB | NPU／CPU算子 |",
             "|---|---:|---:|---:|---:|---:|"]
    for result in results:
        targets = result["native_operator_targets"]
        placement = result["sram_placement"]
        lines.append(f"| {result['mode']} | {result['compiled']['bytes']/1048576:.3f} | {result['scratch_tensor_bytes']/1048576:.3f} | {placement['required_sram_constants_bytes']/1048576:.3f} | {placement['scratch_and_required_constants_bytes']/1048576:.3f} | {targets.get('NPU',0)}／{targets.get('CPU',0)} |")
    lines += ["", "本版本Vela的summary中SRAM memory字段没有计入Sram_Only必须位于SRAM的只读权重缓冲，因此另外读取编译FlatBuffer的read_only缓冲计算驻留下界，不能只看CSV中的SRAM栏。下界还没有计外部I/O、命令流／解释器元数据、对齐或应用数据。Sram_Only的scratch加只读常量已超过当前示例M55数据窗口，需要重新分区才能考虑；这不表示整个E84永远无法使用该模式。", "",
              "算子数量来自Vela内部优化图，包含展开的ReLU6及Mean重写等，不能与原TFLite的69个算子直接作数量对比。另检查编译后的FlatBuffer：保留外部输入／输出形状、INT8类型、scale与zero_point，生成ethos-u自定义算子v1，COP1头符合所查BSP驱动的负载格式。", "",
              "## 配置依据与限制", "", "E84采用Ethos-U55-128，最高400MHz。Vela版本固定5.2.0、COP1、Performance；查阅的Edgi-Talk BSP驱动头为0.16。这里的格式检查不等于已证明完整驱动、TFLM库和硬件兼容。", "",
              "尚未取得原厂PS84_M55_U55_400MHz配置。本次使用 `configs/e84_u55_analysis.ini`，仅把Arm参考模型时钟改为400MHz，内存带宽／延迟仍是未实测的Arm参考值。命名明确区别于原厂配置。不能把原始CSV中的预测时间或FPS当成Edgi-Talk实测值；不据此承诺实时帧率。", "",
              "编译参数、有效配置和完整日志保存在各布局的 `command.json`、`compiler.log`、原始CSV与 `audit.json`。配置和输入模型哈希已固定，后续换配置应新建运行目录。", "",
              "## 板级内存核对", "", f"已固定并查阅RT-Thread BSP提交 `{contract['bsp_commit']}`。视觉示例链接脚本分配M55数据区 `{regions['m55_data_secondary']['origin_hex']}`、{regions['m55_data_secondary']['length']/1048576:.4f} MiB；这包括其他数据与heap，不是当前空闲RAM。示例M55外部Flash窗口为{regions['m55_nvm']['length']/1048576:.1f} MiB，也不是空闲容量。", "",
              f"Shared_Sram编译模型显式scratch为{shared['scratch_tensor_bytes']:,}字节，外部输入／输出另有{shared['external_io_bytes']:,}字节。若分别分配两者，合计{shared['separate_scratch_and_io_bytes']/1048576:.3f} MiB；还未计TFLM持久元数据、其他scratch请求、对齐、驱动、RTOS、LCD与摄像头负载。因此没有给出“最终tensor arena大小”或“必定装得下”的结论。", "",
              "视觉示例的 `.cy_ml_model_data` 在启动时从Flash复制到SoCMEM。如果直接沿用，它会额外占用片内RAM，与Shared_Sram“常量留Flash”的部署意图不同。集成时应为模型选择不复制到RAM的只读Flash放置方式，并核对最终map、MPU／SAU、AXI区域和缓存一致性。编译器不能验证整个固件内存映射。", "",
              "## 静态验证包", "", "`static_validation/`包含每类一张、共20张固定NB验证图片的输入与电脑参考输出，按seed=42选取，不依据是否识别正确筛图。原图标签与CPU预测分别保存；这是联调样例，不是新的精度测试集。", "",
              "每张输入为NHWC `[1,224,224,3]`、150528字节的原始int8；输出为 `[1,20]`、20字节int8。直接读取 `.bin`，不要再次归一化、转uint8或减128。输入已经完成原模型的RGB、方向、白边、缩放、ImageNet归一化及scale／zero_point量化。", "",
              "参考输出来自未编译INT8 TFLite的电脑CPU内核，并与上一轮保存的验证集输出逐字节核对。普通电脑TFLite解释器不能执行带ethos-u自定义算子的编译模型。尚无硬件／FVP输出，不能把参考输出说成NPU输出，编译后数值等价性仍待板端核验。", "",
              "`include/golden_outputs.h`提供输出参考、argmax和误差统计帮助函数；`include/first_input.h`只嵌入第0个样例，便于不接文件系统先联调；其余输入可从SD卡逐张读取，避免同时加载全部20张到RAM。模型本体仍为二进制TFLite文件。", "",
              "## 上板顺序", "", "1. 使用Shared_Sram编译模型，注册ethos-u自定义算子并确认U55-128/COP1驱动配置；按BSP要求先完成M33启动链。", "2. 在最终固件map中确认常量确实位于可访问Flash，arena位于可访问SoCMEM，并预留其他组件内存。", "3. 先运行第0张已量化输入，保存20字节原始int8输出与耗时。再逐类运行20张，使用包内校验工具比较Top-1、逐元素误差及最大LSB差。", "4. NPU与CPU整数内核可能有舍入差异；先报告实际差值及近似并列样本，不凭编译成功宣称逐字节一致。最后才接相机、YUYV转RGB及屏幕显示。", "",
              "原板卡视觉示例是320×320 uint8输入、float检测结果的目标检测接口；本模型是224×224 int8输入、20类int8输出。不能只替换model.c/model.h便宣称完成适配，需要新的分类调用及后处理。", "",
              "## 来源", "", "- [Infineon E84架构说明](https://documentation.infineon.com/psocedge/docs/bwb1750411526047)",
              "- [Infineon ML用户指南](https://www.infineon.com/assets/row/public/documents/30/44/infineon-infineon-modustoolbox-machine-learning-user-guide-usermanual-v02-00-en-usermanual-en.pdf)",
              "- [Arm Vela配置与内存模式](https://arm-software.github.io/CMSIS-Ethos-U/main/vela/index.html)",
              f"- [Edgi-Talk BSP固定版本](https://github.com/RT-Thread-Studio/sdk-bsp-psoc_e84-edgi-talk/tree/{contract['bsp_commit']})", ""]
    pack_summary_path = output / "validation_pack_summary.json"
    if pack_summary_path.exists():
        pack = load_json(pack_summary_path)
        lines += ["## 已生成的交付文件", "",
                  f"- 静态包：`{Path(pack['archive']).as_posix()}`（{pack['archive_bytes']/1048576:.2f} MiB）。",
                  f"- 已验证{pack['host_binary_roundtrips_exact']}张输入二进制的CPU回放输出，与上一轮原INT8验证输出逐字节相同；{pack['hash_checked_files']}个文件通过SHA256校验，ZIP关闭后重新读取并通过CRC校验。",
                  "- 包含源INT8模型、Shared_Sram编译模型、20份输入与参考输出、预览、类别表、C头文件和校验工具入口说明。",
                  "- 编译目录另保存`reference_sources.json`及视觉示例链接脚本快照，便于复查BSP提交与文件哈希。", "",
                  "生成／核对命令见项目README。板端输出尚未采集，这些检查只覆盖电脑参考与交付文件完整性。", ""]
    Path("docs/e84-offline-results.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="runs/e84_u55_224")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    source = (QUANT_RUN / "mobilenet_v2_224_int8.tflite").resolve()
    conversion = load_json(QUANT_RUN / "conversion.json")
    if digest(source) != conversion["artifacts"]["int8"]["sha256"]:
        raise ValueError("The frozen quantized model changed.")
    version = metadata.version("ethos-u-vela")
    if version != "5.2.0":
        raise ValueError("Use pinned ethos-u-vela 5.2.0.")
    import ethosu.vela
    arm_profile = Path(ethosu.vela.__file__).parent.parent / "config_files/Arm/vela.ini"
    sources = load_json(REFERENCE / "sources.json")
    for row in sources["files"]:
        if digest(REFERENCE / row["path"]) != row["sha256"]:
            raise ValueError(f"Pinned BSP reference changed: {row['path']}")
    contract = {"input_model": str(source), "input_sha256": digest(source), "vela_version": version,
                "accelerator": "ethos-u55-128", "clock_hz": 400000000, "cost_model_is_vendor_validated": False,
                "arm_profile_sha256": digest(arm_profile), "analysis_profile_sha256": digest(PROFILE),
                "system_config": SYSTEM, "cop_format": "COP1", "optimise": "Performance",
                "bsp_commit": sources["commit"], "linker_sha256": digest(LINKER)}
    contract_path = output / "target_contract.json"
    if contract_path.exists() and load_json(contract_path) != contract:
        raise ValueError("Compile contract changed; select a new output directory.")
    save_json(contract_path, contract)
    shutil.copyfile(PROFILE, output / "e84_u55_analysis.ini")
    shutil.copyfile(arm_profile, output / "arm_vela_5_2_0.ini")
    shutil.copyfile(REFERENCE / "sources.json", output / "reference_sources.json")
    shutil.copyfile(LINKER, output / "vision_example_link.ld")
    distribution = metadata.distribution("ethos-u-vela")
    license_path = next(path for path in distribution.files if path.name == "LICENSE.txt" and "licenses" in path.parts)
    shutil.copyfile(distribution.locate_file(license_path), output / "ARM_VELA_LICENSE.txt")
    source_info = inspect_flatbuffer(source)
    regions = parse_memory_regions(LINKER.read_text(encoding="utf-8"))
    results = [analyze_mode(source, output, mode, source_info, regions) for mode in MODES]
    save_json(output / "compile_summary.json", {"contract": contract, "regions": regions, "source_model": source_info, "results": results,
                                               "recommended_mode": "Shared_Sram", "hardware_validated": False})
    write_report(output, contract, results, regions)
    print(Path("docs/e84-offline-results.md").resolve(), flush=True)


if __name__ == "__main__":
    main()
