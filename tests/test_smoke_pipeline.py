from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def minimal_pdf_bytes(text: str) -> bytes:
    stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET\n"
    objects = [
        "1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        "2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            "3 0 obj\n"
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\n"
            "endobj\n"
        ),
        "4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
        f"5 0 obj\n<< /Length {len(stream.encode('ascii'))} >>\nstream\n{stream}endstream\nendobj\n",
    ]
    content = "%PDF-1.4\n"
    offsets = [0]
    for obj in objects:
        offsets.append(len(content.encode("ascii")))
        content += obj
    xref_offset = len(content.encode("ascii"))
    content += f"xref\n0 {len(objects) + 1}\n"
    content += "0000000000 65535 f \n"
    for offset in offsets[1:]:
        content += f"{offset:010d} 00000 n \n"
    content += (
        "trailer\n"
        f"<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        "startxref\n"
        f"{xref_offset}\n"
        "%%EOF\n"
    )
    return content.encode("ascii")


def run_step(tmp_path: Path, script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / script), *args],
        cwd=tmp_path,
        check=True,
        text=True,
        capture_output=True,
    )


def test_end_to_end_smoke_pipeline(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "smoke.pdf").write_bytes(
        minimal_pdf_bytes("Smoke test paper text about robot planning and evaluation.")
    )
    (tmp_path / "pdf_summary_prompt_template.md").write_text(
        (ROOT / "pdf_summary_prompt_template.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "data" / "zotero_export.json").write_text(
        json.dumps(
            [
                {
                    "key": "SMOKE",
                    "title": "Smoke Test Paper",
                    "creators": [{"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}],
                    "date": "2026",
                    "DOI": "10.1000/smoke",
                    "url": "https://example.com/smoke",
                    "abstractNote": "This paper studies robot planning.",
                    "tags": [{"tag": "robotics"}],
                    "pdf_url": "fixtures/smoke.pdf",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
zotero:
  source: file
  input_path: data/zotero_export.json
export:
  include_raw_zotero: true
paths:
  metadata_jsonl: data/metadata.jsonl
  metadata_dir: data/metadata
  pdf_dir: data/pdfs
  text_dir: data/text
  summary_dir: data/summaries
  status_dir: data/status
  download_status_jsonl: data/download_status.jsonl
  parse_status_jsonl: data/parse_status.jsonl
  summary_status_jsonl: data/summary_status.jsonl
parse:
  min_text_chars: 10
llm:
  provider: mock
  prompt_template_path: pdf_summary_prompt_template.md
  max_input_chars: 60000
""".strip(),
        encoding="utf-8",
    )

    run_step(tmp_path, "export_zotero.py", "--config", str(config_path))
    run_step(tmp_path, "download_pdfs.py", "--config", str(config_path))
    run_step(tmp_path, "parse_pdfs.py", "--config", str(config_path))
    run_step(tmp_path, "summarize_papers.py", "--config", str(config_path))
    run_step(tmp_path, "validate_mapping.py", "--config", str(config_path))
    run_step(tmp_path, "report_pipeline.py", "--config", str(config_path))

    assert (tmp_path / "data" / "metadata" / "SMOKE.json").exists()
    assert (tmp_path / "data" / "pdfs" / "SMOKE.pdf").exists()
    assert (tmp_path / "data" / "text" / "SMOKE.txt").exists()
    summary = tmp_path / "data" / "summaries" / "SMOKE.md"
    assert summary.exists()
    assert "## 15. 高质量证据片段" in summary.read_text(encoding="utf-8")
    assert (tmp_path / "data" / "report.md").exists()
    assert (tmp_path / "data" / "manifest.json").exists()
