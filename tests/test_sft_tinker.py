"""Behavioral checks for the guarded Tinker chat_sl launcher (evallab.sft_tinker).

No Tinker dependency, network, or spend: rendering uses a fake renderer with
injectable token/weight arithmetic, and the trainer runs through a recorded
fake runner that writes a synthetic ``checkpoints.jsonl``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from evallab.sft_tinker import (
    CONVERSATIONS_FILE,
    EXPORT_MANIFEST_FILE,
    TRAINING_MANIFEST_FILE,
    TraceError,
    dry_run,
    estimate_cost_usd,
    load_conversations,
    read_final_sampler_path,
    run_training,
    training_command,
)


class _ModelInput:
    def __init__(self, length: int) -> None:
        self.length = length


class _Weights:
    def __init__(self, values: list[float]) -> None:
        self.values = values

    def __getitem__(self, key: Any) -> _Weights:
        if isinstance(key, slice):
            return _Weights(self.values[key])
        raise TypeError("fake weights support slicing only")

    def sum(self) -> float:
        return float(sum(self.values))


class _FakeRenderer:
    """4 tokens per message; weight 1.0 on assistant tokens only."""

    def build_supervised_example(
        self, messages: list[dict[str, Any]], train_on_what: Any = None
    ) -> tuple[_ModelInput, _Weights]:
        weights: list[float] = []
        for message in messages:
            weights.extend([1.0 if message["role"] == "assistant" else 0.0] * 4)
        return _ModelInput(len(weights)), _Weights(weights)


class _FakeRendererNoExtension(_FakeRenderer):
    """Upstream renderers like qwen3_5_disable_thinking report no extension."""

    has_extension_property = False


def _fake_factory() -> _FakeRenderer:
    return _FakeRenderer()


def _write_export(tmp_path: Path, rows: list[list[dict[str, Any]]]) -> Path:
    data = tmp_path / "export"
    data.mkdir()
    path = data / CONVERSATIONS_FILE
    path.write_text(
        "".join(
            json.dumps({"messages": messages}, sort_keys=True) + "\n"
            for messages in rows
        )
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    (data / EXPORT_MANIFEST_FILE).write_text(
        json.dumps(
            {
                "contract": "evallab.sft_terminus/1",
                "conversations_sha256": f"sha256:{digest}",
                "teacher_model": "zai/glm-5.3",
                "split_manifest": {"manifest_digest": "sha256:" + "0" * 64},
            }
        )
    )
    return data


def _conversation(user_chars: int = 40, assistant_chars: int = 60) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "u" * user_chars},
        {"role": "assistant", "content": "a" * assistant_chars},
    ]


def test_measure_counts_tokens_and_truncation(tmp_path: Path) -> None:
    data = _write_export(tmp_path, [_conversation(), _conversation()])
    report = dry_run(
        data,
        model="Qwen/Qwen3.6-35B-A3B",
        renderer_name="qwen3_5_disable_thinking",
        max_length=6,
        epochs=1,
        renderer_factory=_fake_factory,
    )
    render = report["render"]
    assert render["conversations"] == 2
    assert render["total_tokens"] == 16  # 2 conversations x 4 messages-tokens
    assert render["total_tokens_after_truncation"] == 12  # capped at 6 each
    # weights [0,0,0,0,1,1,1,1][:6] -> 2 supervised tokens per conversation.
    assert render["supervised_tokens_after_truncation"] == 4.0
    assert render["truncated_conversations"] == 2
    assert report["renderer"] == "qwen3_5_disable_thinking"
    assert report["train_on_what"] == "all_assistant_messages"
    assert report["split_manifest_digest"] == "sha256:" + "0" * 64



def test_dry_run_surfaces_missing_extension_property(tmp_path: Path) -> None:
    data = _write_export(tmp_path, [_conversation()])
    report = dry_run(
        data,
        model="Qwen/Qwen3.6-35B-A3B",
        renderer_name="qwen3_5_disable_thinking",
        max_length=128,
        epochs=1,
        renderer_factory=lambda: _FakeRendererNoExtension(),
    )
    assert report["render"]["renderer_extension_property"] is False
    assert any("extension" in note for note in report["notes"])


def test_cost_uses_pinned_price_table() -> None:
    assert estimate_cost_usd(1_000_000, "Qwen/Qwen3.6-35B-A3B", 1) == pytest.approx(1.177)
    assert estimate_cost_usd(500_000, "Qwen/Qwen3.8-27B", 2) == pytest.approx(4.103)
    assert estimate_cost_usd(250_000, "Qwen/Qwen3.5-9B", 3) == pytest.approx(1.463 * 0.75)
    with pytest.raises(TraceError, match="no pinned Tinker train price"):
        estimate_cost_usd(1, "Qwen/Qwen3.6-99B", 1)


def test_dry_run_refuses_drifted_data(tmp_path: Path) -> None:
    data = _write_export(tmp_path, [_conversation()])
    (data / CONVERSATIONS_FILE).write_text(
        json.dumps({"messages": _conversation()}) + "\n"
    )
    with pytest.raises(TraceError, match="digest mismatch"):
        dry_run(
            data,
            model="Qwen/Qwen3.6-35B-A3B",
            renderer_name="qwen3_5_disable_thinking",
            max_length=128,
            epochs=1,
            renderer_factory=_fake_factory,
        )


def test_load_conversations_enforces_chat_sl_row_shape(tmp_path: Path) -> None:
    good = tmp_path / "good.jsonl"
    good.write_text(json.dumps({"messages": _conversation()}) + "\n")
    assert len(load_conversations(good)) == 1
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"messages": _conversation(), "reward": 1.0}) + "\n")
    with pytest.raises(TraceError, match="exactly a 'messages' key"):
        load_conversations(bad)
    tool_role = tmp_path / "tool.jsonl"
    tool_role.write_text(
        json.dumps({"messages": [{"role": "tool", "content": "x"}]}) + "\n"
    )
    with pytest.raises(TraceError, match="unsupported role"):
        load_conversations(tool_role)


def test_training_command_uses_chz_key_value_form() -> None:
    command = training_command(
        Path("/data/conversations.jsonl"),
        model="Qwen/Qwen3.5-9B",
        renderer_name="qwen3_5_disable_thinking",
        learning_rate=1e-4,
        lora_rank=32,
        batch_size=256,
        num_epochs=1,
        max_length=16384,
        log_dir=Path("/logs/run1"),
    )
    assert command[1:3] == ["-m", "tinker_cookbook.recipes.chat_sl.train"]
    joined = " ".join(command)
    assert "dataset=/data/conversations.jsonl" in joined
    assert "model_name=Qwen/Qwen3.5-9B" in joined
    assert "renderer_name=qwen3_5_disable_thinking" in joined
    assert "train_on_what=all_assistant_messages" in joined
    assert "lora_rank=32" in joined and "batch_size=256" in joined
    assert "num_epochs=1" in joined and "max_length=16384" in joined
    assert "behavior_if_log_dir_exists=raise" in joined
    assert "--dataset" not in joined  # chz 0.5.7 rejects dash-style flags


def test_training_refuses_without_confirm_spend(tmp_path: Path) -> None:
    data = _write_export(tmp_path, [_conversation()])
    with pytest.raises(TraceError, match="--confirm-spend"):
        run_training(
            data,
            model="Qwen/Qwen3.5-9B",
            renderer_name=None,
            log_dir=tmp_path / "logs",
            learning_rate=1e-4,
            lora_rank=32,
            batch_size=256,
            num_epochs=1,
            max_length=16384,
            confirm_spend=False,
        )


def test_training_runs_and_links_data_digest_to_sampler_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TINKER_API_KEY", "test-not-a-real-key")
    data = _write_export(tmp_path, [_conversation(), _conversation()])
    log_dir = tmp_path / "logs"
    commands: list[list[str]] = []

    def fake_runner(command: list[str]) -> int:
        commands.append(command)
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "checkpoints.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "name": "step-1",
                            "batch": 1,
                            "sampler_path": "tinker://run-early:train:0/sampler_weights/1",
                        }
                    ),
                    json.dumps(
                        {
                            "name": "final",
                            "batch": 2,
                            "final": True,
                            "sampler_path": "tinker://run-abc:train:0/sampler_weights/7",
                        }
                    ),
                ]
            )
            + "\n"
        )
        return 0

    manifest = run_training(
        data,
        model="Qwen/Qwen3.5-9B",
        renderer_name=None,
        log_dir=log_dir,
        learning_rate=1e-4,
        lora_rank=32,
        batch_size=256,
        num_epochs=1,
        max_length=16384,
        confirm_spend=True,
        runner=fake_runner,
    )
    assert len(commands) == 1
    assert manifest["sampler_path"] == "tinker://run-abc:train:0/sampler_weights/7"
    assert manifest["tinker_run_id"] == "run-abc"
    assert manifest["conversations_sha256"].startswith("sha256:")
    assert manifest["split_manifest_digest"].startswith("sha256:")
    assert manifest["teacher_model"] == "zai/glm-5.3"
    assert manifest["renderer"] == "qwen3_5_disable_thinking"
    assert manifest["hyperparameters"]["max_length"] == 16384
    assert manifest["trainer"]["exit_code"] == 0
    assert (log_dir / TRAINING_MANIFEST_FILE).is_file()


def test_training_refuses_on_trainer_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TINKER_API_KEY", "test-not-a-real-key")
    data = _write_export(tmp_path, [_conversation()])
    with pytest.raises(TraceError, match="exited with 1"):
        run_training(
            data,
            model="Qwen/Qwen3.5-9B",
            renderer_name=None,
            log_dir=tmp_path / "logs",
            learning_rate=1e-4,
            lora_rank=32,
            batch_size=256,
            num_epochs=1,
            max_length=16384,
            confirm_spend=True,
            runner=lambda command: 1,
        )


def test_read_final_sampler_path_prefers_final_record(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "checkpoints.jsonl").write_text(
        json.dumps({"name": "a", "sampler_path": "tinker://first:train:0/sampler_weights/1"})
        + "\n"
        + json.dumps(
            {"name": "b", "final": True, "sampler_path": "tinker://final:train:0/sampler_weights/9"}
        )
        + "\n"
    )
    record = read_final_sampler_path(log_dir)
    assert record["sampler_path"].endswith("sampler_weights/9")
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "checkpoints.jsonl").write_text("")
    with pytest.raises(TraceError, match="no checkpoint"):
        read_final_sampler_path(empty)


def test_missing_api_key_refuses_even_with_confirm(tmp_path: Path) -> None:
    real = os.environ.pop("TINKER_API_KEY", None)
    try:
        data = _write_export(tmp_path, [_conversation()])
        with pytest.raises(TraceError, match="TINKER_API_KEY"):
            run_training(
                data,
                model="Qwen/Qwen3.5-9B",
                renderer_name=None,
                log_dir=tmp_path / "logs",
                learning_rate=1e-4,
                lora_rank=32,
                batch_size=256,
                num_epochs=1,
                max_length=16384,
                confirm_spend=True,
                runner=lambda command: 0,
            )
    finally:
        if real is not None:
            os.environ["TINKER_API_KEY"] = real
