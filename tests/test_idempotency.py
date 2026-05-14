from __future__ import annotations

from pathlib import Path

from download_pdfs import download_one
from pipeline_utils import load_config, pdf_path, summary_path, write_text
from summarize_papers import mock_markdown_summary, summarize_one


def write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
paths:
  pdf_dir: data/pdfs
  text_dir: data/text
  summary_dir: data/summaries
  status_dir: data/status
download:
  timeout_seconds: 1
llm:
  provider: mock
  prompt_template_path: pdf_summary_prompt_template.md
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "pdf_summary_prompt_template.md").write_text("{{PDF_TEXT}}", encoding="utf-8")
    return config_path


def test_download_reuses_existing_valid_pdf(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    metadata = {
        "zotero_item_key": "IDEMP",
        "citation_key": "",
        "title": "Existing PDF",
        "pdf_url": "https://example.invalid/should-not-be-fetched.pdf",
    }
    pdf = pdf_path(config, "IDEMP")
    pdf.write_bytes(b"%PDF-1.4\nexisting\n")
    before_mtime = pdf.stat().st_mtime_ns

    status = download_one(config, metadata, force=False)

    assert status["status"] == "pdf_downloaded"
    assert status["download_source"] == "existing_pdf"
    assert pdf.read_bytes() == b"%PDF-1.4\nexisting\n"
    assert pdf.stat().st_mtime_ns == before_mtime


def test_summarize_reuses_existing_valid_markdown(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    metadata = {
        "zotero_item_key": "IDEMP",
        "citation_key": "",
        "title": "Existing Summary",
        "authors": ["Ada Lovelace"],
        "year": "2026",
        "tags": ["robotics"],
    }
    write_text(
        tmp_path / "data" / "text" / "IDEMP.txt",
        "This paper studies robot manipulation with language instructions.",
    )
    summary = summary_path(config, "IDEMP")
    summary.write_text(
        mock_markdown_summary(metadata, "This paper studies robot manipulation."),
        encoding="utf-8",
    )
    before_mtime = summary.stat().st_mtime_ns

    status = summarize_one(config, metadata, force=False)

    assert status["status"] == "summarized"
    assert status["validation"] == "ok"
    assert "llm_provider" not in status
    assert summary.stat().st_mtime_ns == before_mtime
