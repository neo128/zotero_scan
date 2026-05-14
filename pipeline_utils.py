#!/usr/bin/env python3
"""Shared helpers for the Zotero paper pipeline."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

REQUIRED_METADATA_FIELDS = [
    "zotero_item_key",
    "citation_key",
    "title",
    "authors",
    "year",
    "doi",
    "arxiv_id",
    "url",
    "pdf_url",
    "abstract",
    "tags",
]

SUMMARY_FIELDS = [
    "one_sentence",
    "problem",
    "method",
    "contribution",
    "experiments",
    "limitations",
    "relevance_to_embodied_ai",
    "keywords",
]

SUMMARY_MARKDOWN_SECTIONS = [
    "## 1. 论文基本信息",
    "## 2. 一句话总结",
    "## 3. 研究问题",
    "## 4. 方法概述",
    "## 5. 技术流程拆解",
    "## 6. 创新点评估",
    "## 7. 技术坐标系定位",
    "## 8. 实验与结果",
    "## 9. 局限性",
    "## 10. 失败模式",
    "## 11. 通用复用价值",
    "## 12. 分类标签",
    "## 13. 跨域适配与具身智能启发",
    "## 14. 潜在研究机会",
    "## 15. 高质量证据片段",
]

FAILED_STATUSES = {
    "pdf_failed",
    "parse_failed",
    "ocr_failed",
    "summary_failed",
}

CONFIG_META_KEY = "__pipeline__"

T = TypeVar("T")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def project_path(path: str | Path, base_dir: str | Path | None = None) -> Path:
    p = Path(path).expanduser()
    if p.is_absolute():
        return p
    return Path(base_dir).expanduser() / p if base_dir is not None else Path.cwd() / p


def ensure_dir(path: str | Path, base_dir: str | Path | None = None) -> Path:
    p = project_path(path, base_dir=base_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def safe_key(key: str) -> str:
    key = str(key).strip()
    if not key:
        raise ValueError("empty zotero_item_key")
    if "/" in key or "\\" in key or key in {".", ".."}:
        raise ValueError(f"unsafe zotero_item_key: {key!r}")
    return key


def compact_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_doi(value: Any) -> str:
    doi = compact_text(value)
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.I)
    return doi.strip()


def normalize_arxiv_id(value: Any) -> str:
    text = compact_text(value)
    if not text:
        return ""
    text = re.sub(r"^arxiv:\s*", "", text, flags=re.I)
    text = re.sub(r"^https?://arxiv\.org/(abs|pdf)/", "", text, flags=re.I)
    text = re.sub(r"\.pdf$", "", text, flags=re.I)
    return text.strip()


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    p = project_path(path)
    if not p.exists():
        return []
    records: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{p}:{line_no}: invalid JSONL: {exc}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"{p}:{line_no}: JSONL row must be an object")
            records.append(item)
    return records


def write_jsonl(path: str | Path, records: Iterable[dict[str, Any]]) -> None:
    p = project_path(path)
    ensure_dir(p.parent)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
                fh.write("\n")
        tmp.replace(p)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def read_json(path: str | Path) -> Any:
    p = project_path(path)
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str | Path, data: Any) -> None:
    p = project_path(path)
    ensure_dir(p.parent)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        tmp.replace(p)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def read_text(path: str | Path) -> str:
    p = project_path(path)
    return p.read_text(encoding="utf-8")


def write_text(path: str | Path, text: str) -> None:
    p = project_path(path)
    ensure_dir(p.parent)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(p)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def write_bytes(path: str | Path, data: bytes) -> None:
    p = project_path(path)
    ensure_dir(p.parent)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(p)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _strip_inline_comment(value: str) -> str:
    in_quote = ""
    escaped = False
    for idx, ch in enumerate(value):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch in {"'", '"'}:
            if in_quote == ch:
                in_quote = ""
            elif not in_quote:
                in_quote = ch
        if ch == "#" and not in_quote:
            return value[:idx].rstrip()
    return value


def _parse_yaml_scalar(raw: str) -> Any:
    raw = _strip_inline_comment(raw).strip()
    if raw == "":
        return ""
    if raw in {"null", "Null", "NULL", "~"}:
        return None
    if raw in {"true", "True", "TRUE"}:
        return True
    if raw in {"false", "False", "FALSE"}:
        return False
    if (raw.startswith('"') and raw.endswith('"')) or (
        raw.startswith("'") and raw.endswith("'")
    ):
        return raw[1:-1]
    if raw.startswith("[") and raw.endswith("]"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return [part.strip().strip("'\"") for part in raw[1:-1].split(",") if part.strip()]
    if re.fullmatch(r"-?\d+", raw):
        try:
            return int(raw)
        except ValueError:
            pass
    if re.fullmatch(r"-?\d+\.\d+", raw):
        try:
            return float(raw)
        except ValueError:
            pass
    return raw


def _load_minimal_yaml(path: Path) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]

    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if indent % 2:
            raise ValueError(f"{path}:{line_no}: indentation must use multiples of two spaces")
        line = raw_line.strip()
        if ":" not in line:
            raise ValueError(f"{path}:{line_no}: expected key: value")
        key, raw_value = line.split(":", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"{path}:{line_no}: empty key")

        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if raw_value.strip() == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _parse_yaml_scalar(raw_value)
    return root


def load_env_file(path: str | Path, override: bool = False) -> None:
    p = project_path(path)
    if not p.exists():
        return
    for line_no, raw_line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise ValueError(f"{p}:{line_no}: expected KEY=VALUE")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"{p}:{line_no}: invalid environment variable name")
        if not override and key in os.environ:
            continue
        value = _strip_inline_comment(raw_value).strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        os.environ[key] = value


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    p = project_path(path)
    if not p.exists():
        raise FileNotFoundError(f"config file not found: {p}")
    load_env_file(p.resolve().parent / ".env")
    try:
        import yaml  # type: ignore

        with p.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except ModuleNotFoundError:
        data = _load_minimal_yaml(p)
    if not isinstance(data, dict):
        raise ValueError(f"config root must be a mapping: {p}")
    data.setdefault(CONFIG_META_KEY, {})
    data[CONFIG_META_KEY]["config_path"] = str(p.resolve())
    data[CONFIG_META_KEY]["base_dir"] = str(p.resolve().parent)
    validate_config(data)
    return data


def cfg_get(config: dict[str, Any], dotted: str, default: Any = None) -> Any:
    current: Any = config
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def cfg_base_dir(config: dict[str, Any]) -> Path:
    meta = config.get(CONFIG_META_KEY)
    if isinstance(meta, dict) and meta.get("base_dir"):
        return Path(str(meta["base_dir"]))
    return Path.cwd()


def cfg_path(config: dict[str, Any], dotted: str, default: str | Path) -> Path:
    return project_path(cfg_get(config, dotted, default), base_dir=cfg_base_dir(config))


def cfg_dir(config: dict[str, Any], dotted: str, default: str | Path, create: bool = True) -> Path:
    path = cfg_path(config, dotted, default)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _validate_int(config: dict[str, Any], dotted: str, default: int, minimum: int = 0) -> None:
    value = cfg_get(config, dotted, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{dotted} must be an integer") from exc
    if parsed < minimum:
        raise ValueError(f"{dotted} must be >= {minimum}")


def _validate_float(config: dict[str, Any], dotted: str, default: float, minimum: float = 0) -> None:
    value = cfg_get(config, dotted, default)
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{dotted} must be a number") from exc
    if parsed < minimum:
        raise ValueError(f"{dotted} must be >= {minimum}")


def validate_config(config: dict[str, Any]) -> None:
    for dotted in (
        "zotero.page_size",
        "download.timeout_seconds",
        "download.max_pdf_mb",
        "download.derived_link_limit",
        "parse.timeout_seconds",
        "parse.max_pages",
        "parse.max_text_chars",
        "parse.min_text_chars",
        "llm.max_input_chars",
        "llm.max_output_tokens",
        "llm.timeout_seconds",
        "retry.max_attempts",
        "retry.initial_delay_seconds",
        "retry.max_delay_seconds",
        "concurrency.download",
        "concurrency.parse",
        "concurrency.summary",
    ):
        _validate_int(config, dotted, 0, 0)
    _validate_float(config, "llm.temperature", 0.1, 0)

    zotero_source = str(cfg_get(config, "zotero.source", "file")).strip().lower()
    if zotero_source not in {"file", "api", "zotero_api"}:
        raise ValueError("zotero.source must be file, api, or zotero_api")

    provider = str(cfg_get(config, "llm.provider", "mock")).strip().lower()
    if provider not in {"mock", "openai", "openai_compatible", "chat_completions"}:
        raise ValueError("llm.provider must be mock, openai, openai_compatible, or chat_completions")


def metadata_jsonl(config: dict[str, Any]) -> Path:
    return cfg_path(config, "paths.metadata_jsonl", "data/metadata.jsonl")


def metadata_dir(config: dict[str, Any], create: bool = True) -> Path:
    return cfg_dir(config, "paths.metadata_dir", "data/metadata", create=create)


def pdf_dir(config: dict[str, Any], create: bool = True) -> Path:
    return cfg_dir(config, "paths.pdf_dir", "data/pdfs", create=create)


def text_dir(config: dict[str, Any], create: bool = True) -> Path:
    return cfg_dir(config, "paths.text_dir", "data/text", create=create)


def summary_dir(config: dict[str, Any], create: bool = True) -> Path:
    return cfg_dir(config, "paths.summary_dir", "data/summaries", create=create)


def status_dir(config: dict[str, Any], create: bool = True) -> Path:
    return cfg_dir(config, "paths.status_dir", "data/status", create=create)


def status_jsonl(config: dict[str, Any], stage: str) -> Path:
    key = f"paths.{stage}_status_jsonl"
    return cfg_path(config, key, f"data/{stage}_status.jsonl")


def report_markdown_path(config: dict[str, Any]) -> Path:
    return cfg_path(config, "paths.report_markdown", "data/report.md")


def report_json_path(config: dict[str, Any]) -> Path:
    return cfg_path(config, "paths.report_json", "data/report.json")


def summary_template_path(config: dict[str, Any]) -> Path:
    return cfg_path(config, "llm.prompt_template_path", "pdf_summary_prompt_template.md")


def manifest_path(config: dict[str, Any]) -> Path:
    return cfg_path(config, "paths.manifest_json", "data/manifest.json")


def metadata_path(config: dict[str, Any], key: str, create: bool = True) -> Path:
    return metadata_dir(config, create=create) / f"{safe_key(key)}.json"


def pdf_path(config: dict[str, Any], key: str, create: bool = True) -> Path:
    return pdf_dir(config, create=create) / f"{safe_key(key)}.pdf"


def text_path(config: dict[str, Any], key: str, create: bool = True) -> Path:
    return text_dir(config, create=create) / f"{safe_key(key)}.txt"


def summary_path(config: dict[str, Any], key: str, create: bool = True) -> Path:
    return summary_dir(config, create=create) / f"{safe_key(key)}.md"


def legacy_summary_json_path(config: dict[str, Any], key: str, create: bool = True) -> Path:
    return summary_dir(config, create=create) / f"{safe_key(key)}.json"


def per_key_status_path(config: dict[str, Any], key: str, create: bool = True) -> Path:
    return status_dir(config, create=create) / f"{safe_key(key)}.json"


def read_metadata_records(config: dict[str, Any]) -> list[dict[str, Any]]:
    records = read_jsonl(metadata_jsonl(config))
    for record in records:
        if "zotero_item_key" not in record:
            raise ValueError("metadata record missing zotero_item_key")
        safe_key(record["zotero_item_key"])
    return records


def index_by_key(records: Iterable[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    indexed: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    for record in records:
        key = str(record.get("zotero_item_key", "")).strip()
        if not key:
            duplicates.append("")
            continue
        if key in indexed:
            duplicates.append(key)
        indexed[key] = record
    return indexed, duplicates


def write_stage_status(
    config: dict[str, Any],
    stage: str,
    records: Iterable[dict[str, Any]],
) -> None:
    sorted_records = sorted(records, key=lambda row: row.get("zotero_item_key", ""))
    write_jsonl(status_jsonl(config, stage), sorted_records)
    for record in sorted_records:
        key = record.get("zotero_item_key")
        if not key:
            continue
        existing: dict[str, Any] = {}
        path = per_key_status_path(config, str(key))
        if path.exists():
            try:
                existing = read_json(path)
            except Exception:
                existing = {}
        existing[stage] = record
        write_json(path, existing)


def fail_record(
    metadata: dict[str, Any],
    status: str,
    stage: str,
    reason: str,
    **extra: Any,
) -> dict[str, Any]:
    record = {
        "zotero_item_key": metadata.get("zotero_item_key", ""),
        "citation_key": metadata.get("citation_key", ""),
        "title": metadata.get("title", ""),
        "status": status,
        "failed_stage": stage,
        "failed_reason": compact_text(reason),
        "updated_at": now_iso(),
    }
    record.update(extra)
    return record


def retry_call(
    func: Callable[[], T],
    *,
    attempts: int = 1,
    initial_delay: float = 0,
    max_delay: float = 30,
    retry_exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    attempts = max(1, int(attempts))
    delay = max(0.0, float(initial_delay))
    max_delay = max(delay, float(max_delay))
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except retry_exceptions as exc:
            last_exc = exc
            if attempt == attempts:
                break
            if delay:
                time.sleep(delay)
                delay = min(max_delay, delay * 2 if delay else max_delay)
    assert last_exc is not None
    raise last_exc


def select_records(
    records: list[dict[str, Any]],
    *,
    keys: set[str] | None = None,
    tags: set[str] | None = None,
    years: set[str] | None = None,
    citation_keys: set[str] | None = None,
    limit: int = 0,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for record in records:
        key = str(record.get("zotero_item_key", ""))
        if keys is not None and key not in keys:
            continue
        if citation_keys is not None and str(record.get("citation_key", "")) not in citation_keys:
            continue
        if years is not None and str(record.get("year", "")) not in years:
            continue
        if tags is not None:
            record_tags = record.get("tags")
            tag_values = {str(tag) for tag in record_tags} if isinstance(record_tags, list) else set()
            if not tags.intersection(tag_values):
                continue
        selected.append(record)
        if limit and len(selected) >= limit:
            break
    return selected


def parse_csv_filter(value: str | None) -> set[str] | None:
    if not value:
        return None
    return {part.strip() for part in value.split(",") if part.strip()}


def read_keys_file(path: str | Path | None) -> set[str] | None:
    if not path:
        return None
    text = read_text(path)
    keys = {line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")}
    return keys or set()


def truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars]


def die(message: str, code: int = 1) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)
