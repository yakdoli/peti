import json
from argparse import Namespace
from pathlib import Path

from scripts.benchmark_vlm_ocr_a4 import (
    BenchPage,
    build_payload,
    load_reference_text,
    reference_metrics,
)


def test_load_reference_text_prefers_agy_peer_correction(tmp_path):
    path = tmp_path / "peer_consensus.json"
    path.write_text(
        json.dumps(
            {
                "primary_text": "원 전사",
                "peers": {
                    "agy_cli": {
                        "corrected_text": "검증 교정문",
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert load_reference_text(path) == "검증 교정문"


def test_reference_metrics_ignore_whitespace_for_cer():
    metrics = reference_metrics("관 보\n1994. 1. 5.", "관보 1994.1.5.")

    assert metrics["distance"] == 0
    assert metrics["cer"] == 0.0
    assert metrics["similarity"] == 1.0


def test_build_payload_includes_fast_ocr_generation_settings():
    page = BenchPage(
        item_path=Path("item.json"),
        pdf_path=Path("item.pdf"),
        source="pety",
        page=1,
        image_path=Path("page.png"),
        width=2066,
        height=2924,
        input_image={"width": 1554, "height": 2200},
        render_sec=0.1,
        encode_sec=0.2,
        image_url="data:image/png;base64,AA==",
    )
    args = Namespace(
        model_id="model",
        dpi=250,
        temperature=0.2,
        top_p=0.8,
        top_k=20,
        min_p=0.0,
        presence_penalty=1.5,
        max_tokens=4096,
        seed=17,
        enable_thinking=False,
    )

    payload = build_payload(page, args)

    assert payload["temperature"] == 0.2
    assert payload["top_p"] == 0.8
    assert payload["top_k"] == 20
    assert payload["min_p"] == 0.0
    assert payload["presence_penalty"] == 1.5
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert "input_pixels=1554x2200" in payload["messages"][0]["content"][0]["text"]
