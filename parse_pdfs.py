#!/usr/bin/env python3
"""Extract text from downloaded PDFs."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from pipeline_utils import (
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
    safe_key,
    select_records,
    status_jsonl,
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


def extract_with_ocr(config: dict[str, Any], path: Path, timeout: int) -> tuple[str, int]:
    binary = shutil.which("ocrmypdf")
    if not binary:
        raise RuntimeError("ocr_required: ocrmypdf is not installed")
    language = str(cfg_get(config, "parse.ocr_language", "eng") or "eng")
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        proc = subprocess.run(
            [binary, "--skip-text", "-l", language, str(path), tmp.name],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            reason = compact_text(proc.stderr) or f"ocrmypdf exited {proc.returncode}"
            raise RuntimeError(f"ocr_failed: {reason}")
        try:
            return extract_with_pypdf(Path(tmp.name), int(cfg_get(config, "parse.max_pages", 0)))
        except Exception:
            return extract_with_pdftotext(Path(tmp.name), timeout)


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

    if bool(cfg_get(config, "parse.ocr_enabled", False)):
        try:
            text, page_count = extract_with_ocr(config, path, timeout)
            if text.strip():
                return text, page_count, "ocrmypdf"
            errors.append("ocrmypdf produced empty text")
        except Exception as exc:
            errors.append(str(exc))
    else:
        errors.append("ocr_required: ordinary PDF text extraction produced no usable text")

    raise RuntimeError("; ".join(errors))


def parse_one(config: dict[str, Any], metadata: dict[str, Any], force: bool) -> dict[str, Any]:
    key = safe_key(metadata["zotero_item_key"])
    pdf = pdf_path(config, key, create=False)
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
        message = str(exc)
        status = "ocr_required" if "ocr_required" in message else "parse_failed"
        if "ocr_failed" in message:
            status = "ocr_failed"
        return fail_record(
            metadata,
            status,
            "parse",
            message,
            pdf_path=str(pdf),
            text_path=str(text_file),
        )


def parse_records(config: dict[str, Any], records: list[dict[str, Any]], force: bool) -> list[dict[str, Any]]:
    workers = int(cfg_get(config, "concurrency.parse", 1) or 1)
    if workers <= 1 or len(records) <= 1:
        return [parse_one(config, record, force=force) for record in records]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(parse_one, config, record, force) for record in records]
        return [future.result() for future in futures]


def failed_keys(config: dict[str, Any]) -> set[str]:
    rows = read_jsonl(status_jsonl(config, "parse"))
    return {
        str(row.get("zotero_item_key", ""))
        for row in rows
        if row.get("failed_stage") == "parse"
        or row.get("status") in {"parse_failed", "ocr_required", "ocr_failed"}
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--force", action="store_true", help="reparse even if {key}.txt exists")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--only-missing", action="store_true", help="only process records without parsed text")
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
            if not text_path(config, str(record.get("zotero_item_key", "")), create=False).exists()
        ]
    if args.limit:
        records = records[: args.limit]

    statuses = parse_records(config, records, force=args.force)
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
