# E84 U55-128：224 INT8 离线编译与静态验证准备

日期：2026-10-22（Asia/Taipei）。

224 INT8模型已完成两种内存布局的离线编译。优先使用Shared_Sram：权重／常量保持在外部Flash，scratch放SoCMEM。原INT8模型不改写。尚未连接开发板、生成完整固件或测量板端精度／耗时。

## 编译结果

| 布局 | 编译文件 MiB | scratch MiB | 必需驻留SRAM的只读常量 MiB | 两者下界 MiB | NPU／CPU算子 |
|---|---:|---:|---:|---:|---:|
| Shared_Sram | 2.164 | 1.440 | 0.000 | 1.440 | 100／0 |
| Sram_Only | 2.162 | 1.436 | 2.154 | 3.589 | 100／0 |

本版本Vela的summary中SRAM memory字段没有计入Sram_Only必须位于SRAM的只读权重缓冲，因此另外读取编译FlatBuffer的read_only缓冲计算驻留下界，不能只看CSV中的SRAM栏。下界还没有计外部I/O、命令流／解释器元数据、对齐或应用数据。Sram_Only的scratch加只读常量已超过当前示例M55数据窗口，需要重新分区才能考虑；这不表示整个E84永远无法使用该模式。

算子数量来自Vela内部优化图，包含展开的ReLU6及Mean重写等，不能与原TFLite的69个算子直接作数量对比。另检查编译后的FlatBuffer：保留外部输入／输出形状、INT8类型、scale与zero_point，生成ethos-u自定义算子v1，COP1头符合所查BSP驱动的负载格式。

## 配置依据与限制

E84采用Ethos-U55-128，最高400MHz。Vela版本固定5.2.0、COP1、Performance；查阅的Edgi-Talk BSP驱动头为0.16。这里的格式检查不等于已证明完整驱动、TFLM库和硬件兼容。

尚未取得原厂PS84_M55_U55_400MHz配置。本次使用 `configs/e84_u55_analysis.ini`，仅把Arm参考模型时钟改为400MHz，内存带宽／延迟仍是未实测的Arm参考值。命名明确区别于原厂配置。不能把原始CSV中的预测时间或FPS当成Edgi-Talk实测值；不据此承诺实时帧率。

编译参数、有效配置和完整日志保存在各布局的 `command.json`、`compiler.log`、原始CSV与 `audit.json`。配置和输入模型哈希已固定，后续换配置应新建运行目录。

## 板级内存核对

已固定并查阅RT-Thread BSP提交 `b135be8a28127e276a04b70d27950c3ba07b917a`。视觉示例链接脚本分配M55数据区 `0x26060000`、3.0625 MiB；这包括其他数据与heap，不是当前空闲RAM。示例M55外部Flash窗口为8.0 MiB，也不是空闲容量。

Shared_Sram编译模型显式scratch为1,509,584字节，外部输入／输出另有150,548字节。若分别分配两者，合计1.583 MiB；还未计TFLM持久元数据、其他scratch请求、对齐、驱动、RTOS、LCD与摄像头负载。因此没有给出“最终tensor arena大小”或“必定装得下”的结论。

视觉示例的 `.cy_ml_model_data` 在启动时从Flash复制到SoCMEM。如果直接沿用，它会额外占用片内RAM，与Shared_Sram“常量留Flash”的部署意图不同。集成时应为模型选择不复制到RAM的只读Flash放置方式，并核对最终map、MPU／SAU、AXI区域和缓存一致性。编译器不能验证整个固件内存映射。

## 静态验证包

`static_validation/`包含每类一张、共20张固定NB验证图片的输入与电脑参考输出，按seed=42选取，不依据是否识别正确筛图。原图标签与CPU预测分别保存；这是联调样例，不是新的精度测试集。

每张输入为NHWC `[1,224,224,3]`、150528字节的原始int8；输出为 `[1,20]`、20字节int8。直接读取 `.bin`，不要再次归一化、转uint8或减128。输入已经完成原模型的RGB、方向、白边、缩放、ImageNet归一化及scale／zero_point量化。

参考输出来自未编译INT8 TFLite的电脑CPU内核，并与上一轮保存的验证集输出逐字节核对。普通电脑TFLite解释器不能执行带ethos-u自定义算子的编译模型。尚无硬件／FVP输出，不能把参考输出说成NPU输出，编译后数值等价性仍待板端核验。

`include/golden_outputs.h`提供输出参考、argmax和误差统计帮助函数；`include/first_input.h`只嵌入第0个样例，便于不接文件系统先联调；其余输入可从SD卡逐张读取，避免同时加载全部20张到RAM。模型本体仍为二进制TFLite文件。

## 上板顺序

1. 使用Shared_Sram编译模型，注册ethos-u自定义算子并确认U55-128/COP1驱动配置；按BSP要求先完成M33启动链。
2. 在最终固件map中确认常量确实位于可访问Flash，arena位于可访问SoCMEM，并预留其他组件内存。
3. 先运行第0张已量化输入，保存20字节原始int8输出与耗时。再逐类运行20张，使用包内校验工具比较Top-1、逐元素误差及最大LSB差。
4. NPU与CPU整数内核可能有舍入差异；先报告实际差值及近似并列样本，不凭编译成功宣称逐字节一致。最后才接相机、YUYV转RGB及屏幕显示。

原板卡视觉示例是320×320 uint8输入、float检测结果的目标检测接口；本模型是224×224 int8输入、20类int8输出。不能只替换model.c/model.h便宣称完成适配，需要新的分类调用及后处理。

## 来源

- [Infineon E84架构说明](https://documentation.infineon.com/psocedge/docs/bwb1750411526047)
- [Infineon ML用户指南](https://www.infineon.com/assets/row/public/documents/30/44/infineon-infineon-modustoolbox-machine-learning-user-guide-usermanual-v02-00-en-usermanual-en.pdf)
- [Arm Vela配置与内存模式](https://arm-software.github.io/CMSIS-Ethos-U/main/vela/index.html)
- [Edgi-Talk BSP固定版本](https://github.com/RT-Thread-Studio/sdk-bsp-psoc_e84-edgi-talk/tree/b135be8a28127e276a04b70d27950c3ba07b917a)

## 已生成的交付文件

- 静态包：`E:/workspace/Projects/TCM-Embeding/runs/e84_u55_224/static_validation.zip`（7.91 MiB）。
- 已验证20张输入二进制的CPU回放输出，与上一轮原INT8验证输出逐字节相同；77个文件通过SHA256校验，ZIP关闭后重新读取并通过CRC校验。
- 包含源INT8模型、Shared_Sram编译模型、20份输入与参考输出、预览、类别表、C头文件和校验工具入口说明。
- 编译目录另保存`reference_sources.json`及视觉示例链接脚本快照，便于复查BSP提交与文件哈希。

生成／核对命令见项目README。板端输出尚未采集，这些检查只覆盖电脑参考与交付文件完整性。
