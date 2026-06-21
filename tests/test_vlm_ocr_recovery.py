from argparse import Namespace

from PIL import Image

from scripts.recover_ocr_needed_with_vlm import (
    A4_250DPI_HEIGHT,
    A4_250DPI_WIDTH,
    opencode_ocr_page,
    page_ocr_images,
)


def test_a4_250dpi_uses_single_page_image(tmp_path):
    page = tmp_path / "a4_250dpi.png"
    Image.new("RGB", (A4_250DPI_WIDTH, A4_250DPI_HEIGHT), "white").save(page)
    args = Namespace(max_side=A4_250DPI_HEIGHT)

    mode, images = page_ocr_images(page, tmp_path / "page", args)

    assert mode == "single_page"
    assert len(images) == 1
    assert images[0]["page_image"] == 1
    assert images[0]["bbox"] == [0, 0, A4_250DPI_WIDTH, A4_250DPI_HEIGHT]


def test_opencode_ocr_page_uses_file_attachment(monkeypatch, tmp_path):
    page = tmp_path / "page.png"
    Image.new("RGB", (100, 100), "white").save(page)
    seen = {}

    class Completed:
        returncode = 0
        stdout = '{"text":"관보","confidence":0.87,"notes":"ok"}'
        stderr = ""

    def fake_run(command, capture_output, text, timeout, check):
        seen["command"] = command
        seen["capture_output"] = capture_output
        seen["text"] = text
        seen["timeout"] = timeout
        seen["check"] = check
        return Completed()

    monkeypatch.setattr("scripts.recover_ocr_needed_with_vlm.subprocess.run", fake_run)

    result = opencode_ocr_page(
        page,
        model_id="opencode/nemotron-3-ultra-free",
        timeout=12,
        context="page=1",
    )

    assert result["status"] == "ok"
    assert result["text"] == "관보"
    assert result["engine"] == "opencode_cli"
    assert seen["command"][:5] == [
        "opencode",
        "run",
        "-m",
        "opencode/nemotron-3-ultra-free",
        "--file",
    ]
    assert seen["command"][5] == str(page)
    assert seen["command"][6] == "--"
    assert "Image context: page=1" in seen["command"][7]
    assert seen["timeout"] == 12
