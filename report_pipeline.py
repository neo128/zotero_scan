#!/usr/bin/env python3
"""Generate aggregate Markdown/JSON reports for the Zotero paper pipeline."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from pipeline_utils import (
    cfg_base_dir,
    load_config,
    manifest_path,
    metadata_jsonl,
    read_jsonl,
    read_metadata_records,
    report_json_path,
    report_markdown_path,
    status_jsonl,
    write_json,
    write_text,
)
from validate_mapping import validate


def file_sha256(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_stage_status(config: dict[str, Any], stage: str) -> list[dict[str, Any]]:
    path = status_jsonl(config, stage)
    if not path.exists():
        return []
    return read_jsonl(path)


def status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(str(row.get("status", "")) for row in rows if row.get("status")))


def failure_reasons(rows: list[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(str(row.get("failed_reason", "")) for row in rows if row.get("failed_reason")))


def metadata_distributions(records: list[dict[str, Any]]) -> dict[str, Any]:
    years = Counter(str(record.get("year", "")) for record in records if record.get("year"))
    tags: Counter[str] = Counter()
    for record in records:
        record_tags = record.get("tags")
        if isinstance(record_tags, list):
            tags.update(str(tag) for tag in record_tags if tag)
    return {
        "years": dict(years.most_common()),
        "top_tags": dict(tags.most_common(30)),
    }


def build_manifest(config: dict[str, Any]) -> dict[str, Any]:
    base_dir = cfg_base_dir(config)
    files = {
        "config": Path(str(config.get("__pipeline__", {}).get("config_path", base_dir / "config.yaml"))),
        "metadata_jsonl": metadata_jsonl(config),
        "download_status_jsonl": status_jsonl(config, "download"),
        "parse_status_jsonl": status_jsonl(config, "parse"),
        "summary_status_jsonl": status_jsonl(config, "summary"),
    }
    return {
        "base_dir": str(base_dir),
        "files": {
            name: {
                "path": str(path),
                "sha256": file_sha256(path),
            }
            for name, path in files.items()
        },
    }


def build_report(config: dict[str, Any]) -> dict[str, Any]:
    records = read_metadata_records(config)
    download_rows = read_stage_status(config, "download")
    parse_rows = read_stage_status(config, "parse")
    summary_rows = read_stage_status(config, "summary")
    return {
        "validation": validate(config),
        "metadata": metadata_distributions(records),
        "stages": {
            "download": {
                "counts": status_counts(download_rows),
                "failure_reasons": failure_reasons(download_rows),
            },
            "parse": {
                "counts": status_counts(parse_rows),
                "failure_reasons": failure_reasons(parse_rows),
            },
            "summary": {
                "counts": status_counts(summary_rows),
                "failure_reasons": failure_reasons(summary_rows),
            },
        },
        "manifest": build_manifest(config),
    }


def render_markdown_report(report: dict[str, Any]) -> str:
    validation = report["validation"]
    lines = [
        "# Zotero Paper Pipeline Report",
        "",
        "## Overview",
        "",
        f"- OK: {validation['ok']}",
        f"- Issue count: {validation['issue_count']}",
        f"- Metadata rows: {validation['counts']['metadata_rows']}",
        f"- Unique keys: {validation['counts']['unique_metadata_keys']}",
        f"- PDF files: {validation['counts']['pdf_files']}",
        f"- Summary files: {validation['counts']['summary_files']}",
        "",
        "## Stage Counts",
        "",
    ]
    for stage, stage_report in report["stages"].items():
        lines.append(f"### {stage}")
        if stage_report["counts"]:
            for status, count in stage_report["counts"].items():
                lines.append(f"- {status}: {count}")
        else:
            lines.append("- no status rows")
        lines.append("")

    lines.extend(["## Failures", ""])
    for key in (
        "missing_metadata",
        "missing_pdf",
        "invalid_pdf",
        "missing_summary",
        "invalid_summary",
        "download_failed",
        "parse_failed",
        "summary_failed",
        "orphan_pdf",
        "orphan_summary",
    ):
        value = validation.get(key)
        if value:
            lines.append(f"### {key}")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(value, ensure_ascii=False, indent=2))
            lines.append("```")
            lines.append("")

    lines.extend(["## Metadata Distribution", ""])
    top_tags = report["metadata"]["top_tags"]
    if top_tags:
        lines.append("### Top Tags")
        for tag, count in top_tags.items():
            lines.append(f"- {tag}: {count}")
        lines.append("")
    years = report["metadata"]["years"]
    if years:
        lines.append("### Years")
        for year, count in years.items():
            lines.append(f"- {year}: {count}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_report(config: dict[str, Any]) -> dict[str, Any]:
    report = build_report(config)
    write_json(report_json_path(config), report)
    write_text(report_markdown_path(config), render_markdown_report(report))
    write_json(manifest_path(config), report["manifest"])
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    report = write_report(config)
    print(
        json.dumps(
            {
                "report_markdown": str(report_markdown_path(config)),
                "report_json": str(report_json_path(config)),
                "manifest_json": str(manifest_path(config)),
                "ok": report["validation"]["ok"],
                "issue_count": report["validation"]["issue_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
