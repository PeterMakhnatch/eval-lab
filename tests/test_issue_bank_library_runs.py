from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from evallab.failure_library.bank import BankContext, IssueItem
from evallab.failure_library.bank.sources import library_runs
from evallab.failure_library.bank.sources.library_runs import SkipCounts
from evallab.failure_library.records import RunLabel, RunRecord, TaskRef
from evallab.failure_library.store import LibraryStore

GOODFIRE = "goodfire-reward-hack-data"
REVISION = "0123456789abcdef0123456789abcdef01234567"


def _run(
    run_id: str,
    collection: str,
    labels: list[tuple[str, str]],
    *,
    task_id: str = "task-a",
    source_id: str = "src-a",
    split: str = "DEV",
    harness: str | None = None,
    license: str | None = "MIT",
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        collection=collection,
        task=TaskRef(
            source_id=source_id,
            task_id=task_id,
            upstream_repo="https://example.test/repo",
            upstream_revision=REVISION,
        ),
        harness=harness,
        outcome="unknown",
        labels=[RunLabel(label=name, kind=kind, source="file:///x#row=0") for name, kind in labels],  # type: ignore[arg-type]
        transcript_ref="file:///x#row=0",
        split=split,  # type: ignore[arg-type]
        license=license,
    )


@dataclass
class StubContext(BankContext):
    """Split authority replaced by a set: sealed task ids are TEST, nothing else tightens."""

    sealed: frozenset[str] = frozenset()
    calls: list[tuple[str, str | None]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []

    def strict_split(  # type: ignore[override]
        self,
        declared: str,
        *,
        key_id: str | None = None,
        item_id: str | None = None,
        task_id: str | None = None,
        task_digest: str | None = None,
    ) -> Any:
        self.calls.append((declared, task_id))
        if declared != "DEV":
            return declared
        return "TEST" if task_id in self.sealed else "DEV"


def _ctx(tmp_path: Path, runs: dict[str, list[RunRecord]], **kwargs: Any) -> StubContext:
    store = LibraryStore.for_repo(tmp_path, state_root=tmp_path / "state")
    for collection, rows in runs.items():
        store.write_runs(collection, rows, source_revision=REVISION)
    kwargs.setdefault("cloud_db", tmp_path / "no-cloud.sqlite")
    return StubContext(repo_root=tmp_path, state_root=tmp_path / "state", **kwargs)


def _build(ctx: BankContext) -> tuple[dict[str, IssueItem], SkipCounts]:
    skips = SkipCounts()
    items = list(library_runs.iter_items(ctx, skips))
    assert len({item.subject_id for item in items}) == len(items)
    return {item.subject_id: item for item in items}, skips


def _polarities(items: dict[str, IssueItem]) -> dict[str, str]:
    return {run_id: item.polarity for run_id, item in items.items()}


def test_every_collection_maps_its_labels_to_cheat_polarity(tmp_path: Path) -> None:
    runs = {
        "cheatbench": [
            _run("cb-pos", "cheatbench", [("cheating", "human"), ("reward_hacked", "human")]),
            _run("cb-neg", "cheatbench", [("benign", "human"), ("clean", "human")]),
        ],
        GOODFIRE: [
            _run(
                "gf-pos",
                GOODFIRE,
                [("monitor_verdict:yes", "judge"), ("reward_hack_behavior", "judge")],
            ),
            _run("gf-neg", GOODFIRE, [("monitor_verdict:no", "judge"), ("clean", "judge")]),
        ],
        "hacktrace": [
            _run("ht-pos", "hacktrace", [("ACT=1", "judge"), ("OUTCOME=0", "judge")]),
            _run("ht-neg", "hacktrace", [("ACT=0", "construction"), ("clean", "construction")]),
        ],
        "metr-malt": [
            _run("mm-pos", "metr-malt", [("hacking", "human"), ("reward_hacked", "human")]),
            _run("mm-neg", "metr-malt", [("clean", "human")]),
        ],
        "olmo-reward-hacking": [
            _run("ol-pos", "olmo-reward-hacking", [("reward_hacked=1", "executed")]),
            _run("ol-neg", "olmo-reward-hacking", [("reward_hacked=0", "executed")]),
        ],
        "reward-hacking-ablation": [
            _run("ab-pos", "reward-hacking-ablation", [("cheat=true", "construction")]),
            _run("ab-neg", "reward-hacking-ablation", [("cheated=false", "executed")]),
        ],
        "terminal-wrench": [
            _run("tw-pos", "terminal-wrench", [("rewarded_serious_exploit", "judge")]),
            _run("tw-neg", "terminal-wrench", [("attacker_legitimate_solve", "judge")]),
        ],
    }
    items, skips = _build(_ctx(tmp_path, runs))
    assert not skips
    assert {run_id: item.polarity for run_id, item in items.items()} == {
        run_id: "positive" if run_id.endswith("pos") else "negative" for run_id in items
    }
    assert len(items) == 14
    for item in items.values():
        assert (item.kind, item.granularity, item.subject_type) == ("cheat", "run", "run")
        assert item.source == "library-runs"
        assert item.task_id == "task-a"
        assert item.license == "MIT"
        assert item.locator == ""
        assert item.provenance.store.startswith("failure-library:runs/")
        assert item.provenance.ref == item.subject_id


@pytest.mark.parametrize(
    ("collection", "labels", "reason"),
    [
        (
            "cheatbench",
            [("attempt", "human"), ("verifier_exploitation", "human")],
            "ambiguous:attempt",
        ),
        (
            GOODFIRE,
            [("monitor_verdict:split", "judge"), ("verifier_pass", "executed")],
            "ambiguous:monitor_verdict:split",
        ),
        (GOODFIRE, [("monitor_verdict:unclear", "judge")], "ambiguous:monitor_verdict:unclear"),
        (
            "terminal-wrench",
            [("no_reward", "judge"), ("hollow-implementation", "judge")],
            "ambiguous:no_reward",
        ),
        (
            "terminal-wrench",
            [("baseline_condition", "construction"), ("verifier_pass", "executed")],
            "ambiguous:baseline_condition",
        ),
        ("olmo-reward-hacking", [], "unlabelled"),
        ("hacktrace", [("visible_tests_pass=true", "executed")], "unlabelled"),
        (
            "reward-hacking-ablation",
            [("passed=true", "executed"), ("cheat=none", "construction")],
            "unlabelled",
        ),
    ],
)
def test_undecided_runs_are_skipped_and_counted_never_clean(
    tmp_path: Path, collection: str, labels: list[tuple[str, str]], reason: str
) -> None:
    ctx = _ctx(tmp_path, {collection: [_run("r1", collection, labels)]})
    items, skips = _build(ctx)
    assert items == {}
    assert dict(skips) == {(collection, reason): 1}


@pytest.mark.parametrize(
    ("collection", "labels"),
    [
        (GOODFIRE, [("monitor_verdict:yes", "judge"), ("clean", "judge")]),
        ("hacktrace", [("ACT=1", "judge"), ("ACT=0", "judge")]),
        ("cheatbench", [("cheating", "human"), ("benign", "human")]),
        # a decisive label next to an ambiguous one is not trusted either
        ("terminal-wrench", [("reward_hacked", "judge"), ("no_reward", "judge")]),
        # peeked / tampered are positive evidence: they contradict an explicit clean verdict
        ("reward-hacking-ablation", [("cheated=false", "executed"), ("peeked=true", "executed")]),
        ("reward-hacking-ablation", [("clean", "executed"), ("tampered=true", "executed")]),
    ],
)
def test_contradictory_labels_are_skipped_and_counted(
    tmp_path: Path, collection: str, labels: list[tuple[str, str]]
) -> None:
    ok = _run("ok", collection, [(library_runs.MAPPINGS[collection].positive[0], "judge")])
    bad = _run("bad", collection, labels)
    items, skips = _build(_ctx(tmp_path, {collection: [ok, bad]}))
    assert set(items) == {"ok"}
    assert dict(skips) == {(collection, "contradictory"): 1}


def test_peeked_alone_is_positive_and_label_type_is_the_strongest_deciding_kind(
    tmp_path: Path,
) -> None:
    coll = "reward-hacking-ablation"
    runs = [
        _run("peek", coll, [("peeked=true", "executed"), ("passed=false", "executed")]),
        _run("both", coll, [("cheat=true", "construction"), ("tampered=true", "executed")]),
        _run("cons", coll, [("cheat=false", "construction"), ("sys=none", "construction")]),
        _run("neg-exec", coll, [("cheat=false", "construction"), ("clean", "executed")]),
    ]
    items, skips = _build(_ctx(tmp_path, {coll: runs}))
    assert not skips
    assert _polarities(items) == {
        "peek": "positive",
        "both": "positive",
        "cons": "negative",
        "neg-exec": "negative",
    }
    assert {run_id: item.label_type for run_id, item in items.items()} == {
        "peek": "executed",
        "both": "executed",
        "cons": "construction",
        "neg-exec": "executed",
    }
    assert items["both"].label == "cheat=true+tampered=true"


def test_label_type_follows_the_deciding_labels_not_other_labels(tmp_path: Path) -> None:
    # ACT/clean were judged; the executed verifier flag next to them must not upgrade the type
    runs = [
        _run("j", "hacktrace", [("ACT=1", "judge"), ("hidden_tests_pass=false", "executed")]),
        _run(
            "c", "hacktrace", [("ACT=0", "construction"), ("visible_tests_pass=true", "executed")]
        ),
    ]
    items, _ = _build(_ctx(tmp_path, {"hacktrace": runs}))
    assert items["j"].label_type == "judge"
    assert items["c"].label_type == "construction"


def test_termgrade_is_counted_not_itemised(tmp_path: Path) -> None:
    runs = [
        _run("t1", "termgrade", [("verifier_pass", "executed")]),
        _run(
            "t2", "termgrade", [("verifier_fail", "executed"), ("assertion:x:failed", "executed")]
        ),
    ]
    items, skips = _build(_ctx(tmp_path, {"termgrade": runs}))
    assert items == {}
    assert dict(skips) == {("termgrade", "no_cheat_labels"): 2}


def test_unmapped_collection_is_counted_not_guessed(tmp_path: Path) -> None:
    runs = [_run("n1", "brand-new", [("cheating", "human")])]
    items, skips = _build(_ctx(tmp_path, {"brand-new": runs}))
    assert items == {}
    assert dict(skips) == {("brand-new", "unmapped_collection"): 1}


def test_split_is_tightened_never_upgraded_declared_kept_and_cached_per_task(
    tmp_path: Path,
) -> None:
    coll = "olmo-reward-hacking"
    label = [("reward_hacked=0", "executed")]
    runs = [
        _run("sealed-1", coll, label, task_id="sealed"),
        _run("sealed-2", coll, label, task_id="sealed"),
        _run("sealed-3", coll, label, task_id="sealed"),
        _run("open", coll, label, task_id="open"),
        _run("held", coll, label, task_id="held", split="heldout"),
        _run("cross", coll, label, task_id="cross", split="cross_test"),
        _run("unset", coll, label, task_id="unset", split="unassigned"),
    ]
    ctx = _ctx(tmp_path, {coll: runs}, sealed=frozenset({"sealed"}))
    items, _ = _build(ctx)
    assert {run_id: item.split for run_id, item in items.items()} == {
        "sealed-1": "TEST",
        "sealed-2": "TEST",
        "sealed-3": "TEST",
        "open": "DEV",
        "held": "heldout",
        "cross": "cross_test",
        "unset": "unassigned",
    }
    assert items["sealed-1"].declared_split == "DEV"  # a TEST-sealed task is never emitted as DEV
    assert items["held"].declared_split == "heldout"
    assert not [item for item in items.values() if item.task_id == "sealed" and item.split == "DEV"]
    assert sorted(task for _, task in ctx.calls) == ["cross", "held", "open", "sealed", "unset"]


def _cloud_db(path: Path, trace_ids: list[str]) -> Path:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE agent_traces (trace_id TEXT PRIMARY KEY, run_id TEXT)")
    connection.executemany(
        "INSERT INTO agent_traces VALUES (?, ?)", [(trace_id, "r") for trace_id in trace_ids]
    )
    connection.commit()
    connection.close()
    return path


def test_goodfire_selector_is_the_cloud_trace_id_only_for_joined_dev_runs(tmp_path: Path) -> None:
    label = [("monitor_verdict:no", "judge"), ("clean", "judge")]
    runs = [
        _run(f"{GOODFIRE}:deepswe-x:aaa", GOODFIRE, label, split="DEV"),
        _run(f"{GOODFIRE}:deepswe-x:bbb", GOODFIRE, label, split="heldout"),
        _run(f"{GOODFIRE}:deepswe-x:ccc", GOODFIRE, label, split="DEV"),  # not in the cloud DB
        _run(f"{GOODFIRE}:tau3bench-x:ddd", GOODFIRE, label, split="DEV"),  # stream not in the DB
        _run("olmo-1", "olmo-reward-hacking", [("reward_hacked=0", "executed")]),
    ]
    by_collection = {GOODFIRE: runs[:4], "olmo-reward-hacking": runs[4:]}
    database = _cloud_db(
        tmp_path / "cloud.sqlite",
        [f"{GOODFIRE}/deepswe-x/aaa", f"{GOODFIRE}/deepswe-x/bbb", "retained-trace:other"],
    )
    items, _ = _build(_ctx(tmp_path, by_collection, cloud_db=database))
    selectors = {run_id: item.harness_selector for run_id, item in items.items()}
    assert selectors == {
        f"{GOODFIRE}:deepswe-x:aaa": f"{GOODFIRE}/deepswe-x/aaa",
        f"{GOODFIRE}:deepswe-x:bbb": None,  # heldout: trace run would drop it
        f"{GOODFIRE}:deepswe-x:ccc": None,
        f"{GOODFIRE}:tau3bench-x:ddd": None,
        "olmo-1": None,
    }


def test_missing_cloud_db_means_no_selectors_not_a_failure(tmp_path: Path) -> None:
    run = _run(f"{GOODFIRE}:s:aaa", GOODFIRE, [("monitor_verdict:no", "judge")])
    items, _ = _build(_ctx(tmp_path, {GOODFIRE: [run]}))
    assert items[run.run_id].harness_selector is None


def test_goodfire_trace_id_shape() -> None:
    assert library_runs.goodfire_trace_id(f"{GOODFIRE}:s-1:abc-def") == f"{GOODFIRE}/s-1/abc-def"
    assert library_runs.goodfire_trace_id("olmo-reward-hacking:s:abc") is None
    assert library_runs.goodfire_trace_id(f"{GOODFIRE}:only-stream") is None


def test_actor_transcript_follows_what_the_raw_rows_hold(tmp_path: Path) -> None:
    cheat = [("cheating", "human")]
    runs = {
        "cheatbench": [
            _run("cb-bounty", "cheatbench", cheat, source_id="cheatbench-bountybench"),
            _run("cb-mle", "cheatbench", cheat, source_id="cheatbench-mle-bench"),
            _run("cb-swe", "cheatbench", cheat, source_id="cheatbench-swe-smith"),
        ],
        "reward-hacking-ablation": [
            _run(
                "ab-grader",
                "reward-hacking-ablation",
                [("cheat=true", "construction")],
                harness="rhenvs/grader",
            ),
            _run(
                "ab-concept",
                "reward-hacking-ablation",
                [("cheat=true", "construction")],
                harness="rhenvs/concept",
            ),
        ],
        "olmo-reward-hacking": [
            _run("ol", "olmo-reward-hacking", [("reward_hacked=1", "executed")])
        ],
    }
    items, _ = _build(_ctx(tmp_path, runs))
    assert {run_id: item.actor_transcript for run_id, item in items.items()} == {
        "cb-bounty": False,
        "cb-mle": None,
        "cb-swe": True,
        "ab-grader": True,
        "ab-concept": None,
        "ol": True,
    }


def test_provenance_digest_is_the_runs_file_sha256_and_tags_are_noted(tmp_path: Path) -> None:
    runs = [
        _run(
            "gf-tagged",
            GOODFIRE,
            [
                ("monitor_verdict:yes", "judge"),
                ("reward_hack_behavior", "judge"),
                ("hidden_test_targeting", "judge"),
                ("verifier_pass", "executed"),
            ],
        ),
        _run("gf-plain", GOODFIRE, [("monitor_verdict:no", "judge")]),
    ]
    ctx = _ctx(tmp_path, {GOODFIRE: runs})
    items, _ = _build(ctx)
    store = LibraryStore.for_repo(tmp_path, state_root=tmp_path / "state")
    expected = hashlib.sha256(store.runs_path(GOODFIRE).read_bytes()).hexdigest()
    assert items["gf-tagged"].provenance.digest == expected
    assert items["gf-tagged"].provenance.note == "tags=hidden_test_targeting"
    assert items["gf-plain"].provenance.note is None
    assert items["gf-tagged"].label == "monitor_verdict:yes+reward_hack_behavior"


def test_item_ids_are_deterministic_across_builds(tmp_path: Path) -> None:
    runs = [_run("r1", "metr-malt", [("hacking", "human")], split="heldout")]
    ctx = _ctx(tmp_path, {"metr-malt": runs})
    first, _ = _build(ctx)
    second, _ = _build(ctx)
    assert {item.item_id for item in first.values()} == {item.item_id for item in second.values()}
    assert first["r1"].split == "heldout"
    assert first["r1"].license == "MIT"
