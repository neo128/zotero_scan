from __future__ import annotations

from export_zotero import (
    extract_arxiv_id,
    extract_authors,
    extract_citation_key,
    extract_tags,
)


def test_extract_citation_key_from_extra() -> None:
    record = {"extra": "Citation Key: smith2024paper\narXiv: 2305.16291"}
    assert extract_citation_key(record) == "smith2024paper"


def test_extract_authors_from_zotero_creators() -> None:
    record = {
        "creators": [
            {"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"},
            {"creatorType": "editor", "firstName": "Ignored", "lastName": "Editor"},
            {"creatorType": "author", "name": "Grace Hopper"},
        ]
    }
    assert extract_authors(record) == ["Ada Lovelace", "Grace Hopper"]


def test_extract_arxiv_id_from_url_and_doi() -> None:
    assert extract_arxiv_id({"url": "https://arxiv.org/abs/2305.16291"}) == "2305.16291"
    assert extract_arxiv_id({"DOI": "10.48550/arxiv.2401.00001"}) == "2401.00001"


def test_extract_tags_from_mixed_zotero_tags() -> None:
    record = {"tags": [{"tag": "robotics"}, {"name": "planning"}, "control"]}
    assert extract_tags(record) == ["robotics", "planning", "control"]
