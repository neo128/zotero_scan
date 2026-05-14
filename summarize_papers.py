#!/usr/bin/env python3
"""Generate structured JSON summaries from parsed paper text."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.request
from typing import Any

from pipeline_utils import (
    SUMMARY_FIELDS,
    cfg_get,
    compact_text,
    fail_record,
    load_config,
    now_iso,
    read_json,
    read_metadata_records,
    read_text,
    safe_key,
    summary_path,
    text_path,
    truncate,
    write_json,
    write_stage_status,
)


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


def mock_summary(metadata: dict[str, Any], text: str) -> dict[str, Any]:
    abstract = compact_text(metadata.get("abstract"))
    title = compact_text(metadata.get("title"))
    source = abstract or text
    one_sentence = first_sentence(source, fallback=title)
    return {
        "one_sentence": one_sentence,
        "problem": first_sentence(abstract or text[:2500], fallback=""),
        "method": "",
        "contribution": "",
        "experiments": "",
        "limitations": "",
        "relevance_to_embodied_ai": "",
        "keywords": title_keywords(metadata),
    }


def build_prompt(metadata: dict[str, Any], text: str, max_input_chars: int) -> list[dict[str, str]]:
    paper_text = truncate(text, max_input_chars)
    metadata_block = json.dumps(
        {
            "zotero_item_key": metadata.get("zotero_item_key", ""),
            "citation_key": metadata.get("citation_key", ""),
            "title": metadata.get("title", ""),
            "authors": metadata.get("authors", []),
            "year": metadata.get("year", ""),
            "doi": metadata.get("doi", ""),
            "arxiv_id": metadata.get("arxiv_id", ""),
            "url": metadata.get("url", ""),
            "abstract": metadata.get("abstract", ""),
            "tags": metadata.get("tags", []),
        },
        ensure_ascii=False,
    )
    field_list = ", ".join(SUMMARY_FIELDS)
    return [
        {
            "role": "system",
            "content": (
                "You summarize academic papers as strict JSON. "
                "Return only one JSON object. Do not include markdown."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Read the metadata and paper text, then return a JSON object with exactly these fields: {field_list}.\n"
                "Use concise English. keywords must be a JSON array of short strings. "
                "If a field is not supported by the paper text, use an empty string instead of inventing details.\n\n"
                f"METADATA:\n{metadata_block}\n\nPAPER_TEXT:\n{paper_text}"
            ),
        },
    ]


def extract_json_object(content: str) -> dict[str, Any]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        data = json.loads(content[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("LLM response must be a JSON object")
    return data


def openai_compatible_summary(config: dict[str, Any], metadata: dict[str, Any], text: str) -> dict[str, Any]:
    api_key_env = str(cfg_get(config, "llm.api_key_env", "OPENAI_API_KEY"))
    api_key = os.getenv(api_key_env, "")
    if not api_key:
        raise RuntimeError(f"environment variable {api_key_env} is not set")

    base_url = str(cfg_get(config, "llm.base_url", "https://api.openai.com/v1")).rstrip("/")
    model = str(cfg_get(config, "llm.model", "") or "").strip()
    if not model:
        raise RuntimeError("llm.model is required for openai_compatible provider")

    max_input_chars = int(cfg_get(config, "llm.max_input_chars", 60000))
    timeout = int(cfg_get(config, "llm.timeout_seconds", 120))
    payload: dict[str, Any] = {
        "model": model,
        "messages": build_prompt(metadata, text, max_input_chars),
        "temperature": float(cfg_get(config, "llm.temperature", 0.1)),
    }
    if bool(cfg_get(config, "llm.response_format_json", True)):
        payload["response_format"] = {"type": "json_object"}
    max_tokens = int(cfg_get(config, "llm.max_output_tokens", 1200))
    if max_tokens:
        payload["max_tokens"] = max_tokens

    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "zotero-paper-pipeline/0.1",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = json.loads(response.read().decode("utf-8"))
    content = raw["choices"][0]["message"]["content"]
    return extract_json_object(content)


def normalize_summary(summary: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for field in SUMMARY_FIELDS:
        value = summary.get(field, [] if field == "keywords" else "")
        if field == "keywords":
            if isinstance(value, str):
                value = [part.strip() for part in re.split(r"[,;]", value) if part.strip()]
            elif not isinstance(value, list):
                value = []
            normalized[field] = [compact_text(item) for item in value if compact_text(item)]
        else:
            normalized[field] = compact_text(value)
    return normalized


def generate_summary(config: dict[str, Any], metadata: dict[str, Any], text: str) -> tuple[dict[str, Any], str, str]:
    provider = str(cfg_get(config, "llm.provider", "mock")).strip().lower()
    if provider == "mock":
        summary = mock_summary(metadata, text)
        return normalize_summary(summary), provider, "mock"
    if provider in {"openai", "openai_compatible", "chat_completions"}:
        summary = openai_compatible_summary(config, metadata, text)
        return normalize_summary(summary), provider, str(cfg_get(config, "llm.model", ""))
    raise RuntimeError(f"unsupported llm.provider: {provider}")


def summarize_one(config: dict[str, Any], metadata: dict[str, Any], force: bool) -> dict[str, Any]:
    key = safe_key(metadata["zotero_item_key"])
    text_file = text_path(config, key)
    output = summary_path(config, key)
    if not force and output.exists():
        try:
            existing = normalize_summary(read_json(output))
            return {
                "zotero_item_key": key,
                "citation_key": metadata.get("citation_key", ""),
                "title": metadata.get("title", ""),
                "status": "summarized",
                "summary_path": str(output),
                "summary_fields": list(existing.keys()),
                "updated_at": now_iso(),
            }
        except Exception:
            pass

    if not text_file.exists():
        return fail_record(
            metadata,
            "parsed",
            "summarize",
            f"missing parsed text: {text_file}",
            text_path=str(text_file),
            summary_path=str(output),
        )

    try:
        text = read_text(text_file)
        if not text.strip():
            raise RuntimeError(f"parsed text is empty: {text_file}")
        summary, provider, model = generate_summary(config, metadata, text)
        payload = {
            "zotero_item_key": key,
            "citation_key": metadata.get("citation_key", ""),
            "title": metadata.get("title", ""),
            "status": "summarized",
            "summarized_at": now_iso(),
            "llm_provider": provider,
            "llm_model": model,
            **summary,
        }
        write_json(output, payload)
        return {
            "zotero_item_key": key,
            "citation_key": metadata.get("citation_key", ""),
            "title": metadata.get("title", ""),
            "status": "summarized",
            "summary_path": str(output),
            "llm_provider": provider,
            "llm_model": model,
            "updated_at": now_iso(),
        }
    except Exception as exc:
        return fail_record(
            metadata,
            "parsed",
            "summarize",
            str(exc),
            text_path=str(text_file),
            summary_path=str(output),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--force", action="store_true", help="resummarize even if {key}.json exists")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    config = load_config(args.config)
    records = read_metadata_records(config)
    if args.limit:
        records = records[: args.limit]

    statuses = [summarize_one(config, record, force=args.force) for record in records]
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

