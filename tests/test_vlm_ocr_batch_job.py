import json
from argparse import Namespace
from pathlib import Path

from scripts.run_vlm_ocr_batch_job import (
    cli_primary_ocr_page,
    filter_completed_paths,
    load_completed_item_paths,
    load_processed,
    parse_partition_spec,
    run_primary_ocr_page,
    weighted_partition_paths,
)
from scripts.subagent_ocr_queue import build_page_tasks, pages_to_schedule, status_payload, task_key


def test_load_processed_can_retry_failed_items(tmp_path):
    checkpoint = tmp_path / "checkpoint.json"
    results = tmp_path / "results.jsonl"
    checkpoint.write_text(
        json.dumps(
            {
                "processed": {
                    "ok.json": "updated",
                    "bad.json": "error",
                }
            }
        ),
        encoding="utf-8",
    )
    results.write_text(
        "\n".join(
            [
                json.dumps({"item_path": "empty.json", "status": "updated_empty"}),
                json.dumps({"item_path": "ok2.json", "status": "updated"}),
            ]
        ),
        encoding="utf-8",
    )

    assert load_processed(checkpoint, results, retry_failed=False) == {
        "ok.json": "updated",
        "bad.json": "error",
        "empty.json": "updated_empty",
        "ok2.json": "updated",
    }
    assert load_processed(checkpoint, results, retry_failed=True) == {
        "ok.json": "updated",
        "ok2.json": "updated",
    }


def test_filter_completed_paths_only_excludes_updated_results(tmp_path):
    repo_root = tmp_path
    output_root = tmp_path / "batch"
    job_dir = output_root / "job"
    job_dir.mkdir(parents=True)
    updated = tmp_path / "artifacts" / "pety" / "metadata" / "items" / "updated.json"
    empty = tmp_path / "artifacts" / "pety" / "metadata" / "items" / "empty.json"
    pending = tmp_path / "artifacts" / "pety" / "metadata" / "items" / "pending.json"
    for path in (updated, empty, pending):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    (job_dir / "results.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"item_path": str(updated), "status": "updated"}),
                json.dumps({"item_path": str(empty), "status": "updated_empty"}),
            ]
        ),
        encoding="utf-8",
    )

    completed = load_completed_item_paths([Path("batch")], repo_root)
    assert str(updated.resolve()) in completed
    assert str(empty.resolve()) not in completed
    assert filter_completed_paths([updated, empty, pending], completed, repo_root) == [empty, pending]


def test_weighted_partition_paths_splits_without_overlap():
    paths = [Path(f"item_{index:03d}.json") for index in range(12)]

    qwen = weighted_partition_paths(paths, "qwen:4,codex:1,agy:1", "qwen")
    codex = weighted_partition_paths(paths, "qwen:4,codex:1,agy:1", "codex")
    agy = weighted_partition_paths(paths, "qwen:4,codex:1,agy:1", "agy")

    combined = qwen + codex + agy
    assert len(qwen) == 8
    assert len(codex) == 2
    assert len(agy) == 2
    assert sorted(combined) == paths
    assert len(set(combined)) == len(paths)


def test_parse_partition_spec_rejects_invalid_entries():
    assert parse_partition_spec("qwen:4,codex:1") == [("qwen", 4.0), ("codex", 1.0)]

    for spec in ["qwen", "qwen:0", "qwen:x", "qwen:1,qwen:2"]:
        try:
            parse_partition_spec(spec)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {spec}")


def test_subagent_queue_counts_pdf_items_and_page_tasks(tmp_path):
    item = tmp_path / "item.json"
    item.write_text(json.dumps({"pdf_text": {"pages": 3}}), encoding="utf-8")

    tasks = build_page_tasks([item], max_pages=2)

    assert [task["task_key"] for task in tasks] == [
        task_key(str(item), 1),
        task_key(str(item), 2),
    ]
    state = {
        "job_name": "subagent",
        "started_at": "now",
        "settings": {},
        "items": [str(item)],
        "tasks": tasks,
        "task_state": {
            tasks[0]["task_key"]: {"status": "updated"},
            tasks[1]["task_key"]: {"status": "claimed"},
        },
    }

    status = status_payload(state)

    assert status["work_unit"] == "page"
    assert status["total_pdf_items"] == 1
    assert status["total_pages_scheduled"] == 2
    assert status["processed_pages"] == 1
    assert status["claimed_pages"] == 1


def test_pages_to_schedule_supports_all_pages():
    assert pages_to_schedule(5, 0) == 5
    assert pages_to_schedule(5, 2) == 2
    assert pages_to_schedule(0, 2) == 2


def test_cli_primary_ocr_page_supports_agy(monkeypatch, tmp_path):
    image = tmp_path / "page.png"
    image.write_bytes(b"fake")
    agent_file = tmp_path / "agent.md"
    skill_file = tmp_path / "skill.md"
    agent_file.write_text("agent rules", encoding="utf-8")
    skill_file.write_text("skill rules", encoding="utf-8")
    seen = {}

    class Completed:
        returncode = 0
        stdout = '{"text":"관보","confidence":0.91,"notes":"ok"}'
        stderr = ""

    def fake_run(command, capture_output, text, timeout, check):
        seen["command"] = command
        seen["timeout"] = timeout
        return Completed()

    monkeypatch.setattr("scripts.run_vlm_ocr_batch_job.subprocess.run", fake_run)

    result = cli_primary_ocr_page(
        image,
        backend="agy_cli",
        timeout=30,
        context="page=1",
        input_image={"width": 100, "height": 200},
        agy_agent_file=agent_file,
        agy_skill_file=skill_file,
        agy_model="Gemini 3.5 Flash (Medium)",
    )

    assert result["status"] == "ok"
    assert result["engine"] == "agy_cli"
    assert result["text"] == "관보"
    assert seen["command"][0] == "agy"
    assert "--print-timeout" in seen["command"]
    assert f"Image path: {image.resolve()}" in seen["command"][2]
    assert "agent rules" in seen["command"][2]
    assert "skill rules" in seen["command"][2]
    assert seen["command"][seen["command"].index("--model") + 1] == "Gemini 3.5 Flash (Medium)"
    assert seen["command"][seen["command"].index("--add-dir") + 1] == str(tmp_path)
    assert "--dangerously-skip-permissions" in seen["command"]
    assert seen["timeout"] == 45


def test_cli_primary_ocr_page_keeps_empty_agy_diagnostics(monkeypatch, tmp_path):
    image = tmp_path / "page.png"
    image.write_bytes(b"fake")

    class Completed:
        returncode = 0
        stdout = ""
        stderr = "tool unavailable"

    monkeypatch.setattr(
        "scripts.run_vlm_ocr_batch_job.subprocess.run",
        lambda command, capture_output, text, timeout, check: Completed(),
    )

    result = cli_primary_ocr_page(
        image,
        backend="agy_cli",
        timeout=30,
        context="page=1",
        input_image={"width": 100, "height": 200},
    )

    assert result["status"] == "empty"
    assert result["stderr"] == "tool unavailable"


def test_agy_primary_falls_back_to_opencode(monkeypatch, tmp_path):
    raw = tmp_path / "raw.png"
    prepared = tmp_path / "prepared.png"
    raw.write_bytes(b"raw")
    prepared.write_bytes(b"prepared")
    seen = {}

    def fake_cli_primary(*args, **kwargs):
        seen["cli_kwargs"] = kwargs
        return {"engine": "agy_cli", "status": "empty", "text": "", "confidence": 0.0}

    def fake_opencode(image_path, *, model_id, agent_id, timeout, max_side, context, pure, skip_permissions):
        seen["opencode"] = {
            "image_path": image_path,
            "model_id": model_id,
            "agent_id": agent_id,
            "timeout": timeout,
            "max_side": max_side,
            "context": context,
            "pure": pure,
            "skip_permissions": skip_permissions,
        }
        return {
            "engine": "opencode_cli",
            "model_id": model_id,
            "agent": agent_id,
            "status": "ok",
            "text": "관보",
            "confidence": 0.82,
        }

    monkeypatch.setattr("scripts.run_vlm_ocr_batch_job.cli_primary_ocr_page", fake_cli_primary)
    monkeypatch.setattr("scripts.run_vlm_ocr_batch_job.opencode_ocr_page", fake_opencode)

    args = Namespace(
        primary="agy_cli",
        primary_cli_timeout=30,
        max_side=3508,
        agy_agent_file=Path(".agy/agents/peti-ocr-primary.md"),
        agy_skill_file=Path(".agy/skills/peti-korean-ocr-primary/SKILL.md"),
        agy_model="GPT-OSS 120B (Medium)",
        agy_add_dir=None,
        agy_dangerously_skip_permissions=True,
        agy_fallback_backend="opencode_cli",
        opencode_fallback_model="zai-coding-plan/glm-5.2",
        opencode_fallback_agent="peti-ocr-primary",
        opencode_fallback_timeout=77,
        opencode_pure=False,
        opencode_skip_permissions=True,
    )

    result = run_primary_ocr_page(
        raw,
        prepared,
        {"width": 100, "height": 200},
        args=args,
        page_number=1,
        context="page=1",
    )

    assert result["status"] == "ok"
    assert result["engine"] == "opencode_cli"
    assert result["text"] == "관보"
    assert result["fallback_backend"] == "opencode_cli"
    assert result["fallback_reason"] == "empty"
    assert result["fallback_from"]["engine"] == "agy_cli"
    assert seen["opencode"]["image_path"] == prepared
    assert seen["opencode"]["model_id"] == "zai-coding-plan/glm-5.2"
    assert seen["opencode"]["agent_id"] == "peti-ocr-primary"
    assert seen["opencode"]["timeout"] == 77
    assert seen["opencode"]["pure"] is False
    assert seen["opencode"]["skip_permissions"] is True


def test_opencode_primary_backend(monkeypatch, tmp_path):
    raw = tmp_path / "raw.png"
    prepared = tmp_path / "prepared.png"
    raw.write_bytes(b"raw")
    prepared.write_bytes(b"prepared")
    seen = {}

    def fake_opencode(image_path, *, model_id, agent_id, timeout, max_side, context, pure, skip_permissions):
        seen.update(
            {
                "image_path": image_path,
                "model_id": model_id,
                "agent_id": agent_id,
                "timeout": timeout,
                "max_side": max_side,
                "context": context,
                "pure": pure,
                "skip_permissions": skip_permissions,
            }
        )
        return {"engine": "opencode_cli", "status": "ok", "text": "관보", "confidence": 0.9}

    monkeypatch.setattr("scripts.run_vlm_ocr_batch_job.opencode_ocr_page", fake_opencode)

    args = Namespace(
        primary="opencode_cli",
        opencode_model="zai-coding-plan/glm-5.2",
        opencode_agent="peti-ocr-primary",
        opencode_timeout=88,
        opencode_pure=False,
        opencode_skip_permissions=True,
        max_side=3508,
    )

    result = run_primary_ocr_page(
        raw,
        prepared,
        {"width": 100, "height": 200},
        args=args,
        page_number=1,
        context="page=1",
    )

    assert result["engine"] == "opencode_cli"
    assert result["text"] == "관보"
    assert seen["image_path"] == prepared
    assert seen["model_id"] == "zai-coding-plan/glm-5.2"
    assert seen["agent_id"] == "peti-ocr-primary"
    assert seen["timeout"] == 88
    assert seen["pure"] is False
    assert seen["skip_permissions"] is True


def test_claude_primary_backend(monkeypatch, tmp_path):
    raw = tmp_path / "raw.png"
    prepared = tmp_path / "prepared.png"
    raw.write_bytes(b"raw")
    prepared.write_bytes(b"prepared")
    seen = {}

    def fake_claude(image_path, *, model_id, timeout, max_side, context):
        seen.update(
            {
                "image_path": image_path,
                "model_id": model_id,
                "timeout": timeout,
                "max_side": max_side,
                "context": context,
            }
        )
        return {"engine": "claude_cli", "status": "ok", "text": "관보", "confidence": 0.9}

    monkeypatch.setattr("scripts.run_vlm_ocr_batch_job.claude_ocr_page", fake_claude)

    args = Namespace(
        primary="claude_cli",
        claude_model="sonnet",
        claude_timeout=99,
        max_side=3508,
    )

    result = run_primary_ocr_page(
        raw,
        prepared,
        {"width": 100, "height": 200},
        args=args,
        page_number=1,
        context="page=1",
    )

    assert result["engine"] == "claude_cli"
    assert result["text"] == "관보"
    assert seen["image_path"] == prepared
    assert seen["model_id"] == "sonnet"
    assert seen["timeout"] == 99
    assert seen["max_side"] == 3508


def test_agy_primary_falls_back_to_codex_by_default(monkeypatch, tmp_path):
    raw = tmp_path / "raw.png"
    prepared = tmp_path / "prepared.png"
    raw.write_bytes(b"raw")
    prepared.write_bytes(b"prepared")
    seen = {}

    def fake_cli_primary(image_path, *, backend, timeout, context, input_image, **_kwargs):
        if backend == "agy_cli":
            return {"engine": "agy_cli", "status": "empty", "text": "", "confidence": 0.0}
        seen.update(
            {
                "image_path": image_path,
                "backend": backend,
                "timeout": timeout,
                "context": context,
                "input_image": input_image,
            }
        )
        return {"engine": "codex_cli", "status": "ok", "text": "관보", "confidence": 0.85}

    monkeypatch.setattr("scripts.run_vlm_ocr_batch_job.cli_primary_ocr_page", fake_cli_primary)

    args = Namespace(
        primary="agy_cli",
        primary_cli_timeout=30,
        max_side=3508,
        agy_agent_file=Path(".agy/agents/peti-ocr-primary.md"),
        agy_skill_file=Path(".agy/skills/peti-korean-ocr-primary/SKILL.md"),
        agy_model="",
        agy_add_dir=None,
        agy_dangerously_skip_permissions=True,
        agy_fallback_backend="codex_cli",
        codex_fallback_timeout=66,
    )

    result = run_primary_ocr_page(
        raw,
        prepared,
        {"width": 100, "height": 200},
        args=args,
        page_number=1,
        context="page=1",
    )

    assert result["status"] == "ok"
    assert result["engine"] == "codex_cli"
    assert result["fallback_backend"] == "codex_cli"
    assert result["fallback_reason"] == "empty"
    assert result["fallback_from"]["engine"] == "agy_cli"
    assert seen["image_path"] == prepared
    assert seen["backend"] == "codex_cli"
    assert seen["timeout"] == 66
    assert "fallback_from=agy_cli" in seen["context"]


def test_agy_primary_can_fall_back_to_claude(monkeypatch, tmp_path):
    raw = tmp_path / "raw.png"
    prepared = tmp_path / "prepared.png"
    raw.write_bytes(b"raw")
    prepared.write_bytes(b"prepared")
    seen = {}

    def fake_cli_primary(*args, **kwargs):
        return {"engine": "agy_cli", "status": "empty", "text": "", "confidence": 0.0}

    def fake_claude(image_path, *, model_id, timeout, max_side, context):
        seen.update(
            {
                "image_path": image_path,
                "model_id": model_id,
                "timeout": timeout,
                "max_side": max_side,
                "context": context,
            }
        )
        return {"engine": "claude_cli", "status": "ok", "text": "관보", "confidence": 0.85}

    monkeypatch.setattr("scripts.run_vlm_ocr_batch_job.cli_primary_ocr_page", fake_cli_primary)
    monkeypatch.setattr("scripts.run_vlm_ocr_batch_job.claude_ocr_page", fake_claude)

    args = Namespace(
        primary="agy_cli",
        primary_cli_timeout=30,
        max_side=3508,
        agy_agent_file=Path(".agy/agents/peti-ocr-primary.md"),
        agy_skill_file=Path(".agy/skills/peti-korean-ocr-primary/SKILL.md"),
        agy_model="",
        agy_add_dir=None,
        agy_dangerously_skip_permissions=True,
        agy_fallback_backend="claude_cli",
        claude_fallback_model="sonnet",
        claude_fallback_timeout=66,
    )

    result = run_primary_ocr_page(
        raw,
        prepared,
        {"width": 100, "height": 200},
        args=args,
        page_number=1,
        context="page=1",
    )

    assert result["status"] == "ok"
    assert result["engine"] == "claude_cli"
    assert result["fallback_backend"] == "claude_cli"
    assert result["fallback_reason"] == "empty"
    assert result["fallback_from"]["engine"] == "agy_cli"
    assert seen["image_path"] == prepared
    assert seen["model_id"] == "sonnet"
    assert seen["timeout"] == 66


def test_qwen_primary_passes_rate_limit_fallback_models(monkeypatch, tmp_path):
    raw = tmp_path / "raw.png"
    prepared = tmp_path / "prepared.png"
    raw.write_bytes(b"raw")
    prepared.write_bytes(b"prepared")
    seen = {}

    def fake_qwen(image_path, **kwargs):
        seen["image_path"] = image_path
        seen.update(kwargs)
        return {"engine": "qwen_vllm", "status": "ok", "text": "관보", "confidence": 0.8}

    monkeypatch.setattr("scripts.run_vlm_ocr_batch_job.qwen_ocr_page", fake_qwen)

    args = Namespace(
        primary="qwen_vllm",
        endpoint_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        model_id="qwen3.6-plus",
        qwen_timeout=30,
        max_tokens=1024,
        seed=17,
        max_side=3508,
        image_preprocess="qwen_vl_250dpi_sharp",
        image_upscale=1.1,
        temperature=0.2,
        top_p=0.8,
        top_k=20,
        min_p=0.0,
        presence_penalty=1.5,
        enable_thinking=True,
        thinking_budget=128,
        qwen_api_profile="dashscope",
        qwen_api_key_env="DASHSCOPE_API_KEY",
        qwen_rate_limit_fallback_models="qwen3.7-plus,qwen-3.7-max-preview",
    )

    result = run_primary_ocr_page(
        raw,
        prepared,
        {"width": 100, "height": 200},
        args=args,
        page_number=2,
        context="page=2",
    )

    assert result["status"] == "ok"
    assert seen["image_path"] == raw
    assert seen["model_id"] == "qwen3.6-plus"
    assert seen["seed"] == 19
    assert seen["rate_limit_fallback_models"] == "qwen3.7-plus,qwen-3.7-max-preview"


def test_cli_primary_ocr_page_supports_codex(monkeypatch, tmp_path):
    image = tmp_path / "page.png"
    image.write_bytes(b"fake")
    seen = {}

    class Completed:
        returncode = 0
        stdout = '{"text":"관보","confidence":0.87,"notes":"ok"}'
        stderr = ""

    def fake_run(command, capture_output, text, timeout, check):
        seen["command"] = command
        seen["timeout"] = timeout
        return Completed()

    monkeypatch.setattr("scripts.run_vlm_ocr_batch_job.subprocess.run", fake_run)

    result = cli_primary_ocr_page(
        image,
        backend="codex_cli",
        timeout=30,
        context="page=1",
        input_image={"width": 100, "height": 200},
    )

    assert result["status"] == "ok"
    assert result["engine"] == "codex_cli"
    assert result["text"] == "관보"
    assert seen["command"][:5] == ["codex", "exec", "--ignore-user-config", "--sandbox", "read-only"]
    assert seen["command"][5:7] == ["-i", str(image)]
    assert seen["timeout"] == 45
