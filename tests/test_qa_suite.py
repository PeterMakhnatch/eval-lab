"""Consumer regressions for retained QA-suite comparisons; no model executions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from evallab.cli import parser
from evallab.qa import suite

DAY = "2026-10-19"
REPORT = "research/qa-scoreboard/suite-20261019.md"
_MISSING = object()


def _item(
    item_id: str,
    *,
    task: str | None = None,
    digest: str | None = "sha256:package-a",
    trial: str | None = None,
    kind: str = "task",
    verdict: str = "fail",
    split: str = "heldout",
) -> dict[str, Any]:
    return {
        "schema": "qa.answer_key/v1",
        "item_id": item_id,
        "subject": {"kind": kind, "task_id": task or item_id, "task_digest": digest, "trial_id": trial},
        "expected_verdict": verdict,
        "expected_category": "weak" if kind == "task" else "reward_hacking",
        "label_source": "synthetic witnessed labels; unknown is not a clean negative",
        "split": split,
    }


def _finding(
    item: dict[str, Any],
    *,
    version: str = "qa-task/v1",
    verdict: str = "fail",
    measurement: str = "measured",
    cost: object = 0.5,
    stage: str = "S2",
) -> dict[str, Any]:
    reviewer: dict[str, Any] = {"agent": version}
    if cost is not _MISSING:
        reviewer["cost_usd"] = cost
    return {
        "schema": "qa.finding/v1",
        "id": f"{item['item_id']}-{stage}",
        "subject": dict(item["subject"]),
        "stage": stage,
        "verdict": verdict,
        "category": item["expected_category"],
        "severity": "high" if verdict == "fail" else None,
        "fault": "eval" if verdict == "fail" else None,
        "reviewer": reviewer,
        "measurement": measurement,
    }


def _jsonl(path: Path, records: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def _key(root: Path, key: str, items: list[dict[str, Any]], pairs: list[dict] | None = None) -> Path:
    directory = root / "research/answer-keys" / key
    directory.mkdir(parents=True)
    (directory / "README.md").write_text("# Synthetic key\n\nWitnessed test labels only.\n", encoding="utf-8")
    _jsonl(directory / "items.jsonl", items)
    if pairs:
        _jsonl(directory / "pairs.jsonl", pairs)
    return directory


def _freeze(root: Path, exposures: list[dict] | None = None) -> dict[Path, bytes]:
    """Create a real guard-produced fixture lock, then leave it immutable."""
    guard = suite._load_split_guard(root)
    _jsonl(guard.EXPOSURE, exposures or [])
    raw_keys = guard.load_keys()
    assignments, report, errors = guard.build(raw_keys, exposures or [])
    assert errors == []
    guard.ASSIGNMENTS.write_text(assignments, encoding="utf-8")
    guard.REPORT.write_text(report, encoding="utf-8")
    return {
        path: path.read_bytes()
        for path in (root / "research/answer-keys").rglob("*")
        if path.is_file()
    }


def _manifest(root: Path, runs: list[dict], name: str = "inputs/runs.json") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"runs": runs}), encoding="utf-8")
    return path


def _run(key: str, path: str, version: str = "qa-task/v1", agent: str = "task", **metadata: str) -> dict:
    return {"agent": agent, "version": version, "key": key, "findings": path, **metadata}


def _invoke(root: Path, *extra: str, agent: str = "task", versions: tuple[str, ...] = ("qa-task/v1",)) -> int:
    argv = ["qa", "suite", "--agent", agent, "--date", DAY, "--json"]
    for version in versions:
        argv += ["--version", version]
    args = parser().parse_args([*argv, *extra])
    return args.func(args, root)


def _row(payload: dict, key: str = "k", split: str = "heldout", version: str = "qa-task/v1") -> dict:
    return next(
        row for row in payload["rows"]
        if row["version"] == version and row["score"]["key_id"] == key and row["score"]["split"] == split
    )


def test_suite_compares_every_native_split_and_exact_versions_without_mutating_custody(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    positive = _item("positive", split="dev")
    negative = _item("negative", verdict="pass", split="heldout")
    _key(tmp_path, "k", [positive, negative])
    _key(tmp_path, "newly-registered", [_item("later", split="dev")])
    _key(tmp_path, "trace-only", [_item("trial", kind="trace", trial="exact-trial", split="test")])
    frozen = _freeze(tmp_path)
    first = _jsonl(tmp_path / "inputs/raw/first.jsonl", [
        _finding(positive, cost=0.3), _finding(negative, verdict="pass", cost=0.7),
    ])
    second = _jsonl(tmp_path / "inputs/raw/second.jsonl", [
        _finding(positive, version="qa-task/v2", verdict="pass", cost=0.9),
        _finding(negative, version="qa-task/v2", verdict="fail", cost=1.1),
    ])
    manifest = _manifest(tmp_path, [
        _run("k", "raw/first.jsonl", run="retained-one", notes="metadata, not new validation"),
        _run("k", "raw/second.jsonl", version="qa-task/v2"),
    ])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    output = tmp_path / "out/comparison.md"
    flags = ("--runs", str(manifest), "--output", str(output))
    assert _invoke(tmp_path, *flags, versions=("qa-task/v2", "qa-task/v1")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["rows"]) == 8
    assert [(r["version"], r["score"]["key_id"], r["score"]["split"]) for r in payload["rows"]] == sorted(
        (r["version"], r["score"]["key_id"], r["score"]["split"]) for r in payload["rows"]
    )
    assert {r["score"]["category"] for r in payload["rows"]} == {"all"}
    assert all(r["score"]["split"] != "all" for r in payload["rows"])
    assert _row(payload, split="dev")["score"]["recall"] == 1.0
    assert _row(payload, split="dev", version="qa-task/v2")["score"]["recall"] == 0.0
    assert _row(payload)["score"]["false_alarm_rate"] == 0.0
    assert _row(payload, version="qa-task/v2")["score"]["false_alarm_rate"] == 1.0
    assert _row(payload, split="dev")["recorded_reviewer_cost_usd"] == pytest.approx(0.3)
    assert _row(payload)["recorded_reviewer_cost_usd"] == pytest.approx(0.7)
    no_run = _row(payload, key="newly-registered", split="dev")
    assert (no_run["score"]["status"], no_run["score"]["n_measured"], no_run["recorded_reviewer_cost_usd"]) == ("no-run", 0, None)
    assert no_run["score"]["recall"] is None
    assert _row(payload, key="trace-only", split="test")["score"]["status"] == "inapplicable"
    assert payload["new_rescore_cost_usd"] == 0.0
    assert payload["new_benchmark_executions"] == 0
    receipts = {source["path"]: source for source in payload["sources"]}
    assert receipts[str(first)]["sha256"] == "sha256:" + hashlib.sha256(first.read_bytes()).hexdigest()
    assert receipts[str(second)]["n_findings"] == 2
    assert receipts[str(first)]["run"] == "retained-one"
    assert "cost_per_item_usd" not in no_run["score"]
    original = output.read_bytes()
    assert _invoke(tmp_path, *flags, versions=("qa-task/v1", "qa-task/v2")) == 0
    capsys.readouterr()
    assert output.read_bytes() == original
    assert {path: path.read_bytes() for path in frozen} == frozen
    assert not list(output.parent.glob(f".{output.name}.*"))


def test_same_task_name_different_digest_is_not_cross_scored_or_charged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = _item("old", task="shared-task", digest="sha256:old")
    new = _item("new", task="shared-task", digest="sha256:new", verdict="pass")
    undigested = _item("undigested", task="no-digest", digest=None)
    directory = _key(tmp_path, "k", [old, new, undigested])
    _freeze(tmp_path)
    wrong = _item("outside", task="shared-task", digest="sha256:unregistered")
    _jsonl(directory / "findings.jsonl", [
        _finding(old, cost=0.3), _finding(new, verdict="pass", cost=0.2),
        _finding(wrong, cost=99), _finding(undigested, cost=80),
    ])
    assert _invoke(tmp_path) == 0
    row = _row(json.loads(capsys.readouterr().out))
    assert (row["score"]["n_measured"], row["score"]["recall"], row["score"]["false_alarm_rate"]) == (2, 1.0, 0.0)
    assert row["n_outside_findings"] == 2
    assert row["n_identity_unavailable"] == 1
    assert row["recorded_reviewer_cost_usd"] == pytest.approx(0.5)
    assert row["cost_per_measured_item_usd"] == pytest.approx(0.25)


def test_unknown_labels_abstentions_not_run_unusable_and_incomplete_pairs_stay_distinct(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    items = [
        _item("child", task="pair", trial="child-trial", kind="trace"),
        _item("parent", task="pair", trial="parent-trial", kind="trace", verdict="pass"),
        _item("unlabelled", kind="trace", trial="u", verdict="unknown"),
        _item("abstained", kind="trace", trial="a"),
        _item("not-run", kind="trace", trial="n", verdict="pass"),
        _item("unusable", kind="trace", trial="r", verdict="pass"),
        _item("missing", kind="trace", trial="m"),
    ]
    _key(tmp_path, "k", items, [{"parent": "parent", "child": "child"}])
    _freeze(tmp_path)
    version = "qa-trace/3"
    records = [
        _finding(items[0], version=version + "/T2", stage="T2", cost=_MISSING),
        _finding(items[1], version=version + "/T1", stage="T1", verdict="unknown", measurement="not_run", cost=0),
        _finding(items[2], version=version + "/T2", stage="T2", verdict="pass", cost=0.25),
        _finding(items[3], version=version + "/T1", stage="T1", verdict="unknown", cost=0.5),
        _finding(items[4], version=version + "/T1", stage="T1", verdict="unknown", measurement="not_run", cost=0),
        _finding(items[5], version=version + "/T1", stage="T1", verdict="pass", measurement="ran_without_usable_result", cost=0.75),
    ]
    source = _jsonl(tmp_path / "raw/traces.jsonl", records)
    manifest = _manifest(tmp_path, [_run("k", str(source), version=version, agent="trace")])
    assert _invoke(tmp_path, "--runs", str(manifest), agent="trace", versions=(version,)) == 0
    payload = json.loads(capsys.readouterr().out)
    row = _row(payload, version=version)
    assert (row["score"]["n_items"], row["score"]["n_measured"]) == (7, 2)
    assert (row["n_positive_measured"], row["n_negative_measured"], row["n_unknown_measured"]) == (1, 0, 1)
    assert row["score"]["recall"] == 1.0
    assert row["score"]["false_alarm_rate"] is None
    assert row["score"]["paired_discrimination"] is None
    assert (row["score"]["n_pairs"], row["score"]["n_pairs_measured"]) == (1, 0)
    assert (row["n_unknown_decisions"], row["n_not_run"], row["n_unusable"], row["n_no_evidence"]) == (1, 2, 1, 1)
    assert row["recorded_reviewer_cost_usd"] is None
    assert row["recorded_cost_subtotal_usd"] == pytest.approx(1.5)
    assert (row["n_recorded_costs"], row["n_cost_findings"]) == (5, 6)
    assert row["cost_per_measured_item_usd"] is None
    report = (tmp_path / REPORT).read_text()
    assert "n/a (n=0)" in report
    assert "n/a (0/1)" in report
    records[1] = _finding(items[1], version=version + "/T1", stage="T1", verdict="pass", cost=0.1)
    _jsonl(source, records)
    assert _invoke(tmp_path, "--runs", str(manifest), agent="trace", versions=(version,)) == 0
    complete_pair = _row(json.loads(capsys.readouterr().out), version=version)
    assert complete_pair["score"]["paired_discrimination"] == 1.0
    assert complete_pair["score"]["n_pairs_measured"] == 1
    assert complete_pair["score"]["false_alarm_rate"] == 0.0


def test_trace_findings_require_the_registered_trial_not_a_same_task_wrapper(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    item = _item("trace", kind="trace", trial="registered-trial")
    directory = _key(tmp_path, "k", [item])
    _freeze(tmp_path)
    wrong = _finding(item, version="qa-trace/8/T1", stage="T1", cost=12)
    wrong["subject"]["trial_id"] = "wrapper-hash"
    _jsonl(directory / "findings.jsonl", [wrong])
    _jsonl(directory / "findings-another-version.jsonl", [
        _finding(item, version="qa-trace/80/T1", stage="T1", cost=90),
    ])
    _jsonl(directory / "findings-mismatched-stage.jsonl", [
        _finding(item, version="qa-trace/8/T1", stage="T3", cost=90),
    ])
    assert _invoke(tmp_path, agent="trace", versions=("qa-trace/8",)) == 0
    row = _row(json.loads(capsys.readouterr().out), version="qa-trace/8")
    assert (row["score"]["status"], row["score"]["n_measured"]) == ("unmeasured", 0)
    assert row["n_outside_findings"] == 1
    assert row["recorded_reviewer_cost_usd"] is None


def test_key_local_discovery_uses_raw_proof_not_paths_and_deduplicates_copies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a, b = _item("a"), _item("b")
    directory = _key(tmp_path, "k", [a, b])
    _freeze(tmp_path)
    recorded = _finding(a, cost=0)
    _jsonl(directory / "findings-supported.jsonl", [recorded])
    _jsonl(directory / "findings/qa-task-v1/decisions.jsonl", [recorded, _finding(b, version="QA Task", cost=40)])
    assert _invoke(tmp_path, versions=("qa-task/v1", "qa-task/v2")) == 0
    payload = json.loads(capsys.readouterr().out)
    measured = _row(payload)
    assert measured["score"]["n_measured"] == 1
    assert measured["recorded_reviewer_cost_usd"] == 0.0
    assert measured["n_recorded_costs"] == 1
    assert any("unproved producer/version" in note for note in measured["notes"])
    absent = _row(payload, version="qa-task/v2")
    assert (absent["score"]["status"], absent["score"]["n_measured"]) == ("no-run", 0)
    assert absent["recorded_reviewer_cost_usd"] is None


@pytest.mark.parametrize(
    ("agent", "version"),
    [("task", "qa-task/v1"), ("trace", "qa-trace/v1")],
)
def test_mixed_keys_keep_all_frozen_items_without_cross_agent_measurement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], agent: str, version: str
) -> None:
    task = _item("task", task="shared")
    trace = _item("trace", task="shared", kind="trace", trial="registered")
    directory = _key(tmp_path, "k", [task, trace])
    _freeze(tmp_path)
    _jsonl(directory / "findings.jsonl", [
        _finding(task), _finding(trace, version="qa-task/v1"),
    ])
    _jsonl(directory / "findings-trace.jsonl", [_finding(trace, version="qa-trace/v1")])
    assert _invoke(tmp_path, agent=agent, versions=(version,)) == 0
    row = _row(json.loads(capsys.readouterr().out), version=version)
    assert (row["score"]["n_items"], row["n_applicable"], row["score"]["n_measured"]) == (2, 1, 1)
    assert row["n_positive_measured"] == 1
    assert row["n_no_evidence"] == 1
    assert row["score"]["recall"] == 1.0
    assert row["recorded_reviewer_cost_usd"] == pytest.approx(0.5)


def test_refuted_flags_and_root_cause_attribution_retain_shared_scorer_meaning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    standing = _item("standing") | {"expected_root_causes": ["wrong_tests"]}
    refuted = _item("refuted")
    directory = _key(tmp_path, "k", [standing, refuted])
    _freeze(tmp_path)
    _jsonl(directory / "findings.jsonl", [
        _finding(standing) | {"root_cause": ["wrong_tests"]},
        _finding(refuted) | {"refutation": {"status": "refuted"}},
    ])
    assert _invoke(tmp_path) == 0
    row = _row(json.loads(capsys.readouterr().out))
    assert row["score"]["n_measured"] == 2
    assert row["score"]["recall"] == 0.5
    assert row["score"]["root_cause_match"] == 1.0


def test_directory_receipts_bind_raw_findings_not_ancillary_reports_or_appledouble(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    item = _item("a")
    _key(tmp_path, "k", [item])
    _freeze(tmp_path)
    directory = tmp_path / "raw/findings"
    directory.mkdir(parents=True)
    finding = json.dumps(_finding(item, cost=0.25))
    (directory / "original.json").write_text(finding)
    (directory / "copy.json").write_text(finding)
    (directory / "report.json").write_text('{"summary": "not a finding"}')
    (directory / "._original.json").write_bytes(b"not JSON metadata")
    manifest = _manifest(tmp_path, [_run("k", str(directory))])
    assert _invoke(tmp_path, "--runs", str(manifest)) == 0
    payload = json.loads(capsys.readouterr().out)
    row = _row(payload)
    assert (row["score"]["n_measured"], row["n_recorded_costs"]) == (1, 1)
    assert row["recorded_reviewer_cost_usd"] == pytest.approx(0.25)
    assert payload["sources"][0]["n_findings"] == 1
    receipt = payload["sources"][0]["sha256"]
    (directory / "report.json").write_text('{"summary": "updated non-authority"}')
    assert _invoke(tmp_path, "--runs", str(manifest)) == 0
    assert json.loads(capsys.readouterr().out)["sources"][0]["sha256"] == receipt


def test_no_run_does_not_need_a_fabricated_exposure_acknowledgement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _key(tmp_path, "k", [_item("exposed")])
    _freeze(tmp_path, [{
        "exposure_id": "retained-training",
        "kind": "tuned",
        "applies_to": [{"agent": "QA Task"}],
        "items": {"k": ["exposed"]},
    }])
    assert _invoke(tmp_path) == 0
    row = _row(json.loads(capsys.readouterr().out))
    assert (row["score"]["status"], row["score"]["n_measured"]) == ("no-run", 0)
    assert any("retained-training" in note for note in row["notes"])


def test_an_explicit_binding_replaces_discovery_rather_than_pooling_same_version_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a, b = _item("a"), _item("b")
    directory = _key(tmp_path, "k", [a, b])
    _freeze(tmp_path)
    _jsonl(directory / "findings.jsonl", [_finding(a, cost=10)])
    source = _jsonl(tmp_path / "chosen.jsonl", [_finding(b, verdict="pass", cost=0.2)])
    manifest = _manifest(tmp_path, [_run("k", str(source))])
    assert _invoke(tmp_path, "--runs", str(manifest)) == 0
    row = _row(json.loads(capsys.readouterr().out))
    assert (row["score"]["n_measured"], row["score"]["recall"]) == (1, 0.0)
    assert row["recorded_reviewer_cost_usd"] == pytest.approx(0.2)
    assert row["sources"] == [str(source)]


@pytest.mark.parametrize("change", ["stale-split", "pair-straddle"])
def test_frozen_assignments_and_pair_units_cannot_silently_move(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], change: str
) -> None:
    a, b = _item("a"), _item("b")
    pairs = [{"parent": "a", "child": "b"}] if change == "pair-straddle" else None
    directory = _key(tmp_path, "k", [a, b], pairs)
    _freeze(tmp_path)
    a["split"] = "dev"
    _jsonl(directory / "items.jsonl", [a, b])
    current_bytes = (directory / "items.jsonl").read_bytes()
    output = tmp_path / "retained.md"
    output.write_text("previous complete comparison\n")
    assert _invoke(tmp_path, "--output", str(output)) == 2
    assert "split/exposure guard" in capsys.readouterr().err
    assert output.read_text() == "previous complete comparison\n"
    assert (directory / "items.jsonl").read_bytes() == current_bytes


def test_actual_exposed_item_cannot_hide_behind_an_aggregate_exclusion_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    items = [_item(name) for name in ("exposed", "other-a", "other-b", "other-c")]
    directory = _key(tmp_path, "k", items)
    exposure = {
        "exposure_id": "selected-on-output",
        "kind": "selected",
        "applies_to": [{"agent": "QA Task", "system_prefix": "qa-task/"}],
        "items": {"k": ["exposed"]},
        "acknowledged_rows": [{"key_id": "k", "how": "excluded", "reason": "must leave this exact item unmeasured"}],
    }
    _freeze(tmp_path, [exposure])
    _jsonl(directory / "findings.jsonl", [_finding(items[0])])
    assert _invoke(tmp_path) == 2
    assert "actually measured excluded" in capsys.readouterr().err
    assert not (tmp_path / REPORT).exists()


def test_disclosed_source_derived_labels_keep_the_guard_caveat_visible(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    item = _item("derived", verdict="unknown")
    directory = _key(tmp_path, "k", [item])
    exposure = {
        "exposure_id": "source-derived",
        "kind": "selected",
        "applies_to": [{"agent": "QA Task"}],
        "items": {"k": ["derived"]},
        "acknowledged_rows": [{"key_id": "k", "how": "disclosed", "reason": "same retained audit selected and labelled these subjects"}],
    }
    _freeze(tmp_path, [exposure])
    _jsonl(directory / "findings.jsonl", [_finding(item, verdict="pass", cost=0)])
    assert _invoke(tmp_path) == 0
    row = _row(json.loads(capsys.readouterr().out))
    assert row["score"]["n_measured"] == 1
    assert row["score"]["recall"] is None
    assert row["score"]["false_alarm_rate"] is None
    assert any("same retained audit" in note and "not independent" in note for note in row["notes"])
    report = (tmp_path / REPORT).read_text()
    assert "not total historical" in report
    assert "source-derived" in report.lower()


@pytest.mark.parametrize(
    ("agent", "version", "kind"),
    [
        ("task", "qa-task/review-v1", "task"),
        ("trace", "qa-trace/8", "trace"),
        ("breaker", "cheat-ladder-v3.0.0", "task"),
        ("breaker", "cheat-ladder-3.0.0", "task"),
        ("tripwire", "tripwire/rules-v1", "trace"),
    ],
)
def test_all_four_agents_accept_exact_raw_versioned_producers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], agent: str, version: str, kind: str
) -> None:
    item = _item("subject", kind=kind, trial="exact-trial" if kind == "trace" else None)
    directory = _key(tmp_path, "k", [item])
    _freeze(tmp_path)
    _jsonl(directory / "findings.jsonl", [_finding(item, version=version)])
    assert _invoke(tmp_path, agent=agent, versions=(version,)) == 0
    row = _row(json.loads(capsys.readouterr().out), version=version)
    assert (row["score"]["n_measured"], row["score"]["recall"]) == (1, 1.0)


@pytest.mark.parametrize(
    "bad",
    [
        "duplicate", "conflict", "unknown-key", "unknown-field", "wrong-version",
        "mixed-version", "schema", "invalid-finding", "empty", "missing",
        "conflicting-id", "duplicate-json-field", "duplicate-raw-json-field",
        "custom-decisions", "wrong-kind", "nonfinite-cost", "manifest-list",
    ],
)
def test_invalid_manifest_or_evidence_fails_actionably_without_replacing_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], bad: str
) -> None:
    item = _item("a")
    _key(tmp_path, "k", [item])
    _freeze(tmp_path)
    record = _finding(item)
    source = _jsonl(tmp_path / "raw/findings.jsonl", [record])
    runs = [_run("k", str(source))]
    if bad == "duplicate":
        runs += [dict(runs[0])]
    elif bad == "conflict":
        other = _jsonl(tmp_path / "raw/other.jsonl", [record])
        runs += [_run("k", str(other))]
    elif bad == "unknown-key":
        runs[0]["key"] = "not-registered"
    elif bad == "unknown-field":
        runs[0]["force_version"] = True
    elif bad == "wrong-version":
        _jsonl(source, [_finding(item, version="qa-task/v2")])
    elif bad == "mixed-version":
        _jsonl(source, [record, _finding(item, version="qa-task/v2", stage="S1")])
    elif bad == "schema":
        _jsonl(source, [record | {"schema": "qa.finding/v2"}])
    elif bad == "invalid-finding":
        _jsonl(source, [record | {"severity": None}])
    elif bad == "empty":
        _jsonl(source, [])
    elif bad == "missing":
        runs[0]["findings"] = str(tmp_path / "absent.jsonl")
    elif bad == "conflicting-id":
        _jsonl(source, [record, record | {"verdict": "pass"}])
    elif bad == "custom-decisions":
        _jsonl(source, [{"item_id": "a", "flag": True}])
    elif bad == "wrong-kind":
        record["subject"]["kind"] = "trace"
        record["subject"]["trial_id"] = "trace-not-task"
        _jsonl(source, [record])
    elif bad == "nonfinite-cost":
        record["reviewer"]["cost_usd"] = float("inf")
        _jsonl(source, [record])
    elif bad == "duplicate-raw-json-field":
        source.write_text(json.dumps(record)[:-1] + ', "schema": "qa.finding/v1"}\n')
    manifest = _manifest(tmp_path, runs)
    if bad == "duplicate-json-field":
        manifest.write_text('{"runs": [], "runs": ' + json.dumps(runs) + '}')
    elif bad == "manifest-list":
        manifest.write_text(json.dumps(runs))
    output = tmp_path / "comparison.md"
    output.write_bytes(b"existing report\n")
    assert _invoke(tmp_path, "--runs", str(manifest), "--output", str(output)) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error: evallab qa suite:" in captured.err
    assert output.read_bytes() == b"existing report\n"
    assert not list(tmp_path.glob(".comparison.md.*"))


def test_failed_atomic_publication_keeps_previous_report_and_removes_staged_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _key(tmp_path, "k", [_item("a")])
    _freeze(tmp_path)
    output = tmp_path / "report.md"
    output.write_bytes(b"complete previous report\n")

    def disk_full(_source: Path, _destination: Path) -> None:
        raise OSError("disk full before replacement")

    monkeypatch.setattr(suite, "durable_replace", disk_full)
    assert _invoke(tmp_path, "--output", str(output)) == 2
    assert "disk full" in capsys.readouterr().err
    assert output.read_bytes() == b"complete previous report\n"
    assert not list(tmp_path.glob(".report.md.*"))


@pytest.mark.parametrize("target", ["findings", "unselected-findings", "items", "manifest"])
def test_output_override_cannot_destroy_its_input_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], target: str
) -> None:
    item = _item("a")
    directory = _key(tmp_path, "k", [item])
    _freeze(tmp_path)
    source = _jsonl(tmp_path / "findings.jsonl", [_finding(item)])
    unselected = _jsonl(
        tmp_path / "unselected.jsonl", [_finding(item, version="qa-task/v2")],
    )
    manifest = _manifest(tmp_path, [
        _run("k", str(source)), _run("k", str(unselected), version="qa-task/v2"),
    ])
    paths = {
        "findings": source,
        "unselected-findings": unselected,
        "items": directory / "items.jsonl",
        "manifest": manifest,
    }
    destination = paths[target]
    original = destination.read_bytes()
    assert _invoke(tmp_path, "--runs", str(manifest), "--output", str(destination)) == 2
    assert "must not overwrite" in capsys.readouterr().err
    assert destination.read_bytes() == original


def test_one_report_compares_several_agents_and_lists_unmeasured_cells(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task = _item("task")
    trace = _item("trace", kind="trace", trial="registered", verdict="pass")
    _key(tmp_path, "k", [task, trace])
    _key(tmp_path, "later", [_item("later", split="dev")])
    _freeze(tmp_path)
    task_source = _jsonl(tmp_path / "raw/task.jsonl", [_finding(task)])
    wire_source = _jsonl(tmp_path / "raw/wire.jsonl", [_finding(trace, version="tripwires@abc/actions", verdict="pass", cost=0)])
    manifest = _manifest(tmp_path, [
        _run("k", str(task_source)),
        _run("k", str(wire_source), version="tripwires@abc/actions", agent="tripwire"),
    ])
    published = {
        "agent": "trace", "system": "qa-trace/6-pk1-luna", "key": "k", "split": "heldout",
        "reported": "recall 0.5 (n=2)", "reason": "aggregate row only", "source": "scoreboard.csv",
    }
    document = json.loads(manifest.read_text())
    manifest.write_text(json.dumps(document | {"not_rescored": [published, published | {"agent": "task"}]}))
    args = ("--runs", str(manifest), "--agent", "tripwire")
    assert _invoke(tmp_path, *args, versions=("qa-task/v1", "tripwires@abc/actions")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["agents"] == ["task", "tripwire"]
    assert [entry["agent"] for entry in payload["not_rescored"]] == ["task"]
    assert len(payload["rows"]) == 4
    assert _row(payload)["agent"] == "task"
    assert _row(payload)["score"]["recall"] == 1.0
    wire = _row(payload, version="tripwires@abc/actions")
    assert (wire["agent"], wire["score"]["n_measured"], wire["score"]["false_alarm_rate"]) == ("tripwire", 1, 0.0)
    assert _row(payload, key="later", split="dev", version="tripwires@abc/actions")["score"]["status"] == "inapplicable"
    report = (tmp_path / REPORT).read_text()
    assert "| tripwire | tripwires@abc/actions | inapplicable | later/dev (1) |" in report
    assert "| task | qa-task/v1 | no-run | later/dev (1) |" in report
    assert "| raw/task.jsonl |" in report
    assert str(tmp_path) not in report
    assert "| task | qa-trace/6-pk1-luna | k | heldout | recall 0.5 (n=2) | aggregate row only | scoreboard.csv |" in report
    capsys.readouterr()
    assert _invoke(tmp_path, "--runs", str(manifest), versions=("tripwires@abc/actions",)) == 2
    assert "unversioned labels are unavailable" in capsys.readouterr().err


def test_a_retained_id_reused_on_different_subjects_is_not_a_conflict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a, b = _item("a", kind="trace", trial="ta"), _item("b", kind="trace", trial="tb", verdict="pass")
    _key(tmp_path, "k", [a, b])
    _freeze(tmp_path)
    stub = {"id": "shared-t0-stub", "stage": "T0", "verdict": "unknown", "measurement": "not_run", "severity": None, "fault": None}
    records = [
        _finding(a, version="qa-trace/3/T0", stage="T0") | stub,
        _finding(b, version="qa-trace/3/T0", stage="T0") | stub,
        _finding(a, version="qa-trace/3/T1", stage="T1"),
        _finding(b, version="qa-trace/3/T1", stage="T1", verdict="pass"),
    ]
    source = _jsonl(tmp_path / "raw/v3.jsonl", records)
    manifest = _manifest(tmp_path, [_run("k", str(source), version="qa-trace/3", agent="trace")])
    assert _invoke(tmp_path, "--runs", str(manifest), agent="trace", versions=("qa-trace/3",)) == 0
    row = _row(json.loads(capsys.readouterr().out), version="qa-trace/3")
    assert (row["score"]["n_measured"], row["score"]["recall"], row["score"]["false_alarm_rate"]) == (2, 1.0, 0.0)
