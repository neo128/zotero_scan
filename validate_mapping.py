#!/usr/bin/env python3
"""Validate zotero_item_key mapping across metadata, PDFs, and summaries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pipeline_utils import (
    index_by_key,
    load_config,
    metadata_jsonl,
    metadata_path,
    pdf_dir,
    pdf_path,
    read_jsonl,
    safe_key,
    status_jsonl,
    summary_dir,
    summary_path,
)
from summarize_papers import validate_summary_markdown


def valid_pdf(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        with path.open("rb") as fh:
            return b"%PDF" in fh.read(1024)
    except OSError:
        return False


def summary_validation_errors(path: Path) -> list[str]:
    if not path.exists():
        return ["missing summary markdown"]
    try:
        data = path.read_text(encoding="utf-8")
    except Exception:
        return ["summary markdown is unreadable"]
    return validate_summary_markdown(data)


def read_status(config: dict[str, Any], stage: str) -> list[dict[str, Any]]:
    path = status_jsonl(config, stage)
    if not path.exists():
        return []
    return read_jsonl(path)


def keyed_status_failures(rows: list[dict[str, Any]], stage: str) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for row in rows:
        if row.get("failed_reason") or row.get("failed_stage") == stage:
            failures.append(
                {
                    "zotero_item_key": str(row.get("zotero_item_key", "")),
                    "status": str(row.get("status", "")),
                    "failed_stage": str(row.get("failed_stage", stage)),
                    "failed_reason": str(row.get("failed_reason", "")),
                }
            )
    return failures


def orphan_keys(directory: Path, suffix: str, valid_keys: set[str]) -> list[str]:
    if not directory.exists():
        return []
    keys: list[str] = []
    for path in directory.glob(f"*{suffix}"):
        key = path.name[: -len(suffix)]
        if key not in valid_keys:
            keys.append(key)
    return sorted(keys)


def validate(config: dict[str, Any]) -> dict[str, Any]:
    metadata_rows = read_jsonl(metadata_jsonl(config))
    metadata_by_key, duplicates = index_by_key(metadata_rows)
    keys = sorted(metadata_by_key)

    missing_metadata_files: list[str] = []
    missing_pdfs: list[str] = []
    invalid_pdfs: list[str] = []
    missing_summaries: list[str] = []
    invalid_summaries: dict[str, list[str]] = {}
    invalid_status_transitions: dict[str, list[str]] = {}

    for key in keys:
        safe_key(key)
        if not metadata_path(config, key, create=False).exists():
            missing_metadata_files.append(key)

        pdf = pdf_path(config, key, create=False)
        if not pdf.exists():
            missing_pdfs.append(key)
        elif not valid_pdf(pdf):
            invalid_pdfs.append(key)

        summary = summary_path(config, key, create=False)
        if not summary.exists():
            missing_summaries.append(key)
        else:
            errors = summary_validation_errors(summary)
            if errors:
                invalid_summaries[key] = errors

    download_rows = read_status(config, "download")
    summary_rows = read_status(config, "summary")
    parse_rows = read_status(config, "parse")
    status_by_stage = {
        "download": {str(row.get("zotero_item_key", "")): row for row in download_rows},
        "parse": {str(row.get("zotero_item_key", "")): row for row in parse_rows},
        "summary": {str(row.get("zotero_item_key", "")): row for row in summary_rows},
    }
    for key in keys:
        issues: list[str] = []
        download_status = str(status_by_stage["download"].get(key, {}).get("status", ""))
        parse_status = str(status_by_stage["parse"].get(key, {}).get("status", ""))
        summary_status = str(status_by_stage["summary"].get(key, {}).get("status", ""))
        if parse_status == "parsed" and download_status not in {"", "pdf_downloaded"}:
            issues.append("parse status is parsed but download status is not pdf_downloaded")
        if summary_status == "summarized" and parse_status not in {"", "parsed"}:
            issues.append("summary status is summarized but parse status is not parsed")
        if issues:
            invalid_status_transitions[key] = issues

    pdf_directory = pdf_dir(config, create=False)
    summary_directory = summary_dir(config, create=False)

    report = {
        "counts": {
            "metadata_rows": len(metadata_rows),
            "unique_metadata_keys": len(keys),
            "pdf_files": len(list(pdf_directory.glob("*.pdf"))) if pdf_directory.exists() else 0,
            "summary_files": len(list(summary_directory.glob("*.md"))) if summary_directory.exists() else 0,
        },
        "duplicates": sorted(set(duplicates)),
        "missing_metadata": sorted(missing_metadata_files),
        "missing_pdf": sorted(missing_pdfs),
        "invalid_pdf": sorted(invalid_pdfs),
        "missing_summary": sorted(missing_summaries),
        "invalid_summary": invalid_summaries,
        "download_failed": keyed_status_failures(download_rows, "download"),
        "parse_failed": keyed_status_failures(parse_rows, "parse"),
        "summary_failed": keyed_status_failures(summary_rows, "summarize"),
        "invalid_status_transition": invalid_status_transitions,
        "orphan_pdf": orphan_keys(pdf_directory, ".pdf", set(keys)),
        "orphan_summary": orphan_keys(summary_directory, ".md", set(keys)),
    }
    issue_count = 0
    for name, value in report.items():
        if name == "counts":
            continue
        issue_count += len(value)
    report["ok"] = issue_count == 0
    report["issue_count"] = issue_count
    return report


def print_text_report(report: dict[str, Any]) -> None:
    print(f"ok: {report['ok']}")
    print(json.dumps(report["counts"], ensure_ascii=False, indent=2))
    for section, value in report.items():
        if section in {"ok", "counts", "issue_count"}:
            continue
        if value:
            print(f"\n{section}:")
            print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    parser.add_argument("--no-fail", action="store_true", help="always exit 0")
    args = parser.parse_args()

    config = load_config(args.config)
    report = validate(config)
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_text_report(report)
    if not args.no_fail and not report["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
