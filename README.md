# CNKI Metadata Exporter

一个可复用的知网检索结果元数据导出工具。它使用 Playwright 打开知网专业检索，按独立检索集合执行检索，通过知网自定义导出页面分批下载 XLS 元数据，并将多个集合合并、筛选文献类型和去重。
本项目基于 JYao-Chen/cnki-metadata-exporter修改，新增时间范围限制、超 6k 自动拆分爬取、多次失败跳过等功能。

本项目从真实文献数据建设任务中的采集流程整理而来。**改造重点是把面向单次课题的采集流程，优化为可配置、可续接、可复用的数据处理工具**：将检索条件与程序解耦，优先使用知网原生批量导出，并为大结果集拆分、任务中断和跨集合去重补上完整处理流程。项目已移除原任务的检索词、论文数据、浏览器登录状态和专用清洗规则。

## 相较原始采集流程的优化

| 改造方向 | 优化后的实现 |
| --- | --- |
| 检索条件与代码耦合 | 使用经过校验的 JSON 检索配置；也可以从约定格式的 DOCX 检索策略中读取集合，并记录本次实际使用的配置快照。 |
| 逐篇打开详情页，采集成本较高 | 将知网自定义 XLS 原生导出作为批量采集主流程，按页选择、按批下载；详情页采集保留为可选补充方式。 |
| 大结果集超出可翻页范围或容易漏采 | 默认限制到上一个完整自然月；可显式指定发表日期范围，超过单片上限时自动按日期递归拆分。若精确到单日仍超过上限，会明确报错，而不是静默跳过。 |
| 中断后难以确认进度、重复采集 | 导出批次落盘并写入 manifest；已有批次可复用。集合执行中断后会重试并从已完成批次续接，最终汇总完成情况；跳过未完成集合时以非零状态退出。 |
| 多检索集合结果需要人工整理 | 提供独立合并命令，支持来源库筛选、跨集合去重、字段合并，并保留记录命中的集合、集合名称和数据层级。 |

这些改进着重于流程复用、数据完整性和故障可见性；本项目没有提供与原始流程的定量速度对比。

## 功能

- 使用 JSON 配置任意数量的 CNKI 专业检索式
- 可选从结构化 DOCX 中提取检索集合
- 自动切换每页 50 条、逐页全选、按批次导出
- 单个时间范围超过 6000 条时自动按发表日期递归切分，直至每片可稳定分页
- 默认每批最多 500 条，已有批次自动续接
- 支持期刊、博士论文、硕士论文及会议论文筛选
- 解析知网自定义导出的 HTML-XLS 文件
- 按 DOI、URL、题名与作者等字段合并去重
- 保留论文命中的检索集合、集合名称和数据层级
- 可选进入详情页补充元数据
- 安全验证出现时停留在可见浏览器中等待用户完成

## 环境

- Python 3.11+
- Chromium
- 能正常访问 CNKI 的个人或机构账号

## 安装

使用 `uv`：

```bash
uv sync --extra dev
uv run playwright install chromium
```

或使用常规 Python：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
playwright install chromium
```

## 1. 准备检索配置

```bash
cp examples/queries.example.json queries.json
```

编辑 `queries.json`：

```json
[
  {
    "id": "Q1",
    "name": "材料主题核心集",
    "field": "SU",
    "query": "SU=('耐磨钢'+'低合金耐磨钢')",
    "publication_time": {
      "start": "2010-01-01",
      "end": "2018-12-31"
    },
    "purpose": "核心主题论文",
    "tier": "核心材料"
  }
]
```

`id`、`name` 和 `query` 为必填字段。可选的 `publication_time` 字段可填写发表时间起止日期，适合把较大的检索结果按时间范围缩小。详细格式见 [`docs/query-format.md`](docs/query-format.md)。

## 2. 使用知网原生导出批量获取元数据

这是推荐方式，不需要逐条打开详情页：

```bash
uv run cnki-native-export  --queries queries.json  --root workspace
```

只运行指定集合：

```bash
uv run cnki-native-export  --queries queries.json   --root workspace  --collection Q1   --batch-size 500
```

原生导出默认会为未配置 `publication_time` 的集合填写上一个完整自然月，以避免超大结果集超过知网可翻页范围。要检索指定年份，请在 JSON 中显式设置日期范围，例如 `2010-01-01` 至 `2018-12-31`。如需保持全时间检索行为，可使用：

```bash
uv run cnki-native-export   --queries queries.json   --root workspace  --all-time
```

默认月份仅用于没有明确发表时间的集合，不会覆盖配置中设置的时间范围。
单个日期范围超过 6000 条时，程序会自动二分日期范围并分别导出；可通过 `--max-results-per-slice` 调整单个时间片上限。若指定范围精确到单日仍超过上限，程序会报错提示缩小检索式或调整上限，不会在页数不确定时静默遗漏结果。

首次运行会打开可见浏览器。完成登录后，程序执行专业检索、逐页选择记录，并从知网自定义导出页面下载 XLS。遇到知网安全验证时，在浏览器窗口中完成人工验证即可继续。

运行数据保存在：

```text
workspace/
├── .cnki-profile/             # 浏览器会话，不提交 Git
├── data/queries.resolved.json # 本次实际使用的检索配置快照
├── logs/                      # 运行日志
└── output/native_exports/     # 分集合、分批次的 XLS 和 manifest
```

重新执行相同命令时，已成功下载的批次会被复用。
单个检索集合连续中断两次后会跳过该集合并继续执行后续集合；全部集合处理结束后，终端和 `output/native-export-summary.json` 会列出各集合已完成的批次及最后完成记录。若有集合被跳过，命令以非零状态退出，便于发现本次导出未全部完成。

## 3. 合并、筛选和去重

```bash
uv run cnki-merge-metadata  --root workspace  --queries queries.json  --out workspace/merged
```

默认保留：期刊、博士、硕士、中国会议和国际会议。也可以明确指定：

```bash
uv run cnki-merge-metadata   --root workspace  --queries queries.json   --out workspace/merged   --include-database 期刊   --include-database 博士   --include-database 硕士
```

输出包括：

- `raw.json`：筛选文献类型后的全部原始记录
- `unique.json`：跨检索集合合并去重后的记录
- `stats.json`：各集合导出和保留数量
- `queries.json`：实际检索式及用途
- `summary.json`：总体统计

支持的元数据字段见 [`docs/metadata-fields.md`](docs/metadata-fields.md)。

## 4. 可选：逐条详情页采集

当知网原生导出缺少某些字段时，可使用详情模式：

```bash
uv run cnki-metadata-exporter   --queries queries.json   --root workspace   --collection Q1
```

仅抓取结果列表：

```bash
uv run cnki-metadata-exporter   --queries queries.json   --root workspace   --collection Q1  --list-only
```

详情模式速度明显慢于原生导出，适合小规模补充摘要、关键词、机构、基金和 DOI。

## 从 DOCX 读取检索式

DOCX 符合约定的章节和段落结构时，可以使用：

```bash
uv run cnki-native-export   --docx search-strategy.docx   --root workspace
```

自定义章节标题：

```bash
uv run cnki-native-export   --docx search-strategy.docx  --docx-start "CNKI检索策略"   --docx-end "Web of Science检索策略"   --root workspace
```

## 测试

```bash
pytest
ruff check src tests
```

## 数据与账号

`.gitignore` 已排除浏览器会话、Cookie、运行日志、下载文件和采集结果。公开代码仓库中只保留程序、文档和示例配置。使用者应通过自己的合法访问权限使用知网，并遵守账号授权范围和平台规则。

## 页面兼容性

工具针对当前知网 `kns8s` 专业检索与自定义导出页面实现。知网页面结构更新后，主要需要调整 `exporter.py` 和 `native_export.py` 中的页面选择器。

## License

[MIT](LICENSE)

---

**English:** A reusable Playwright-based workflow for running CNKI professional-search queries, exporting native XLS metadata in batches, resuming interrupted jobs, merging collections, filtering publication types, and deduplicating records. See the command examples above and `examples/queries.example.json` to get started.
