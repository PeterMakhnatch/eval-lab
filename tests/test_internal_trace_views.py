"""Behavioral contracts for internal paired actor inputs (no model/runtime access)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "research/answer-keys/internal_traces.py"
spec = importlib.util.spec_from_file_location("internal_traces", MODULE)
assert spec and spec.loader
internal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(internal)


def _atif() -> dict:
    return {
        "schema_version": "ATIF-v1.6",
        "session_id": "label-bearing-source",
        "agent": {"name": "label-bearing-agent"},
        "metadata": {"expected_verdict": "fail"},
        "final_metrics": {"reward": 1},
        "steps": [
            {"step_id": 7, "source": "user", "message": "Complete the task."},
            {
                "step_id": 12,
                "source": "agent",
                "message": "Agent prose.",
                "reasoning_content": "Private reasoning.",
                "model_name": "source model",
                "metrics": {"reward": 1},
                "extra": {"label": "wrong_pass"},
                "tool_calls": [
                    {
                        "tool_call_id": "c1",
                        "function_name": "bash",
                        "arguments": {"command": "printf done"},
                    }
                ],
                "observation": {"results": [{"source_call_id": "c1", "content": "done"}]},
            },
        ],
    }


def test_full_projection_keeps_actor_evidence_not_evaluator_headers() -> None:
    raw = _atif()
    doc = internal.project_internal(raw, "a" * 24)
    assert doc["session_id"] == "a" * 24
    assert doc["agent"] == {"name": "trace-agent", "version": "1"}
    assert "reward" not in json.dumps(doc)
    assert "label-bearing" not in json.dumps(doc)
    assert doc["steps"][1]["reasoning_content"] == "Private reasoning."
    for key in ("tool_calls", "observation"):
        assert doc["steps"][1][key] == raw["steps"][1][key]
    assert doc == internal.project_internal(internal.counterfactual(raw), "a" * 24)
    actions = internal.actions_only(doc)
    assert actions["steps"][1]["message"] == ""
    assert "reasoning_content" not in actions["steps"][1]
    assert actions["steps"][0] == doc["steps"][0]
    assert doc["steps"][1]["message"] == "Agent prose."


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(internal.canonical(row) for row in rows))


def _fixture(tmp_path: Path) -> tuple[Path, Path, dict]:
    repo, results, metadata = tmp_path / "repo", tmp_path / "results", tmp_path / "metadata"
    repo.mkdir()
    results.mkdir()
    metadata.mkdir()
    rows, items = [], []
    for i, verdict in enumerate(("fail", "pass")):
        path = results / f"source-{i}.json"
        path.write_bytes(internal.canonical(_atif()))
        item = {
            "schema": "qa.answer_key/v1",
            "key_id": "tiny-key",
            "item_id": f"item-{i}",
            "subject": {"kind": "trace", "task_id": "tiny-task", "trial_id": f"trial-{i}"},
            "expected_verdict": verdict,
            "expected_category": "weak" if i == 0 else None,
            "label_source": "fixture",
            "split": "test",
        }
        items.append(item)
        rows.append(
            {
                "dataset": "tiny-key",
                "item": item,
                "input": {
                    "root": "results",
                    "path": path.name,
                    "format": "atif",
                    "sha256": internal.file_digest(path),
                },
            }
        )
    labels = repo / "labels.jsonl"
    _write_jsonl(labels, items)
    _write_jsonl(metadata / "inputs.jsonl", rows)
    source = {
        "inputs_sha256": internal.file_digest(metadata / "inputs.jsonl"),
        "sources": [
            {
                "dataset": "tiny-key",
                "kind": "trial-key",
                "n_items": 2,
                "verdict_counts": {"fail": 1, "pass": 1},
                "labels": {"root": "repo", "path": labels.name},
                "labels_sha256": internal.file_digest(labels),
            }
        ],
    }
    internal.write_json(metadata / "source.json", source)
    return (
        metadata,
        results / "views",
        {"repo": repo, "results": results, "context": tmp_path, "checkout": repo},
    )


def test_build_publishes_both_loader_ready_keys_and_is_byte_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab.qa.trace import key_trial_dir

    metadata, artifacts, roots = _fixture(tmp_path)
    monkeypatch.setenv("EVALLAB_RESULTS_HOME", str(roots["results"]))
    receipt = internal.build(metadata, artifacts, roots)
    assert receipt["audit"]["items"] == 2
    assert receipt["audit"]["trajectories_scanned"] == 4
    assert receipt["audit"]["label_leaks"] == 0
    for view in ("full", "actions"):
        key = internal.load_answer_key(receipt["keys"]["tiny-key"][view]["path"])
        assert {i.item_id for i in key.items} == {"item-0", "item-1"}
        for item in key.items:
            assert item.model_extra["trial_ref"]["kind"] == "published"
            target = roots["results"] / item.model_extra["trial_ref"]["path"]
            assert key_trial_dir(item) == target
            doc = json.loads((target / "agent/trajectory.json").read_bytes())
            assert doc["steps"][1]["tool_calls"][0]["arguments"] == {"command": "printf done"}
            if view == "actions":
                assert doc["steps"][1]["message"] == ""
    before = {p.name: p.read_bytes() for p in metadata.glob("*.json*")}
    internal.build(metadata, artifacts, roots)
    assert before == {p.name: p.read_bytes() for p in metadata.glob("*.json*")}


@pytest.mark.parametrize("tamper", ["label", "drop"])
def test_changed_labels_or_incomplete_population_fail_closed(tmp_path: Path, tamper: str) -> None:
    metadata, artifacts, roots = _fixture(tmp_path)
    rows = internal.jsonl(metadata / "inputs.jsonl")
    if tamper == "label":
        rows[0]["item"]["expected_verdict"] = "pass"
    else:
        rows.pop()
    _write_jsonl(metadata / "inputs.jsonl", rows)
    config = json.loads((metadata / "source.json").read_bytes())
    config["inputs_sha256"] = internal.file_digest(metadata / "inputs.jsonl")
    internal.write_json(metadata / "source.json", config)
    with pytest.raises(ValueError, match="selected labels|incomplete frozen population"):
        internal.build(metadata, artifacts, roots)


def test_changed_source_bytes_and_existing_actor_bytes_are_rejected(tmp_path: Path) -> None:
    metadata, artifacts, roots = _fixture(tmp_path)
    internal.build(metadata, artifacts, roots)
    target = next(artifacts.glob("*/full/*/agent/trajectory.json"))
    target.write_text("{}")
    with pytest.raises(ValueError, match="frozen actor input"):
        internal.build(metadata, artifacts, roots)
    native = roots["results"] / "source-0.json"
    native.write_text("{}")
    with pytest.raises(ValueError, match="agent source SHA256"):
        internal.build(metadata, artifacts, roots)


def test_canonical_mimo_population_requires_admitted_original_passes() -> None:
    row = {
        "item_id": "mimo:original",
        "task_id": "mimo/task",
        "label": "wrong_pass",
        "original_pass": True,
        "excluded_from_primary": False,
        "population": "official",
        "source_trial_dir": "/fixture/original",
        "source_uid": ["job", "trial"],
        "arm": "B",
        "attempt_index": 2,
    }
    item = internal.canonical_item(row)
    assert item["key_id"] == "mimo-v7-canonical-pass70"
    assert item["expected_verdict"] == "fail"
    row["label"] = "real_pass"
    assert internal.canonical_item(row)["expected_verdict"] == "pass"
    row["excluded_from_primary"] = True
    assert internal.canonical_item(row)["excluded_from_primary"] is True
    row["original_pass"] = False
    with pytest.raises(ValueError, match="not an accepted"):
        internal.canonical_item(row)


def test_unavailable_slots_require_approval_and_stay_out_of_denominators(tmp_path: Path) -> None:
    metadata, artifacts, roots = _fixture(tmp_path)
    rows = internal.jsonl(metadata / "inputs.jsonl")
    rows[1]["input"] = None
    rows[1]["unavailable"] = {"reason": "original actor trajectory was not captured"}
    _write_jsonl(metadata / "inputs.jsonl", rows)
    config = json.loads((metadata / "source.json").read_bytes())
    config["inputs_sha256"] = internal.file_digest(metadata / "inputs.jsonl")
    config["sources"][0]["n_available"] = 1
    internal.write_json(metadata / "source.json", config)
    with pytest.raises(ValueError, match="explicit coverage decision"):
        internal.build(metadata, artifacts, roots)
    config["sources"][0]["allow_unavailable"] = True
    internal.write_json(metadata / "source.json", config)
    receipt = internal.build(metadata, artifacts, roots)
    assert receipt["audit"]["source_items"] == 2
    assert receipt["audit"]["items"] == 1
    assert receipt["audit"]["unavailable_items"] == 1
    coverage = internal.jsonl(metadata / "coverage.jsonl")
    assert {row["item_id"] for row in coverage} == {"item-0", "item-1"}
    assert coverage[1]["status"] == "unavailable"
    predictions = internal.jsonl(metadata / "predictions.jsonl")
    assert [row["item_id"] for row in predictions] == ["item-0"]
    result = json.loads((metadata / "baseline.json").read_bytes())["tiny-key"]
    assert result["coverage"] == {"source_items": 2, "measured_items": 1, "unavailable_items": 1}
