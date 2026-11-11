# 224 模型：全 INT8 量化与电脑端精度对照

日期：2026-10-21（Asia/Taipei）。

验证集Top-1下降不超过2个百分点的工程目标：**达到**。模型文件从同格式FP32的8.54 MiB缩至INT8的2.60 MiB（3.29倍）。

## 固定对照

| 数据划分 | 张数 | 原PyTorch FP32 Top-1 | TFLite FP32 Top-1 | INT8 Top-1 | INT8 Top-3 | INT8 Macro F1 | Top-1下降（百分点） |
|---|---:|---:|---:|---:|---:|---:|---:|
| NB验证 | 515 | 94.56% | 94.56% | 94.56% | 99.22% | 94.40% | +0.00 |
| NB网络测试 | 512 | 94.73% | 94.73% | 94.34% | 99.41% | 94.54% | +0.39 |
| 固定手机对比集 | 400 | 77.00% | 77.00% | 75.50% | 91.75% | 74.17% | +1.50 |

已逐图检查转换前后FP32 logits：按atol=5e-4、rtol=1e-4通过，最大绝对差0.00074196，Top-1不同0张。表中FP32来自本轮在CPU重新评估，三者使用完全相同的图片、标签顺序和预处理。负的“下降”表示准确率提高。

本轮CPU FP32与前轮GPU存在少量边界预测差异：NB网络测试CPU 94.73%／前轮GPU 94.92%；固定手机对比集CPU 77.00%／前轮GPU 77.25%。已逐一复现2张差异样本：GPU默认允许cuDNN TF32时复现旧预测，关闭TF32后与CPU预测一致，严格GPU与CPU logits最大差0.00000715。权重、图片及标签均未变化。量化精度变化按本轮相同CPU参考计算；复核细节见 `backend_boundary_check.json`。

手机图片在前轮已用于错误诊断，此处仅是固定对比集；没有用于校准或量化参数选择。每种精度只运行本次固定方案，没有依据手机成绩反复尝试配方。

## 校准与转换

校准仅使用原NB训练集400张，每类20张、每个重复组至多一张，seed=42。验证／测试／手机图片不参与校准。抽样清单、原图哈希和分组保存在 `calibration.json`，原始数据清单与权重保持不变。

本机未发现DEEPCRAFT转换器、Docker/Podman或可用WSL发行版。使用项目内 `.venv-int8`，继承common的训练依赖，只在该虚拟环境安装TensorFlow CPU；没有升级common的PyTorch或NumPy。具体版本见 `conversion.json`。

转换采用现有torchvision MobileNetV2权重的显式图映射：Conv-BN按推理公式折叠，卷积核布局转为NHWC，保持原始对称padding、ReLU6、残差相加、全局平均池化与20类线性头。没有改用Keras预训练权重或重新训练。转换先通过20张训练图的FP32检查，再做400张训练图校准。

量化使用TFLite静态PTQ，启用TFLITE_BUILTINS_INT8并强制INT8输入和输出。检查FlatBuffer全部子图：无浮点张量、无Flex／Custom算子；卷积、深度卷积和全连接权重均为INT8，偏置／形状辅助张量可为INT32。详细算子及版本、逐通道权重scale数量在 `conversion.json`。

输入仍需按原模型做RGB、方向校正、白边填充320、缩放224与ImageNet归一化，随后转为NHWC，再按模型的scale与zero_point量化；输出先反量化为logits。量化式为 `clip(round(x/scale)+zero_point,-128,127)`，禁止直接强制转int8导致溢出。

## 手机图各类召回率

| 类别 | FP32 | INT8 |
|---|---:|---:|
| 草豆蔻 | 35% | 35% |
| 砂仁 | 65% | 65% |
| 白豆蔻 | 100% | 85% |
| 木瓜 | 100% | 100% |
| 山茱萸 | 95% | 100% |
| 山楂 | 45% | 45% |
| 小茴香 | 90% | 90% |
| 连翘 | 70% | 50% |
| 栀子 | 80% | 80% |
| 地肤子 | 100% | 100% |
| 枸杞子 | 100% | 100% |
| 乌梅 | 80% | 80% |
| 桃仁 | 65% | 70% |
| 补骨脂 | 95% | 90% |
| 金樱子 | 90% | 90% |
| 覆盆子 | 30% | 30% |
| 五味子 | 95% | 95% |
| 苦杏仁 | 10% | 10% |
| 川楝子 | 95% | 95% |
| 瓜蒌类（按原数据标签） | 100% | 100% |

## 文件与边界

- [INT8模型](E:/workspace/Projects/TCM-Embeding/runs/mobilenet_v2_nb_tcm_chm_224_int8/mobilenet_v2_224_int8.tflite)
- [同格式FP32模型](E:/workspace/Projects/TCM-Embeding/runs/mobilenet_v2_nb_tcm_chm_224_int8/mobilenet_v2_224_fp32.tflite)
- [完整指标与来源](E:/workspace/Projects/TCM-Embeding/runs/mobilenet_v2_nb_tcm_chm_224_int8/summary.json)

`*_predictions.csv`、`*_per_class.csv`、`*_logits.npz`保存逐图结果、逐类对照及原始输出；`int8_*`图片保存混淆矩阵与错误示例。

本次使用电脑端TFLite原生CPU内核、关闭默认delegate。INT8输出的有限分辨率可能产生分数并列；Top-1使用首个最大值，Top-3沿用sklearn高类别索引优先的并列规则。分数仍不是校准后的置信度，未知药材拒识尚未实现。

模型文件大小不是板端运行内存。本次没有进行DEEPCRAFT/Vela编译，未测U55算子覆盖、tensor arena、板端耗时；不能据电脑结果宣称已能在E84 NPU运行。

转换参考：[Google全整数PTQ](https://developers.google.com/edge/litert/conversion/tensorflow/quantization/post_training_integer_quant)、[Infineon支持格式](https://developer.imagimob.com/deepcraft-model-converter/additional-resources/supported-model-formats)、[Infineon容器要求](https://developer.imagimob.com/deepcraft-model-converter/additional-resources/container-setup)。
