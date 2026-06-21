from argparse import Namespace

from PIL import Image

from scripts.recover_ocr_needed_with_vlm import (
    A4_250DPI_HEIGHT,
    A4_250DPI_WIDTH,
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
