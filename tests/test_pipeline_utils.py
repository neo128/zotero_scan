from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_utils import (
    load_config,
    metadata_jsonl,
    normalize_arxiv_id,
    normalize_doi,
    read_jsonl,
    safe_key,
    write_jsonl,
)


def test_safe_key_rejects_path_traversal() -> None:
    assert safe_key("ABC123") == "ABC123"
    with pytest.raises(ValueError):
        safe_key("../ABC123")
    with pytest.raises(ValueError):
        safe_key("")


def test_normalizers() -> None:
    assert normalize_doi("https://doi.org/10.1000/xyz") == "10.1000/xyz"
    assert normalize_doi("doi: 10.1000/xyz") == "10.1000/xyz"
    assert normalize_arxiv_id("https://arxiv.org/pdf/2305.16291.pdf") == "2305.16291"
    assert normalize_arxiv_id("arXiv:2305.16291v2") == "2305.16291v2"


def test_jsonl_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "rows.jsonl"
    write_jsonl(path, [{"b": 2, "a": 1}, {"zotero_item_key": "K"}])
    assert read_jsonl(path) == [{"a": 1, "b": 2}, {"zotero_item_key": "K"}]


def test_config_relative_paths_are_based_on_config_file(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
zotero:
  source: file
  input_path: data/zotero_export.json
paths:
  metadata_jsonl: data/metadata.jsonl
llm:
  provider: mock
""".strip(),
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert metadata_jsonl(config) == tmp_path / "data" / "metadata.jsonl"
