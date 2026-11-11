# E84：sample_00 静态推理固件

已使用用户解压的 Arm GNU Toolchain 13.3.Rel1（GCC 13.3.1）和 RT-Thread Studio 自带的 SCons 3.1.2 构建 Cortex-M55 应用。2026-11-05 已烧录并回读校验 M55 镜像，板上 `sample_00` 推理通过，Top-1 为草豆蔻。

- 源码：`firmware/e84_static_probe`。生成工程在 `data/e84_bsp_src/projects/TCM_Static_Probe`，与 BSP 原视觉示例分开。
- 产物：`runs/e84_u55_224/board_probe`，含 ELF、HEX、链接 map、构建日志和 `build_check.json`。
- 运行时只注册 `ethos-u`，使用 BSP 原有 U55 初始化与中断驱动，不启动摄像头。程序校验输入和输出张量的类型、维度、量化参数，然后运行一次 `sample_00`。
- 模型及首张样本嵌入 Flash，HEX 中的对应数据与静态包逐字节相同。推理 arena 预留 1.75 MiB、位于 SoCMEM，实际用量由板上 TFLM 回报。所有 HEX 写入地址均落在此 BSP 的 M55 Flash 窗口；这不能替代对板上实际分区和启动链的确认。
- 链接器仍报告一个 RWX LOAD 段，BSP 的 `rt_vsnprintf` 包有一处编译警告；构建完成。未修改共享 BSP 源文件或安装新工具。

## 复现编译

本次采用 [RT-Thread Edgi-Talk BSP 固定提交](https://github.com/RT-Thread-Studio/sdk-bsp-psoc_e84-edgi-talk/tree/b135be8a28127e276a04b70d27950c3ba07b917a)。BSP 源码树须包含 `libraries`、`rt-thread`、`projects/libs` 和视觉示例；当前工作区已有副本。工具链见 [Arm 官方 Windows `arm-none-eabi` ZIP](https://developer.arm.com/-/media/Files/downloads/gnu/13.3.rel1/binrel/arm-gnu-toolchain-13.3.rel1-mingw-w64-i686-arm-none-eabi.zip)。

在项目根目录运行。生成工程已经存在时跳过第一条；生成命令拒绝覆盖已有探针工程。

```powershell
python -m tcm_herb.e84_probe stage --sdk-root data/e84_bsp_src

python firmware/e84_static_probe/build_with_scons.py `
  --project data/e84_bsp_src/projects/TCM_Static_Probe `
  --gcc-bin 'E:/workspace/Tools/arm-gnu-toolchain-13.3.rel1-mingw-w64-i686-arm-none-eabi/bin' `
  --scons-package "$env:USERPROFILE/Workspace/RT-ThreadStudio/platform/env_released/env/tools/Python27/Lib/site-packages/scons"
```

编译命令只在当前进程指定工具链路径，不需要修改系统 PATH。原始构建结果为生成工程中的 `rt-thread.elf`、`rtthread.map` 和 `Debug/rtthread.hex`。

## 上板验证

本次使用 [Infineon OpenOCD 5.20 Windows ZIP](https://github.com/Infineon/openocd/releases/tag/release-v5.20.0) 在项目目录内解压运行。KitProg3 识别到 `PSE846GPS2DBZC4A`；使用 BSP 生成的 SMIF 加载器，仅擦写 `0x60580000` 至 `0x607FFFFF`，M33 未写入。原受影响的 FallCheck 扇区已读出，备份为 `board_probe/fallcheck_m55_affected_sectors.bin`。Flash 回读校验通过，共 2,563,788 字节。

复位后通过 COM3（`115200 8N1`）捕获了 `TCM_PROBE` 输出。可以用以下命令复核现有日志：

```powershell
python -m tcm_herb.e84_probe compare-log --log runs/e84_u55_224/board_probe/uart-flash.log
```

`sample_00` 板端与电脑参考 Top-1 均为草豆蔻。20 个 INT8 值中 15 个不同，最大绝对差为 3 个量化单位。`Invoke()` 用时 62 个 1 ms ticks（约 62 ms），TFLM 报告 arena 用量 1,510,052 字节，约 1.44 MiB；不含整机其他 SRAM 开销。对照数据见 `board_probe/board_result_sample_00.json`，板端原始输出见 `board_probe/actual/sample_00.bin`。

## 20 张静态样本批量验证

`stage --all-samples` 生成独立的 `TCM_Static_Probe_20_RAM` 工程。固件在 HyperRAM 堆分配 3,010,560 字节的输入缓冲以及独立的控制字。电脑通过 KitProg3/SWD 写入已核对的输入包，读回校验后才写入就绪标记；M55 校验 CRC32 后依次推理。这样保留了原有 Flash 范围之外的数据，并让 20 张样本共用一个片内推理 arena。

板端串口日志可用以下命令复核。校验要求样本编号 `00` 到 `19` 全部按顺序出现，且开始与结束标记的模型、输入包与本地一致。

```powershell
python -m tcm_herb.e84_probe compare-log --all-samples --log runs/e84_u55_224/board_probe_20_ram/uart-flash.log
```

2026-11-05 实测：20 张全部返回结果，Top-1 与电脑参考及真实标签均一致（20/20）。400 个原始 INT8 值中 308 个与电脑参考不同，最大绝对差 6 个量化单位；`sample_00` 与首轮板端输出逐字节一致。20 次 `Invoke()` 均记录为 62 个 1 ms ticks，推理 arena 用量均为 1,510,052 字节（约 1.44 MiB）。耗时不含 SWD 传输、摄像头或前处理；20 张是每类一张固定样本，不能作为完整板端准确率估计。

实际镜像回读校验 2,414,292 字节，擦写区间 `[0x60580000, 0x607D0000)`，完全落在首轮已擦写范围内。更大的全 Flash 备选镜像没有写入，因为扩展区内已有数据。M33 未改动。详细 JSON、逐样本 CSV、串口日志和原始输出均位于 `runs/e84_u55_224/board_probe_20_ram`。
