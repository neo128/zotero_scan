#!/usr/bin/env python3
"""Shared helpers for the Zotero paper pipeline."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


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


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def project_path(path: str | Path) -> Path:
    p = Path(path).expanduser()
    if p.is_absolute():
        return p
    return Path.cwd() / p


def ensure_dir(path: str | Path) -> Path:
    p = project_path(path)
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
    with tmp.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
    tmp.replace(p)


def read_json(path: str | Path) -> Any:
    p = project_path(path)
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str | Path, data: Any) -> None:
    p = project_path(path)
    ensure_dir(p.parent)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(p)


def read_text(path: str | Path) -> str:
    p = project_path(path)
    return p.read_text(encoding="utf-8")


def write_text(path: str | Path, text: str) -> None:
    p = project_path(path)
    ensure_dir(p.parent)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)


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


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    p = project_path(path)
    if not p.exists():
        raise FileNotFoundError(f"config file not found: {p}")
    try:
        import yaml  # type: ignore

        with p.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except ModuleNotFoundError:
        data = _load_minimal_yaml(p)
    if not isinstance(data, dict):
        raise ValueError(f"config root must be a mapping: {p}")
    return data


def cfg_get(config: dict[str, Any], dotted: str, default: Any = None) -> Any:
    current: Any = config
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def metadata_jsonl(config: dict[str, Any]) -> Path:
    return project_path(cfg_get(config, "paths.metadata_jsonl", "data/metadata.jsonl"))


def metadata_dir(config: dict[str, Any]) -> Path:
    return ensure_dir(cfg_get(config, "paths.metadata_dir", "data/metadata"))


def pdf_dir(config: dict[str, Any]) -> Path:
    return ensure_dir(cfg_get(config, "paths.pdf_dir", "data/pdfs"))


def text_dir(config: dict[str, Any]) -> Path:
    return ensure_dir(cfg_get(config, "paths.text_dir", "data/text"))


def summary_dir(config: dict[str, Any]) -> Path:
    return ensure_dir(cfg_get(config, "paths.summary_dir", "data/summaries"))


def status_dir(config: dict[str, Any]) -> Path:
    return ensure_dir(cfg_get(config, "paths.status_dir", "data/status"))


def status_jsonl(config: dict[str, Any], stage: str) -> Path:
    key = f"paths.{stage}_status_jsonl"
    return project_path(cfg_get(config, key, f"data/{stage}_status.jsonl"))


def metadata_path(config: dict[str, Any], key: str) -> Path:
    return metadata_dir(config) / f"{safe_key(key)}.json"


def pdf_path(config: dict[str, Any], key: str) -> Path:
    return pdf_dir(config) / f"{safe_key(key)}.pdf"


def text_path(config: dict[str, Any], key: str) -> Path:
    return text_dir(config) / f"{safe_key(key)}.txt"


def summary_path(config: dict[str, Any], key: str) -> Path:
    return summary_dir(config) / f"{safe_key(key)}.json"


def per_key_status_path(config: dict[str, Any], key: str) -> Path:
    return status_dir(config) / f"{safe_key(key)}.json"


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


def truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars]


def die(message: str, code: int = 1) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)

