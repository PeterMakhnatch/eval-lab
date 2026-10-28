from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import pytest

from evallab.production import trace_harness as th


def _run(run_id: str = "r1", *, actor: bool = True) -> th.HarnessRun:
    steps: list[dict[str, Any]] = [{"step_id": 3, "source": "user", "message": "do the task"}]
    if actor:
        steps.append({
            "step_id": 5, "source": "agent", "message": "looking",
            "tool_calls": [{"tool_call_id": "c", "function_name": "bash",
                            "arguments": {"command": "git log --all -p"}}],
        })
    return th.HarnessRun(run_id, "src", "DEV", "unknown", tuple(steps), th.actor_inputs_available(steps))


class _Fake:
    def __init__(self, verdict: str = "clear", *, name: str = "fake", version: str = "v1",
                 reader_model: str | None = None, emit_for: Iterable[str] | None = None) -> None:
        self.name, self.version, self.reader_model = name, version, reader_model
        self.verdict = verdict
        self.emit_for = None if emit_for is None else set(emit_for)
        self.calls: list[list[str]] = []

    def detect(self, runs: Sequence[th.HarnessRun]) -> Iterable[dict[str, Any]]:
        self.calls.append([run.run_id for run in runs])
        for run in runs:
            if self.emit_for is not None and run.run_id not in self.emit_for:
                continue
            yield th.finding(run.run_id, self.name, detector_version=self.version,
                             reader_model=self.reader_model, verdict=self.verdict, kind="cheat",
                             score=1, evidence_steps=[5])


def test_finding_validation_rejects_contract_breaks() -> None:
    record = th.finding("r", "d", detector_version="v", reader_model=None, verdict="alarm", kind="cheat")
    assert th.validate_finding(record) == []
    assert th.validate_finding({**record, "verdict": "maybe"})
    assert th.validate_finding({**record, "reader_model": "no-provider"})
    assert th.validate_finding({**record, "evidence_steps": ["5"]})
    assert th.validate_finding({**record, "summary": "w " * 41})
    assert th.validate_finding({**record, "extra": 1})
    with pytest.raises(ValueError):
        th.finding("r", "d", detector_version="v", reader_model=None, verdict="alarm", kind="weird")


def test_summary_is_trimmed_to_forty_words_and_600_characters() -> None:
    record = th.finding("r", "d", detector_version="v", reader_model=None, verdict="clear",
                        kind="cheat", summary="word " * 60)
    assert len(record["summary"].split()) == th.SUMMARY_WORDS
    long_words = th.finding("r", "d", detector_version="v", reader_model=None, verdict="clear",
                            kind="cheat", summary=("x" * 50 + " ") * 30)
    assert len(long_words["summary"]) == th.SUMMARY_CHARS
    assert th.validate_finding({**long_words, "summary": "y" * 601})


def test_run_without_actor_transcript_is_never_clear(tmp_path: Path) -> None:
    store = th.FindingStore(tmp_path / "f.jsonl")
    silent, active = _run("silent", actor=False), _run("active")
    th.run_detectors([silent, active], [_Fake("clear")], store)
    verdicts = {key[0]: record["verdict"] for key, record in store.records().items()}
    assert verdicts == {"silent": "unknown", "active": "clear"}
    th.run_detectors([silent], [_Fake("alarm", name="infra")], store)
    assert store.records()[("silent", "infra", "v1", None)]["verdict"] == "alarm"


def test_cache_key_includes_version_and_reader(tmp_path: Path) -> None:
    store = th.FindingStore(tmp_path / "f.jsonl")
    runs = [_run("a"), _run("b")]
    first = _Fake(reader_model="anthropic/claude-haiku-5-5")
    th.run_detectors(runs, [first], store)
    again = _Fake(reader_model="anthropic/claude-haiku-5-5")
    summary = th.run_detectors(runs, [again], store)
    assert again.calls == [[]]
    assert next(iter(summary["detectors"].values()))["cached"] == 2
    other_reader = _Fake(reader_model="openai-codex/gpt-6-luna")
    newer = _Fake(version="v2", reader_model="anthropic/claude-haiku-5-5")
    th.run_detectors(runs, [other_reader, newer], store)
    assert other_reader.calls == [["a", "b"]] and newer.calls == [["a", "b"]]
    refreshed = _Fake(reader_model="anthropic/claude-haiku-5-5")
    th.run_detectors(runs, [refreshed], store, refresh=True)
    assert refreshed.calls == [["a", "b"]]
    assert len(store.records()) == 6


def test_missing_result_is_not_cached_and_retried(tmp_path: Path) -> None:
    store = th.FindingStore(tmp_path / "f.jsonl")
    summary = th.run_detectors([_run("a"), _run("b")], [_Fake(emit_for={"a"})], store)
    assert next(iter(summary["detectors"].values()))["no_result"] == 1
    retry = _Fake()
    th.run_detectors([_run("a"), _run("b")], [retry], store)
    assert retry.calls == [["b"]]


def test_detector_cannot_write_under_another_identity_or_run(tmp_path: Path) -> None:
    store = th.FindingStore(tmp_path / "f.jsonl")

    class Liar(_Fake):
        def detect(self, runs: Sequence[th.HarnessRun]) -> Iterable[dict[str, Any]]:
            yield th.finding(runs[0].run_id, "other", detector_version="v1", reader_model=None,
                             verdict="alarm", kind="cheat")

    class Stray(_Fake):
        def detect(self, runs: Sequence[th.HarnessRun]) -> Iterable[dict[str, Any]]:
            yield th.finding("never-asked", self.name, detector_version="v1", reader_model=None,
                             verdict="alarm", kind="cheat")

    for bad in (Liar(), Stray()):
        with pytest.raises(ValueError):
            th.run_detectors([_run("a")], [bad], store)
    assert store.records() == {}


def test_store_refuses_corrupt_lines(tmp_path: Path) -> None:
    path = tmp_path / "f.jsonl"
    path.write_text(json.dumps({"run_id": "x"}) + "\n")
    with pytest.raises(ValueError):
        th.FindingStore(path).records()


def test_detector_specs_name_a_registered_detector_and_optional_reader() -> None:
    assert th.parse_detector_spec("facets:anthropic/claude-haiku-5-5") == ("facets", "anthropic/claude-haiku-5-5")
    assert th.parse_detector_spec("cascade") == ("cascade", None)
    with pytest.raises(ValueError):
        th.parse_detector_spec("nope")


def test_card_is_label_free_and_within_budget() -> None:
    run = _run("goodfire-like")
    findings = [
        th.finding(run.run_id, f"det{i}", detector_version="v", reader_model="anthropic/claude-haiku-5-5",
                   verdict="alarm" if i % 2 else "clear", kind="cheat", score=i,
                   evidence_steps=range(i, i + 12), summary="long " * 40)
        for i in range(40)
    ]
    text = th.render_card(run, findings, run_id=run.run_id, max_tokens=300)
    assert len(text) <= 300 * th.CHARS_PER_TOKEN
    full = th.render_card(run, findings[:3], run_id=run.run_id)
    assert "src" not in full.split("\n", 2)[1] and "outcome" not in full
    assert "ALARM" in full and "git_history_peek@5" in full
    assert "--around" in full


def test_coverage_counts_findings_per_corpus_and_shows_detectors_that_never_ran(monkeypatch) -> None:
    monkeypatch.setattr(th, "DETECTORS", {"cascade": "x:y", "facets": "x:y", "run_health": "x:y"})
    records = {
        th.cache_key(r): r for r in (
            th.finding("a1", "cascade", detector_version="v8", reader_model=None, verdict="alarm", kind="cheat"),
            th.finding("a2", "cascade", detector_version="v8", reader_model=None, verdict="clear", kind="cheat"),
            th.finding("b1", "cascade", detector_version="v8", reader_model=None, verdict="abstain", kind="cheat"),
            th.finding("x9", "cascade", detector_version="v8", reader_model=None, verdict="alarm", kind="cheat"),
            th.finding("a1", "run_health.loop", detector_version="1", reader_model=None, verdict="clear", kind="loop"),
        )
    }
    rows = th.coverage(records, {"A": frozenset({"a1", "a2", "a3"}), "B": frozenset({"b1"})})
    by = {(r["detector"], r["corpus"]): r for r in rows}
    assert (by["cascade", "A"]["findings"], by["cascade", "A"]["cohort"]) == (2, 3)
    assert by["cascade", "A"]["verdicts"]["alarm"] == 1 and by["cascade", "B"]["verdicts"]["abstain"] == 1
    assert by["facets", None]["findings"] == 0  # registered, never ran on these corpora
    assert ("run_health", None) not in by  # covered through its run_health.* identities
