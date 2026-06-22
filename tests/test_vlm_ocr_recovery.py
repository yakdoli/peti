from argparse import Namespace
from pathlib import Path

from PIL import Image

from scripts.recover_ocr_needed_with_vlm import (
    A4_250DPI_HEIGHT,
    A4_250DPI_WIDTH,
    QWEN_VL_250DPI_BINARIZE_PREPROCESSOR,
    QWEN_VL_250DPI_GRAY_PREPROCESSOR,
    QWEN_VL_250DPI_PREPROCESSOR,
    QWEN_VL_250DPI_SHARP_PREPROCESSOR,
    claude_ocr_page,
    choose_final_text,
    extract_json_object,
    opencode_ocr_page,
    page_ocr_images,
    prepare_ocr_image_bytes,
    qwen_ocr_page,
    recovery_scope,
    run_peer_cli,
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


def test_qwen_vl_250dpi_preprocess_preserves_a4_target_size(tmp_path):
    page = tmp_path / "a4_250dpi.png"
    Image.new("RGB", (A4_250DPI_WIDTH, A4_250DPI_HEIGHT), "white").save(page)

    data, metadata = prepare_ocr_image_bytes(
        page,
        max_side=A4_250DPI_HEIGHT,
        preprocess=QWEN_VL_250DPI_PREPROCESSOR,
        upscale=1.0,
    )

    assert data.startswith(b"\x89PNG")
    assert metadata["source_width"] == A4_250DPI_WIDTH
    assert metadata["source_height"] == A4_250DPI_HEIGHT
    assert metadata["width"] == A4_250DPI_WIDTH
    assert metadata["height"] == A4_250DPI_HEIGHT
    assert metadata["preprocess"] == QWEN_VL_250DPI_PREPROCESSOR
    assert metadata["resized"] is False
    assert metadata["operations"][:4] == ["grayscale", "autocontrast", "median3", "unsharp"]


def test_quality_preprocess_variants_preserve_page_size(tmp_path):
    page = tmp_path / "a4_250dpi.png"
    Image.new("RGB", (A4_250DPI_WIDTH, A4_250DPI_HEIGHT), "white").save(page)

    for preprocess in (
        QWEN_VL_250DPI_GRAY_PREPROCESSOR,
        QWEN_VL_250DPI_SHARP_PREPROCESSOR,
        QWEN_VL_250DPI_BINARIZE_PREPROCESSOR,
    ):
        data, metadata = prepare_ocr_image_bytes(
            page,
            max_side=A4_250DPI_HEIGHT,
            preprocess=preprocess,
            upscale=1.0,
        )

        assert data.startswith(b"\x89PNG")
        assert metadata["width"] == A4_250DPI_WIDTH
        assert metadata["height"] == A4_250DPI_HEIGHT
        assert metadata["preprocess"] == preprocess
        assert metadata["operations"][:2] == ["grayscale", "autocontrast"]


def test_qwen_ocr_page_uses_optimized_250dpi_payload(monkeypatch, tmp_path):
    page = tmp_path / "page.png"
    Image.new("RGB", (A4_250DPI_WIDTH, A4_250DPI_HEIGHT), "white").save(page)
    seen = {}

    def fake_chat_completion(endpoint_url, payload, timeout):
        seen["endpoint_url"] = endpoint_url
        seen["payload"] = payload
        seen["timeout"] = timeout
        return {
            "choices": [{"finish_reason": "stop", "message": {"content": '{"text":"관보","confidence":0.91,"notes":"ok"}'}}],
            "usage": {"completion_tokens": 2, "prompt_tokens": 100, "total_tokens": 102},
        }

    monkeypatch.setattr("scripts.recover_ocr_needed_with_vlm.openai_chat_completion", fake_chat_completion)

    result = qwen_ocr_page(
        page,
        endpoint_url="http://127.0.0.1:30000",
        model_id="HauhauCS/Qwen3.5-9B-Uncensored-HauhauCS-Aggressive",
        timeout=11,
        max_tokens=4096,
        seed=19,
        max_side=A4_250DPI_HEIGHT,
        image_preprocess=QWEN_VL_250DPI_PREPROCESSOR,
        image_upscale=1.0,
        temperature=0.2,
        top_p=0.8,
        top_k=20,
        min_p=0.0,
        presence_penalty=1.5,
        enable_thinking=False,
        context="page=1",
    )

    payload = seen["payload"]
    image_url = payload["messages"][0]["content"][1]["image_url"]["url"]
    assert seen["endpoint_url"] == "http://127.0.0.1:30000"
    assert seen["timeout"] == 11
    assert image_url.startswith("data:image/png;base64,")
    assert payload["temperature"] == 0.2
    assert payload["top_p"] == 0.8
    assert payload["top_k"] == 20
    assert payload["min_p"] == 0.0
    assert payload["presence_penalty"] == 1.5
    assert payload["max_tokens"] == 4096
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert result["status"] == "ok"
    assert result["text"] == "관보"
    assert result["input_image"]["preprocess"] == QWEN_VL_250DPI_PREPROCESSOR
    assert result["input_image"]["width"] == A4_250DPI_WIDTH
    assert result["input_image"]["height"] == A4_250DPI_HEIGHT
    assert result["generation"]["temperature"] == 0.2
    assert result["generation"]["top_k"] == 20
    assert result["generation"]["min_p"] == 0.0
    assert result["usage"]["total_tokens"] == 102
    assert result["finish_reason"] == "stop"


def test_qwen_recovery_scope_includes_preprocess_and_generation_settings():
    args = Namespace(
        ocr_backend="qwen_vllm",
        max_pages=1,
        dpi=250,
        max_side=A4_250DPI_HEIGHT,
        image_preprocess=QWEN_VL_250DPI_PREPROCESSOR,
        image_upscale=1.0,
        temperature=0.2,
        top_p=0.8,
        top_k=20,
        min_p=0.0,
        presence_penalty=1.5,
        enable_thinking=False,
    )

    assert recovery_scope(args).endswith(
        "_preprocessqwen_vl_250dpi_up1_temp0.2_tp0.8_tk20_mp0_pp1.5_think0"
    )


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
        model_id="zai-coding-plan/glm-5.2",
        agent_id="peti-ocr-primary",
        timeout=12,
        max_side=100,
        context="page=1",
    )

    assert result["status"] == "ok"
    assert result["text"] == "관보"
    assert result["engine"] == "opencode_cli"
    assert result["agent"] == "peti-ocr-primary"
    assert result["pure"] is False
    assert result["skip_permissions"] is True
    assert seen["command"][:7] == [
        "opencode",
        "run",
        "--dangerously-skip-permissions",
        "--agent",
        "peti-ocr-primary",
        "-m",
        "zai-coding-plan/glm-5.2",
    ]
    file_index = seen["command"].index("--file")
    assert seen["command"][file_index + 1] == str(page)
    prompt_index = seen["command"].index("--") + 1
    assert "Image context: page=1" in seen["command"][prompt_index]
    assert seen["timeout"] == 12


def test_opencode_ocr_page_downscales_file_attachment(monkeypatch, tmp_path):
    page = tmp_path / "page.png"
    Image.new("RGB", (200, 100), "white").save(page)
    seen = {}

    class Completed:
        returncode = 0
        stdout = '{"text":"관보","confidence":0.87,"notes":"ok"}'
        stderr = ""

    def fake_run(command, capture_output, text, timeout, check):
        attachment = command[command.index("--file") + 1]
        with Image.open(attachment) as image:
            seen["size"] = image.size
        seen["attachment"] = attachment
        return Completed()

    monkeypatch.setattr("scripts.recover_ocr_needed_with_vlm.subprocess.run", fake_run)

    result = opencode_ocr_page(
        page,
        model_id="zai-coding-plan/glm-5.2",
        agent_id="peti-ocr-primary",
        timeout=12,
        max_side=50,
        context="page=1",
    )

    assert result["status"] == "ok"
    assert seen["attachment"] != str(page)
    assert seen["size"] == (50, 25)
    assert not Path(seen["attachment"]).exists()


def test_claude_ocr_page_uses_read_only_image_access(monkeypatch, tmp_path):
    page = tmp_path / "page.png"
    Image.new("RGB", (100, 100), "white").save(page)
    seen = {}

    class Completed:
        returncode = 0
        stdout = '{"text":"관보","confidence":0.88,"notes":"ok"}'
        stderr = ""

    def fake_run(command, capture_output, text, timeout, check):
        seen["command"] = command
        seen["timeout"] = timeout
        return Completed()

    monkeypatch.setattr("scripts.recover_ocr_needed_with_vlm.subprocess.run", fake_run)

    result = claude_ocr_page(page, model_id="sonnet", timeout=20, max_side=100, context="page=1")

    assert result["status"] == "ok"
    assert result["engine"] == "claude_cli"
    assert result["model_id"] == "sonnet"
    assert result["text"] == "관보"
    assert seen["command"][:2] == ["claude", "-p"]
    assert "--permission-mode" in seen["command"]
    assert "--safe-mode" in seen["command"]
    assert seen["command"][seen["command"].index("--tools") + 1] == "Read"
    assert seen["command"][seen["command"].index("--model") + 1] == "sonnet"
    assert f"![page]({page.resolve()})" in seen["command"][2]
    assert seen["timeout"] == 35


def test_run_peer_cli_supports_vibe_agent(monkeypatch, tmp_path):
    page = tmp_path / "page.png"
    Image.new("RGB", (100, 100), "white").save(page)
    seen = {}

    class Completed:
        returncode = 0
        stdout = '{"verdict":"reject","corrected_text":"","issues":["low quality"],"confidence":0.7}'
        stderr = ""

    def fake_run(command, capture_output, text, timeout, check):
        seen["command"] = command
        seen["timeout"] = timeout
        return Completed()

    monkeypatch.setattr("scripts.recover_ocr_needed_with_vlm.subprocess.run", fake_run)

    result = run_peer_cli("vibe", page, "primary text", timeout=20, context="page=1")

    assert result["status"] == "ok"
    assert result["verdict"] == "reject"
    assert seen["command"][:2] == ["vibe", "-p"]
    assert seen["command"][3:5] == ["--agent", "peti-ocr-peer"]
    assert f"@{page.resolve()}" in seen["command"][2]
    assert "--add-dir" in seen["command"]
    assert seen["timeout"] == 35


def test_run_peer_cli_supports_claude_agent(monkeypatch, tmp_path):
    page = tmp_path / "page.png"
    Image.new("RGB", (100, 100), "white").save(page)
    seen = {}

    class Completed:
        returncode = 0
        stdout = '{"verdict":"accept","corrected_text":"","issues":[],"confidence":0.92}'
        stderr = ""

    def fake_run(command, capture_output, text, timeout, check):
        seen["command"] = command
        seen["timeout"] = timeout
        return Completed()

    monkeypatch.setattr("scripts.recover_ocr_needed_with_vlm.subprocess.run", fake_run)

    result = run_peer_cli("claude", page, "primary text", timeout=20, context="page=1", claude_model="sonnet")

    assert result["status"] == "ok"
    assert result["verdict"] == "accept"
    assert seen["command"][:2] == ["claude", "-p"]
    assert "--safe-mode" in seen["command"]
    assert seen["command"][seen["command"].index("--tools") + 1] == "Read"
    assert seen["command"][seen["command"].index("--model") + 1] == "sonnet"
    assert "Primary OCR text:" in seen["command"][2]
    assert seen["timeout"] == 35


def test_run_peer_cli_supports_opencode_agent(monkeypatch, tmp_path):
    page = tmp_path / "page.png"
    Image.new("RGB", (100, 100), "white").save(page)
    seen = {}

    class Completed:
        returncode = 0
        stdout = '{"verdict":"accept","corrected_text":"","issues":[],"confidence":0.9}'
        stderr = ""

    def fake_run(command, capture_output, text, timeout, check):
        seen["command"] = command
        seen["timeout"] = timeout
        return Completed()

    monkeypatch.setattr("scripts.recover_ocr_needed_with_vlm.subprocess.run", fake_run)

    result = run_peer_cli("opencode", page, "primary text", timeout=20, context="page=1")

    assert result["status"] == "ok"
    assert result["verdict"] == "accept"
    assert seen["command"][:7] == [
        "opencode",
        "run",
        "--dangerously-skip-permissions",
        "--agent",
        "peti-ocr-peer",
        "-m",
        "zai-coding-plan/glm-5.2",
    ]
    assert seen["command"][7:9] == ["--file", str(page)]
    assert seen["command"][9] == "--"
    assert "Primary OCR text:" in seen["command"][10]
    assert seen["timeout"] == 35


def test_choose_final_text_uses_high_confidence_peer_revision():
    primary = {"engine": "opencode_cli", "text": "원문"}
    low_confidence = {
        "status": "ok",
        "verdict": "revise",
        "corrected_text": "추측 교정",
        "confidence": 0.75,
    }
    high_confidence = {
        "status": "ok",
        "verdict": "revise",
        "corrected_text": "검증 교정",
        "confidence": 0.95,
    }

    assert choose_final_text(primary, {"vibe": low_confidence}) == ("원문", "opencode_cli_primary")
    assert choose_final_text(primary, {"vibe": low_confidence, "agy": high_confidence}) == (
        "검증 교정",
        "peer_revision",
    )


def test_extract_json_object_prefers_final_verdict_object():
    stdout = """
look_at {"file_path":"/tmp/page.png","goal":"read page"} failed
Some analysis text.
{"verdict":"reject","corrected_text":"","issues":["truncated"],"confidence":0.9}
"""

    parsed = extract_json_object(stdout)

    assert parsed["verdict"] == "reject"
    assert parsed["issues"] == ["truncated"]
