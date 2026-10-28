from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from evallab.failure_library.bank import BankContext, IssueItem
from evallab.failure_library.bank.sources import grader_repairs as source
from evallab.failure_library.records import Split
from evallab.failure_library.sources import normalize_split

CLOUD_SCHEMA = """
CREATE TABLE corpus_tasks (task_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL,
    split TEXT NOT NULL, instruction_text TEXT NOT NULL DEFAULT '',
    original_grader_digest TEXT, repaired_grader_digest TEXT, cas_uri TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}');
"""
TS_PRS = [
    1325,
    1326,
    1327,
    1328,
    1329,
    1330,
    1331,
    1332,
    1343,
    1344,
    1345,
    1346,
    1347,
    1348,
    1349,
    1351,
]


@dataclass
class StubContext(BankContext):
    """A ``BankContext`` whose split authority is a set of sealed identities."""

    sealed: frozenset[str] = frozenset()

    def strict_split(
        self,
        declared: str,
        *,
        key_id: str | None = None,
        item_id: str | None = None,
        task_id: str | None = None,
        task_digest: str | None = None,
    ) -> Split:
        split = normalize_split(declared)
        if split != "DEV":
            return split
        if {item_id, task_id, task_digest} & self.sealed:
            return "TEST"
        return "DEV"


def _digest(label: str) -> str:
    return "sha256:" + (label.encode().hex() * 8)[:64]


def _parent(pr: int) -> str:
    return _digest(f"parent{pr}")


def _variant(pr: int) -> str:
    return _digest(f"variant{pr}")


def _write_variants(root: Path) -> None:
    """One synthetic variant record per merged repair: ts9 states DEV assignments, ts11 does not."""
    for pr, _merge, mission, record in source._TS_REPAIRS:
        family, _, task = record.split("/")[0].partition("__")
        prefix = "mimo-v2.6-rl" if family == "mimo-v2.6-rl" else "terminal-bench"
        inputs: dict[str, Any] = {}
        if mission == "ts9":
            inputs["dev_assignments"] = [["mimo-weak-tests-v2", "dev"]]
        path = root / "library/task-variants" / f"{record}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "task_name": f"{prefix}/{task}",
                    "variant_digest": _variant(pr),
                    "parent": {"digest": _parent(pr)},
                    "transform": f"verifier-repair@{mission}-1",
                    "inputs": inputs,
                }
            ),
            encoding="utf-8",
        )


def _case(pr: int, *, original: float = 1.0, repaired: float = 1.0, **extra: Any) -> dict[str, Any]:
    def side(task_digest: str, reward: float) -> dict[str, Any]:
        return {"status": "measured", "reward": reward, "task_digest": task_digest}

    return {
        "pr": pr,
        "oracle_original": side(_parent(pr), original),
        "oracle_repaired": side(_variant(pr), repaired),
        "native_qualification": False,
        **extra,
    }


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _write_variants(tmp_path)
    monkeypatch.setenv(source.TS14_SUMMARY_ENV, str(tmp_path / "absent-summary.json"))
    return tmp_path


def _summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cases: list[dict[str, Any]]) -> None:
    path = tmp_path / "summary.json"
    path.write_text(json.dumps({"cases": cases}), encoding="utf-8")
    monkeypatch.setenv(source.TS14_SUMMARY_ENV, str(path))


def _ctx(repo: Path, sealed: frozenset[str] = frozenset()) -> StubContext:
    return StubContext(repo_root=repo, cloud_db=repo / "cloud.sqlite", sealed=sealed)


def _items(ctx: BankContext) -> list[IssueItem]:
    found = list(source.build(ctx))
    assert all(isinstance(item, IssueItem) for item in found)
    return [item for item in found if isinstance(item, IssueItem)]


def _ts(items: list[IssueItem]) -> dict[int, IssueItem]:
    return {
        int(item.locator): item for item in items if item.provenance.store.startswith("git:pr/")
    }


# ---- TS-9 / TS-11 -----------------------------------------------------------------


def test_sixteen_merged_repairs_without_native_receipt_are_static_only(repo: Path) -> None:
    ts = _ts(_items(_ctx(repo)))
    assert sorted(ts) == TS_PRS
    for pr, item in ts.items():
        assert (item.kind, item.polarity) == ("grader_repair", "positive")
        assert (item.granularity, item.subject_type) == ("repair_pair", "repair")
        assert item.label_type == "static" and item.label.endswith(":static_only")
        assert item.actor_transcript is None and item.harness_selector is None
        assert item.provenance.store == f"git:pr/{pr}"
        assert item.subject_id == (f"{item.task_id}@{_parent(pr)[7:23]}->{_variant(pr)[7:23]}")
    assert sum(item.label.startswith("ts9:") for item in ts.values()) == 8
    assert sum(item.label.startswith("ts11:") for item in ts.values()) == 8
    assert ts[1325].task_id == "terminal-bench/gsea-proteomics"
    assert ts[1343].task_id == "format-code-task-001426"  # MiMo family prefix dropped


def test_bound_native_receipt_makes_pair_native_partial_and_executed(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _summary(repo, monkeypatch, [_case(1325), _case(1328, repaired=0.0)])
    ts = _ts(_items(_ctx(repo)))
    assert ts[1325].label == "ts9:native_partial" and ts[1325].label_type == "executed"
    assert ts[1328].label == "ts9:native_partial (repaired reference rejected)"
    assert ts[1328].label_type == "executed"
    assert ts[1326].label == "ts9:static_only"  # no case for this PR


def test_native_receipt_for_a_different_digest_pair_does_not_count(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = _case(1325)
    stale["oracle_repaired"]["task_digest"] = _digest("another-variant")
    _summary(repo, monkeypatch, [stale])
    item = _ts(_items(_ctx(repo)))[1325]
    assert item.label == "ts9:static_only" and item.label_type == "static"
    assert "not bound" in (item.provenance.note or "")


def test_unmeasured_native_side_does_not_count(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    errored = _case(1325)
    errored["oracle_original"] = {"status": "error", "reward": None, "task_digest": _parent(1325)}
    _summary(repo, monkeypatch, [errored])
    assert _ts(_items(_ctx(repo)))[1325].label == "ts9:static_only"


def test_only_an_explicit_native_qualification_is_native_validated(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _summary(repo, monkeypatch, [_case(1325, native_qualification=True), _case(1326)])
    ts = _ts(_items(_ctx(repo)))
    assert ts[1325].label == "ts9:native_validated"
    assert ts[1326].label == "ts9:native_partial"


def test_sealed_parent_digest_is_never_emitted_as_dev(repo: Path) -> None:
    ts = _ts(_items(_ctx(repo, sealed=frozenset({_parent(1325)}))))
    assert ts[1325].declared_split == "DEV"
    assert ts[1325].split == "TEST"
    assert ts[1326].split == "DEV"


def test_declared_split_comes_from_assignments_then_the_cloud_row(repo: Path) -> None:
    _write_cloud(repo, [("format-code-task-001426", "mimo-defect-map-v1", "DEV", [])])
    ts = _ts(_items(_ctx(repo)))
    assert ts[1325].declared_split == "DEV"  # ts9 variant states dev assignments
    assert ts[1343].declared_split == "DEV"  # ts11 has none: cloud corpus row
    assert ts[1344].declared_split == "unassigned"  # ts11, not in the cloud
    assert ts[1344].split == "unassigned"  # never upgraded to DEV


# ---- cloud corpus_tasks variants --------------------------------------------------


def _cloud_variant(
    variant_id: str,
    before: str | None,
    after: str | None,
    *,
    source_name: str = "muse-r1",
    scope: str = "seeker-evaluated-package",
    qualified: bool = False,
    witness: bool = False,
    package: str | None = None,
) -> dict[str, Any]:
    found: dict[str, Any] = {
        "variant_id": variant_id,
        "source": source_name,
        "scope": scope,
        "original_grader_digest": before,
        "repaired_grader_digest": after,
        "original_package_digest": package,
        "repaired_package_digest": None,
        "admission": {"item_id": f"{variant_id}-item", "source_task_digest": package},
        "evidence": [{"purpose": "retained-native-execution-and-witness"}] if witness else [],
    }
    if qualified:
        found["confirmation_scope"] = {"repair_qualified": True}
    return found


def _write_cloud(
    repo: Path, rows: list[tuple[str, str, str, list[dict[str, Any]]] | tuple[Any, ...]]
) -> None:
    connection = sqlite3.connect(repo / "cloud.sqlite")
    connection.executescript(CLOUD_SCHEMA)
    for task_id, dataset_id, split, variants in rows:
        first = variants[0] if variants else {}
        connection.execute(
            "INSERT INTO corpus_tasks (task_id, dataset_id, split, original_grader_digest, "
            "repaired_grader_digest, metadata_json) VALUES (?,?,?,?,?,?)",
            (
                task_id,
                dataset_id,
                split,
                first.get("original_grader_digest"),
                first.get("repaired_grader_digest"),
                json.dumps({"variants": variants} if variants else {}),
            ),
        )
    connection.commit()
    connection.close()


def _cloud_items(items: list[IssueItem]) -> list[IssueItem]:
    return [item for item in items if item.provenance.store == "cloud-trace-db:corpus_tasks"]


def test_cloud_variants_need_two_different_grader_digests(repo: Path) -> None:
    g1, g2 = _digest("g1"), _digest("g2")
    _write_cloud(
        repo,
        [
            (
                "task-a",
                "tb4-audit-v1",
                "DEV",
                [
                    _cloud_variant("v-ok", g1, g2),
                    _cloud_variant("v-same", g1, g1),  # equal digests: no repair
                    _cloud_variant("v-partial", g1, None),  # no after identity
                ],
            ),
            ("task-b", "tb4-audit-v1", "DEV", []),  # no variants at all
        ],
    )
    (only,) = _cloud_items(_items(_ctx(repo)))
    assert only.locator == "v-ok"
    assert only.subject_id == f"task-a@{g1[7:23]}->{g2[7:23]}"
    assert (only.kind, only.polarity, only.task_id) == ("grader_repair", "positive", "task-a")
    assert only.provenance.ref == "task-a#v-ok"


def test_cloud_validation_status_follows_qualification_and_native_witness(repo: Path) -> None:
    g = [_digest(f"g{i}") for i in range(6)]
    _write_cloud(
        repo,
        [
            (
                "task-q",
                "mimo-defect-map-v1",
                "DEV",
                [
                    _cloud_variant(
                        "v-q", g[0], g[1], source_name="mimo30",
                        scope=source._QUALIFIED_SCOPE, qualified=True,
                    ),
                    _cloud_variant("v-w", g[2], g[3], source_name="kimi-tb4", witness=True),
                    _cloud_variant("v-s", g[4], g[5], source_name="muse-w5"),
                ],
            )
        ],
    )  # fmt: skip
    by_variant = {item.locator: item for item in _cloud_items(_items(_ctx(repo)))}
    assert by_variant["v-q"].label == "cloud-variant:mimo30:native_validated"
    assert by_variant["v-q"].label_type == "executed"
    assert by_variant["v-w"].label == "cloud-variant:kimi-tb4:native_partial"
    assert by_variant["v-w"].label_type == "executed"
    assert by_variant["v-s"].label == "cloud-variant:muse-w5:static_only"
    assert by_variant["v-s"].label_type == "static"


def test_qualified_scope_without_the_qualification_flag_is_not_validated(repo: Path) -> None:
    _write_cloud(
        repo,
        [
            (
                "task-q",
                "mimo-defect-map-v1",
                "DEV",
                [
                    _cloud_variant(
                        "v-q", _digest("a"), _digest("b"), source_name="mimo30",
                        scope=source._QUALIFIED_SCOPE, qualified=False,
                    )
                ],
            )
        ],
    )  # fmt: skip
    (item,) = _cloud_items(_items(_ctx(repo)))
    assert item.label == "cloud-variant:mimo30:static_only"


def test_cloud_variant_with_a_sealed_package_digest_is_tightened_to_test(repo: Path) -> None:
    package = _digest("sealed-package")
    _write_cloud(
        repo,
        [
            (
                "task-s",
                "tb2-to-tb21-v1",
                "DEV",
                [_cloud_variant("v-s", _digest("a"), _digest("b"), package=package)],
            )
        ],
    )
    (item,) = _cloud_items(_items(_ctx(repo, sealed=frozenset({package}))))
    assert item.declared_split == "DEV"
    assert item.split == "TEST"


def test_cloud_correspondence_matches_before_side_but_not_a_different_repair(repo: Path) -> None:
    _write_cloud(
        repo,
        [
            (
                "terminal-bench/gsea-proteomics",
                "tb4-audit-v1",
                "DEV",
                [_cloud_variant("earlier", _digest("o"), _digest("r"), package=_parent(1325))],
            ),
            (
                "unrelated",
                "tb4-audit-v1",
                "DEV",
                [_cloud_variant("same", _digest("x"), _digest("x"))],
            ),
        ],
    )
    found = source.cloud_correspondence(_ctx(repo))
    assert found["cloud_rows_with_both_digests"] == 2
    assert found["cloud_variants"] == {"both_set": 2, "differ": 1, "equal": 1}
    assert found["ts_repairs"] == 16
    assert found["ts_before_side_matches_cloud_original"] == 1
    assert found["ts_after_side_matches_cloud_repaired"] == 0


def test_missing_cloud_db_yields_only_the_other_sets(repo: Path) -> None:
    items = _items(_ctx(repo))
    assert not _cloud_items(items)
    assert len(_ts(items)) == 16


# ---- mimo-clean-v6-repaired --------------------------------------------------------

PORTS = ["task", "disposition", "old_variant_digest", "v6_parent_digest", "repaired_variant"]
RESULTS = [
    "task",
    "disposition",
    "oracle_x3",
    "nop_x3",
    "kill_replay",
    "oracle_status",
    "nop_status",
]
COMPLETE = {
    "oracle_x3": "[1.0,1.0,1.0]",
    "nop_x3": "[0.0,0.0,0.0]",
    "kill_replay": "kill-replay:pass:2/2",
    "oracle_status": "ok",
    "nop_status": "ok",
}
NOT_RUN = {
    "oracle_x3": "[]",
    "nop_x3": "[]",
    "kill_replay": "kill-replay:not-run:1/1",
    "oracle_status": "not-run",
    "nop_status": "not-run",
}


def _write_v6(repo: Path, tasks: dict[str, dict[str, str]]) -> None:
    base = repo / "research/experiments/mimo-clean-v6-repaired"
    base.mkdir(parents=True)
    with (base / "ports.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, PORTS)
        writer.writeheader()
        for task in tasks:
            writer.writerow(
                {
                    "task": task,
                    "disposition": "full",
                    "old_variant_digest": _digest(f"old-{task}"),
                    "v6_parent_digest": _digest(f"parent-{task}"),
                    "repaired_variant": _digest(f"after-{task}"),
                }
            )
    with (base / "results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, RESULTS)
        writer.writeheader()
        for task, controls in tasks.items():
            writer.writerow({"task": task, "disposition": "full", **controls})


def _v6(items: list[IssueItem]) -> dict[str, IssueItem]:
    return {
        item.task_id: item
        for item in items
        if item.task_id and item.provenance.store == "research-experiments:mimo-clean-v6-repaired"
    }


def test_v6_ports_are_native_validated_only_with_complete_controls(repo: Path) -> None:
    _write_v6(
        repo,
        {
            "format-code-task-000001": COMPLETE,
            "format-code-task-000002": NOT_RUN,
            "format-code-task-000003": {**COMPLETE, "kill_replay": "kill-replay:not-run:2/2"},
            "format-code-task-000004": {**COMPLETE, "nop_x3": "[0.0,1.0,0.0]"},
        },
    )
    found = _v6(_items(_ctx(repo)))
    assert found["format-code-task-000001"].label == "mimo-clean-v6:native_validated"
    assert found["format-code-task-000001"].label_type == "executed"
    for task in ("format-code-task-000002", "format-code-task-000003", "format-code-task-000004"):
        assert found[task].label == "mimo-clean-v6:static_only"
        assert found[task].label_type == "static"
    assert found["format-code-task-000001"].subject_id == (
        f"format-code-task-000001@{_digest('parent-format-code-task-000001')[7:23]}"
        f"->{_digest('after-format-code-task-000001')[7:23]}"
    )


def test_v6_split_is_never_upgraded_to_dev_without_an_assignment(repo: Path) -> None:
    _write_v6(repo, {"format-code-task-000001": COMPLETE, "format-code-task-000002": COMPLETE})
    _write_cloud(repo, [("format-code-task-000001", "mimo-defect-map-v1", "DEV", [])])
    found = _v6(_items(_ctx(repo)))
    assert found["format-code-task-000001"].split == "DEV"
    assert found["format-code-task-000002"].declared_split == "unassigned"
    assert found["format-code-task-000002"].split == "unassigned"


def test_v6_sealed_parent_digest_is_tightened_to_test(repo: Path) -> None:
    _write_v6(repo, {"format-code-task-000001": COMPLETE})
    _write_cloud(repo, [("format-code-task-000001", "mimo-defect-map-v1", "DEV", [])])
    sealed = frozenset({_digest("parent-format-code-task-000001")})
    assert _v6(_items(_ctx(repo, sealed)))["format-code-task-000001"].split == "TEST"


def test_ids_are_unique_and_deterministic(repo: Path) -> None:
    _write_v6(repo, {"format-code-task-000001": COMPLETE})
    first = [item.item_id for item in _items(_ctx(repo))]
    second = [item.item_id for item in _items(_ctx(repo))]
    assert first == second
    assert len(set(first)) == len(first) == 17


def test_skipped_sets_name_every_unitemized_experiment() -> None:
    assert set(source.SKIPPED_SETS) == {
        "tb21-test-repairs-20261019",
        "envcheck-repair-2026-10-15",
        "har177-repair",
        "test-repair-performance-20261017",
    }


def test_survey_groups_by_set_status_and_split(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _summary(repo, monkeypatch, [_case(pr) for pr in (1325, 1326, 1343)])
    counts = source.survey(_ctx(repo))["by_set_status_split"]
    assert counts["ts9|native_partial|DEV"] == 2
    assert counts["ts9|static_only|DEV"] == 6
    assert counts["ts11|native_partial|unassigned"] == 1
    assert counts["ts11|static_only|unassigned"] == 7
