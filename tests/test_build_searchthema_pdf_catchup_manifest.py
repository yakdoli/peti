from pathlib import Path

from scripts.build_searchthema_pdf_catchup_manifest import build_pdf_index, candidate_paths


def test_candidate_paths_rejects_item_pdf_outside_artifacts(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "artifacts"
    outside_pdf = tmp_path / "outside.pdf"
    outside_pdf.write_bytes(b"%PDF-1.4\n%%EOF")

    paths = candidate_paths(
        {"id": "doc", "pdf": {"path": str(outside_pdf)}},
        artifacts_root / "searchThema" / "metadata" / "items" / "doc.json",
        tmp_path,
        artifacts_root,
        {},
    )

    assert outside_pdf not in paths


def test_build_pdf_index_skips_symlink_escape(tmp_path: Path) -> None:
    pdf_dir = tmp_path / "artifacts" / "searchThema" / "pdfs"
    pdf_dir.mkdir(parents=True)
    outside_pdf = tmp_path / "outside.pdf"
    outside_pdf.write_bytes(b"%PDF-1.4\n%%EOF")
    (pdf_dir / "linked.pdf").symlink_to(outside_pdf)

    assert build_pdf_index(pdf_dir) == {}


def test_candidate_paths_rejects_symlink_inside_artifacts(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "artifacts"
    pdf_dir = artifacts_root / "searchThema" / "pdfs"
    pdf_dir.mkdir(parents=True)
    target = pdf_dir / "target.pdf"
    target.write_bytes(b"%PDF-1.4\n%%EOF")
    link = pdf_dir / "linked.pdf"
    link.symlink_to(target)

    paths = candidate_paths(
        {"id": "doc", "pdf": {"path": str(link)}},
        artifacts_root / "searchThema" / "metadata" / "items" / "doc.json",
        tmp_path,
        artifacts_root,
        {},
    )

    assert target not in paths


def test_candidate_paths_rejects_expected_path_symlink_escape(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "artifacts"
    expected_dir = artifacts_root / "searchThema" / "pdfs" / "2024" / "20240101"
    expected_dir.mkdir(parents=True)
    outside_pdf = tmp_path / "outside.pdf"
    outside_pdf.write_bytes(b"%PDF-1.4\n%%EOF")
    (expected_dir / "doc.pdf").symlink_to(outside_pdf)

    paths = candidate_paths(
        {"id": "doc", "date": "2024-01-01"},
        artifacts_root / "searchThema" / "metadata" / "items" / "doc.json",
        tmp_path,
        artifacts_root,
        {},
    )

    assert outside_pdf not in paths
