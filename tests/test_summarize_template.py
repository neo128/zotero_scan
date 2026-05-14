from __future__ import annotations

from pathlib import Path

from pipeline_utils import load_config, summary_path
from summarize_papers import (
    mock_markdown_summary,
    prompt_variables,
    render_prompt,
    validate_summary_markdown,
)


def test_render_prompt_replaces_all_variables() -> None:
    template = "{{PDF_FILENAME}} {{PDF_STEM}} {{ZOTERO_KEY}} {{ARXIV_ID}} {{PDF_TEXT}}"
    rendered = render_prompt(
        template,
        {
            "PDF_FILENAME": "A.pdf",
            "PDF_STEM": "A",
            "ZOTERO_KEY": "KEY",
            "ARXIV_ID": "2305.1",
            "PDF_TEXT": "text",
        },
    )
    assert rendered == "A.pdf A KEY 2305.1 text"


def test_summary_path_uses_markdown(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("llm:\n  provider: mock\n", encoding="utf-8")
    config = load_config(config_path)
    assert summary_path(config, "ABC", create=False).name == "ABC.md"


def test_mock_summary_matches_required_markdown_structure() -> None:
    metadata = {
        "zotero_item_key": "KEY",
        "title": "A Test Paper",
        "authors": ["Ada Lovelace"],
        "year": "2026",
        "doi": "10.1000/test",
        "arxiv_id": "2305.16291",
        "tags": ["robotics"],
    }
    markdown = mock_markdown_summary(metadata, "This paper studies robot planning. It reports results.")
    assert validate_summary_markdown(markdown) == []


def test_prompt_variables_use_pdf_filename_and_truncate_text(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
paths:
  pdf_dir: data/pdfs
llm:
  provider: mock
  max_input_chars: 4
""".strip(),
        encoding="utf-8",
    )
    config = load_config(config_path)
    variables = prompt_variables(config, {"zotero_item_key": "KEY", "arxiv_id": "1234.5"}, "abcdef")
    assert variables["PDF_FILENAME"] == "KEY.pdf"
    assert variables["PDF_STEM"] == "KEY"
    assert variables["PDF_TEXT"] == "abcd"
