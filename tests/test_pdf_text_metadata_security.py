from pathlib import Path

import pytest

from src.pdf_text_metadata import generate_source_text_metadata


def test_generate_source_text_metadata_skips_symlinked_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "artifacts"
    pdf_dir = root / "pety" / "pdfs"
    pdf_dir.mkdir(parents=True)
    outside_pdf = tmp_path / "outside.pdf"
    outside_pdf.write_bytes(b"%PDF-1.4\n%%EOF")
    (pdf_dir / "linked.pdf").symlink_to(outside_pdf)
    original_is_file = Path.is_file

    def reject_symlink_stat(path: Path) -> bool:
        if path.is_symlink():
            raise AssertionError("symlink target was statted before trust validation")
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", reject_symlink_stat)

    summary = generate_source_text_metadata("pety", artifacts_root=root)

    assert summary["total_pdfs"] == 0
    assert summary["processed"] == 0
