#!/usr/bin/env python3
"""Export/normalize Zotero metadata into JSONL keyed by zotero_item_key."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from pipeline_utils import (
    REQUIRED_METADATA_FIELDS,
    cfg_get,
    cfg_path,
    compact_text,
    die,
    index_by_key,
    metadata_jsonl,
    metadata_path,
    normalize_arxiv_id,
    normalize_doi,
    now_iso,
    project_path,
    read_json,
    read_jsonl,
    read_metadata_records,
    retry_call,
    safe_key,
    write_json,
    write_jsonl,
)


def item_data(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data")
    if isinstance(data, dict):
        merged = dict(item)
        merged.update(data)
        return merged
    return item


def collect_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("items", "records", "results", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if any(k in data for k in ("key", "zotero_item_key", "title", "data")):
            return [data]
    raise ValueError("source must be a JSON object/list or JSONL rows")


def load_source_file(path: str | Path) -> list[dict[str, Any]]:
    p = project_path(path)
    if not p.exists():
        die(f"Zotero source file not found: {p}")
    if p.suffix.lower() == ".jsonl":
        return read_jsonl(p)
    return collect_items(read_json(p))


def fetch_zotero_api(config: dict[str, Any]) -> list[dict[str, Any]]:
    library_id = str(cfg_get(config, "zotero.library_id", "") or "").strip()
    if not library_id:
        die("zotero.library_id is required when zotero.source is zotero_api")

    api_key_env = str(cfg_get(config, "zotero.api_key_env", "ZOTERO_API_KEY"))
    api_key = os.getenv(api_key_env, "")
    if not api_key:
        die(f"environment variable {api_key_env} is required for Zotero API export")

    library_type = str(cfg_get(config, "zotero.library_type", "user")).strip().lower()
    if library_type not in {"user", "group"}:
        die("zotero.library_type must be user or group")
    segment = "users" if library_type == "user" else "groups"

    collection_key = str(cfg_get(config, "zotero.collection_key", "") or "").strip()
    collection_part = f"/collections/{urllib.parse.quote(collection_key)}/items" if collection_key else "/items"
    base_url = str(cfg_get(config, "zotero.api_base_url", "https://api.zotero.org")).rstrip("/")
    timeout = int(cfg_get(config, "zotero.timeout_seconds", 60) or 60)
    attempts = int(cfg_get(config, "retry.max_attempts", 1) or 1)
    initial_delay = float(cfg_get(config, "retry.initial_delay_seconds", 1) or 1)
    max_delay = float(cfg_get(config, "retry.max_delay_seconds", 30) or 30)

    limit = int(cfg_get(config, "zotero.page_size", 100))
    start = 0
    items: list[dict[str, Any]] = []
    total_results: int | None = None

    def fetch_json(url: str) -> tuple[list[dict[str, Any]], int | None]:
        def request_once() -> tuple[list[dict[str, Any]], int | None]:
            request = urllib.request.Request(
                url,
                headers={
                    "Zotero-API-Key": api_key,
                    "Zotero-API-Version": "3",
                    "User-Agent": "zotero-paper-pipeline/0.2",
                },
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                page = json.loads(response.read().decode("utf-8"))
                total_header = response.headers.get("Total-Results")
            if not isinstance(page, list):
                raise ValueError("unexpected Zotero API response; expected a list")
            total = int(total_header) if total_header and total_header.isdigit() else None
            return [item for item in page if isinstance(item, dict)], total

        return retry_call(
            request_once,
            attempts=attempts,
            initial_delay=initial_delay,
            max_delay=max_delay,
            retry_exceptions=(urllib.error.URLError, TimeoutError, ValueError),
        )

    while True:
        query = urllib.parse.urlencode(
            {
                "format": "json",
                "include": "data",
                "limit": limit,
                "start": start,
            }
        )
        url = f"{base_url}/{segment}/{urllib.parse.quote(library_id)}{collection_part}?{query}"
        page, total = fetch_json(url)
        if total is not None:
            total_results = total
        items.extend(page)
        start += len(page)
        if len(page) < limit or (total_results is not None and start >= total_results):
            break

    if bool(cfg_get(config, "zotero.fetch_child_attachments", True)):
        seen = {compact_text(item_data(item).get("key") or item.get("key")) for item in items}
        parent_keys = [
            compact_text(item_data(item).get("key") or item.get("key"))
            for item in items
            if compact_text(item_data(item).get("itemType")) != "attachment"
        ]
        for parent_key in parent_keys:
            if not parent_key:
                continue
            child_query = urllib.parse.urlencode({"format": "json", "include": "data", "limit": limit})
            child_url = (
                f"{base_url}/{segment}/{urllib.parse.quote(library_id)}/items/"
                f"{urllib.parse.quote(parent_key)}/children?{child_query}"
            )
            try:
                children, _total = fetch_json(child_url)
            except Exception:
                continue
            for child in children:
                child_key = compact_text(item_data(child).get("key") or child.get("key"))
                if child_key and child_key not in seen:
                    seen.add(child_key)
                    items.append(child)
    return items


def value_from(record: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in record and record[name] not in (None, ""):
            return record[name]
    return ""


def extract_citation_key(record: dict[str, Any]) -> str:
    direct = value_from(record, "citation_key", "citationKey", "citekey", "cite_key")
    if direct:
        return compact_text(direct)
    extra = str(record.get("extra", "") or "")
    for pattern in (
        r"(?im)^(?:citation key|citationkey|bbt citation key)\s*:\s*(.+)$",
        r"(?im)^tex\.ids\s*:\s*(.+)$",
    ):
        match = re.search(pattern, extra)
        if match:
            return compact_text(match.group(1).split(",")[0])
    return ""


def extract_authors(record: dict[str, Any]) -> list[str]:
    authors = value_from(record, "authors", "author")
    if isinstance(authors, str) and compact_text(authors):
        return [compact_text(part) for part in re.split(r"\s+and\s+|;", authors) if compact_text(part)]
    if isinstance(authors, list) and authors and all(isinstance(item, str) for item in authors):
        return [compact_text(item) for item in authors if compact_text(item)]

    creators = record.get("creators") or authors
    result: list[str] = []
    if isinstance(creators, list):
        for creator in creators:
            if isinstance(creator, str):
                name = compact_text(creator)
            elif isinstance(creator, dict):
                if creator.get("creatorType") not in (None, "", "author"):
                    continue
                name = compact_text(
                    creator.get("name")
                    or creator.get("literal")
                    or " ".join(
                        part
                        for part in [
                            compact_text(creator.get("firstName") or creator.get("given")),
                            compact_text(creator.get("lastName") or creator.get("family")),
                        ]
                        if part
                    )
                )
            else:
                name = ""
            if name:
                result.append(name)
    return result


def extract_year(record: dict[str, Any]) -> str:
    direct = value_from(record, "year")
    if direct:
        return str(direct)
    issued = record.get("issued")
    if isinstance(issued, dict):
        parts = issued.get("date-parts")
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            return str(parts[0][0])
    date = compact_text(value_from(record, "date", "publicationDate", "datePublished"))
    match = re.search(r"(18|19|20)\d{2}", date)
    return match.group(0) if match else ""


def extract_arxiv_id(record: dict[str, Any]) -> str:
    direct = value_from(record, "arxiv_id", "arxivId", "arXiv", "eprint")
    if direct:
        return normalize_arxiv_id(direct)
    haystack = "\n".join(
        compact_text(value_from(record, name))
        for name in ("extra", "url", "URL", "doi", "DOI", "archiveLocation")
    )
    patterns = [
        r"(?i)arxiv(?:\s*id)?\s*:\s*([a-z\-]+/\d{7}|\d{4}\.\d{4,5}(?:v\d+)?)",
        r"(?i)arxiv\.org/(?:abs|pdf)/([^?#\s]+)",
        r"(?i)10\.48550/arxiv\.([^?#\s]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, haystack)
        if match:
            return normalize_arxiv_id(match.group(1))
    return ""


def extract_tags(record: dict[str, Any]) -> list[str]:
    tags = record.get("tags") or record.get("keywords") or []
    if isinstance(tags, str):
        return [compact_text(tag) for tag in re.split(r"[,;]", tags) if compact_text(tag)]
    result: list[str] = []
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, str):
                value = tag
            elif isinstance(tag, dict):
                value = tag.get("tag") or tag.get("name") or tag.get("label") or ""
            else:
                value = ""
            value = compact_text(value)
            if value:
                result.append(value)
    return result


def link_href(item: dict[str, Any], *names: str) -> str:
    links = item.get("links")
    if not isinstance(links, dict):
        return ""
    for name in names:
        link = links.get(name)
        if isinstance(link, dict) and link.get("href"):
            return compact_text(link["href"])
    return ""


def collect_pdf_attachment_urls(items: list[dict[str, Any]]) -> dict[str, str]:
    attachments: dict[str, str] = {}
    for item in items:
        record = item_data(item)
        item_type = compact_text(record.get("itemType"))
        if item_type != "attachment":
            continue
        content_type = compact_text(record.get("contentType")).lower()
        url = compact_text(record.get("url") or link_href(item, "enclosure", "alternate"))
        title = compact_text(record.get("title")).lower()
        if "pdf" not in content_type and ".pdf" not in url.lower() and "pdf" not in title:
            continue
        parent_key = compact_text(record.get("parentItem") or item.get("parentItem"))
        if parent_key and url and parent_key not in attachments:
            attachments[parent_key] = url
    return attachments


def normalize_item(
    item: dict[str, Any],
    attachment_urls: dict[str, str],
    include_raw: bool,
) -> dict[str, Any] | None:
    record = item_data(item)
    if compact_text(record.get("itemType")) == "attachment":
        return None

    key = compact_text(
        value_from(record, "zotero_item_key", "key")
        or value_from(item, "zotero_item_key", "key")
        or value_from(record, "id")
    )
    if not key:
        return None
    key = safe_key(key)

    url = compact_text(value_from(record, "url", "URL"))
    doi = normalize_doi(value_from(record, "doi", "DOI"))
    pdf_url = compact_text(
        value_from(record, "pdf_url", "pdfUrl", "PDF", "pdf")
        or attachment_urls.get(key, "")
        or link_href(item, "enclosure")
    )
    normalized = {
        "zotero_item_key": key,
        "citation_key": extract_citation_key(record),
        "title": compact_text(value_from(record, "title")),
        "authors": extract_authors(record),
        "year": extract_year(record),
        "doi": doi,
        "arxiv_id": extract_arxiv_id(record),
        "url": url,
        "pdf_url": pdf_url,
        "abstract": compact_text(value_from(record, "abstract", "abstractNote")),
        "tags": extract_tags(record),
        "status": "exported",
        "exported_at": now_iso(),
        "zotero_version": record.get("version") or item.get("version") or "",
        "zotero_date_modified": compact_text(record.get("dateModified") or record.get("date_modified")),
    }
    for field in REQUIRED_METADATA_FIELDS:
        normalized.setdefault(field, "" if field != "authors" and field != "tags" else [])
    if include_raw:
        normalized["raw_zotero"] = item
    return normalized


def load_items(config: dict[str, Any], source_override: str | None) -> list[dict[str, Any]]:
    if source_override:
        return load_source_file(source_override)
    source = str(cfg_get(config, "zotero.source", "file")).strip().lower()
    if source == "file":
        input_path = cfg_path(config, "zotero.input_path", "data/zotero_export.json")
        return load_source_file(input_path)
    if source in {"api", "zotero_api"}:
        return fetch_zotero_api(config)
    die(f"unsupported zotero.source: {source}")
    return []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--source", help="override zotero.input_path and read this JSON/JSONL file")
    parser.add_argument("--replace", action="store_true", help="replace metadata.jsonl instead of merging by key")
    parser.add_argument("--limit", type=int, default=0, help="process at most N source items")
    args = parser.parse_args()

    from pipeline_utils import load_config

    config = load_config(args.config)
    raw_items = load_items(config, args.source)
    if args.limit:
        raw_items = raw_items[: args.limit]

    attachment_urls = collect_pdf_attachment_urls(raw_items)
    include_raw = bool(cfg_get(config, "export.include_raw_zotero", True))
    normalized = [
        item
        for item in (
            normalize_item(raw_item, attachment_urls, include_raw=include_raw) for raw_item in raw_items
        )
        if item is not None
    ]

    existing: dict[str, dict[str, Any]] = {}
    if not args.replace and metadata_jsonl(config).exists():
        existing, _duplicates = index_by_key(read_metadata_records(config))

    for record in normalized:
        existing[record["zotero_item_key"]] = record

    records = sorted(existing.values(), key=lambda row: row["zotero_item_key"])
    write_jsonl(metadata_jsonl(config), records)
    for record in records:
        write_json(metadata_path(config, record["zotero_item_key"]), record)

    print(
        json.dumps(
            {
                "metadata_jsonl": str(metadata_jsonl(config)),
                "source_items": len(raw_items),
                "exported_or_updated": len(normalized),
                "total_metadata_records": len(records),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
