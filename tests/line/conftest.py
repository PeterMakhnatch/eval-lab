from __future__ import annotations

from pathlib import Path

import pytest

from evallab import hack_drop as hd
from evallab.line import core
from evallab.line.io import write_json, write_rows


@pytest.fixture
def fixture_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(hd, "_get_corpus_registry", lambda **_: None)
    monkeypatch.setattr(core, "_code", lambda _: ("a" * 40, False))
    clean = tmp_path / "clean.py"
    clean.write_text("def add(a, b):\n    return a + b\n")
    manifest = tmp_path / "rollouts.jsonl"
    write_rows(
        manifest,
        [
            {"id": "clean-run", "task_id": "plain-task", "path": str(clean), "reward": 1},
            {"id": "weak-run", "task_id": "weak-task", "path": str(clean)},
        ],
    )
    labels = tmp_path / "labels.jsonl"
    write_rows(
        labels, [{"id": "clean-run", "is_cheat": False}, {"id": "weak-run", "is_cheat": True}]
    )
    task_status = tmp_path / "status.json"
    write_json(
        task_status,
        {
            "tasks": [
                {"task_id": "plain-task", "status": "certified", "reason": "ALL_GATES_PASS"},
                {"task_id": "weak-task", "status": "weak", "reason": "ATTACK_ACCEPTED"},
            ]
        },
    )
    path = tmp_path / "spec.json"
    write_json(
        path,
        {
            "schema": "evallab.line_spec/v1",
            "batch_id": "synthetic",
            "split": "dev",
            "sources": [{"corpus": "synthetic", "rollouts": str(manifest), "labels": str(labels)}],
            "task_status": str(task_status),
            "task_action": "review",
            "autoreview": {"mode": "off", "max_usd": 0},
            "models": {"allowed": []},
            "audit": {"rate": 1, "seed": "fixed"},
            "judge": {
                "queue": str(tmp_path / "queue.jsonl"),
                "write": True,
                "verdicts": str(tmp_path / "no-verdicts.jsonl"),
            },
            "labels_policy": {"attempted_cheat": "exclude"},
            "out_root": str(tmp_path / "batches"),
        },
    )
    return path
