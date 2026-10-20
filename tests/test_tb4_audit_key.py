"""Task-level holdout, source integrity and answer-label confidentiality."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

from evallab.registry import task_directory_digest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "tb4_audit_views", ROOT / "research/answer-keys/tb4-audit-v1/views.py"
)
assert _spec is not None and _spec.loader is not None
views = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(views)

SECRET = "SEALED_ANSWER_LABEL_NOT_AN_AUDITOR_INPUT"


def _task(root: Path) -> tuple[dict, Path]:
    upstream = root / "upstream"
    package = upstream / "square"
    for directory in (
        package,
        package / "tests",
        package / "solution",
        package / "environment",
        package / "cheat",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    (package / "instruction.md").write_text("Implement square(x) for integer inputs.\n")
    (package / "task.toml").write_text('[task]\nname = "terminal-bench/square"\n')
    (package / "solution/solve.py").write_text("def square(x):\n    return x * x\n")
    (package / "tests/test_square.py").write_text("assert square(-3) == 9\n")
    (package / "environment/Dockerfile").write_text("FROM python:3.12-slim\n")
    (package / "README.md").write_text(SECRET + "\n")
    (package / "cheat/review.txt").write_text(SECRET + "\n")
    row = {
        "split": "test",
        "scope": "task",
        "subject": {
            "kind": "task",
            "task_id": "terminal-bench/square",
            "task_digest": task_directory_digest(package),
        },
        "package": {"name": "square", "pin": "a" * 40},
        "title": SECRET,
        "port_tests": [{"nodeid": SECRET}],
        "expected_verdict": "fail",
        "arbitrary_future_label_field": {"label": SECRET},
    }
    return row, upstream


def test_holdout_is_order_independent_and_disjoint_at_task_level() -> None:
    cohort = [f"task-{index:03}" for index in range(66)]
    assignments = views.task_splits(cohort)
    assert assignments == views.task_splits(reversed(cohort))
    dev = {task for task, split in assignments.items() if split == "dev"}
    test = {task for task, split in assignments.items() if split == "test"}
    assert dev.isdisjoint(test)
    assert dev | test == set(cohort)
    assert (len(dev), len(test)) == (25, 41)
    with pytest.raises(ValueError, match="unique"):
        views.task_splits(cohort + [cohort[0]])


def test_auditor_sees_original_task_surfaces_but_no_answer_labels(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    row, upstream = _task(root)
    outside = root / "scorer-items.jsonl"
    outside.write_text(SECRET)
    taxonomy = {
        "version": 1,
        "label_rules": ["Unsupported class evidence remains suspected."],
        "decision_order": [{"q": "Is a required behavior not checked?", "class": "weak_check"}],
        "task_side": [
            {
                "id": "TS01",
                "key": "weak_check",
                "definition": "A requirement is checked too weakly.",
                "examples": [{"task": "square", "label": SECRET}],
                "pm_count": SECRET,
            }
        ],
        "run_side": [],
        "private_findings": [SECRET],
    }
    destination = root / "actor"
    views.materialize_agent_view(
        [row, row | {"title": SECRET + "_SECOND"}],
        destination,
        split="test",
        task_root=upstream,
        taxonomy=taxonomy,
    )
    tools = views.agent_tools(destination)
    assert "return x * x" in tools.run("read", {"path": "tasks/square/solution/solve.py"})
    assert "A requirement is checked too weakly" in tools.run("read", {"path": "taxonomy.json"})
    assert tools.run("grep", {"path": ".", "pattern": SECRET}).startswith("[no matches")
    inputs = [json.loads(line) for line in (destination / "inputs.jsonl").read_text().splitlines()]
    assert [item["subject"]["task_id"] for item in inputs] == ["terminal-bench/square"]
    for path in (
        str(outside),
        "../scorer-items.jsonl",
        "items.jsonl",
        "bundle.json",
        "tasks/square/README.md",
        "tasks/square/cheat/review.txt",
    ):
        with pytest.raises(ValueError):
            tools.run("read", {"path": path})
    for tool in ("bash", "python", "write", "http"):
        with pytest.raises(ValueError):
            tools.run(tool, {})
    with pytest.raises(FileExistsError):
        views.materialize_agent_view([row], destination, split="test", task_root=upstream)


def test_changed_package_cannot_be_bound_to_an_old_answer_key(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    row, upstream = _task(root)
    (upstream / "square/instruction.md").write_text("Implement cube(x), not square(x).\n")
    destination = root / "actor"
    with pytest.raises(ValueError, match="package digest"):
        views.materialize_agent_view([row], destination, split="test", task_root=upstream)
    assert not (destination / "bundle.json").exists()


def test_symlinked_source_and_changed_exports_cannot_cross_the_read_boundary(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    row, upstream = _task(root)
    private = root / "private-label.json"
    private.write_text(SECRET)
    link = upstream / "square/tests/private-label.json"
    link.symlink_to(private)
    with pytest.raises(ValueError, match="symlink"):
        views.materialize_agent_view([row], root / "rejected", split="test", task_root=upstream)
    link.unlink()
    destination = root / "actor"
    views.materialize_agent_view([row], destination, split="test", task_root=upstream)
    changed = destination / "tasks/square/instruction.md"
    changed.chmod(0o600)
    changed.write_text("Mutated after the frozen export.\n")
    with pytest.raises(ValueError, match="changed after export"):
        views.agent_tools(destination)


KEY = ROOT / "research/answer-keys/tb4-audit-v1"


def _build_module():
    sys.path.insert(0, str(KEY))
    try:
        spec = importlib.util.spec_from_file_location("tb4_audit_build", KEY / "build.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(KEY))
    return module


build = _build_module()
SOURCE = json.loads((KEY / "source.json").read_text(encoding="utf-8"))
REGISTRY = json.loads((KEY / "witnesses.json").read_text(encoding="utf-8"))


def _confirmed_registry(root: Path) -> dict:
    """One registered witness, copied to a scratch root and marked as confirmed."""
    witness = copy.deepcopy(REGISTRY["witnesses"][0])
    shutil.copytree(KEY / witness["solution_dir"], root / witness["solution_dir"])
    witness |= {
        "upstream_replay": {"reward": 1.0, "cost_usd": 0.02},
        "port_replay": witness["port_replay"] | {"reward": 0.0},
        "confirmed": True,
    }
    return {"schema": REGISTRY["schema"], "witnesses": [witness]}


def test_registered_witnesses_are_dev_only_and_digest_bound() -> None:
    assert REGISTRY["schema"] == "tb4.audit.witnesses/v1"
    assert len(REGISTRY["witnesses"]) == 10
    for witness in REGISTRY["witnesses"]:
        assert witness["split"] == "dev"
        assert (KEY / witness["solution_dir"] / "solve.sh").is_file()
    build.build_outputs(SOURCE)


def test_confirmed_witness_is_attached_to_its_item(tmp_path: Path) -> None:
    registry = _confirmed_registry(tmp_path)
    witness = registry["witnesses"][0]
    outputs = build.build_outputs(SOURCE, registry, witness_root=tmp_path)
    items = {row["item_id"]: row for row in map(json.loads, outputs["items.jsonl"].splitlines())}
    item = items[witness["finding_id"]]
    assert item["label_strength"] == "confirmed"
    assert item["reproducer"] == {
        "kind": "command",
        "path": f"research/answer-keys/tb4-audit-v1/{witness['solution_dir']}/solve.sh",
        "expected_reward": 1.0,
    }
    assert item["witness"]["witness_id"] == witness["witness_id"]
    summary = json.loads(outputs["summary.json"])
    assert summary["witness_replay"]["confirmed"] == 1
    assert summary["native_reproductions_performed"] == 1
    assert summary["paid_cost_usd"] == 0.02
    assert summary["witness_replay"]["port_replay_modes"] == {"full": 1}


GT2_RESULTS = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-10-20/tb4-audit-gt2")


def test_registered_port_replays_are_full_mode_with_custody() -> None:
    for witness in REGISTRY["witnesses"]:
        port = witness["port_replay"]
        assert port["mode"] == "full", witness["witness_id"]
        assert port["reward"] == 0.0
        for field in build.PORT_CUSTODY_FIELDS:
            assert build._is_sha256(port[field]), (witness["witness_id"], field)
        assert port["submitted_artifact_files_sha256"]
        assert all(map(build._is_sha256, port["submitted_artifact_files_sha256"].values()))


@pytest.mark.skipif(not GT2_RESULTS.is_dir(), reason="GT-2 result bundle not on this machine")
def test_registered_port_receipts_match_the_result_bundle() -> None:
    for witness in REGISTRY["witnesses"]:
        wid = witness["witness_id"]
        port = witness["port_replay"]
        receipt_path = GT2_RESULTS / "port" / f"{wid}.score.json"
        digest = "sha256:" + hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        assert digest == port["receipt_sha256"], wid
        receipt = json.loads(receipt_path.read_text())
        custody = receipt["custody"]
        assert receipt["mode"] == "full" and receipt["score"] == 0.0
        assert custody["registered_witness_files_sha256"] == witness["files"]
        assert "sha256:" + custody["port_scoring_script_sha256"] == port["port_scoring_script_sha256"]
        assert {
            k: "sha256:" + v for k, v in custody["submitted_artifact_files_sha256"].items()
        } == port["submitted_artifact_files_sha256"]
        for stream in ("stdout", "stderr"):
            log = custody[f"raw_{stream}_log"]
            data = (GT2_RESULTS / "port" / log["path"]).read_bytes()
            assert len(data) == log["bytes"]
            assert hashlib.sha256(data).hexdigest() == log["sha256"]
        assert "sha256:" + custody["raw_stdout_log"]["sha256"] == port["raw_stdout_log_sha256"]
        if witness["task"] == "html-js-filter":
            assert receipt["corpus"] == {
                "corpus_vectors": 417,
                "embedded_vectors": 27,
                "clean_samples": 12,
            }
    closure = json.loads((GT2_RESULTS / "modal-closure-verification.json").read_text())
    assert closure["sandboxes_terminal"] == closure["sandboxes_checked"] == 21
    assert closure["active_tasks"] == 0 and closure["running_containers"] == 0
    assert closure["closed"] is True


def test_single_cap_accounting_reaches_the_summary() -> None:
    summary = json.loads(build.build_outputs(SOURCE)["summary.json"])
    spend = summary["witness_replay"]["spend"]
    assert spend["single_cap_strict_peak_qualified_witnesses"] == 9
    assert spend["peak_reservation_usd"] == 1.434147 <= spend["cap_usd"] == 1.5
    assert spend["strict_finalized_usd"] == 0.649567
    assert spend["all_confirmed_witnesses"] == 10
    assert spend["finalized_usd"] == 0.786752
    assert spend["combined_incl_aborted_elapsed_usd"] == 0.922084
    assert spend["actual_task_sandbox_runtime_excl_cached_builder_usd"] == 0.1862
    assert spend["aborted_sandboxes_affirmatively_closed"] is True
    assert summary["witness_replay"]["port_replay_modes"] == {"full": 10}


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "embedded-vectors-only (corpus_vectors=lambda: [])"},
        {"receipt_sha256": None},
        {"port_scoring_script_sha256": "sha256:not-a-digest"},
        {"raw_stdout_log_sha256": None},
        {"submitted_artifact_files_sha256": {}},
    ],
)
def test_confirmed_port_replay_needs_full_mode_custody(tmp_path: Path, change: dict) -> None:
    registry = _confirmed_registry(tmp_path)
    registry["witnesses"][0]["port_replay"] |= change
    with pytest.raises(ValueError, match="full-mode|custody|artifact"):
        build.build_outputs(SOURCE, registry, witness_root=tmp_path)


def test_witness_on_a_test_split_task_is_rejected(tmp_path: Path) -> None:
    registry = _confirmed_registry(tmp_path)
    tasks = [json.loads(line) for line in (KEY / "tasks.jsonl").read_text().splitlines()]
    held_out = next(row["task"] for row in tasks if row["split"] == "test")
    finding = next(row for row in SOURCE["findings"] if row["task"] == held_out)
    registry["witnesses"][0] |= {"task": held_out, "finding_id": finding["finding_id"]}
    with pytest.raises(ValueError, match="dev-split"):
        build.build_outputs(SOURCE, registry, witness_root=tmp_path)
    registry["witnesses"][0]["finding_id"] = "pm-tb4:unknown:0000000000"
    with pytest.raises(ValueError, match="unknown finding"):
        build.build_outputs(SOURCE, registry, witness_root=tmp_path)


def test_tampered_witness_files_are_rejected(tmp_path: Path) -> None:
    registry = _confirmed_registry(tmp_path)
    solve = tmp_path / registry["witnesses"][0]["solution_dir"] / "solve.sh"
    solve.write_text(solve.read_text() + "echo tampered\n")
    with pytest.raises(ValueError, match="sha256"):
        build.build_outputs(SOURCE, registry, witness_root=tmp_path)


@pytest.mark.parametrize(("upstream", "port"), [(0.0, 0.0), (1.0, 1.0), (None, 0.0), (1.0, None)])
def test_unconfirmed_rewards_cannot_be_registered_as_confirmed(
    tmp_path: Path, upstream: float | None, port: float | None
) -> None:
    registry = _confirmed_registry(tmp_path)
    registry["witnesses"][0] |= {
        "upstream_replay": {"reward": upstream},
        "port_replay": {"reward": port},
    }
    with pytest.raises(ValueError, match="upstream reward 1.0"):
        build.build_outputs(SOURCE, registry, witness_root=tmp_path)


def test_witness_files_never_enter_the_auditor_view(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    row, upstream = _task(root)
    package = upstream / "square"
    (package / "tests/witnesses.json").write_text(SECRET + "\n")
    (package / "solution/witnesses").mkdir()
    (package / "solution/witnesses/solve.sh").write_text(SECRET + "\n")
    row["subject"]["task_digest"] = task_directory_digest(package)
    destination = root / "actor"
    manifest = views.materialize_agent_view([row], destination, split="test", task_root=upstream)
    assert not any("witnesses" in name for name in manifest["files"])
    tools = views.agent_tools(destination)
    assert tools.run("grep", {"path": ".", "pattern": SECRET}).startswith("[no matches")
    for path in ("tasks/square/tests/witnesses.json", "tasks/square/solution/witnesses/solve.sh"):
        with pytest.raises(ValueError):
            tools.run("read", {"path": path})

    # A view whose manifest was edited to list a witness file is refused outright.
    forged = root / "forged"
    views.materialize_agent_view([row], forged, split="test", task_root=upstream)
    (forged / "witnesses.json").write_text(SECRET)
    bundle = forged / "bundle.json"
    manifest = json.loads(bundle.read_text())
    manifest["files"]["witnesses.json"] = views.hashlib.sha256(SECRET.encode()).hexdigest()
    bundle.chmod(0o600)
    bundle.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="scorer-only"):
        views.agent_tools(forged)


def _rewrite(view: Path, relative: str, data: bytes, *, listed: bool = True) -> None:
    """Forge a view file and, if listed, rehash it into bundle.json."""
    target = view / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.chmod(0o600)
    target.write_bytes(data)
    if listed:
        bundle = view / "bundle.json"
        manifest = json.loads(bundle.read_text())
        manifest["files"][relative] = views.hashlib.sha256(data).hexdigest()
        bundle.chmod(0o600)
        bundle.write_text(json.dumps(manifest))


def _export(root: Path, name: str = "actor", taxonomy: dict | None = None) -> Path:
    row, upstream = _task(root)
    destination = root / name
    views.materialize_agent_view(
        [row], destination, split="test", task_root=upstream, taxonomy=taxonomy
    )
    return destination


def test_files_outside_the_package_digest_are_not_exported(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    row, upstream = _task(root)
    package = upstream / "square"
    (package / "tests/scratch.tmp").write_text(SECRET)
    (package / "solution/__pycache__").mkdir()
    (package / "solution/__pycache__/solve.cpython-312.pyc").write_bytes(SECRET.encode())
    (package / "environment/.DS_Store").write_text(SECRET)
    assert task_directory_digest(package) == row["subject"]["task_digest"]
    destination = root / "actor"
    manifest = views.materialize_agent_view([row], destination, split="test", task_root=upstream)
    assert not any(n.endswith((".tmp", ".pyc", ".DS_Store")) for n in manifest["files"])
    assert (
        views.agent_tools(destination)
        .run("grep", {"path": ".", "pattern": SECRET})
        .startswith("[no matches")
    )
    _rewrite(destination, "tasks/square/tests/scratch.tmp", SECRET.encode())
    with pytest.raises(ValueError, match="non-input"):
        views.agent_tools(destination)


@pytest.mark.parametrize(
    ("relative", "listed"),
    [
        ("answers.json", True),
        ("tasks/square/README.md", True),
        ("tasks/square/cheat/review.txt", True),
        ("tasks/other/instruction.md", True),
        ("notes.txt", False),
        ("tasks/square/tests/sidecar.py", False),
    ],
)
def test_unlisted_sidecars_and_extra_metadata_are_rejected(
    tmp_path: Path, relative: str, listed: bool
) -> None:
    destination = _export(tmp_path.resolve())
    _rewrite(destination, relative, SECRET.encode(), listed=listed)
    with pytest.raises(ValueError):
        views.agent_tools(destination)


@pytest.mark.parametrize(
    "manifest_patch",
    [{"split": "train"}, {"key_id": "other"}, {"schema": "other"}, {"task_count": 2}],
)
def test_manifest_semantics_are_checked(tmp_path: Path, manifest_patch: dict) -> None:
    destination = _export(tmp_path.resolve())
    bundle = destination / "bundle.json"
    manifest = json.loads(bundle.read_text()) | manifest_patch
    bundle.chmod(0o600)
    bundle.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        views.agent_tools(destination)


def test_rehashed_inputs_and_taxonomy_with_labels_are_rejected(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    taxonomy = {
        "version": 1,
        "label_rules": [],
        "decision_order": [],
        "task_side": [{"id": "TS01", "key": "weak_check", "examples": [SECRET]}],
        "run_side": [],
    }
    clean = _export(root / "clean", taxonomy=taxonomy)
    views.agent_tools(clean)
    record = json.loads((clean / "inputs.jsonl").read_text())
    assert set(record) == {"schema", "key_id", "split", "subject", "package"}

    forged_inputs = [
        record | {"title": SECRET},
        record | {"split": "dev"},
        record | {"package": record["package"] | {"label": SECRET}},
        record | {"subject": record["subject"] | {"task_digest": "sha256:bad"}},
        record | {"subject": record["subject"] | {"task_id": "terminal-bench/other"}},
    ]
    for index, forged in enumerate(forged_inputs):
        view = _export(root / f"inputs-{index}", taxonomy=taxonomy)
        _rewrite(view, "inputs.jsonl", (json.dumps(forged) + "\n").encode())
        with pytest.raises(ValueError, match="input"):
            views.agent_tools(view)

    public = json.loads((clean / "taxonomy.json").read_text())
    for index, forged in enumerate(
        [
            public | {"private_findings": [SECRET]},
            public | {"task_side": [public["task_side"][0] | {"examples": [SECRET]}]},
            public | {"run_side": [{"id": "RS01", "finding_id": SECRET}]},
        ]
    ):
        view = _export(root / f"taxonomy-{index}", taxonomy=taxonomy)
        _rewrite(view, "taxonomy.json", json.dumps(forged).encode())
        with pytest.raises(ValueError, match="taxonomy"):
            views.agent_tools(view)


def test_postload_mutation_is_rejected_on_the_next_call(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    destination = _export(root)
    tools = views.agent_tools(destination)
    path = {"path": "tasks/square/instruction.md"}
    assert "square" in tools.run("read", path)

    changed = destination / "tasks/square/instruction.md"
    changed.chmod(0o600)
    changed.write_text(SECRET + "\n")
    with pytest.raises(ValueError, match="changed after export"):
        tools.run("read", path)

    # Rehashing the manifest after load does not re-legitimise the bytes.
    other = _export(root, "other")
    tools = views.agent_tools(other)
    _rewrite(other, "tasks/square/instruction.md", SECRET.encode())
    with pytest.raises(ValueError, match="changed after export"):
        tools.run("read", path)

    third = _export(root, "third")
    tools = views.agent_tools(third)
    (third / "sidecar.txt").write_text(SECRET)
    with pytest.raises(ValueError, match="not listed"):
        tools.run("grep", {"path": ".", "pattern": "square"})


def test_pr_comment_links_are_not_concrete_issue_reports() -> None:
    sys.path.insert(0, str(KEY))
    try:
        spec = importlib.util.spec_from_file_location(
            "tb4_audit_sources", KEY / "auditor_sources.py"
        )
        assert spec is not None and spec.loader is not None
        sources = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sources)
    finally:
        sys.path.remove(str(KEY))
    base = "https://github.com/harbor-framework/terminal-bench"
    row = {
        "github_issue_numbers": [1972, 1645],
        "github_refs": [
            {
                "number": 1972,
                "where": "sources",
                "kind": "issue",
                "href": f"{base}/issues/1972#issuecomment-123",
            },
            {
                "number": 1980,
                "where": "sources",
                "kind": "issue",
                "href": f"{base}/pull/1980#discussion_r9",
            },
            {
                "number": 1981,
                "where": "upstream",
                "kind": "issue",
                "href": f"{base}/pull/1981#pullrequestreview-7",
            },
            {"number": 1645, "where": "sources", "kind": "issue", "href": f"{base}/issues/1645"},
        ],
    }
    assert sources._finding_issue_refs(row) == ({1645}, set())
    for finding in SOURCE["findings"]:
        if any(
            r.get("kind") == "comment_on_pr" and r["number"] == 1972 for r in finding["github_refs"]
        ):
            assert 1972 not in finding["auditor_evidence"]["github_issue_numbers_concrete"]
    items = [json.loads(line) for line in (KEY / "items.jsonl").read_text().splitlines()]
    assert not any(1972 in i["auditor_evidence"]["github_issue_numbers_concrete"] for i in items)


def test_exposure_stratification_partitions_tasks_and_findings() -> None:
    outputs = build.build_outputs(SOURCE)
    assert outputs["summary.json"] == (KEY / "summary.json").read_text()
    summary = json.loads(outputs["summary.json"])["exposure_stratification"]
    tasks = [json.loads(line) for line in outputs["tasks.jsonl"].splitlines()]
    items = [json.loads(line) for line in outputs["items.jsonl"].splitlines()]
    strata = {"unexposed", "inspected_only", "tuned"}
    assert set(summary["tasks"]) == set(summary["task_findings"]) == {"dev", "test"}
    assert sum(sum(v.values()) for v in summary["tasks"].values()) == len(tasks) == 66
    assert sum(sum(v.values()) for v in summary["task_findings"].values()) == 238
    by_task = {row["task"]: row for row in tasks}
    for row in tasks:
        assert row["exposure_stratum"] in strata
        assert (row["exposure_stratum"] == "unexposed") == (not row["historical_exposures"])
        assert summary["tasks"][row["split"]][row["exposure_stratum"]] > 0
    for item in items:
        if item["scope"] == "task":
            task = by_task[item["task"]]
            assert item["historical_exposures"] == task["historical_exposures"]
            assert item["exposure_stratum"] == task["exposure_stratum"]
        else:
            assert (item["historical_exposures"], item["exposure_stratum"]) == ([], "unexposed")
    # The S2 prompt was hardened on these four dev positives: tuned, not held out.
    for name in (
        "interleaved-vigenere",
        "wal-recovery-ordering",
        "data-anonymization",
        "atrx-vep-crispr",
    ):
        assert "qa-task-s2-prompt-dev" in by_task[name]["historical_exposures"]
        assert by_task[name]["exposure_stratum"] == "tuned"
