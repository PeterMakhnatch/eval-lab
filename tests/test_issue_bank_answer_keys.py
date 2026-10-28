"""Answer-key source of the issue bank: polarity, kind, split tightening, OQ-6 marks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from evallab.failure_library.bank import BankContext, IssueItem
from evallab.failure_library.bank.sources import answer_keys
from evallab.failure_library.sources import normalize_split


@dataclass
class StubContext(BankContext):
    """Dict-backed split authority: any sealed identity tightens DEV to TEST."""

    sealed: frozenset[str] = frozenset()

    def strict_split(
        self,
        declared: str,
        *,
        key_id: str | None = None,
        item_id: str | None = None,
        task_id: str | None = None,
        task_digest: str | None = None,
    ) -> Any:
        split = normalize_split(declared)
        if split != "DEV":
            return split
        if {item_id, task_id, task_digest} & self.sealed:
            return "TEST"
        return "DEV"


def task_row(
    key: str, name: str, verdict: str, split: str = "dev", **fields: Any
) -> dict[str, Any]:
    return {
        "key_id": key,
        "item_id": f"{key}:{name}",
        "expected_verdict": verdict,
        "split": split,
        "subject": {"kind": "task", "task_id": f"bench/{name}", "task_digest": f"sha256:{name}"},
        **fields,
    }


def trace_row(
    key: str, name: str, verdict: str, split: str = "dev", **fields: Any
) -> dict[str, Any]:
    return {
        "key_id": key,
        "item_id": f"{key}:{name}",
        "expected_verdict": verdict,
        "split": split,
        "subject": {"kind": "trace", "task_id": f"fam/{name}", "task_digest": None,
                    "trial_id": name},
        **fields,
    }


def make_repo(tmp_path: Path, rows: dict[str, Iterable[dict[str, Any]]]) -> Path:
    """A repo root with every mapped key present (empty unless ``rows`` names it)."""
    root = tmp_path / "repo"
    for key in answer_keys.KEYS:
        directory = root / answer_keys.KEYS_RELATIVE / key
        directory.mkdir(parents=True)
        lines = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows.get(key, ()))
        (directory / "items.jsonl").write_text(lines, encoding="utf-8")
    return root


def build(tmp_path: Path, rows: dict[str, Iterable[dict[str, Any]]], **ctx: Any) -> list[IssueItem]:
    context = StubContext(repo_root=make_repo(tmp_path, rows), state_root=tmp_path / "state", **ctx)
    return [item for item in answer_keys.build(context) if isinstance(item, IssueItem)]


def by_ref(items: list[IssueItem]) -> dict[str, IssueItem]:
    return {item.provenance.ref: item for item in items}


@pytest.fixture(autouse=True)
def results_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "results"
    home.mkdir()
    monkeypatch.setenv("EVALLAB_RESULTS_HOME", str(home))
    return home


def write_trajectory(home: Path, reference: str) -> None:
    path = home / reference / "agent" / "trajectory.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")


def test_polarity_and_skipped_verdicts(tmp_path: Path) -> None:
    key = "mimo-weak-tests-v1"
    items = build(
        tmp_path,
        {
            key: [
                task_row(key, "a", "fail", expected_category="weak", label_strength="confirmed",
                         expected_root_causes=["untested_reference_block"]),
                task_row(key, "b", "pass", label_strength="refuted_claim"),
            ],
            "mimo-stratified-sample100": [
                task_row("mimo-stratified-sample100", "u", "unknown", label_strength="unknown"),
                task_row("mimo-stratified-sample100", "w", "fail", expected_category="weak",
                         label_strength="confirmed"),
            ],
            "mimo-fresh-window3": [
                trace_row("mimo-fresh-window3", "wp", "fail", source_label="wrong_pass",
                          expected_category="weak"),
                trace_row("mimo-fresh-window3", "rp", "pass", source_label="real_pass"),
                trace_row("mimo-fresh-window3", "np", "fail", source_label="not_a_pass",
                          expected_category="failure_cause"),
            ],
        },
    )
    refs = by_ref(items)
    assert refs[f"{key}:a"].polarity == "positive"
    assert refs[f"{key}:a"].kind == "grader_wrong_pass"
    assert refs[f"{key}:a"].label_type == "executed"
    assert refs[f"{key}:b"].polarity == "negative"
    assert refs[f"{key}:b"].kind == "grader_wrong_pass"  # key default for a category-less negative
    # unknown is not clean and never an item; a failed (not_a_pass) run is not a wrong pass
    assert "mimo-stratified-sample100:u" not in refs
    assert "mimo-fresh-window3:np" not in refs
    assert refs["mimo-stratified-sample100:w"].polarity == "positive"
    assert refs["mimo-fresh-window3:wp"].polarity == "positive"
    assert refs["mimo-fresh-window3:rp"].polarity == "negative"
    assert refs["mimo-fresh-window3:wp"].granularity == "run"
    assert refs["mimo-fresh-window3:wp"].subject_type == "run"


def test_task_kinds_follow_the_key_definition(tmp_path: Path) -> None:
    tb4 = "tb4-audit-v1"
    delphik = "delphik-open-defect-tb2"
    tb2 = "tb2-to-tb21-v1"
    rows = {
        tb4: [
            task_row(tb4, "weak", "fail", taxonomy_class="weak_check", label_strength="pm_reported",
                     scope="task"),
            task_row(tb4, "unstated", "fail", taxonomy_class="unstated_requirement",
                     label_strength="pm_reported", scope="task"),
            task_row(tb4, "leak", "fail", taxonomy_class="answer_reachable",
                     label_strength="pm_reported", scope="task"),
            task_row(tb4, "witnessed", "fail", taxonomy_class="grader_tamperable",
                     label_strength="confirmed", scope="task"),
        ],
        delphik: [
            task_row(delphik, "p", "fail", expected_category="broken", label_strength="confirmed",
                     root_cause=[{"id": "tb__x__pr-36", "source_class": "Verifier path defect"}]),
            task_row(delphik, "i", "fail", expected_category="weak", label_strength="confirmed",
                     root_cause=[{"id": "tb__x__issue-7", "source_class": "loophole"}]),
            task_row(delphik, "n", "pass", label_strength="weak_negative"),
        ],
        tb2: [
            task_row(tb2, "mod-old", "fail", change_kind="verifier_relaxation_or_fix", role="child"),
            task_row(tb2, "mod-new", "pass", change_kind="verifier_relaxation_or_fix", role="parent"),
            task_row(tb2, "fmt", "pass", change_kind="format_only", role="child"),
        ],
    }
    refs = by_ref(build(tmp_path, rows))
    assert refs[f"{tb4}:weak"].kind == "grader_wrong_pass"
    assert refs[f"{tb4}:weak"].label_type == "human"
    assert refs[f"{tb4}:unstated"].kind == "grader_unfair_fail"
    assert refs[f"{tb4}:leak"].kind == "task_defect"
    assert refs[f"{tb4}:witnessed"].label_type == "executed"
    assert refs[f"{delphik}:p"].kind == "task_defect"
    assert refs[f"{delphik}:p"].label_type == "upstream_fix"
    assert refs[f"{delphik}:i"].kind == "grader_wrong_pass"
    assert refs[f"{delphik}:i"].label_type == "static"
    assert refs[f"{delphik}:n"].polarity == "negative"
    assert refs[f"{delphik}:n"].kind == "task_defect"
    assert refs[f"{tb2}:mod-old"].kind == "grader_unfair_fail"
    assert refs[f"{tb2}:mod-new"].polarity == "negative"
    assert refs[f"{tb2}:mod-new"].kind == "grader_unfair_fail"
    assert refs[f"{tb2}:fmt"].kind == "task_defect"
    assert refs[f"{tb2}:fmt"].label_type == "upstream_fix"


def test_breaker_parent_takes_its_childs_kind(tmp_path: Path) -> None:
    key = "breaker-bench-v1"
    child = task_row(key, "c", "fail", expected_category="reachable", source_set="terminal",
                     role="child", expected_root_cause="x", paired_counterpart_id=f"{key}:p")
    parent = task_row(key, "p", "pass", expected_category="tamperable",
                      source_set="benchflow_0.2.0_vs_0.2.2", role="parent",
                      paired_counterpart_id=f"{key}:c")
    quirk = task_row(key, "q", "fail", expected_category="broken",
                     source_set="benchflow_0.2.0_vs_0.2.2", role="known_quirk")
    unmeasured = task_row(key, "t", "unknown", source_set="benchflow_0.2.0_vs_0.2.2",
                          role="unmeasured")
    refs = by_ref(build(tmp_path, {key: [child, parent, quirk, unmeasured]}))
    assert refs[f"{key}:c"].kind == "task_defect"
    assert refs[f"{key}:c"].label_type == "judge"
    assert refs[f"{key}:p"].kind == "task_defect"  # hardened counterpart of a reachable child
    assert refs[f"{key}:p"].polarity == "negative"
    assert refs[f"{key}:q"].kind == "grader_wrong_pass"  # a no-op patch is accepted
    assert f"{key}:t" not in refs


def test_split_is_tightened_never_upgraded(tmp_path: Path) -> None:
    key = "executed-outcomes-v1"
    rows = {
        key: [
            task_row(key, "free", "fail", "dev", expected_category="weak"),
            task_row(key, "sealed", "fail", "dev", expected_category="weak"),
            task_row(key, "held", "pass", "heldout"),
            task_row(key, "test", "fail", "test", expected_category="weak"),
        ]
    }
    refs = by_ref(build(tmp_path, rows, sealed=frozenset({f"{key}:sealed"})))
    assert refs[f"{key}:free"].split == "DEV"
    assert refs[f"{key}:sealed"].split == "TEST"  # a TEST-sealed identity is never DEV
    assert refs[f"{key}:sealed"].declared_split == "dev"
    assert refs[f"{key}:held"].split == "heldout"
    assert refs[f"{key}:test"].split == "TEST"
    # sealing by task name or package digest tightens too
    by_task = build(tmp_path / "t", rows, sealed=frozenset({"bench/free"}))
    assert by_ref(by_task)[f"{key}:free"].split == "TEST"
    by_digest = build(tmp_path / "d", rows, sealed=frozenset({"sha256:free"}))
    assert by_ref(by_digest)[f"{key}:free"].split == "TEST"


def test_oq6_marks_only_dev_task_findings(tmp_path: Path) -> None:
    key = "tb4-audit-v1"
    base = {"taxonomy_class": "weak_check", "label_strength": "pm_reported"}
    rows = {
        key: [
            task_row(key, "k1", "fail", scope="task", **base),
            task_row(key, "k2", "fail", scope="task", **base),
            task_row(key, "sealed", "fail", scope="task", **base),
            task_row(key, "test", "fail", "test", scope="task", **base),
            {
                **task_row(key, "bench", "fail", scope="benchmark", **base),
                "subject": {"kind": "task", "task_id": "__benchmark__/terminal-bench-4",
                            "task_digest": None},
            },
        ]
    }
    items = build(tmp_path, rows, sealed=frozenset({f"{key}:sealed"}))
    refs = by_ref(items)
    assert refs[f"{key}:k1"].provenance.note == answer_keys.OQ6_NOTE
    assert refs[f"{key}:sealed"].split == "TEST"
    assert refs[f"{key}:sealed"].provenance.note is None
    assert refs[f"{key}:test"].provenance.note is None
    # the benchmark-wide carrier is not a task: no task_id, never keyed
    assert refs[f"{key}:bench"].task_id is None
    assert "not a task" in (refs[f"{key}:bench"].provenance.note or "")
    assert answer_keys.oq6_keyed(items) == (2, 2)


def test_cheatbench_selector_and_bountybench_quarantine(
    tmp_path: Path, results_home: Path
) -> None:
    key = "cheatbench-actions-v1"

    def row(name: str, verdict: str, split: str, family: str) -> dict[str, Any]:
        labels = {"label": "cheating", "cheating_mechanism": "m"}
        return {
            **trace_row(key, name, verdict, split, source_labels=labels),
            "subject": {"kind": "trace", "task_id": f"cheatbench/{family}", "task_digest": None,
                        "trial_id": name},
            "trial_ref": {"kind": "published", "path": f"external/{name}/full"},
        }

    rows = [
        row("dev-ok", "fail", "dev", "terminal-bench-2"),
        row("dev-missing", "fail", "dev", "terminal-bench-2"),
        row("dev-bounty", "pass", "dev", "bountybench"),
        row("test-ok", "pass", "test", "swe-bench-verified"),
    ]
    for name in ("dev-ok", "dev-bounty", "test-ok"):
        write_trajectory(results_home, f"external/{name}/full")
    refs = by_ref(build(tmp_path, {key: rows}))
    ok = refs[f"{key}:dev-ok"]
    assert (ok.kind, ok.label_type, ok.actor_transcript, ok.harness_selector) == (
        "cheat", "human", True, "cheatbench")
    assert refs[f"{key}:dev-missing"].actor_transcript is False
    assert refs[f"{key}:dev-missing"].harness_selector is None
    bounty = refs[f"{key}:dev-bounty"]
    assert bounty.split == "DEV"
    assert bounty.actor_transcript is False  # quarantined: no actor provenance
    assert bounty.harness_selector is None
    test = refs[f"{key}:test-ok"]
    assert test.split == "TEST"
    assert test.harness_selector is None  # only DEV items are reachable through the selector

    sealed = by_ref(build(tmp_path / "s", {key: rows}, sealed=frozenset({f"{key}:dev-ok"})))
    assert sealed[f"{key}:dev-ok"].split == "TEST"
    assert sealed[f"{key}:dev-ok"].harness_selector is None  # tightened: the harness refuses it


def test_trace_paired_actor_and_cross_key_lookup(tmp_path: Path, results_home: Path) -> None:
    paired = "trace-paired-v1"
    weak = "mimo-weak-tests-v1"
    write_trajectory(results_home, "published/model-child")
    rows = {
        paired: [
            trace_row(paired, "model-child", "fail", expected_category="reward_hacking",
                      trace_kind="model", role="child", has_atif=True,
                      label_source="contamination-audit@2026 (GPT + Claude)",
                      expected_root_cause="secret", trial_ref={"path": "published/model-child"}),
            trace_row(paired, "scripted", "fail", expected_category="reward_hacking",
                      trace_kind="cheat_agent", role="child", has_atif=False,
                      label_source="scripted evallab CheatAgent attack",
                      expected_root_cause="cheat_ladder: skip_plant",
                      trial_ref={"path": "published/scripted"}),
            trace_row(paired, "weak-pass", "fail", expected_category="false_positive",
                      trace_kind="model", role="child", has_atif=True,
                      label_source="HAR-207 diff-against-reference review",
                      trial_ref={"path": "published/weak-pass"}),
        ],
        weak: [
            trace_row(weak, "scripted", "pass", expected_category=None, role="parent"),
            trace_row(weak, "model-child", "fail", expected_category="false_positive",
                      role="child"),
        ],
    }
    refs = by_ref(build(tmp_path, rows))
    child = refs[f"{paired}:model-child"]
    assert (child.kind, child.label_type, child.actor_transcript) == ("cheat", "consensus", True)
    assert refs[f"{paired}:scripted"].label_type == "executed"
    assert refs[f"{paired}:scripted"].actor_transcript is False  # no actor steps exist
    weak_pass = refs[f"{paired}:weak-pass"]
    assert (weak_pass.kind, weak_pass.label_type) == ("grader_wrong_pass", "static")
    assert weak_pass.actor_transcript is False  # ATIF claimed but the file is not local
    # mimo-weak-tests-v1 trace rows resolve their transcript through trace-paired-v1
    assert refs[f"{weak}:scripted"].actor_transcript is False
    assert refs[f"{weak}:scripted"].granularity == "run"
    assert refs[f"{weak}:model-child"].actor_transcript is True
    assert refs[f"{weak}:model-child"].kind == "grader_wrong_pass"


def test_trajectory_judge_is_construction_cheat_with_local_transcripts(tmp_path: Path) -> None:
    key = "trajectory-judge-400"
    root = make_repo(
        tmp_path,
        {
            key: [
                trace_row(key, "f", "fail", expected_category="false_positive", silent=True,
                          expected_root_cause={"failure_type": "wrong_tool", "failure_step": 3}),
                trace_row(key, "c", "pass", expected_category="false_positive", silent=False),
            ]
        },
    )
    upstream = root / answer_keys.KEYS_RELATIVE / key / "upstream"
    upstream.mkdir()
    (upstream / "trajectories.jsonl").write_text("{}\n", encoding="utf-8")
    context = StubContext(repo_root=root, state_root=tmp_path / "state")
    refs = by_ref([i for i in answer_keys.build(context) if isinstance(i, IssueItem)])
    faulty = refs[f"{key}:f"]
    assert (faulty.kind, faulty.polarity, faulty.label_type) == ("cheat", "positive", "construction")
    assert faulty.label == "wrong_tool:silent"
    assert faulty.actor_transcript is True
    assert refs[f"{key}:c"].polarity == "negative"
    assert refs[f"{key}:c"].label == "clean"


def test_no_key_bodies_reach_items(tmp_path: Path) -> None:
    key = "mimo-weak-tests-v2"
    row = task_row(key, "x", "fail", expected_category="weak", label_strength="confirmed",
                   expected_root_causes=["untested_reference_block"],
                   evidence=["SECRET-EVIDENCE the hidden test only checks X"],
                   notes="SECRET-NOTES",
                   root_cause=[{"summary": "SECRET-SUMMARY"}],
                   package={"root_hint": "SECRET-PATH"})
    (item,) = build(tmp_path, {key: [row]})
    assert "SECRET" not in item.model_dump_json()


def test_provenance_digest_is_the_key_files_sha256(tmp_path: Path) -> None:
    key = "executed-outcomes-v1"
    root = make_repo(
        tmp_path, {key: [task_row(key, "a", "fail", expected_category="tamperable")]}
    )
    context = StubContext(repo_root=root, state_root=tmp_path / "state")
    (item,) = [i for i in answer_keys.build(context) if isinstance(i, IssueItem)]
    path = root / answer_keys.KEYS_RELATIVE / key / "items.jsonl"
    assert item.provenance.store == f"answer-keys:{key}"
    assert item.provenance.ref == f"{key}:a"
    assert item.provenance.digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert item.kind == "grader_wrong_pass"  # tamperable: the grader accepts forged success
    again = [i for i in answer_keys.build(context) if isinstance(i, IssueItem)]
    assert [i.item_id for i in again] == [item.item_id]


def test_unmapped_key_fails_loudly(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {})
    extra = root / answer_keys.KEYS_RELATIVE / "brand-new-key"
    extra.mkdir()
    (extra / "items.jsonl").write_text("", encoding="utf-8")
    context = StubContext(repo_root=root, state_root=tmp_path / "state")
    with pytest.raises(ValueError, match="brand-new-key"):
        list(answer_keys.build(context))


def test_every_live_key_is_mapped() -> None:
    """The mapping table covers exactly the committed answer keys."""
    live = Path(__file__).resolve().parents[1] / answer_keys.KEYS_RELATIVE
    assert sorted(p.parent.name for p in live.glob("*/items.jsonl")) == sorted(answer_keys.KEYS)
