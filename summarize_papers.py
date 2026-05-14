#!/usr/bin/env python3
"""Generate template-compliant Markdown summaries from parsed paper text."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from pipeline_utils import (
    SUMMARY_MARKDOWN_SECTIONS,
    cfg_get,
    compact_text,
    fail_record,
    load_config,
    now_iso,
    parse_csv_filter,
    pdf_path,
    read_jsonl,
    read_keys_file,
    read_metadata_records,
    read_text,
    retry_call,
    safe_key,
    select_records,
    status_jsonl,
    summary_path,
    summary_template_path,
    text_path,
    truncate,
    write_stage_status,
    write_text,
)

FORBIDDEN_SUMMARY_PHRASES = [
    "当前批量总结",
    "题名和元数据表明",
    "需要进一步阅读全文确认",
    "中文题名辅助理解",
]


def first_sentence(text: str, fallback: str = "") -> str:
    text = compact_text(text)
    if not text:
        return fallback
    match = re.search(r"(.+?[.!?。！？])\s", text + " ")
    return compact_text(match.group(1)) if match else text[:240]


def title_keywords(metadata: dict[str, Any]) -> list[str]:
    tags = metadata.get("tags")
    keywords: list[str] = []
    if isinstance(tags, list):
        keywords.extend(compact_text(tag) for tag in tags if compact_text(tag))
    title = compact_text(metadata.get("title"))
    stop = {"the", "and", "for", "with", "from", "using", "based", "paper", "study"}
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", title):
        lower = token.lower()
        if lower not in stop and token not in keywords:
            keywords.append(token)
    return keywords[:12]


def load_summary_template(config: dict[str, Any]) -> str:
    path = summary_template_path(config)
    if not path.exists():
        raise FileNotFoundError(f"summary prompt template not found: {path}")
    return read_text(path)


def prompt_variables(config: dict[str, Any], metadata: dict[str, Any], text: str) -> dict[str, str]:
    key = safe_key(metadata["zotero_item_key"])
    pdf = pdf_path(config, key, create=False)
    max_input_chars = int(cfg_get(config, "llm.max_input_chars", 60000))
    return {
        "PDF_FILENAME": pdf.name,
        "PDF_STEM": pdf.stem,
        "ZOTERO_KEY": key,
        "ARXIV_ID": compact_text(metadata.get("arxiv_id")),
        "SUMMARY_LANGUAGE": compact_text(cfg_get(config, "llm.summary_language", "中文")),
        "PDF_TEXT": truncate(text, max_input_chars),
    }


def render_prompt(template: str, variables: dict[str, str]) -> str:
    rendered = template
    for key, value in variables.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    unresolved = sorted(set(re.findall(r"\{\{[A-Z0-9_]+\}\}", rendered)))
    if unresolved:
        raise ValueError(f"unresolved template variables: {', '.join(unresolved)}")
    return rendered


def validate_summary_markdown(markdown: str) -> list[str]:
    problems: list[str] = []
    stripped = markdown.strip()
    if not stripped:
        return ["summary is empty"]
    if "{{" in stripped or "}}" in stripped:
        problems.append("summary contains unresolved template placeholders")
    if stripped.startswith("{") or stripped.startswith("["):
        problems.append("summary appears to be JSON instead of Markdown")
    if "```" in stripped:
        problems.append("summary contains code fences")
    for phrase in FORBIDDEN_SUMMARY_PHRASES:
        if phrase in stripped:
            problems.append(f"summary contains forbidden phrase: {phrase}")
    lines = [line.strip() for line in stripped.splitlines()]
    for section in SUMMARY_MARKDOWN_SECTIONS:
        if not any(line.startswith(section) for line in lines):
            problems.append(f"missing required section: {section}")
    if "[原文]" not in stripped:
        problems.append("summary does not contain [原文] source markers")
    return problems


def clean_llm_markdown(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        return content
    return content


def mock_markdown_summary(metadata: dict[str, Any], text: str) -> str:
    title = compact_text(metadata.get("title")) or "原文未包含该类信息"
    authors = metadata.get("authors")
    if isinstance(authors, list) and authors:
        author_text = ", ".join(compact_text(author) for author in authors if compact_text(author))
    else:
        author_text = "原文未包含该类信息"
    year = compact_text(metadata.get("year")) or "原文未包含该类信息"
    doi = compact_text(metadata.get("doi")) or "原文未包含该类信息"
    arxiv_id = compact_text(metadata.get("arxiv_id")) or "原文未包含该类信息"
    key = safe_key(metadata["zotero_item_key"])
    abstract = compact_text(metadata.get("abstract"))
    sentence = first_sentence(abstract or text, fallback=title)
    keywords = title_keywords(metadata)
    keyword_text = "、".join(keywords) if keywords else "原文未包含该类信息"
    evidence = first_sentence(text, fallback=title)[:180]

    return f"""## 1. 论文基本信息

- 标题：[原文] {title}
- 作者：[原文] {author_text}
- 年份：[原文] {year}
- 机构：[原文] 原文未包含该类信息
- 会议/期刊/来源：[原文] 原文未包含该类信息
- DOI：[原文] {doi}
- arXiv ID：[原文] {arxiv_id}
- Zotero key：[原文] {key}
- 研究领域：[推断] {keyword_text}
- 关键词：[原文] {keyword_text}
- 论文类型：[推断] 方法/研究论文，需以原文细节为准

## 2. 一句话总结

[原文] {sentence}

## 3. 研究问题

[原文] {sentence}

## 4. 方法概述

[原文] mock 模式不调用外部模型，只基于元数据和抽取文本生成结构化占位总结；原文未包含该类信息时不补造细节。

## 5. 技术流程拆解（Step-by-step）

Step 1

- 输入：[原文] PDF 抽取文本
- 处理：[推断] 读取文本并保留可追溯信息
- 输出：[原文] Markdown 总结
- 解决问题：[推断] 为后续真实 LLM 总结验证输出结构

## 6. 创新点评估（必须分级）

- 内容：[原文] 原文未包含该类信息
- 创新类型：分析
- 创新强度：弱
- 是否已有类似工作：[推断] 原文未包含该类信息
- 是否容易被替代：高
- 证据依据：[原文] mock 模式未进行真实论文创新性判断

## 7. 技术坐标系定位

- black-box vs mechanistic：[推断] 原文未包含该类信息
- 行为优化 vs 内部机制解释：[推断] 原文未包含该类信息
- 表征学习 vs 控制学习：[推断] 原文未包含该类信息
- 离线分析 vs 在线干预：[推断] 原文未包含该类信息
- 单体智能 vs 多智能体：[推断] 原文未包含该类信息
- 仿真验证 vs 真实部署：[推断] 原文未包含该类信息

## 8. 实验与结果

- 数据集 / 任务 / 环境：[原文] 原文未包含该类信息
- Baseline：[原文] 原文未包含该类信息
- 指标：[原文] 原文未包含该类信息
- 主要结果：[原文] 原文未包含该类信息
- 消融实验：[原文] 原文未包含该类信息
- 泛化实验：[原文] 原文未包含该类信息
- 失败案例：[原文] 原文未包含明确失败案例

## 9. 局限性

[推断] mock 模式无法替代真实 LLM 对全文证据的细读；该限制来自当前 provider 设置。

## 10. 失败模式（关键）

- 失败现象：[原文] 原文未包含明确失败案例
- 触发条件：[推断] 原文未包含该类信息
- 根本原因：[推断] 原文未包含该类信息
- 是否可检测：[推断] 可通过摘要校验发现结构缺失
- 是否可修复：[推断] 可切换真实 LLM provider 后重跑

## 11. 通用复用价值

[启发] 该结构可用于后续综述、技术路线图和方法对比，但 mock 内容只适合流程试跑。

## 12. 分类标签（结构化）

- 任务类型：[推断] 原文未包含该类信息
- 方法类型：[推断] 原文未包含该类信息
- 数据类型：[原文] 原文未包含该类信息
- 是否使用真实机器人数据：[原文] 原文未包含该类信息
- 是否使用仿真：[原文] 原文未包含该类信息
- 是否使用语言：[原文] 原文未包含该类信息
- 是否使用视觉：[原文] 原文未包含该类信息
- 是否涉及动作序列：[原文] 原文未包含该类信息
- 是否涉及世界模型：[原文] 原文未包含该类信息
- 是否涉及 VLA：[原文] 原文未包含该类信息
- 是否涉及记忆/语义表示：[原文] 原文未包含该类信息
- 是否支持长程任务：[原文] 原文未包含该类信息
- 开源状态：[原文] 原文未包含该类信息

## 13. 跨域适配与具身智能启发（可选）

[启发] 非具身智能论文或暂未确认是否为具身智能论文。

### 13.1 任务建模启发

[启发] 原文信息不足时，不做跨域强行迁移。

### 13.2 数据与平台启发

[启发] 原文信息不足时，不做数据平台判断。

### 13.3 感知-决策-控制启发

[启发] 原文信息不足时，不做控制系统推断。

### 13.4 泛化与部署启发

[启发] 原文信息不足时，不做部署结论。

## 14. 潜在研究机会

- 背景问题：[启发] 需要真实总结才能提出高质量方向
- 未解决空白：[启发] 原文未包含该类信息
- 技术路线：[启发] 切换真实 LLM provider 并按模板重跑
- 预期价值：[启发] 获得可复用的 canonical summary
- 难点：[启发] 需要控制幻觉和证据定位

## 15. 高质量证据片段（强约束）

- 原文定位短语：{evidence}
- 支撑的结论：mock 模式已读取当前论文文本
- 为什么能支撑：该片段来自当前抽取文本或标题
- 来源类型：`[原文]`

原文证据数量有限。
"""


def build_prompt(config: dict[str, Any], metadata: dict[str, Any], text: str) -> list[dict[str, str]]:
    template = load_summary_template(config)
    prompt = render_prompt(template, prompt_variables(config, metadata, text))
    return [
        {
            "role": "system",
            "content": "严格遵守用户提供的模板，只输出 Markdown 正文，不输出 JSON、解释或代码围栏。",
        },
        {
            "role": "user",
            "content": prompt,
        },
    ]


def openai_compatible_summary(config: dict[str, Any], metadata: dict[str, Any], text: str) -> str:
    api_key_env = str(cfg_get(config, "llm.api_key_env", "OPENAI_API_KEY"))
    api_key = os.getenv(api_key_env, "")
    if not api_key:
        raise RuntimeError(f"environment variable {api_key_env} is not set")

    base_url = str(cfg_get(config, "llm.base_url", "https://api.openai.com/v1")).rstrip("/")
    model = str(cfg_get(config, "llm.model", "") or "").strip()
    if not model:
        raise RuntimeError("llm.model is required for openai_compatible provider")

    timeout = int(cfg_get(config, "llm.timeout_seconds", 120))
    payload: dict[str, Any] = {
        "model": model,
        "messages": build_prompt(config, metadata, text),
        "temperature": float(cfg_get(config, "llm.temperature", 0.1)),
    }
    max_tokens = int(cfg_get(config, "llm.max_output_tokens", 3000))
    if max_tokens:
        payload["max_tokens"] = max_tokens

    def request_once() -> str:
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "zotero-paper-pipeline/0.2",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = json.loads(response.read().decode("utf-8"))
        try:
            return str(raw["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"unexpected LLM response shape: {raw}") from exc

    attempts = int(cfg_get(config, "retry.max_attempts", 1))
    initial_delay = float(cfg_get(config, "retry.initial_delay_seconds", 1))
    max_delay = float(cfg_get(config, "retry.max_delay_seconds", 30))
    return retry_call(
        request_once,
        attempts=attempts,
        initial_delay=initial_delay,
        max_delay=max_delay,
        retry_exceptions=(urllib.error.URLError, TimeoutError, RuntimeError),
    )


def generate_summary(config: dict[str, Any], metadata: dict[str, Any], text: str) -> tuple[str, str, str]:
    provider = str(cfg_get(config, "llm.provider", "mock")).strip().lower()
    if provider == "mock":
        return mock_markdown_summary(metadata, text), provider, "mock"
    if provider in {"openai", "openai_compatible", "chat_completions"}:
        markdown = clean_llm_markdown(openai_compatible_summary(config, metadata, text))
        return markdown, provider, str(cfg_get(config, "llm.model", ""))
    raise RuntimeError(f"unsupported llm.provider: {provider}")


def summarize_one(config: dict[str, Any], metadata: dict[str, Any], force: bool) -> dict[str, Any]:
    key = safe_key(metadata["zotero_item_key"])
    text_file = text_path(config, key, create=False)
    output = summary_path(config, key)
    if not force and output.exists():
        existing = read_text(output)
        problems = validate_summary_markdown(existing)
        if not problems:
            return {
                "zotero_item_key": key,
                "citation_key": metadata.get("citation_key", ""),
                "title": metadata.get("title", ""),
                "status": "summarized",
                "summary_path": str(output),
                "summary_format": "markdown",
                "validation": "ok",
                "updated_at": now_iso(),
            }

    if not text_file.exists():
        return fail_record(
            metadata,
            "parse_failed",
            "summarize",
            f"missing parsed text: {text_file}",
            text_path=str(text_file),
            summary_path=str(output),
        )

    try:
        text = read_text(text_file)
        if not text.strip():
            raise RuntimeError(f"parsed text is empty: {text_file}")
        markdown, provider, model = generate_summary(config, metadata, text)
        problems = validate_summary_markdown(markdown)
        if problems:
            raise RuntimeError("; ".join(problems))
        write_text(output, markdown.rstrip() + "\n")
        return {
            "zotero_item_key": key,
            "citation_key": metadata.get("citation_key", ""),
            "title": metadata.get("title", ""),
            "status": "summarized",
            "summary_path": str(output),
            "summary_format": "markdown",
            "llm_provider": provider,
            "llm_model": model,
            "summarized_at": now_iso(),
            "updated_at": now_iso(),
        }
    except Exception as exc:
        return fail_record(
            metadata,
            "summary_failed",
            "summarize",
            str(exc),
            text_path=str(text_file),
            summary_path=str(output),
        )


def summarize_records(config: dict[str, Any], records: list[dict[str, Any]], force: bool) -> list[dict[str, Any]]:
    workers = int(cfg_get(config, "concurrency.summary", 1) or 1)
    if workers <= 1 or len(records) <= 1:
        return [summarize_one(config, record, force=force) for record in records]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(summarize_one, config, record, force) for record in records]
        return [future.result() for future in futures]


def failed_keys(config: dict[str, Any]) -> set[str]:
    rows = read_jsonl(status_jsonl(config, "summary"))
    return {
        str(row.get("zotero_item_key", ""))
        for row in rows
        if row.get("failed_stage") == "summarize" or row.get("status") == "summary_failed"
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--force", action="store_true", help="resummarize even if {key}.md exists")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--only-missing", action="store_true", help="only process records without Markdown summary")
    parser.add_argument("--retry-failed", action="store_true", help="only process records that failed previously")
    parser.add_argument("--keys", help="comma-separated zotero_item_key filter")
    parser.add_argument("--keys-file", help="newline-separated zotero_item_key filter")
    parser.add_argument("--tag", help="comma-separated tag filter")
    parser.add_argument("--year", help="comma-separated year filter")
    parser.add_argument("--citation-key", help="comma-separated citation key filter")
    args = parser.parse_args()

    config = load_config(args.config)
    records = read_metadata_records(config)
    keys = parse_csv_filter(args.keys)
    file_keys = read_keys_file(args.keys_file)
    if keys is not None and file_keys is not None:
        keys = keys.intersection(file_keys)
    elif file_keys is not None:
        keys = file_keys
    if args.retry_failed:
        retry_keys = failed_keys(config)
        keys = retry_keys if keys is None else keys.intersection(retry_keys)
    records = select_records(
        records,
        keys=keys,
        tags=parse_csv_filter(args.tag),
        years=parse_csv_filter(args.year),
        citation_keys=parse_csv_filter(args.citation_key),
        limit=0,
    )
    if args.only_missing:
        records = [
            record
            for record in records
            if not summary_path(config, str(record.get("zotero_item_key", "")), create=False).exists()
        ]
    if args.limit:
        records = records[: args.limit]

    statuses = summarize_records(config, records, force=args.force)
    write_stage_status(config, "summary", statuses)
    print(
        json.dumps(
            {
                "total": len(statuses),
                "summarized": sum(row["status"] == "summarized" for row in statuses),
                "summary_failed": sum(row.get("failed_stage") == "summarize" for row in statuses),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
