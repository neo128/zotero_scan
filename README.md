# zotero-paper-pipeline

一个可持续导入的 Zotero PDF 论文流水线：

1. 导出/规范化 Zotero 条目元数据为 JSONL。
2. 按 `zotero_item_key` 下载或复制 PDF。
3. 解析 PDF 文本，必要时标记 OCR 需求。
4. 按 `pdf_summary_prompt_template.md` 调用大模型生成 canonical Markdown 总结。
5. 验证 metadata、PDF、text、summary 与状态文件是否能用 `zotero_item_key` 一一对应。
6. 生成聚合报告和 manifest，便于审计和增量运行。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

只运行流水线也可以：

```bash
pip install -r requirements.txt
```

`PyYAML` 和 `pypdf` 是运行依赖。`ocrmypdf` 是可选系统工具，只在 `parse.ocr_enabled: true` 时用于扫描版 PDF。

## 快速试跑

把 Zotero 或 Better BibTeX 导出的 JSON/JSONL 放到：

```text
data/zotero_export.json
```

然后运行：

```bash
python -m zotero_scan run-all --config config.yaml --no-fail
```

也可以继续使用单个脚本：

```bash
python export_zotero.py --config config.yaml
python download_pdfs.py --config config.yaml
python parse_pdfs.py --config config.yaml
python summarize_papers.py --config config.yaml
python validate_mapping.py --config config.yaml --format text
python report_pipeline.py --config config.yaml
```

## 数据约定

`zotero_item_key` 是全局主键。产物默认命名为：

```text
data/metadata/{zotero_item_key}.json
data/pdfs/{zotero_item_key}.pdf
data/text/{zotero_item_key}.txt
data/summaries/{zotero_item_key}.md
data/status/{zotero_item_key}.json
```

聚合文件：

```text
data/metadata.jsonl
data/download_status.jsonl
data/parse_status.jsonl
data/summary_status.jsonl
data/report.md
data/report.json
data/manifest.json
```

相对路径基于 `config.yaml` 所在目录解析，不依赖当前 shell 的工作目录。

## Zotero 输入

### 文件导出

```yaml
zotero:
  source: file
  input_path: data/zotero_export.json
```

脚本会尽量识别 `key`/`zotero_item_key`、`citationKey`、`title`、`creators`/`author`、`date`、`DOI`、`url`、`abstractNote`、`tags`、`extra`、PDF attachment URL 等字段。

### Zotero Web API

```yaml
zotero:
  source: zotero_api
  library_type: user
  library_id: "你的 Zotero user id"
  api_key_env: ZOTERO_API_KEY
  fetch_child_attachments: true
```

```bash
export ZOTERO_API_KEY=...
```

也可以把 `ZOTERO_API_KEY` 放在项目根目录的 `.env` 中；脚本会在读取 `config.yaml` 时自动加载 `.env`。`.env` 已被 `.gitignore` 忽略，不应提交到版本库。

API 模式会分页读取条目，并尝试补拉每个条目的 child attachments。

## PDF 下载

默认候选顺序：

1. Zotero attachment 或元数据里的 `pdf_url`
2. `arxiv_id` 拼接 `https://arxiv.org/pdf/{arxiv_id}.pdf`
3. 配置了 `download.unpaywall_email` 时查询 Unpaywall
4. DOI 跳转解析
5. 普通 `url` 页面解析 PDF 链接

常用参数：

```bash
python download_pdfs.py --config config.yaml --only-missing
python download_pdfs.py --config config.yaml --retry-failed
python download_pdfs.py --config config.yaml --keys KEY1,KEY2
```

失败不会中断整个流程，失败条目会写入状态文件并带有分类原因，例如 `http_404`、`network_error`、`invalid_or_oversized_pdf`。

## PDF 解析与 OCR

解析优先使用 `pypdf`，然后尝试系统 `pdftotext`。如果普通解析没有可用文本：

- `parse.ocr_enabled: false` 时标记 `ocr_required`
- `parse.ocr_enabled: true` 且系统安装 `ocrmypdf` 时尝试 OCR
- OCR 失败会标记 `ocr_failed`

## AI 总结

总结阶段严格读取并执行：

```text
pdf_summary_prompt_template.md
```

模板变量包括 `{{PDF_FILENAME}}`、`{{PDF_STEM}}`、`{{ZOTERO_KEY}}`、`{{ARXIV_ID}}`、`{{PDF_TEXT}}`。输出必须是同名 Markdown：`data/summaries/{PDF_STEM}.md`。

默认配置：

```yaml
llm:
  provider: mock
  prompt_template_path: pdf_summary_prompt_template.md
  summary_language: 中文
```

`mock` 用于本地跑通流程，不会调用外部 API。真实调用使用兼容 Chat Completions 的接口：

```yaml
llm:
  provider: openai_compatible
  api_key_env: OPENAI_API_KEY
  base_url: https://api.openai.com/v1
  model: "你的模型名"
```

总结校验会检查 15 个必需章节、未替换占位符、JSON 输出、代码块围栏和模板禁止句。

## 验证与报告

```bash
python validate_mapping.py --config config.yaml --format text
python report_pipeline.py --config config.yaml
```

检查项包括：

- 每个 `zotero_item_key` 是否有 metadata、PDF、Markdown summary
- PDF 是否像有效 PDF
- summary 是否符合模板结构
- 是否存在重复 key、孤立 PDF/summary、阶段失败和非法状态迁移

`report_pipeline.py` 会生成 `data/report.md`、`data/report.json` 和 `data/manifest.json`。

## 批处理控制

下载、解析、总结都支持：

```bash
--limit N
--keys KEY1,KEY2
--keys-file keys.txt
--tag robotics,planning
--year 2024,2025
--citation-key smith2024paper
```

并发通过配置控制：

```yaml
concurrency:
  download: 1
  parse: 1
  summary: 1
```

网络和 LLM 请求重试通过配置控制：

```yaml
retry:
  max_attempts: 2
  initial_delay_seconds: 1
  max_delay_seconds: 20
```

## 数据隐私

`llm.provider: mock` 不会发送论文文本到外部服务。启用 `openai_compatible` 后，`summarize_papers.py` 会把当前论文的抽取文本填入模板并发送到 `llm.base_url`。可通过 `llm.max_input_chars` 限制发送文本长度。

## 常见故障

- Zotero API 报错：检查 `ZOTERO_API_KEY`、`library_id`、`library_type`。
- PDF 下载 403/404：优先补充 Zotero attachment 或 `pdf_url`，也可配置 Unpaywall email。
- `ocr_required`：说明 PDF 可能是扫描版，安装 `ocrmypdf` 并设置 `parse.ocr_enabled: true`。
- LLM summary failed：检查模型是否输出了 Markdown、是否缺章节、是否出现未替换占位符或 JSON。
- 路径不对：确认相对路径都是相对 `config.yaml` 所在目录。

## 开发检查

```bash
python -m py_compile *.py
ruff check .
pytest
```
