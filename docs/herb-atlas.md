# 中药资料结构化数据库

来源为用户提供的 `assets/中西医药材药理.pdf`，实际标题为《好中醫 親試藥 / 尝药活动资料汇总》。提取范围是 PDF 的文字层，按用户要求跳过图片及图片内文字。原文包含个人笔记和标有「现代药理研究」的段落，数据库分别记录所属段落，内容按原文转录。

## 交付文件

| 文件 | 用途 |
| --- | --- |
| `data/herb_atlas/atlas.sqlite3` | 可直接查询的 SQLite 数据库，含目录、条目正文、字段、来源页和搜索索引 |
| `data/herb_atlas/atlas.json` | UTF-8 JSON 完整导出，方便检查、修改和后续生成板端资源 |
| `data/herb_atlas/validation.json` | 条目覆盖、字段统计、原文缺录清单、完整性检查和文件 SHA256 |

这两份数据是电脑端完整数据库，保留了原页文字、条目、字段及其追溯信息。SQLite 约 16.6 MiB，JSON 约 17.1 MiB。板端单独导出文本和索引，电脑端数据库的体积不等于板端资源的体积。

## 内容范围和目录

- 523 个 PDF 页面，541 个平级药材条目。
- 原目录的 474 个主编号和 67 个子编号均对应独立条目。
- 编号以字符串保留，例如 `003.1` 是紫苏梗的独立 ID。没有父条目字段，也不要求先打开 `003`。
- 21 个大类保留为 `category`，原节名保留为可选的 `section` 属性，可供筛选。
- 3,565 个带标签字段，覆盖 17 种字段类型。每个字段保留原标签、原始文本、便于显示的文字和来源。
- 57 个条目原文为「略」「没试过」或其他简短缺录说明，标记为 `source_omitted`，原文仍可查看。
- 另外 5 个条目有未加标准字段标签的正文，完整保存在正文和 `narrative` 块中。
- 图片中的文字按约定跳过。正文文字层内容可追溯到 13,161 个非空行，目录、引言、结束语也保存在库内。

## 表和字段

| 表或视图 | 主要内容 |
| --- | --- |
| `entries` | 一条药材一行，含 `id`、`name`、`display_order`、`category`、`section`、`raw_text`、`display_text` 和起止页码 |
| `fields` | 字段查询视图，含 `entry_id`、`field_key`、`label`、`context`、`value`、`display_text` 和来源 |
| `entry_blocks` | 按原文顺序保存的字段、无标签叙述和段落标题 |
| `entry_references` | 明确的条目引用，例如枸杞子原文引用桑椹 `430`，同时保留两条各自正文 |
| `catalog_categories` | 原目录章、节信息 |
| `source_pages` | 每页原始排版文字、清理后的行及被去除的重复页眉和页脚 |
| `document_blocks` | 全部非空文字行的归属，区分条目标题、条目内容及其他文档内容 |
| `entries_fts` | 中文正文搜索的 trigram 索引 |
| `metadata` | 数据结构版本、原 PDF SHA256、提取规则和字段标签对应关系 |

`pdf_page_start` 和 `pdf_page_end` 是从 1 开始的实际 PDF 页码，`printed_page_start` 和 `printed_page_end` 是书内页码。两个编号都保留。例如紫苏梗位于 PDF 第 36 页、书内第 14 页。

`raw_text` 保留提取后的行断点；`display_text` 合并排版换行并保留段落，便于触屏滚动显示。`entry_blocks.start_char/end_char` 是相对于条目 `raw_text` 的 Python Unicode 字符偏移，采用左闭右开区间，导出嵌入式二进制资源时需重新计算 UTF-8 字节偏移。

`context=author_notes` 表示原作者笔记；`context=modern_pharmacology` 表示原文「现代药理研究」标题之后的内容。后者是来源分段标记，未对原书的药学结论进行事实核实。

同一字段可以多次出现，按 `block_order` 返回全部记录。没有标准标签的叙述保留为 `narrative`，没有在原文出现的字段查询结果为空。

### 常用字段键

| 字段键 | 中文含义 |
| --- | --- |
| `appearance` | 性状 |
| `properties_meridians` | 性味归经 |
| `functions_indications` | 功能主治 |
| `chemical_components` | 化学成分 |
| `pharmacology` | 药理 |
| `toxicology` | 毒理 |
| `pharmacokinetics` | 药代动力学 |
| `taste_notes` | 口味笔记 |
| `reported_effects` | 原文标注的功效 |
| `secondary_effects` | 继发、间接功效 |
| `adverse_effect_notes` | 副作用笔记 |
| `source_dosage` | 原文用量字段 |
| `source_restricted_drug_flag` | 原文「是否毒麻药」标记 |
| `cautions` | 原文注意事项 |
| `notes` | 注、备注、补充 |
| `usage_notes` | 使用建议笔记 |
| `limitations` | 缺点、缺陷笔记 |

## 使用方法

在项目根目录、已有 `common` 环境中执行，不需要新增 Python 依赖。重新提取需要本机现有的 `pdftotext`。

```powershell
conda activate common
python -m tcm_herb.knowledge search 枸杞子
python -m tcm_herb.knowledge search 豆蔻
python -m tcm_herb.knowledge show 003.1
python -m tcm_herb.knowledge show 427 --field appearance
python -m tcm_herb.knowledge validate
```

`search` 同时查药名和正文，药名完全匹配的条目排在最前面。支持一字、两字中文查询，以及三字以上的全文检索。

重新生成并校验全部文件。

```powershell
python -m tcm_herb.knowledge build --pdf "assets/中西医药材药理.pdf" --output data/herb_atlas
```

SQL 查询示例。

```sql
SELECT id, name, category
FROM entries
ORDER BY display_order;

SELECT label, context, display_text, source_spans_json
FROM fields
WHERE entry_id = '427' AND field_key = 'appearance'
ORDER BY block_order;
```

## 检查记录

构建时验证了全部 541 个目录编号、正文标题和顺序，所有起始页码与目录一致。条目文本按段切分后可逐字符还原，原文非空内容行全部有归属。SQLite 外键、数据库完整性和与 JSON 全表一致性检查通过。

人工查看原 PDF 第 36、189、473、515 页，抽查了紫苏梗提升为独立条目、豆蔻与草豆蔻同页切分、枸杞子与前后条目及引用的边界，以及最后一条生硼砂的结束位置。另检查了生麻黄和生五味子的跨页延续。

6 个回归测试覆盖跨页、平级子条目、同一行的多个标签、标签后无标题段落、独占一行的字段标签及其编号列表、前向引用、中文检索，以及内容丢失时校验失败的路径。

```powershell
python -m unittest discover -s tests -p test_knowledge.py -v
```

检查结论针对结构、文字层提取、来源与查询可用性。图片内文字按用户要求跳过，原文中的简略条目和原文措辞均有保留。

## Edgi-Talk 触屏图鉴

首页标题栏右侧的「中药图鉴」打开本地目录。页面标题为「本草图鉴」，不显示「离线」标记或底部说明；目录行只显示编号、药名和分类。全部 541 个条目平级排列，可以按 21 个分类筛选，每页 6 条；点编号框弹出数字键盘，支持原编号前缀，例如 `003` 或 `003.1`。分类和编号条件同时生效。

点药材进入详情页，按「笔记」和「现代药理研究」两个部分连续滚动阅读。两部分标题分别使用浅绿、浅青灰的圆角背景，方便滚动时寻找。保留原文中的标签、叙述和段落，不显示 PDF 页码或书内页码，也不再设置性状、归经、化学成分等细分问答菜单。原文没有某部分时不显示该部分标题；原文为「略」「没试过」等说明的条目仍可阅读原文。返回目录保留筛选条件和页数。

板端直接读取 Flash 中的 UTF-8 文本池和条目索引，不把整本数据库加载到 RAM。电脑端的字段、来源页码、SQLite 查询功能仍保留。导出前检查 JSON 与成功校验报告的 SHA256 一致，设备启动时校验文本池 CRC32。

在已有 `common` 环境中生成独立的 `TCM_Home_Atlas` SDK 工程：

```powershell
python firmware/e84_home/prepare_home.py --sdk data/e84_bsp_src --assets data/e84_home_assets --weather --atlas data/herb_atlas
python firmware/e84_static_probe/build_with_scons.py --project data/e84_bsp_src/projects/TCM_Home_Atlas --gcc-bin <ARM-GCC-bin> --scons-package <SCons包所在目录>
```

此工程复用已有首页和联网恢复功能，凭证从已有的本地配置文件生成。图鉴的文本池、索引和统计清单生成到工程的 `applications` 目录；主页原工程可独立构建。

串口 MSH 调试命令由 GUI 定时器接收后执行，避免串口线程直接操作 LVGL：

```text
home_atlas_show catalog
home_atlas_show 159
home_atlas_show 159 1
home_atlas_show 003.1
home_atlas_show home
```

末尾参数 `1` 将「现代药理研究」标题滚动到可见范围，省略时从条目顶部阅读。

2026-11-11 板上验证：文本池 984,308 字节，CRC32 为 `15bd6ee6`。541 个条目的两部分文本已逐项与 JSON 正文核对，537 条含笔记、456 条含现代药理内容；字库无缺字，HEX 中的文本与导出文件完全一致。M55 固件 3,379,748 字节通过烧录读回校验，启动 CRC 和首页首帧正常。串口抽查生草果、紫苏梗、缺少现代药理部分的条目和最后一条生硼砂，均正常打开。实屏检查了目录、详情和首页，记录位于 `runs/e84_home_atlas/two_sections/`。
