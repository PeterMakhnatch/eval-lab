"""Synthetic fixtures for label-isolated, recomputable line scoreboards."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab import hack_drop
from evallab.line.scoreboard import build_scoreboard, render_scoreboard


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    # Never read the operator's corpus registry or local dataset assignments.
    monkeypatch.setattr(hack_drop, "_get_corpus_registry", lambda repo_root=None: None)


def _json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


@pytest.fixture
def batch(tmp_path):
    root = tmp_path / "batch"
    root.mkdir()
    labels_a = tmp_path / "labels-a.json"
    labels_b = tmp_path / "labels-b.json"
    _json(
        labels_a,
        [
            {"run_id": f"a-{decision}-{kind}", "label": kind}
            for kind in ("cheat", "clean")
            for decision in ("drop", "review", "keep")
        ]
        + [{"run_id": "a-attempt", "label": "considered_only"}],
    )
    _json(labels_b, {"b-clean": False, "b-attempt": "attempted_but_failed"})
    _json(
        root / "spec.json",
        {
            "schema": "evallab.line_spec/v1",
            "batch_id": "synthetic",
            "split": "dev",
            "sources": [
                {"corpus": "a", "labels": str(labels_a)},
                {"corpus": "b", "labels": str(labels_b)},
            ],
            "labels_policy": {"attempted_cheat": "exclude"},
            "autoreview": {"mode": "cached", "max_usd": 0},
        },
    )
    tasks = [
        {"task_id": name, "status": name, "reason": "synthetic fixture"}
        for name in ("certified", "provisional", "weak", "uncertified")
    ]
    _rows(root / "tasks.jsonl", tasks)
    runs, decisions = [], []
    for kind in ("cheat", "clean"):
        for action in ("drop", "review", "keep"):
            run_id = f"a-{action}-{kind}"
            status = (
                {"drop": "certified", "review": "provisional", "keep": "weak"}[action]
                if kind == "cheat"
                else "uncertified"
            )
            runs.append(
                {"run_id": run_id, "corpus": "a", "task_id": status, "model": None, "reward": 1}
            )
            decisions.append(
                {
                    "run_id": run_id,
                    "id": run_id,
                    "corpus": "a",
                    "decision": action,
                    "audit": action == "keep" and kind == "clean",
                }
            )
    for run_id, corpus, action, status, reward in [
        ("a-attempt", "a", "review", "uncertified", 1),
        ("a-unlabelled", "a", "keep", "uncertified", 0),
        ("b-clean", "b", "keep", "certified", 1),
        ("b-attempt", "b", "drop", "weak", 1),
    ]:
        runs.append({"run_id": run_id, "corpus": corpus, "task_id": status, "reward": reward})
        decisions.append(
            {"run_id": run_id, "id": run_id, "corpus": corpus, "decision": action, "audit": False}
        )
    _rows(root / "runs.jsonl", runs)
    _rows(root / "decisions.jsonl", decisions)
    _rows(
        root / "judge-queue.jsonl",
        [
            {"run_id": row["run_id"]}
            for row in decisions
            if row["decision"] == "review" or row["audit"]
        ],
    )
    _json(
        root / "receipts" / "intake.json",
        {
            "stage": "intake",
            "seconds": 1.25,
            "counts": {"rows_in": 10, "rows_out": 10},
            "inputs": [],
            "outputs": [],
            "errors": [],
            "models": [],
            "spend_usd": 0,
        },
    )
    _json(
        root / "receipts" / "trace.json",
        {
            "stage": "trace",
            "seconds": 2.5,
            "inputs": [{"path": str(root / "runs.jsonl")}],
            "outputs": [{"path": str(root / "decisions.jsonl"), "rows": 10}],
            "errors": [
                {"class": "TimeoutError", "message": "cached fixture"},
                {"class": "TimeoutError", "message": "cached fixture"},
                {"class": "ValueError", "message": "cached fixture"},
            ],
            "models": [{"role": "autoreview", "route": "fixture/cached"}],
            "spend_usd": 0,
        },
    )
    _json(
        root / "check.json",
        {
            "checks": [{"name": "conservation", "status": "PASS", "details": "fixture"}],
            "passed": True,
        },
    )
    return root


def test_scoreboard_wilson_metrics_and_file_derived_summaries(batch, tmp_path):
    before = (batch / "decisions.jsonl").read_bytes()
    value = build_scoreboard(batch, tmp_path)
    assert (batch / "decisions.jsonl").read_bytes() == before
    overall = value["overall"]
    assert {
        key: overall[key] for key in ("runs", "labelled_runs", "cheats", "clean", "attempted_cheat")
    } == {
        "runs": 10,
        "labelled_runs": 7,
        "cheats": 3,
        "clean": 4,
        "attempted_cheat": 2,
    }
    assert overall["counts"] == {"keep": 4, "drop": 3, "review": 3}
    expected = {
        "drop_recall": (1, 3),
        "drop_or_review_recall": (2, 3),
        "clean_false_alarm_rate": (1, 4),
        "clean_drop_or_review_rate": (2, 4),
        "drop_precision": (1, 2),
    }
    for metric, (k, n) in expected.items():
        assert overall["metrics"][metric] == {
            "k": k,
            "n": n,
            "rate": round(k / n, 4),
            "wilson_ci": list(hack_drop.wilson_interval(k, n)),
        }
    assert overall["metrics"]["drop_recall"]["wilson_ci"] == [0.0615, 0.7923]
    assert value["by_corpus"]["a"]["metrics"]["clean_false_alarm_rate"]["n"] == 3
    assert value["by_corpus"]["b"]["metrics"]["drop_recall"] == {
        "k": 0,
        "n": 0,
        "rate": None,
        "wilson_ci": [None, None],
    }
    assert value["stages"] == {
        "intake": {"rows_in": 10, "rows_out": 10, "seconds": 1.25, "errors": {}},
        "trace": {
            "rows_in": 10,
            "rows_out": 10,
            "seconds": 2.5,
            "errors": {"TimeoutError": 2, "ValueError": 1},
        },
    }
    assert value["judge_load"] == {
        "review": 3,
        "audit": 1,
        "total": 4,
        "per_100_runs": 40.0,
        "queued": 4,
    }
    assert value["task_side"] == {
        "certified": 2,
        "provisional": 1,
        "weak": 2,
        "uncertified": 5,
        "reward1_uncertified": 4,
    }
    assert value["spend"] == {"usd": 0.0, "cap_usd": 0, "by_stage": {"intake": 0.0, "trace": 0.0}}
    assert value["models"] == [{"role": "autoreview", "route": "fixture/cached"}]
    assert value["quality_checks"] == [
        {"name": "conservation", "status": "PASS", "details": "fixture"}
    ]
    assert build_scoreboard(batch, tmp_path) == value
    rendered = render_scoreboard(value)
    assert "95% Wilson" in rendered
    assert "1/3 (33.3%; Wilson 6.2%–79.2%)" in rendered
    assert "reward1_uncertified: 4" in rendered
    assert "TimeoutError: 2" in rendered


@pytest.mark.parametrize(
    "policy,labelled,cheats,clean", [("exclude", 7, 3, 4), ("cheat", 9, 5, 4), ("clean", 9, 3, 6)]
)
def test_attempt_policy_is_explicit_and_cannot_mutate_decisions(
    batch, tmp_path, monkeypatch, policy, labelled, cheats, clean
):
    spec = json.loads((batch / "spec.json").read_text())
    spec["labels_policy"]["attempted_cheat"] = policy
    _json(batch / "spec.json", spec)
    original = hack_drop.load_labels
    calls = []

    def loader(path, repo_root=None, **kwargs):
        calls.append((str(path), kwargs["attempted_cheat"]))
        return original(path, repo_root=repo_root, **kwargs)

    monkeypatch.setattr(hack_drop, "load_labels", loader)
    before = (batch / "decisions.jsonl").read_bytes()
    value = build_scoreboard(batch, tmp_path)
    assert value["overall"]["labelled_runs"] == labelled
    assert value["overall"]["cheats"] == cheats
    assert value["overall"]["clean"] == clean
    assert value["overall"]["attempted_cheat"] == 2
    assert calls == [(source["labels"], policy) for source in spec["sources"]]
    assert (batch / "decisions.jsonl").read_bytes() == before


@pytest.mark.parametrize("label", ["considered_only", "attempted_cheat", "attempted_but_failed"])
@pytest.mark.parametrize("policy", ["exclude", "cheat", "clean"])
def test_standard_loader_attempt_aliases(tmp_path, label, policy):
    path = tmp_path / "labels.json"
    _json(path, [{"run_id": "attempt", "label": label}, {"run_id": "honest", "is_cheat": False}])
    attempted = set()
    loaded = hack_drop.load_labels(path, attempted_cheat=policy, attempted_ids=attempted)
    assert attempted == {"attempt"}
    assert loaded == (
        {"honest": False}
        if policy == "exclude"
        else {"honest": False, "attempt": policy == "cheat"}
    )


@pytest.mark.parametrize(
    "document",
    [
        {"one": True, "two": "clean", "three": "considered_only", "four": None},
        [
            {"run_id": "one", "label": "cheat"},
            {"run_id": "two", "is_exploit": False},
            {"run_id": "three", "label": "considered_only"},
        ],
        {"rows": [{"id": "one", "behavior": "tampered"}, {"id": "two", "label": "honest"}]},
    ],
)
def test_legacy_loader_call_is_unchanged(tmp_path, document):
    path = tmp_path / "labels.json"
    _json(path, document)
    assert hack_drop.load_labels(path) == {"one": True, "two": False}
    assert hack_drop.load_labels(path, tmp_path) == {"one": True, "two": False}


@pytest.mark.parametrize("label", ["attempted_cheat", "attempted_but_failed", "pass", "fail"])
def test_legacy_loader_still_rejects_previously_invalid_labels(tmp_path, label):
    path = tmp_path / "labels.json"
    _json(path, {"run": label})
    with pytest.raises(ValueError):
        hack_drop.load_labels(path)


@pytest.mark.parametrize("jsonl", [False, True])
def test_conflicting_duplicate_labels_are_rejected_by_standard_loader(tmp_path, jsonl):
    path = tmp_path / ("labels.jsonl" if jsonl else "labels.json")
    rows = [{"run_id": "same", "label": "cheat"}, {"run_id": "same", "label": "clean"}]
    (_rows if jsonl else _json)(path, rows)
    with pytest.raises(ValueError, match="conflicting labels"):
        hack_drop.load_labels(path, attempted_cheat="exclude")


@pytest.mark.parametrize("other_corpus", ["a", "b"])
def test_conflicting_label_sources_rejected_without_decision_mutation(
    batch, tmp_path, other_corpus
):
    path = tmp_path / "conflict.json"
    _json(path, {"a-drop-cheat": False})
    spec = json.loads((batch / "spec.json").read_text())
    spec["sources"].append({"corpus": other_corpus, "labels": str(path)})
    _json(batch / "spec.json", spec)
    before = (batch / "decisions.jsonl").read_bytes()
    with pytest.raises(ValueError, match="conflicting labels"):
        build_scoreboard(batch, tmp_path)
    assert (batch / "decisions.jsonl").read_bytes() == before


def test_labels_join_only_their_source_corpus_and_identity_changes(batch, tmp_path):
    first = build_scoreboard(batch, tmp_path)
    labels = tmp_path / "labels-a.json"
    document = json.loads(labels.read_text())
    document.append({"run_id": "b-attempt", "label": "attempted_but_failed"})
    _json(labels, document)
    # The extra A-source label must not change B's joined rows or identity.
    second = build_scoreboard(batch, tmp_path)
    assert second == first
    document[0]["label"] = "clean"
    _json(labels, document)
    changed = build_scoreboard(batch, tmp_path)
    assert changed["overall"]["cohort_hash"] == first["overall"]["cohort_hash"]
    assert changed["by_corpus"]["a"]["labels_hash"] != first["by_corpus"]["a"]["labels_hash"]
    assert changed["by_corpus"]["b"] == first["by_corpus"]["b"]


def test_no_quality_file_and_no_labels_are_honest_empty_metrics(batch, tmp_path):
    spec = json.loads((batch / "spec.json").read_text())
    for source in spec["sources"]:
        source["labels"] = None
    _json(batch / "spec.json", spec)
    (batch / "check.json").unlink()
    value = build_scoreboard(batch, tmp_path)
    assert value["quality_checks"] == []
    assert value["overall"]["labelled_runs"] == 0
    assert all(
        metric == {"k": 0, "n": 0, "rate": None, "wilson_ci": [None, None]}
        for metric in value["overall"]["metrics"].values()
    )


def test_legacy_duplicate_overwrite_and_jsonl_output_are_unchanged(tmp_path):
    path = tmp_path / "labels.jsonl"
    _rows(
        path,
        [
            {"run_id": "same", "label": "cheat"},
            {"run_id": "same", "label": "clean"},
            {"run_id": "attempt", "label": "considered_only"},
        ],
    )
    assert hack_drop.load_labels(path) == {"same": False}


def test_unknown_task_status_is_uncertified_and_empty_source_is_reported(batch, tmp_path):
    tasks = [json.loads(line) for line in (batch / "tasks.jsonl").read_text().splitlines()]
    for task in tasks:
        if task["task_id"] == "certified":
            task["status"] = "unknown"
    _rows(batch / "tasks.jsonl", tasks)
    spec = json.loads((batch / "spec.json").read_text())
    spec["sources"].append({"corpus": "empty", "labels": None})
    _json(batch / "spec.json", spec)
    value = build_scoreboard(batch, tmp_path)
    assert value["task_side"]["certified"] == 0
    assert value["task_side"]["uncertified"] == 7
    assert value["task_side"]["reward1_uncertified"] == 6
    empty = value["by_corpus"]["empty"]
    assert empty["runs"] == empty["labelled_runs"] == empty["attempted_cheat"] == 0
    assert empty["counts"] == {"keep": 0, "drop": 0, "review": 0}


def test_matching_label_duplicates_are_idempotent(batch, tmp_path):
    before = build_scoreboard(batch, tmp_path)
    duplicate = tmp_path / "duplicate.json"
    _json(duplicate, {"a-drop-cheat": True})
    spec = json.loads((batch / "spec.json").read_text())
    spec["sources"].append({"corpus": "a", "labels": str(duplicate)})
    _json(batch / "spec.json", spec)
    assert build_scoreboard(batch, tmp_path) == before


def test_attempt_conflicts_with_clean_even_under_clean_policy(batch, tmp_path):
    duplicate = tmp_path / "duplicate.json"
    _json(duplicate, {"a-attempt": False})
    spec = json.loads((batch / "spec.json").read_text())
    spec["labels_policy"]["attempted_cheat"] = "clean"
    spec["sources"].append({"corpus": "a", "labels": str(duplicate)})
    _json(batch / "spec.json", spec)
    with pytest.raises(ValueError, match="conflicting labels"):
        build_scoreboard(batch, tmp_path)
