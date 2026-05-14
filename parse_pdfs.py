#!/usr/bin/env python3
"""Extract text from downloaded PDFs."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from pipeline_utils import (
    cfg_get,
    compact_text,
    fail_record,
    load_config,
    now_iso,
    pdf_path,
    read_metadata_records,
    safe_key,
    text_path,
    truncate,
    write_stage_status,
    write_text,
)


def extract_with_pypdf(path: Path, max_pages: int) -> tuple[str, int]:
    try:
        from pypdf import PdfReader  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError("pypdf is not installed") from exc

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            pass
    page_count = len(reader.pages)
    limit = min(max_pages, page_count) if max_pages else page_count
    chunks: list[str] = []
    for idx in range(limit):
        page = reader.pages[idx]
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            text = f"\n[page {idx + 1} extraction failed: {exc}]\n"
        if text.strip():
            chunks.append(text)
    return "\n\n".join(chunks), page_count


def extract_with_pdftotext(path: Path, timeout: int) -> tuple[str, int]:
    binary = shutil.which("pdftotext")
    if not binary:
        raise RuntimeError("pdftotext is not installed")
    proc = subprocess.run(
        [binary, "-layout", str(path), "-"],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(compact_text(proc.stderr) or f"pdftotext exited {proc.returncode}")
    return proc.stdout, 0


def extract_pdf_text(config: dict[str, Any], path: Path) -> tuple[str, int, str]:
    max_pages = int(cfg_get(config, "parse.max_pages", 0))
    timeout = int(cfg_get(config, "parse.timeout_seconds", 90))
    errors: list[str] = []
    try:
        text, page_count = extract_with_pypdf(path, max_pages=max_pages)
        if text.strip():
            return text, page_count, "pypdf"
        errors.append("pypdf produced empty text")
    except Exception as exc:
        errors.append(str(exc))

    try:
        text, page_count = extract_with_pdftotext(path, timeout=timeout)
        if text.strip():
            return text, page_count, "pdftotext"
        errors.append("pdftotext produced empty text")
    except Exception as exc:
        errors.append(str(exc))

    raise RuntimeError("; ".join(errors))


def parse_one(config: dict[str, Any], metadata: dict[str, Any], force: bool) -> dict[str, Any]:
    key = safe_key(metadata["zotero_item_key"])
    pdf = pdf_path(config, key)
    text_file = text_path(config, key)
    if not pdf.exists():
        return fail_record(metadata, "pdf_failed", "parse", f"missing PDF: {pdf}", pdf_path=str(pdf))

    if not force and text_file.exists() and text_file.stat().st_size > 0:
        text = text_file.read_text(encoding="utf-8", errors="ignore")
        return {
            "zotero_item_key": key,
            "citation_key": metadata.get("citation_key", ""),
            "title": metadata.get("title", ""),
            "status": "parsed",
            "pdf_path": str(pdf),
            "text_path": str(text_file),
            "char_count": len(text),
            "parser": "existing_text",
            "updated_at": now_iso(),
        }

    try:
        text, page_count, parser = extract_pdf_text(config, pdf)
        max_chars = int(cfg_get(config, "parse.max_text_chars", 0))
        text = truncate(text, max_chars)
        min_chars = int(cfg_get(config, "parse.min_text_chars", 100))
        if len(text.strip()) < min_chars:
            raise RuntimeError(f"extracted text shorter than parse.min_text_chars={min_chars}")
        write_text(text_file, text)
        return {
            "zotero_item_key": key,
            "citation_key": metadata.get("citation_key", ""),
            "title": metadata.get("title", ""),
            "status": "parsed",
            "pdf_path": str(pdf),
            "text_path": str(text_file),
            "char_count": len(text),
            "page_count": page_count,
            "parser": parser,
            "updated_at": now_iso(),
        }
    except Exception as exc:
        return fail_record(
            metadata,
            "pdf_downloaded",
            "parse",
            str(exc),
            pdf_path=str(pdf),
            text_path=str(text_file),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--force", action="store_true", help="reparse even if {key}.txt exists")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    config = load_config(args.config)
    records = read_metadata_records(config)
    if args.limit:
        records = records[: args.limit]

    statuses = [parse_one(config, record, force=args.force) for record in records]
    write_stage_status(config, "parse", statuses)
    print(
        json.dumps(
            {
                "total": len(statuses),
                "parsed": sum(row["status"] == "parsed" for row in statuses),
                "parse_failed": sum(row.get("failed_stage") == "parse" for row in statuses),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
