# 中药嵌入式助手：识别、图鉴与环境首页

面向 PSoC E84 Edgi-Talk 开发板，包含中药材识别训练与 INT8/U55 部署、RT-Thread/LVGL 首页、中药文字图鉴及天气生活建议。

已完成 NB-TCM-CHM 数据审计、GPU 训练、手机图片评估、224 模型 INT8 量化、U55 编译及 20 张固定样本的板端静态推理验证。首页已在板上实现温湿度、日历、节气、自动定位与手动选城、天气、今日推荐，以及 541 条药材的本地图鉴。摄像头实时识别、自由知识问答和语音养生咨询尚未实现。

本仓库仅包含源码、配置、测试和文档。数据集、PDF、图片、字体、第三方 BSP/SDK、预训练及导出模型、生成的药材数据库、运行日志和固件产物均不上传。实际热点与天气凭据通过被 Git 忽略的本地配置生成，准备资源的方法见 [首页说明](docs/e84-home.md) 和 [药材数据库说明](docs/herb-atlas.md)。

源码目录：`tcm_herb/` 为数据处理、训练和部署代码，`firmware/` 为板端应用和工程生成脚本，`tools/e84_programmer/` 为串口和历史上板实验辅助脚本，`tests/` 为回归测试。

当前推荐 **MobileNetV2 224×224**：NB网络测试Top-1为94.92%，400张固定手机对比集Top-1为77.25%（160基线68.25%）。仅补146张CHM训练图的160组为65.50%，本次没有整体收益。候选按原NB验证macro F1选择；完整指标与限制见 [第二轮结果](docs/round2-results.md)。

## 当前方案

- 使用现有 conda 环境 `common` 和 PyTorch CUDA；第二轮审计新增OpenCV，没有替换原有PyTorch或NumPy依赖。
- MobileNetV2 **width=1.0**、官方 ImageNet 预训练权重、20 类；160×160为基线，224×224为当前候选。
- 选择 1.0 是为了直接使用 torchvision 官方预训练权重；0.75 尚未训练或比较。
- 两轮只训练分类头，随后微调整网；最多 20 轮，按验证集 macro F1 保存最佳模型并早停。
- RGB、EXIF 方向校正、保比例白边填充到 320×320，再缩放到模型对应的160×160或224×224；归一化参数随模型保存。
- 训练时使用适度仿射、翻转和颜色增强；评估无随机增强。

## 数据与划分

原始目录为 `assets/NB-TCM-CHM/Dataset 1` 和 `Dataset 2`。准备脚本不改原始图片，只写 `data/nb_tcm_chm` 缓存和清单。

Dataset 1 的 3,384 张网络图片按类别分组划分约 70% / 15% / 15% 为训练、验证、内部测试。Dataset 2 的 400 张手机白底实拍图初次作为独立测试，不参与权重选择、早停或量化校准；首轮已查看其错误，后续实验将其称为固定对比集，不再当作新的盲测。这是本项目的划分，不是原论文划分的精确复现。

准备脚本验证 20 类名称，显式将 Dataset 2 的 `doukou` 对齐到训练类别 `Amomi Fructus Rotundus`。中文名称用于界面展示，原始英文类别名保留；`Trichosanhes kirilowii` 暂展示为“瓜蒌类（按原数据标签）”，未把药用部位另行确认。

原文件 SHA256、标准化后像素 SHA256 相同的图片，以及同类别中 pHash 距离≤4 且缩略图 RGB 均方根误差≤8 的近似图归为同组。近似规则仅是保守的泄漏防护，不能识别所有裁剪、旋转或同一实物照片。若分组触及外部测试，相关网络图排除出开发集。分组内样本仍保留，统计为图片数，不能当成同等数量的独立实物。

## 运行

在项目根目录的 PowerShell 中：

```powershell
conda activate common

# 数据审计及缓存，通常只需执行一次
python -m tcm_herb.data

# 不覆盖已有运行；重复实验请改 --output
python -m tcm_herb.train --output runs/mobilenet_v2_nb_tcm_chm_baseline

# 模型固定后再运行内部测试和400张手机图片评估
python -m tcm_herb.evaluate --checkpoint runs/mobilenet_v2_nb_tcm_chm_baseline/best.pt

# 按模型自带的预处理及类别顺序预测本机图片
python -m tcm_herb.predict --checkpoint runs/mobilenet_v2_nb_tcm_chm_baseline/best.pt --image '你的图片.jpg'

# CPU推理也可运行
python -m tcm_herb.predict --checkpoint runs/mobilenet_v2_nb_tcm_chm_baseline/best.pt --image '你的图片.jpg' --device cpu

# 导出固定 batch=1 的FP32 .pt2计算图并验证数值一致性
python -m tcm_herb.export --checkpoint runs/mobilenet_v2_nb_tcm_chm_baseline/best.pt

# 检查类别对齐、分组不泄漏和保比例填充
python -m unittest discover -s tests -v
```

可选摄像头入口：`python -m tcm_herb.predict --checkpoint runs/mobilenet_v2_nb_tcm_chm_baseline/best.pt --camera 0`。第二轮审计已安装 `opencv-python`，本次没有打开摄像头。空格进行一次识别，Q 退出；图片不上传、不自动存盘。

## 第二轮：分辨率与公开数据补充对照

原始 NB 清单和第一轮模型保持不变。第二轮分别改变一个因素，仍只按原 NB 验证集 macro F1 选择权重：

```powershell
# A：仅把输入改为224，NB数据、初始化、训练配置相同
python -m tcm_herb.train --output runs/mobilenet_v2_nb_tcm_chm_224 --image-size 224

# B：审计CHM-100全部图片，只匹配作者类别表中与原20类同名的类别
python -m tcm_herb.supplement

# 复查裁剪同源图、人工记录的同场景照片与训练图片质量
python -m tcm_herb.refine_supplement

# 保持160输入，仅加入筛选后的CHM训练图片
python -m tcm_herb.train --manifest data/nb_chm100_curated/manifest.json --output runs/mobilenet_v2_nb_chm100_160_curated --image-size 160

# A：先用原清单评估NB，再用派生清单评估新增的CHM三类留出集
python -m tcm_herb.evaluate --checkpoint runs/mobilenet_v2_nb_tcm_chm_224/best.pt
python -m tcm_herb.evaluate --checkpoint runs/mobilenet_v2_nb_tcm_chm_224/best.pt --manifest data/nb_chm100_curated/manifest.json --splits chm_valid chm_test

# 基线的NB评估已完成，仅补充CHM留出集结果
python -m tcm_herb.evaluate --checkpoint runs/mobilenet_v2_nb_tcm_chm_baseline/best.pt --manifest data/nb_chm100_curated/manifest.json --splits chm_valid chm_test

# B：训练清单包含保持不变的NB划分及新增CHM留出集
python -m tcm_herb.evaluate --checkpoint runs/mobilenet_v2_nb_chm100_160_curated/best.pt --manifest data/nb_chm100_curated/manifest.json --splits test external chm_valid chm_test

# 检查权重与样本一致性，按验证指标选候选，并生成对照报告
python -m tcm_herb.compare
```

CHM-100只重合栀子、连翘、枸杞子三类。清理后增加146张训练图；原NB的515张验证、512张内部测试和400张手机图完全保持原划分。CHM官方验证和测试按重复／同场景组优先保留，最终另有23张 `chm_valid` 和25张 `chm_test`，仅覆盖三类，不能代表全部20类的实拍泛化。

审计采用文件／像素哈希、保守pHash、SIFT候选检索与几何和像素相关性验证，并在 `configs/chm100_quality_flags.json` 中显式记录人工复核的质量问题和同场景组。没有改写原图或自动纠正标签；未确认身份的图片隔离。该过程仍不能保证穷尽所有同源图片。审计记录在 `data/nb_chm100_aug` 和 `data/nb_chm100_curated`。

第二轮仅新增OpenCV依赖，安装时使用 `--no-deps`，未升级或降级已有PyTorch、NumPy等包。保持所有运行相同训练轮数上限；补数据组每轮更新步数会随样本数增加，因此不是严格等更新步数实验。

完整结果见 [第二轮对照报告](docs/round2-results.md)。`runs/mobilenet_v2_nb_chm100_160` 是数据审计尚未冻结时中止的旧尝试，没有进行测试评估，不纳入对照；有效补数据实验目录以 `_curated` 结尾。

## 224模型的全INT8量化

使用项目内独立虚拟环境，继承conda `common`的训练依赖，仅将TensorFlow CPU和新增依赖安装到 `.venv-int8`，保留common原有PyTorch和NumPy版本。

本轮CPU配对评估：验证Top-1为94.56%→94.56%，网络测试94.73%→94.34%，固定手机对比集77.00%→75.50%。同格式模型文件由8.54 MiB缩至2.60 MiB。CPU与前轮GPU各有一张网络／手机边界预测差异，已复现为cuDNN TF32计算差异，详见报告；量化损失按同轮CPU结果计算。

```powershell
conda activate common
python -m venv --system-site-packages .venv-int8
.\.venv-int8\Scripts\python.exe -m pip install -r requirements-quantization-lock.txt

# 新实验：转换、校准、三种精度评估及报告；重复运行须使用新的输出目录
.\.venv-int8\Scripts\python.exe -m tcm_herb.quantize --output runs/mobilenet_v2_nb_tcm_chm_224_int8_repeat

# 已完成转换的固定实验可分阶段评估、重新生成报告
.\.venv-int8\Scripts\python.exe -m tcm_herb.quantize --stage evaluate --resume --splits val test external
.\.venv-int8\Scripts\python.exe -m tcm_herb.quantize --stage report --resume
```

固定校准方案为原NB训练集每类20张、共400张，seed=42，同一重复组至多一张。转换显式映射原torchvision权重与推理结构，先检查FP32数值一致性，再执行TFLite全整数静态PTQ；输入和输出均为INT8，全部权重逐通道量化。校准不使用验证、测试或手机图片。

结果位于 `runs/mobilenet_v2_nb_tcm_chm_224_int8`，包含两种精度的 `.tflite`、校准图片清单、算子与量化参数、逐图logits和逐类指标。完整对照见 [INT8结果报告](docs/int8-results.md)。该报告记录电脑端TFLite结果；后续U55离线编译见下一节。

## E84 U55离线编译与静态验证包

使用Vela 5.2.0、U55-128、COP1，已完成Shared_Sram和Sram_Only两种布局的编译，Vela内部优化图均为100个NPU算子、0个CPU算子。优先使用Shared_Sram：编译模型2.164 MiB，显式scratch约1.440 MiB；输入／输出另需150548字节，完整arena和固件内存仍待集成测量。Sram_Only的scratch加驻留常量约3.589 MiB，超过所查BSP视觉示例的3.0625 MiB M55数据窗口。

目前使用明确命名的Arm参考成本模型，将NPU时钟设为400MHz；未取得原厂PS84配置，继承的内存带宽与延迟没有板卡实测依据。编译日志里的预测速度不作为Edgi-Talk实际帧率。完整依据、资源表及移植限制见 [E84离线结果](docs/e84-offline-results.md)。

```powershell
# 使用已有量化虚拟环境，不修改common训练依赖
.\.venv-int8\Scripts\python.exe -m pip install --no-deps -r requirements-deployment.txt

# 固定模型和配置；已有完整编译会复用并重新检查其结构、I/O和报告
.\.venv-int8\Scripts\python.exe -m tcm_herb.compile_u55

# 新建静态包；本地已有同一合同的包时加 --resume
.\.venv-int8\Scripts\python.exe -m tcm_herb.validation_pack build

# 上板后，将每次推理的20字节原始INT8输出保存为sample_XX.bin再比较
python -m tcm_herb.validation_pack verify --outputs '板端输出目录' --sample 0
```

编译审计使用 `data/e84_reference` 内固定BSP提交的源码与链接脚本，检查其SHA256；该目录是本次准备的只读参考资料。当前版本按项目根目录运行。原模型、配置或工具版本变化时应使用新的编译目录；静态包的 `--resume` 仅接受相同模型、源清单、样本和目标合同。

`runs/e84_u55_224/static_validation.zip` 包含20类各一张固定验证图的量化输入、电脑参考输出、预览、类别表、源INT8模型与Shared_Sram编译模型。样本按seed=42选择，与校准组无交集，也没有按预测正确与否筛选。`include/first_input.h`只嵌入第一张输入，`include/golden_outputs.h`提供输出参考与差值统计；包内README说明逐张联调步骤。20张样例用于检查接口，不能替代完整精度评估。

本轮只做离线编译和电脑参考回放，没有板端／FVP输出或可直接烧录的固件。编译模型需要ethos-u运行时；原BSP的320×320 uint8检测API需适配为224×224 int8分类API，同时调整模型Flash放置及arena分配。

后续已在 E84 上运行全部 20 张固定静态样本。板端 Top-1 与电脑参考及真实标签均为 20/20；单次 `Invoke()` 约 62 ms，最大 INT8 差 6 个量化单位。Flash 回读校验通过，原 FallCheck 受影响的扇区已备份，未写入 M33。构建和串口实测见 [E84 静态推理固件](docs/e84-static-probe.md)。

## E84 首页「四时小记」

首页固件提供板载温湿度、环境提醒、月历、节气、IP 定位、手动选城、和风天气与生活指数建议。点击「今日推荐」查看运动、穿衣和舒适度详情。源码位于 `firmware/e84_home`，构建与配置见 [E84 首页说明](docs/e84-home.md)。

「中药图鉴」包含 541 个平级条目，支持分类、原编号筛选和触屏翻页，详情按「笔记」「现代药理研究」连续阅读。图鉴从用户本地 PDF 文字层生成，图片跳过；数据库、PDF 及字体需在本地准备，生成和校验方法见 [药材数据库说明](docs/herb-atlas.md)。

## 结果文件

运行目录 `runs/mobilenet_v2_nb_tcm_chm_baseline` 包含：

- `best.pt`：按验证集 macro F1 选择的最佳权重，内含类别顺序、输入尺寸和清单校验值。
- `config.json`、`manifest.json`、`history.csv`、`training_summary.json`：参数、数据划分、逐轮记录、训练汇总。
- `metrics.json`：内部测试及手机固定对比集的 Top-1、Top-3、macro F1 和逐类指标。
- `*_predictions.csv`、`*_per_class.csv`：逐图结果和各类别表现。
- `*_confusion_matrix.png`、`*_errors.png`、`training_curves.png`：混淆矩阵、错误示例、训练曲线。
- `export/mobilenet_v2_fp32.pt2` 与 `metadata.json`：FP32计算图及预处理约定。

`data/`、`runs/` 和原始 `assets/` 未纳入 Git。运行目录里的清单为可追溯快照，缓存仍在 `data/nb_tcm_chm/cache`；评估默认读取原始清单并检查与训练时的 SHA256 一致。

## 当前边界

这是已知 20 类的闭集分类，softmax 分数不是经过校准的置信度。当前没有可靠的未知药材拒识功能；Top-3 只作为候选参考。白底手机测试表现不能直接代表复杂背景、其他摄像头和全部中药材的表现。

`.pt2` 是电脑端 FP32 导出；独立量化实验生成使用训练图片校准的 INT8 `.tflite`，部署目录另有 U55 编译模型。U55 板端已验证 20 张固定静态样本，摄像头、实时前处理和完整精度回归仍待联调。模型文件体积不等于板端运行内存。

数据来源与许可：[NB-TCM-CHM](https://data.mendeley.com/datasets/2kjmzjyrmd/2)，CC BY 4.0；引用原始数据和[论文](https://doi.org/10.1016/j.dib.2024.110405)。本地压缩包由用户准备，本次记录实际文件和哈希，不推断压缩包版本。更多数据源见 [调查记录](docs/public-dataset-research.md)。
