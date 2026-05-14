# zotero-paper-pipeline

一个最小可运行的 Zotero 论文流水线：

1. 导出/规范化 Zotero 条目元数据为 JSONL。
2. 按 `zotero_item_key` 下载 PDF。
3. 解析 PDF 文本。
4. 调用大模型生成结构化总结 JSON。
5. 验证 metadata、PDF、summary 是否能用 `zotero_item_key` 一一对应。

## 数据约定

`zotero_item_key` 是全局主键。真实中间产物都使用这个 key 命名：

```text
data/metadata/{zotero_item_key}.json
data/pdfs/{zotero_item_key}.pdf
data/text/{zotero_item_key}.txt
data/summaries/{zotero_item_key}.json
data/status/{zotero_item_key}.json
```

`data/metadata/{zotero_item_key}.json` 默认同时保存规范化字段和 `raw_zotero` 原始条目，便于之后回溯 Zotero 导出内容。

聚合文件用于持续导入和审计：

```text
data/metadata.jsonl
data/download_status.jsonl
data/parse_status.jsonl
data/summary_status.jsonl
```

每条记录都会有 `status` 字段。下载失败使用 `pdf_failed`；解析或总结失败会保留最近成功阶段的 `status`，并写入 `failed_stage` 与 `failed_reason`。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`PyYAML` 是可选增强；脚本内置了能解析本项目 `config.yaml` 的轻量 YAML loader。`pypdf` 用于 PDF 文本解析。

## Zotero 输入

### 方式 1：文件导出

在 Zotero 或 Better BibTeX 中导出 JSON/JSONL，把文件放到：

```text
data/zotero_export.json
```

或修改 `config.yaml`：

```yaml
zotero:
  source: file
  input_path: /path/to/zotero_export.json
```

脚本会尽量识别这些字段：`key`/`zotero_item_key`、`citationKey`、`title`、`creators`/`author`、`date`、`DOI`、`url`、`abstractNote`、`tags`、`extra`、PDF attachment URL 等。

### 方式 2：Zotero Web API

修改 `config.yaml`：

```yaml
zotero:
  source: zotero_api
  library_type: user
  library_id: "你的 Zotero user id"
  api_key_env: ZOTERO_API_KEY
```

然后设置环境变量：

```bash
export ZOTERO_API_KEY=...
```

## 运行流水线

```bash
python export_zotero.py --config config.yaml
python download_pdfs.py --config config.yaml
python parse_pdfs.py --config config.yaml
python summarize_papers.py --config config.yaml
python validate_mapping.py --config config.yaml
```

持续导入时重复运行同一组命令即可：

- `export_zotero.py` 按 `zotero_item_key` 合并更新，不会因为新增条目覆盖旧条目。
- `download_pdfs.py`、`parse_pdfs.py`、`summarize_papers.py` 默认跳过已有产物。
- 需要重跑某一步时加 `--force`。
- 单次调试可加 `--limit N`。

## PDF 下载优先级

`download_pdfs.py` 对每篇论文按以下顺序尝试：

1. Zotero attachment 或元数据里的 `pdf_url`
2. `arxiv_id` 拼接 `https://arxiv.org/pdf/{arxiv_id}.pdf`
3. `doi` 解析
4. `url` 页面解析

失败不会中断整个流程，失败条目会写入 `data/download_status.jsonl` 和 `data/status/{zotero_item_key}.json`。

## 大模型总结

默认配置：

```yaml
llm:
  provider: mock
```

`mock` 用于本地跑通流程，不会调用外部 API。要调用真实大模型，改成兼容 Chat Completions 的接口：

```yaml
llm:
  provider: openai_compatible
  api_key_env: OPENAI_API_KEY
  base_url: https://api.openai.com/v1
  model: "你的模型名"
```

输出 JSON 字段固定为：

```json
{
  "one_sentence": "",
  "problem": "",
  "method": "",
  "contribution": "",
  "experiments": "",
  "limitations": "",
  "relevance_to_embodied_ai": "",
  "keywords": []
}
```

## 验证映射

```bash
python validate_mapping.py --config config.yaml --format text
```

检查项包括：

- 每个 `zotero_item_key` 是否有对应 metadata 文件
- 是否有 PDF
- 是否有 summary
- 是否存在重复 key
- 是否存在下载失败或总结失败的条目
- 是否有孤立 PDF/summary

发现问题时默认返回非零退出码；只想生成报告可加 `--no-fail`。
