import json
from pathlib import Path

from scripts.update_pdf_text_metadata_items import (
    classify_existing_unextractable_candidate,
    classify_item_for_update,
    existing_metadata_current,
)


def write_item(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_existing_metadata_requires_ocr_skip_for_text_extractable() -> None:
    item = {
        "pdf": {"path": "artifacts/searchThema/pdfs/2024/20240101/a.pdf", "status": "completed"},
        "pdf_text": {
            "analysis_scope": "first_3_pages",
            "pdf_path_text": "artifacts/searchThema/pdfs/2024/20240101/a.pdf",
            "text_extractable": True,
            "pdf_text_class": "text_extractable",
            "needs_ocr": False,
        },
        "ocr": {"status": "pending", "skip_reason": ""},
    }

    assert existing_metadata_current(item, "artifacts/searchThema/pdfs/2024/20240101/a.pdf", 3) is False

    item["ocr"] = {"status": "skipped_text_extractable", "skip_reason": "text_extractable_pdf"}

    assert existing_metadata_current(item, "artifacts/searchThema/pdfs/2024/20240101/a.pdf", 3) is True


def test_classify_item_for_update_detects_missing_pdf_text(tmp_path: Path) -> None:
    item_path = tmp_path / "item.json"
    write_item(
        item_path,
        {"pdf": {"path": "artifacts/searchThema/pdfs/2024/20240101/a.pdf", "status": "completed"}},
    )

    assert (
        classify_item_for_update(item_path, max_pages=3, force=False, include_non_completed=False)
        == "needs_update"
    )


def test_classify_item_for_update_skips_current_metadata(tmp_path: Path) -> None:
    item_path = tmp_path / "item.json"
    write_item(
        item_path,
        {
            "pdf": {"path": "artifacts/searchThema/pdfs/2024/20240101/a.pdf", "status": "completed"},
            "pdf_text": {
                "analysis_scope": "first_3_pages",
                "pdf_path_text": "artifacts/searchThema/pdfs/2024/20240101/a.pdf",
                "text_extractable": False,
                "pdf_text_class": "image_or_scanned",
                "needs_ocr": True,
            },
        },
    )

    assert classify_item_for_update(item_path, max_pages=3, force=False, include_non_completed=False) == "current"


def test_classify_item_for_update_marks_legacy_missing_class_stale(tmp_path: Path) -> None:
    item_path = tmp_path / "item.json"
    write_item(
        item_path,
        {
            "pdf": {"path": "artifacts/searchThema/pdfs/2024/20240101/a.pdf", "status": "completed"},
            "pdf_text": {
                "analysis_scope": "first_3_pages",
                "pdf_path_text": "artifacts/searchThema/pdfs/2024/20240101/a.pdf",
                "text_extractable": False,
            },
        },
    )

    assert (
        classify_item_for_update(item_path, max_pages=3, force=False, include_non_completed=False)
        == "needs_update"
    )


def test_classify_existing_unextractable_candidate_selects_existing_false(tmp_path: Path) -> None:
    item_path = tmp_path / "item.json"
    write_item(
        item_path,
        {
            "pdf": {"path": "artifacts/searchThema/pdfs/2024/20240101/a.pdf", "status": "completed"},
            "pdf_text": {"text_extractable": False},
        },
    )

    assert (
        classify_existing_unextractable_candidate(
            item_path,
            max_pages=3,
            force=False,
            include_non_completed=False,
        )
        == "existing_unextractable"
    )


def test_classify_existing_unextractable_candidate_skips_existing_true(tmp_path: Path) -> None:
    item_path = tmp_path / "item.json"
    write_item(
        item_path,
        {
            "pdf": {"path": "artifacts/searchThema/pdfs/2024/20240101/a.pdf", "status": "completed"},
            "pdf_text": {"text_extractable": True},
        },
    )

    assert (
        classify_existing_unextractable_candidate(
            item_path,
            max_pages=3,
            force=False,
            include_non_completed=False,
        )
        == "existing_text_extractable"
    )


def test_classify_existing_unextractable_candidate_skips_current_false(tmp_path: Path) -> None:
    item_path = tmp_path / "item.json"
    write_item(
        item_path,
        {
            "pdf": {"path": "artifacts/searchThema/pdfs/2024/20240101/a.pdf", "status": "completed"},
            "pdf_text": {
                "analysis_scope": "first_3_pages",
                "pdf_path_text": "artifacts/searchThema/pdfs/2024/20240101/a.pdf",
                "text_extractable": False,
                "pdf_text_class": "image_or_scanned",
                "needs_ocr": True,
            },
        },
    )

    assert (
        classify_existing_unextractable_candidate(
            item_path,
            max_pages=3,
            force=False,
            include_non_completed=False,
        )
        == "current_unextractable"
    )
