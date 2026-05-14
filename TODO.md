# TODO - zotero-paper-pipeline 优化清单

评估日期：2026-05-14

## 项目现状简评

这是一个脚本式 Zotero 论文处理流水线，主流程清晰：导出/规范化元数据、下载 PDF、解析文本、生成总结、校验映射。优点是入口简单、数据产物命名统一、失败不会中断整条流水线，也有 `mock` LLM 便于本地跑通。

当前主要短板集中在可验证性、网络可靠性、路径/配置边界、批处理可观测性和真实大模型调用的稳健性。下面 TODO 按优先级排列，每项都尽量写成可以验收的工程任务。

实现状态：本轮已完成核心功能落地，并通过 `py_compile`、`ruff check`、`pytest` 验证。

## P0 - 正确性与数据安全

- [x] 增加端到端 smoke test。
  - 覆盖 `export_zotero.py -> download_pdfs.py -> parse_pdfs.py -> summarize_papers.py -> validate_mapping.py` 的最小样例。
  - 使用本地 fixture：一份 Zotero JSON/JSONL、一个小 PDF、`llm.provider=mock`。
  - 验收：CI 或本地一条命令能确认产物路径、状态 JSONL、summary 字段完整。

- [x] 为核心纯函数补单元测试。
  - 覆盖 `pipeline_utils.safe_key`、`normalize_doi`、`normalize_arxiv_id`、`read_jsonl/write_jsonl`。
  - 覆盖 `export_zotero.extract_*` 系列函数，尤其是 creators、tags、Extra 里的 citation key/arXiv 解析。
  - 验收：新增 `tests/`，常见 Zotero/Better BibTeX/CSL 输入格式不再靠人工回归。

- [x] 修复 `validate_mapping.py` 的隐式目录创建副作用。
  - 当前 `validate()` 中调用 `pdf_dir(config)`、`summary_dir(config)` 会通过 `ensure_dir()` 创建目录。
  - 优化：增加只解析路径不创建目录的 helper，校验命令不应改变工作区。
  - 验收：在空仓库运行校验不会自动生成 `data/` 子目录。

- [x] 让相对路径基于配置文件位置或项目根目录，而不是当前工作目录。
  - 当前 `project_path()` 使用 `Path.cwd()`，从其他目录调用脚本可能读写到错误位置。
  - 方案：加载 config 时记录 `config_dir` 或显式传入 `--workdir`。
  - 验收：从 repo 外执行 `python /path/to/export_zotero.py --config /path/to/config.yaml` 产物仍写到预期目录。

- [x] 增加文件写入失败和临时文件清理策略。
  - 当前使用 `.tmp` 原子替换是好基础，但网络下载、PDF 复制、JSON 写入失败后的残留处理不统一。
  - 验收：异常中断后不会留下被误认为有效产物的半成品文件。

- [x] AI 总结必须严格按照 `pdf_summary_prompt_template.md` 执行。
  - `summarize_papers.py` 应从模板文件读取 prompt，而不是在代码里维护另一套固定 prompt。
  - 必须填充 `{{PDF_FILENAME}}`、`{{PDF_STEM}}`、`{{ZOTERO_KEY}}`、`{{ARXIV_ID}}`、`{{PDF_TEXT}}` 等模板变量。
  - 输出应为模板要求的 Markdown 正文与同名 `.md` 文件，不能输出 JSON、解释文字、代码块围栏或文件保存说明。
  - 保留模板中的隔离约束、来源类型、中文正文、15 节严格结构和证据片段要求。
  - 验收：使用真实 LLM 时，每篇 PDF 独立生成一个符合模板结构的 Markdown summary；校验逻辑能发现缺章、JSON 输出、占位符未替换或跨论文污染。

## P1 - 批处理可靠性

- [x] 为 Zotero API、PDF 下载、LLM 请求增加 retry/backoff。
  - 处理 429、5xx、连接重置、超时等瞬时错误。
  - 配置项建议：`retry.max_attempts`、`retry.initial_delay_seconds`、`retry.max_delay_seconds`。
  - 验收：日志/status 中能看到每次尝试和最终失败原因。

- [x] 增强 Zotero API 导出完整性。
  - 确认 attachment 是否一定包含在当前 API 返回中；必要时为每个条目补拉 children/attachments。
  - 读取分页响应头或总数信息，避免只依赖 `len(page) < limit`。
  - 验收：Zotero Web API 模式下，条目及其 PDF attachment URL 覆盖率可被测试验证。

- [x] 增加下载候选源策略和失败分类。
  - 当前顺序为 `pdf_url -> arxiv_id -> doi -> url`，可以补充开放获取源解析，例如 Unpaywall 或 Crossref metadata。
  - 将失败区分为无候选源、非 PDF、403/404、超大小、超时、HTML 未找到 PDF。
  - 验收：`download_status.jsonl` 中的失败原因可用于后续自动重试或人工处理。

- [x] 为 PDF 解析增加可观测指标。
  - 记录 `parser`、`page_count`、`char_count`、`min_text_chars` 命中情况和失败页面数。
  - 对扫描版 PDF 给出明确的 OCR 待处理状态，例如 `ocr_required`。
  - 验收：解析失败不只是异常文本，而能说明是加密、扫描版、空文本还是工具缺失。

- [x] 改进 LLM 调用的稳定性和输出校验。
  - 对 HTTP 状态、响应结构、JSON 解析失败分别报错。
  - 校验 `keywords` 长度、字段类型、空值策略。
  - 可选增加 JSON Schema 或 Pydantic model。
  - 验收：模型返回 markdown、缺字段、字段类型错误时可自动归一化或给出清晰失败状态。

- [x] 避免重复处理已知失败项。
  - 目前 `download/parse/summarize` 会对每条 metadata 再跑一遍。
  - 增加 `--retry-failed`、`--only-missing`、`--keys-file` 或按状态筛选。
  - 验收：大批量持续导入时可以只处理新增/缺失/失败条目。

- [x] PDF 下载和 AI 总结必须默认幂等，避免重复执行昂贵操作。
  - PDF 已存在且通过 `%PDF` 头校验时，`download_pdfs.py` 默认不得重新下载；只有显式 `--force` 才允许覆盖。
  - Markdown summary 已存在且通过模板结构校验时，`summarize_papers.py` 默认不得重新调用 LLM；只有显式 `--force` 或 summary 校验失败时才重跑。
  - 验收：重复运行同一批 key 时，已有 PDF 和有效 summary 的文件修改时间不变化，并在 status 中标记为已有产物复用。

## P1 - 开发体验与可维护性

- [x] 引入统一 CLI 入口。
  - 例如 `python -m zotero_scan export/download/parse/summarize/validate/run-all`。
  - 保留现有脚本作为兼容 wrapper。
  - 验收：README 中一条 `run-all` 命令可以跑完整流水线。

- [x] 将项目打包为标准 Python 包。
  - 增加 `pyproject.toml`，声明依赖、测试、格式化、类型检查配置。
  - 将共享逻辑移动到包目录，减少脚本间隐式导入。
  - 验收：`pip install -e .` 后 CLI 可用，测试不依赖当前目录。

- [x] 增加静态质量工具。
  - 建议接入 `ruff` 做 lint/format，`mypy` 或 `pyright` 做基础类型检查。
  - 验收：本地和 CI 都能执行 `ruff check`、`ruff format --check`、类型检查。

- [x] 明确配置 schema 与默认值。
  - 当前配置通过 `cfg_get()` 分散读取，类型错误会延迟到运行时暴露。
  - 增加配置 dataclass/Pydantic model，集中校验路径、数值范围、provider 枚举。
  - 验收：启动阶段即可指出配置错误，例如负数 timeout、未知 provider、空 model。

- [x] 梳理 YAML 依赖策略。
  - `requirements.txt` 已要求 PyYAML，但代码仍维护 minimal YAML loader。
  - 决策：要么把 PyYAML 设为硬依赖并删除 fallback，要么将 fallback 的限制写入测试和文档。
  - 验收：配置解析路径简单、可测试、行为一致。

- [x] 统一状态模型。
  - 当前 status、failed_stage、failed_reason 分散在各阶段产物里。
  - 定义阶段枚举和状态迁移规则：exported、pdf_downloaded、pdf_failed、parsed、parse_failed、summarized、summary_failed 等。
  - 验收：`validate_mapping.py` 能识别状态迁移是否合法，而不只是检查文件存在。

## P2 - 功能增强

- [x] 增加 OCR 支持。
  - 对扫描版 PDF 增加可选 OCR backend，例如 `ocrmypdf` 或 `tesseract`。
  - 配置项控制是否启用、最大页数和语言。
  - 验收：扫描版论文可以输出可总结文本，失败时标记 `ocr_failed`。

- [x] 增加摘要语言和模板配置。
  - 当前 prompt 固定要求 concise English。
  - 支持 `llm.summary_language`、字段模板、领域相关字段开关。
  - 验收：可配置中文摘要或不同研究领域字段，不需要改代码。

- [x] 生成聚合报告。
  - 汇总成功率、失败分布、缺失 PDF、缺失 summary、Top tags、年份分布。
  - 输出 Markdown/JSON/CSV，便于人工审计。
  - 验收：一条命令生成 `data/report.md`。

- [x] 增加并发处理。
  - 下载和总结阶段适合有限并发，解析阶段可按 CPU/IO 控制并发数。
  - 配置项：`concurrency.download`、`concurrency.parse`、`concurrency.summary`。
  - 验收：并发不会破坏状态文件写入；失败仍能按 key 定位。

- [x] 增加条目选择器。
  - 支持按 collection、tag、year、citation_key、zotero_item_key 过滤。
  - 验收：能只处理某个研究方向或某批新增条目。

- [x] 支持增量 manifest。
  - 记录每个阶段输入内容 hash、配置摘要、代码版本。
  - 当 metadata 或配置变化时自动判定是否需要重跑后续阶段。
  - 验收：同一输入重复运行不会产生无意义更新；输入变化会触发正确阶段重算。

## P2 - 文档与运维

- [x] README 增加「快速试跑」章节。
  - 提供最小 fixture 或说明如何放置样例 Zotero export 和 PDF。
  - 验收：新用户 5 分钟内可以跑出 mock summary。

- [x] 增加常见故障排查。
  - Zotero API key、PDF 下载 403、扫描版 PDF、LLM JSON 解析失败、路径写错等。
  - 验收：每种常见失败都能对应到 status 字段和修复建议。

- [x] 增加数据隐私说明。
  - 说明哪些数据会发送到 LLM provider，如何关闭真实 LLM，如何限制输入长度。
  - 验收：用户在启用 `openai_compatible` 前能明确知道论文文本会被发送到外部服务。

- [x] 增加 CI。
  - 至少运行 py_compile、单元测试、lint。
  - 后续可加入 smoke test fixture。
  - 验收：PR 或 push 时自动发现基本破坏。

## 建议的第一轮落地顺序

1. 增加 `tests/` 和最小 fixture，先锁住现有行为。
2. 修复 `validate_mapping.py` 目录创建副作用和相对路径解析问题。
3. 引入 `pyproject.toml`、`ruff`、统一测试命令。
4. 给网络请求和 LLM 请求补 retry/backoff 与更清晰的失败分类。
5. 再做并发、OCR、聚合报告等功能增强。
