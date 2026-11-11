# 中药材图像公开数据集调查

调查日期：2026-10-21。目标：电脑端训练单种干燥药材／饮片图像分类，之后迁移至 PSOC E84；当前本地 assets 主要用于图鉴，不把每类一张的图库当作足够的监督训练集。

## 初步结论

建议保留 MobileNetV2 基线，优先用 **NB-TCM-CHM 的 20 类跑通训练、独立测试和 INT8 导出**；它提供网络图片训练来源和另外拍摄的手机图片测试来源，且数据页明确 CC BY 4.0。需要更多药用部位和类别时，再评估 **CHM-100**。**Figshare 的 95 类集**是许可清楚、文件公开可见的备选，但还需要实际解包检查图像、类别和重复情况。

这不是下载完成后的数据质量验收。此次只读取原作者论文、仓库、托管页和公开文件元数据；没有下载完整图片压缩包，没有联系作者，没有登录或同意共享个人账户资料。

| 数据集 | 作者公布规模 | 当前访问与许可 | 对本项目的判断 |
|---|---|---|---|
| NB-TCM-CHM | 20 类；3,384 网络图＋400 手机图 | Mendeley 公开目录／单图链接／整包链接；CC BY 4.0 | 小规模首选，400 手机图保留为独立测试 |
| CHM-100 | 100 类、6,556 图，7 种药用部位 | 作者仓库链接 Google Drive／百度盘；README 限学术研究并要求引用 | 扩大类别的优先候选；须读包内声明 |
| Chinese Materia Medica Image Dataset | 95 类、5,640 图 | Figshare 公开 tar 文件；CC BY 4.0 | 直接开放备选，拍摄类型与划分待验收 |
| CNH-98 | 完整集 98 类、9,184 图 | GitHub 仅 20 类样本；完整集需邮件申请非商用 | 不当作完整集已可直接获取 |
| AI Studio 163 类中草药集 | 发布页称 163 类；原始／增强总图数未核实 | 页面及 4.91 GB 文件名公开可见；页面标 GPL-2.0 | 存在增强及错标提示，后置处理 |
| TCMP-300 | 300 类、52,089 图 | Figshare 公开文件；CC BY 4.0 | 主要任务是药用植物识别，不能直接替代干药材数据 |

## NB-TCM-CHM：先完成 20 类基线

原作者论文发表于 **Data in Brief 54 (2024), 110405**。Dataset 1 为 Google 搜集、经专业人员筛选的 3,384 图；Dataset 2 为药房手机拍摄、白纸背景及补光的 400 图，均为 20 类果实／种子药材。论文把 Dataset 1 分训练、验证、测试（每类随机取 20 张测试），Dataset 2 全部用于另一次测试；增强是训练时动态翻转、裁剪和旋转，没有固定的“增强后独立图数”。论文表 3 报告其 MobileNetV2 在 Dataset 2 的准确率 89.75%，这只是该论文条件下的结果，不能承诺复现或设备效果。[原论文](https://pmc.ncbi.nlm.nih.gov/articles/PMC11063984/)

[Mendeley v2](https://data.mendeley.com/datasets/2kjmzjyrmd/2) 明确 CC BY 4.0；公开 API 实际返回 NB-TCM-CHM／Dataset 1／Dataset 2 及各类目录，抽查 Dataset 1 枸杞类文件列表实际存在 `Lycii Fructus0.jpg` 等单图与公开下载链接。整包元数据返回约 **1,285,732,095 字节**、状态 FINISH 及 S3 zip 地址。没有下载整包，因而尚未全量核对张数。原论文还指向 [v3](https://data.mendeley.com/datasets/2kjmzjyrmd/3)；本次深入核对的文件元数据来自 v2，正式落地应固定版本，不能静默混用。复核接口：[目录](https://data.mendeley.com/public-api/datasets/2kjmzjyrmd/folders/2)、[整包元数据](https://data.mendeley.com/api/datasets-v2/datasets/2kjmzjyrmd/zip?version=2)。

作者公布的类别可初步映射为：补骨脂、草豆蔻、川楝子、地肤子、白豆蔻、覆盆子、枸杞子、瓜蒌、金樱子、苦杏仁、连翘、木瓜、砂仁、山楂、山茱萸、桃仁、乌梅、五味子、小茴香、栀子。**这是依据其拉丁名做的暂定中文映射**，尚未逐类看图确认，尤其瓜蒌的药用部位、豆蔻与草豆蔻及拼写错误需检查；Dataset 2 还出现 `doukou` 文件夹，不能直接按两部分目录字符串相等对齐。[作者数据说明](https://data.mendeley.com/datasets/2kjmzjyrmd/2)

实施建议：只用 Dataset 1 决定超参数和阈值，Dataset 2 的 400 图只做最终独立评估；在任何增强之前，先去除跨划分完全重复和近似重复。不能把 Dataset 2 混入训练后仍引用原论文的“独立手机测试”设定。

## CHM-100：扩充至多种药用部位

作者论文描述 **100 类、6,556 图、7 种药用部位**，采用 8:1:1 随机划分。[原论文](https://www.sciencedirect.com/science/article/pii/S003132032500528X)

[作者 MPM-Net 仓库](https://github.com/KevinYang95/MPM-Net) 提供 Google Drive 和百度网盘（提取码 `6666`），README 表示用于学术研究并需引用。实际在未登录状态打开 [Google Drive 目录](https://drive.google.com/drive/folders/1ZX6j7I7ZhRpT-YowEHUfJyD3dCHp9CK4)，可见 `CHM-100.zip`（页面约 1.5 GB）、`CHM-100_Data_Description.xlsx`（约 14 KB）、`CHM-100_Data_Statement.md`（约 4 KB）。**尚未下载压缩包，6,556 是论文数，未全量计数；包内声明未核对，不能宣称 CC 许可或任意商业使用。**

实施建议：先读声明和类别表，再检查实际文件、图片质量及是否原图／增强图，并完成与 assets 的中文名称映射；不要直接沿用随机划分而忽略同一原图或实物近似图泄漏。

## Figshare 95 类：文件与许可明确，内容待检查

[作者原数据页](https://figshare.com/articles/dataset/Chinese_Materia_Medica_Image_Dataset/13073087) 由 bucm sunxin 发布，说明为 **95 类、5,640 图**，CC BY 4.0。[公开 API](https://api.figshare.com/v2/articles/13073087) 实际列出 `medicine_image.tar`，**513,515,520 字节**，文件 ID `25012019`，MD5 `85452f02b6ea26ceb0d94bb3d674c808`，`is_link_only=false`，并返回公开下载地址。

该原始托管说明没有足够信息核实拍摄场景、药材部位、类别表、原图与增强图比例、官方训练测试划分。本次也没有读 tar 内容。因此应称“可公开获取、值得验收”，不能直接称“已确认高质量实拍饮片集”。下载时保存 DOI `10.6084/m9.figshare.13073087.v1`、许可、校验值与来源记录。

## CNH-98：完整集需要申请

[作者仓库](https://github.com/scut-aitcm/Chinese-Herbs-Dataset) 声明完整集 **98 类、9,184 图**、每类 14–246 图，来源包括医院、药材店和 Google；分果实种子、根茎、花、皮、菌藻等 8 大类。README 要求完整集非商用使用先邮件联系；没有公开的全量直接下载许可。

通过 [作者 GitHub 文件树 API](https://api.github.com/repos/scut-aitcm/Chinese-Herbs-Dataset/git/trees/master?recursive=1) 核实 `Chinese-Herbs-98_Sample` 只有 **20 个类别目录，每类 10 张，共 200 图**；仓库内另有示意图。README 的 `32×32` 字样没有被此次实际图片尺寸核实。未发现完整 9,184 图、官方固定划分或明确独立样本许可，不应把公开仓库等同完整数据集授权。

## AI Studio 163 类：需要额外清洗

[发布者数据页](https://aistudio.baidu.com/datasetdetail/246739) 说明来源为百度图片，做过裁剪和旋转，分 train／eval；发布者提示部分名称不对应，麦芽与谷芽难区分。页面标 GPL-2.0。未登录可看见 `cls_chinese_medicine.zip`、约 **4.91 GB**，作者梦泽ovo；未点击下载，不能认定完整下载不需要登录。

原始／增强后图数和同源图片是否跨 train／eval 未核实；不要用其他论文“163 类”数据的张数代替这里的真实计数。Hugging Face 上另一个 [Herb163CMR](https://huggingface.co/datasets/SimonWJ/Herb163CMR) 需要登录并同意共享账户资料，也没有在本次接受条件。建议放在后续候选，不作为现在许可和质量最清楚的起点。

## TCMP-300：药用植物与干药材任务应区分

[原作者仓库](https://github.com/winycg/TCMP-300) 与 [原论文](https://www.nature.com/articles/s41597-025-05522-7) 明确为 **Traditional Chinese Medicinal Plant** 识别，300 类、52,089 图，Bing 搜集并经模型与人工清理，包含植物及不同植物部位。它不能直接当作 300 类干燥饮片数据集。

[作者 Figshare 数据页](https://figshare.com/articles/dataset/TCMP-300/29432726) 的 [公开 API](https://api.figshare.com/v2/articles/29432726) 核实 CC BY 4.0、下载未禁用，实际文件为 `tcmp-info-20250329.csv`（20,103 字节）和 `tcmp-300-release.tar.gz`（12,820,492,346 字节）。未下载，不再检查具体划分。若以后做原植物科普识别可以单独建任务，当前不宜与干药材类别直接混合训练。

## 落地时的验收标准

1. 固定来源版本、许可、原始压缩包校验值，保留未经修改的原文件。
2. 从实际文件生成类别／张数／尺寸／损坏图报告，不用论文数字替代本地实测。
3. 统一中文名、药用部位和炮制状态；相同植物不同部位不能机械合并。
4. 先去重和划分，再仅对训练集增强；网络搜图集与本地图库之间也要查重复。
5. 本地 assets 可用于图鉴、类别对齐或额外域测试；选定测试图后不再把它们放回训练。
6. 报告宏平均 F1、每类召回率、混淆矩阵和独立来源测试结果；公开集测试通过仍不足以证明真实摄像头环境可靠。
