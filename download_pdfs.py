#!/usr/bin/env python3
"""Download PDFs for exported Zotero records."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from pipeline_utils import (
    cfg_base_dir,
    cfg_get,
    compact_text,
    fail_record,
    load_config,
    normalize_arxiv_id,
    normalize_doi,
    now_iso,
    parse_csv_filter,
    pdf_path,
    project_path,
    read_jsonl,
    read_keys_file,
    read_metadata_records,
    retry_call,
    safe_key,
    select_records,
    status_jsonl,
    write_bytes,
    write_stage_status,
)


class PDFLinkParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() not in {"a", "link", "meta", "iframe", "embed"}:
            return
        attr_map = {key.lower(): value or "" for key, value in attrs}
        for attr in ("href", "content", "src"):
            href = attr_map.get(attr, "")
            if not href:
                continue
            lower = href.lower()
            if ".pdf" in lower or "/pdf" in lower or "download" in lower:
                self.links.append(urllib.parse.urljoin(self.base_url, href))


def is_probably_pdf(data: bytes, content_type: str = "") -> bool:
    if "pdf" in content_type.lower():
        return True
    return b"%PDF" in data[:1024]


def read_response_bytes(response: Any, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(1024 * 128)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if max_bytes and total > max_bytes:
            raise ValueError(f"download exceeds max size of {max_bytes} bytes")
    return b"".join(chunks)


def fetch_url(url: str, timeout: int, max_bytes: int, accept_pdf: bool = False) -> tuple[bytes, str, str]:
    headers = {
        "User-Agent": "zotero-paper-pipeline/0.1",
    }
    if accept_pdf:
        headers["Accept"] = "application/pdf,text/html;q=0.9,*/*;q=0.8"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get("Content-Type", "")
        final_url = response.geturl()
        data = read_response_bytes(response, max_bytes)
    return data, content_type, final_url


def save_pdf_bytes(path: Path, data: bytes) -> None:
    if not is_probably_pdf(data):
        raise ValueError("response is not a PDF")
    write_bytes(path, data)


def copy_local_pdf(config: dict[str, Any], source: str, dest: Path) -> None:
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme == "file":
        source_path = Path(urllib.request.url2pathname(parsed.path))
    else:
        source_path = (
            project_path(source, base_dir=cfg_base_dir(config))
            if not Path(source).is_absolute()
            else Path(source)
        )
    if not source_path.exists():
        raise FileNotFoundError(f"local PDF not found: {source_path}")
    data = source_path.read_bytes()
    if not is_probably_pdf(data, mimetypes.guess_type(source_path.name)[0] or ""):
        raise ValueError(f"local file is not a PDF: {source_path}")
    write_bytes(dest, data)


def find_pdf_links(html: bytes, base_url: str) -> list[str]:
    parser = PDFLinkParser(base_url)
    try:
        parser.feed(html[:2_000_000].decode("utf-8", errors="ignore"))
    except Exception:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for link in parser.links:
        if link not in seen:
            seen.add(link)
            result.append(link)
    return result


def arxiv_pdf_url(arxiv_id: str) -> str:
    normalized = normalize_arxiv_id(arxiv_id)
    if not normalized:
        return ""
    return f"https://arxiv.org/pdf/{normalized}.pdf"


def candidate_sources(config: dict[str, Any], metadata: dict[str, Any]) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    pdf_url = compact_text(metadata.get("pdf_url"))
    if pdf_url:
        candidates.append(("zotero_pdf_url", pdf_url))
    arxiv_id = normalize_arxiv_id(metadata.get("arxiv_id"))
    if arxiv_id:
        candidates.append(("arxiv_id", arxiv_pdf_url(arxiv_id)))
    doi = normalize_doi(metadata.get("doi"))
    email = compact_text(cfg_get(config, "download.unpaywall_email", ""))
    if doi and email:
        query = urllib.parse.urlencode({"email": email})
        candidates.append(("unpaywall", f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi, safe='')}?{query}"))
    if doi:
        candidates.append(("doi", f"https://doi.org/{urllib.parse.quote(doi, safe='/')}"))
    url = compact_text(metadata.get("url"))
    if url:
        candidates.append(("url", url))
    return candidates


def try_candidate(
    config: dict[str, Any],
    label: str,
    url: str,
    dest: Path,
    timeout: int,
    max_bytes: int,
    derived_link_limit: int,
) -> tuple[bool, str, str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in {"", "file"}:
        copy_local_pdf(config, url, dest)
        return True, label, url
    if parsed.scheme not in {"http", "https"}:
        return False, label, f"unsupported URL scheme: {parsed.scheme}"

    data, content_type, final_url = fetch_url(url, timeout, max_bytes, accept_pdf=label == "doi")
    if label == "unpaywall":
        try:
            payload = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError as exc:
            return False, label, f"invalid Unpaywall JSON: {exc}"
        locations = []
        best = payload.get("best_oa_location") if isinstance(payload, dict) else None
        if isinstance(best, dict):
            locations.append(best)
        if isinstance(payload, dict) and isinstance(payload.get("oa_locations"), list):
            locations.extend(item for item in payload["oa_locations"] if isinstance(item, dict))
        for location in locations:
            for field in ("url_for_pdf", "url"):
                pdf_link = compact_text(location.get(field))
                if not pdf_link:
                    continue
                try:
                    pdf_data, pdf_content_type, pdf_final_url = fetch_url(
                        pdf_link, timeout, max_bytes, accept_pdf=True
                    )
                    if is_probably_pdf(pdf_data, pdf_content_type):
                        save_pdf_bytes(dest, pdf_data)
                        return True, f"{label}:{field}", pdf_final_url
                except Exception:
                    continue
        return False, label, f"no open PDF found from {final_url}"

    if is_probably_pdf(data, content_type):
        save_pdf_bytes(dest, data)
        return True, label, final_url

    for pdf_link in find_pdf_links(data, final_url)[:derived_link_limit]:
        try:
            pdf_data, pdf_content_type, pdf_final_url = fetch_url(
                pdf_link, timeout, max_bytes, accept_pdf=True
            )
            if is_probably_pdf(pdf_data, pdf_content_type):
                save_pdf_bytes(dest, pdf_data)
                return True, f"{label}:html_pdf_link", pdf_final_url
        except Exception:
            continue
    return False, label, f"no PDF found from {url}"


def classify_failure(label: str, exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"{label}: http_{exc.code}: {compact_text(exc.reason)}"
    if isinstance(exc, urllib.error.URLError):
        return f"{label}: network_error: {compact_text(exc.reason)}"
    if isinstance(exc, TimeoutError):
        return f"{label}: timeout: {exc}"
    if isinstance(exc, ValueError):
        return f"{label}: invalid_or_oversized_pdf: {exc}"
    if isinstance(exc, OSError):
        return f"{label}: io_error: {exc}"
    return f"{label}: {type(exc).__name__}: {exc}"


def valid_existing_pdf(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        with path.open("rb") as fh:
            return b"%PDF" in fh.read(1024)
    except OSError:
        return False


def download_one(
    config: dict[str, Any],
    metadata: dict[str, Any],
    force: bool,
) -> dict[str, Any]:
    key = safe_key(metadata["zotero_item_key"])
    dest = pdf_path(config, key)
    if not force and valid_existing_pdf(dest):
        return {
            "zotero_item_key": key,
            "citation_key": metadata.get("citation_key", ""),
            "title": metadata.get("title", ""),
            "status": "pdf_downloaded",
            "pdf_path": str(dest),
            "download_source": "existing_pdf",
            "download_url": "",
            "updated_at": now_iso(),
        }

    timeout = int(cfg_get(config, "download.timeout_seconds", 45))
    max_bytes = int(cfg_get(config, "download.max_pdf_mb", 150)) * 1024 * 1024
    derived_link_limit = int(cfg_get(config, "download.derived_link_limit", 6))
    failures: list[str] = []

    attempts = int(cfg_get(config, "retry.max_attempts", 1) or 1)
    initial_delay = float(cfg_get(config, "retry.initial_delay_seconds", 1) or 1)
    max_delay = float(cfg_get(config, "retry.max_delay_seconds", 30) or 30)

    for label, url in candidate_sources(config, metadata):
        try:
            success, source, resolved = retry_call(
                lambda label=label, url=url: try_candidate(
                    config, label, url, dest, timeout, max_bytes, derived_link_limit
                ),
                attempts=attempts,
                initial_delay=initial_delay,
                max_delay=max_delay,
                retry_exceptions=(urllib.error.URLError, TimeoutError, OSError, ValueError),
            )
            if success:
                return {
                    "zotero_item_key": key,
                    "citation_key": metadata.get("citation_key", ""),
                    "title": metadata.get("title", ""),
                    "status": "pdf_downloaded",
                    "pdf_path": str(dest),
                    "download_source": source,
                    "download_url": resolved,
                    "updated_at": now_iso(),
                }
            failures.append(f"{source}: {resolved}")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            failures.append(classify_failure(label, exc))
        except Exception as exc:
            failures.append(classify_failure(label, exc))

    reason = "; ".join(failures) if failures else "no pdf_url, arxiv_id, doi, or url available"
    return fail_record(metadata, "pdf_failed", "download", reason, pdf_path=str(dest))


def failed_keys(config: dict[str, Any]) -> set[str]:
    rows = read_jsonl(status_jsonl(config, "download"))
    return {
        str(row.get("zotero_item_key", ""))
        for row in rows
        if row.get("status") == "pdf_failed" or row.get("failed_stage") == "download"
    }


def download_records(config: dict[str, Any], records: list[dict[str, Any]], force: bool) -> list[dict[str, Any]]:
    workers = int(cfg_get(config, "concurrency.download", 1) or 1)
    if workers <= 1 or len(records) <= 1:
        return [download_one(config, record, force=force) for record in records]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(download_one, config, record, force) for record in records]
        return [future.result() for future in futures]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--force", action="store_true", help="redownload even if {key}.pdf exists")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--only-missing", action="store_true", help="only process records without a valid PDF")
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
            if not valid_existing_pdf(pdf_path(config, str(record.get("zotero_item_key", "")), create=False))
        ]
    if args.limit:
        records = records[: args.limit]

    statuses = download_records(config, records, force=args.force)
    write_stage_status(config, "download", statuses)

    print(
        json.dumps(
            {
                "total": len(statuses),
                "pdf_downloaded": sum(row["status"] == "pdf_downloaded" for row in statuses),
                "pdf_failed": sum(row["status"] == "pdf_failed" for row in statuses),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
