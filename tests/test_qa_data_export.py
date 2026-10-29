"""Blinded QA exports are all-or-nothing on admission, with original-step evidence joins."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from evallab.cheatbench_fidelity import ANNOTATION_MARKER
from evallab.cli import parser as cli_parser
from evallab.production import qa_data_export
from evallab.production.cloud_trace_db import CloudTraceDatabase, CloudTraceRetriever
from evallab.production.corpus_queue import CorpusRegistry
from evallab.production.qa_data import UNAVAILABLE, Budget, QaContext, QaDataError, estimate_tokens
from evallab.production.qa_data_export import export, steps

DATASET = "mimo-defect-map-v1"
TASKS = {
    "synthetic/alpha": ("alpha-item", "dev"),
    "synthetic/beta": ("beta-item", "dev"),
    "synthetic/test-only": ("test-item", "test"),
    "synthetic/sealed": ("sealed-item", "dev"),
    "synthetic/locked": ("locked-item", "dev"),
    "synthetic/unassigned": ("unassigned-item", "dev"),
}
RUNS = {"trace-a": ("synthetic/alpha", 1), "trace-z": ("synthetic/alpha", 3), "trace-b": ("synthetic/beta", 2)}


class SyntheticRegistry(CorpusRegistry):
    def __init__(self) -> None:
        self._assignments = {
            (DATASET, item): split for item, split in TASKS.values() if item != "unassigned-item"
        }
        self._assignments[("other-key", "locked-item")] = "test"
        authority = json.dumps(sorted(self._assignments.items())).encode()
        self.split_assignments_digest = "sha256:" + hashlib.sha256(authority).hexdigest()
        self._assignment_keys = frozenset(key for key, _item in self._assignments)
        self._assignment_names = {}
        self._assigned_test_ids = {"test-item", "locked-item"}
        self._assigned_test_names = set()
        self._sealed_names = set()
        self._sealed_digests = set()
        self._sealed_ids = {"sealed-item"}


def _trace(database: CloudTraceDatabase, trace_id: str, task_id: str) -> None:
    database.upsert_trace({
        "trace_id": trace_id, "run_id": "batch", "task_id": task_id, "model_id": "private-model",
        "reward": 0.375, "trust_tier": "tier1_container_verified",
    }, steps=[
        {"step_index": 2, "role": "assistant", "content_text": "Inspect the parser."},
        {"step_index": 5, "role": "user", "content_text": ANNOTATION_MARKER + "] scoring only"},
        {"step_index": 9, "role": "tool", "content_text": "The parser returned a value."},
        {"step_index": 13, "role": "assistant", "content_text": "Report the value."},
        {"step_index": 99, "role": "user", "content_text": ANNOTATION_MARKER + "] scoring only"},
    ])


def _facet(trace_id: str, suspicion: int) -> dict[str, Any]:
    return {
        "run_id": trace_id, "source": DATASET, "split": "DEV", "facet_version": "run-facets/v1",
        "facet_model": "synthetic/model", "input_tokens": 10, "output_tokens": 5,
        "goal": "Inspect a parser", "approach": "Read its output", "outcome": "pass", "tests": "read",
        "grader_or_harness_touched": False, "answer_material_accessed": True,
        "success_claim_without_evidence": False, "eval_awareness": "none", "shortcut": "none",
        "suspicion": suspicion, "evidence_steps": [9, 5, 99, 2],
    }


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[QaContext]:
    path = tmp_path / "cloud.sqlite"
    registry = SyntheticRegistry()
    with CloudTraceDatabase(path, sqlite_fts=False) as database:
        for task_id, (item, split) in TASKS.items():
            database.upsert_task({
                "task_id": task_id, "dataset_id": DATASET, "split": split, "metadata_json": {"item_id": item},
                "instruction_text": "Inspect the parser and report its output.",
            })
        for trace_id, (task_id, _suspicion) in RUNS.items():
            _trace(database, trace_id, task_id)
        _trace(database, "trace-unfaceted", "synthetic/alpha")
        for kind in ("test", "sealed", "locked", "unassigned"):
            _trace(database, "trace-" + kind, "synthetic/test-only" if kind == "test" else "synthetic/" + kind)
        assert database.upsert_run_facets(
            [_facet(trace, suspicion) for trace, (_task, suspicion) in RUNS.items()], registry=registry,
        )["stored"] == 3
    with CloudTraceDatabase(path, read_only=True) as database:
        yield QaContext(
            database=database, retriever=CloudTraceRetriever(database, registry=registry),
            repo_root=tmp_path, state_dir=tmp_path, budget=Budget(4000), database_path=str(path),
        )


def _export(ctx: QaContext, out: Path, **changes: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "facets": {"answer_material_accessed": True}, "trace_ids": [], "ids_file": None,
        "limit": None, "format": "atif", "out": out, **changes,
    }
    return export(ctx, **options)


def _manifest(out: Path) -> Path:
    return out.with_name(out.name + ".sealed-manifest.json")


def test_facets_export_most_suspicious_first_with_original_and_blinded_evidence(ctx: QaContext, tmp_path: Path) -> None:
    out = tmp_path / "ordered"
    before = Path(ctx.database_path or "").read_bytes()
    data = _export(ctx, out, limit=2)
    assert [run["trace_id"] for run in data["runs"]] == ["trace-z", "trace-b"]
    # admitted counts every facet match, so the run the limit left out stays visible.
    assert (data["receipt"]["admitted"], data["receipt"]["exported"], data["receipt"]["refused"]) == (3, 2, 0)
    assert data["receipt"]["format"] == "atif"
    assert data["receipt"]["manifest_sha256"] == hashlib.sha256(_manifest(out).read_bytes()).hexdigest()
    assert data["output_directory"] == str(out) and data["sealed_manifest"] == str(_manifest(out))
    manifest = json.loads(_manifest(out).read_text())
    for run in data["runs"]:
        entry = manifest[run["blind_id"]]
        assert entry["trace_id"] == run["trace_id"]
        assert entry["step_origin"] == [2, 9, 13]
        assert run["actor_inputs_available"] is True
        assert run["evidence_steps_original"] == [2, 5, 9, 99]
        assert run["evidence_steps_blinded"] == [1, None, 2, None]
        document = json.loads(Path(run["file"]).read_text())
        assert run["steps"] == len(document["steps"]) == len(entry["step_origin"]) == 3
        assert [step["step_id"] for step in document["steps"]] == [1, 2, 3]
    assert "reward" not in json.dumps(data) and "0.375" not in json.dumps(data)
    assert not data["truncated"] and data["omitted"] == 0
    assert Path(ctx.database_path or "").read_bytes() == before
    assert estimate_tokens(data) <= ctx.budget.used <= ctx.budget.limit


def test_a_limit_smaller_than_the_facet_match_count_still_reports_every_match(
    ctx: QaContext, tmp_path: Path,
) -> None:
    """A limited facet export names the matched runs it did not publish, so none vanish silently."""
    out = tmp_path / "limited-selection"
    data = _export(ctx, out, limit=1)
    assert (data["receipt"]["admitted"], data["receipt"]["exported"], data["receipt"]["refused"]) == (3, 1, 0)
    assert len(data["runs"]) == 1 and len(json.loads(_manifest(out).read_text())) == 1
    # The selection count is a pre-limit count, not a reply-budget omission.
    assert not data["truncated"] and data["omitted"] == 0


def test_explicit_ids_merge_file_comments_and_duplicates_and_preserve_requested_order(ctx: QaContext, tmp_path: Path) -> None:
    ids = tmp_path / "ids.txt"
    ids.write_text("# saved selection\n\n trace-a \ntrace-unfaceted\ntrace-z\n  # comment\n")
    data = _export(ctx, tmp_path / "explicit", facets={}, trace_ids=["trace-z", "trace-z"], ids_file=ids)
    assert [run["trace_id"] for run in data["runs"]] == ["trace-z", "trace-a", "trace-unfaceted"]
    assert data["receipt"]["exported"] == 3
    assert data["runs"][-1]["evidence_steps_original"] == data["runs"][-1]["evidence_steps_blinded"] == []


@pytest.mark.parametrize("bad", ("trace-test", "trace-sealed", "trace-locked", "trace-unassigned", "trace-missing"))
@pytest.mark.parametrize("from_file", (False, True))
def test_one_bad_explicit_id_refuses_everything_even_beyond_limit(
    ctx: QaContext, tmp_path: Path, bad: str, from_file: bool,
) -> None:
    out = tmp_path / "new-parent" / "refused"
    ids = tmp_path / "ids.txt"
    ids.write_text(bad + "\n")
    with pytest.raises(QaDataError) as raised:
        _export(
            ctx, out, facets={}, trace_ids=["trace-z"] if from_file else ["trace-z", bad],
            ids_file=ids if from_file else None, limit=1,
        )
    assert str(raised.value) == UNAVAILABLE
    assert not out.parent.exists() and not _manifest(out).exists()


@pytest.mark.parametrize("occupied", ("directory", "file", "manifest", "output-link", "manifest-link"))
def test_output_and_manifest_must_be_new_and_existing_entries_are_untouched(
    ctx: QaContext, tmp_path: Path, occupied: str,
) -> None:
    out = tmp_path / "destination"
    target = _manifest(out) if occupied.startswith("manifest") else out
    if occupied == "directory":
        target.mkdir()
        (target / "keep").write_bytes(b"unchanged")
    elif occupied.endswith("link"):
        target.symlink_to(tmp_path / "missing-target")
    else:
        target.write_bytes(b"unchanged")

    def mutation_state() -> dict[str, tuple[int, ...]]:
        # Read-only admission and symlink probes may change atime, not mutation metadata.
        snapshots = {}
        for path in tmp_path.iterdir():
            info = path.lstat()
            snapshots[path.name] = (
                info.st_mode, info.st_ino, info.st_dev, info.st_nlink,
                info.st_uid, info.st_gid, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            )
        return snapshots

    before = mutation_state()
    with pytest.raises(QaDataError, match="must both be new"):
        _export(ctx, out)
    assert mutation_state() == before
    if occupied == "directory":
        assert (target / "keep").read_bytes() == b"unchanged"
    elif occupied.endswith("link"):
        assert target.is_symlink() and target.readlink() == tmp_path / "missing-target"
    else:
        assert target.read_bytes() == b"unchanged"


@pytest.mark.parametrize("selection", ("neither", "both-ids", "both-file"))
def test_selection_is_exactly_one_of_facets_or_explicit_ids(ctx: QaContext, tmp_path: Path, selection: str) -> None:
    ids = tmp_path / "empty-ids.txt"
    ids.write_text("")
    with pytest.raises(QaDataError, match="either --facet"):
        _export(
            ctx, tmp_path / "invalid", facets={} if selection == "neither" else {"suspicion": 3},
            trace_ids=["trace-z"] if selection == "both-ids" else [],
            ids_file=ids if selection == "both-file" else None,
        )
    assert not (tmp_path / "invalid").exists()


@pytest.mark.parametrize("limit", (-1, 0, 201, True, 1.5))
def test_limit_must_be_a_positive_integer_at_most_200(ctx: QaContext, tmp_path: Path, limit: Any) -> None:
    with pytest.raises(QaDataError, match="between 1 and 200"):
        _export(ctx, tmp_path / "invalid", limit=limit)
    assert not (tmp_path / "invalid").exists()


@pytest.mark.parametrize("by_facets", (False, True))
def test_more_than_200_selected_runs_requires_a_limit(ctx: QaContext, tmp_path: Path, by_facets: bool) -> None:
    ids = ["large-" + str(index) for index in range(201)]
    with CloudTraceDatabase(ctx.database_path, sqlite_fts=False) as database:
        for trace in ids:
            _trace(database, trace, "synthetic/alpha")
        if by_facets:
            assert database.upsert_run_facets(
                [_facet(trace, 1) for trace in ids], registry=ctx.retriever.registry,
            )["stored"] == 201
    with pytest.raises(QaDataError, match="more than 200"):
        _export(
            ctx, tmp_path / "too-many", facets={"answer_material_accessed": True} if by_facets else {},
            trace_ids=[] if by_facets else ids,
        )
    assert not (tmp_path / "too-many").exists() and not _manifest(tmp_path / "too-many").exists()
    data = _export(
        ctx, tmp_path / "capped", facets={"answer_material_accessed": True} if by_facets else {},
        trace_ids=[] if by_facets else ids, limit=2,
    )
    assert data["receipt"]["exported"] == 2


def test_no_matching_facets_is_a_refusal_without_creating_output(ctx: QaContext, tmp_path: Path) -> None:
    with pytest.raises(QaDataError, match="no admitted runs selected"):
        _export(ctx, tmp_path / "none", facets={"answer_material_accessed": False})
    assert not (tmp_path / "none").exists()


@pytest.mark.parametrize("format", ("harbor", "scout"))
def test_other_formats_report_paths_without_an_atif_step_map(ctx: QaContext, tmp_path: Path, format: str) -> None:
    out = tmp_path / format
    data = _export(ctx, out, format=format, limit=2)
    assert data["step_map"] == "atif only" and data["receipt"]["exported"] == 2
    for run in data["runs"]:
        assert set(run) == {"trace_id", "blind_id", "path", "actor_inputs_available"}
        assert run["actor_inputs_available"] is True
        path = Path(run["path"])
        if format == "harbor":
            assert (path / "agent/trajectory.json").is_file()
            assert json.loads((path / "result.json").read_text())["verifier_result"]["rewards"]["reward"] is None
        else:
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            assert run["blind_id"] in {row["transcript_id"] for row in rows}
    assert "reward" not in json.dumps(data)
    assert estimate_tokens(data) <= ctx.budget.used


def test_budget_omits_whole_reply_entries_but_never_partially_exports(ctx: QaContext, tmp_path: Path) -> None:
    reference = _export(ctx, tmp_path / "first")
    frame = {**reference, "runs": [], "omitted": 3, "truncated": False}
    allowance = estimate_tokens(frame) + estimate_tokens(",,") + estimate_tokens(reference["runs"][0]) + 3
    ctx.budget = Budget(allowance)
    out = tmp_path / "small"
    data = _export(ctx, out)
    assert len(data["runs"]) == 1 and data["omitted"] == ctx.budget.omitted == 2 and data["truncated"]
    assert len(json.loads(_manifest(out).read_text())) == data["receipt"]["exported"] == 3
    assert len(list(out.glob("*.json"))) == 3
    assert estimate_tokens(data) <= ctx.budget.used <= allowance


def test_too_small_receipt_budget_refuses_before_publishing(ctx: QaContext, tmp_path: Path) -> None:
    ctx.budget = Budget(1)
    with pytest.raises(QaDataError, match="raise --budget"):
        _export(ctx, tmp_path / "small")
    assert not (tmp_path / "small").exists() and not _manifest(tmp_path / "small").exists()


@pytest.mark.parametrize("database_path", (None, ":memory:", "postgresql://must-not-contact.invalid/cloud"))
def test_export_requires_a_sqlite_file(ctx: QaContext, tmp_path: Path, database_path: str | None) -> None:
    ctx.database_path = database_path
    if database_path and database_path.startswith("postgresql:"):
        ctx.database._postgres = True
    with pytest.raises(QaDataError, match="SQLite file"):
        _export(ctx, tmp_path / "unsupported")
    ctx.database._postgres = False
    assert not (tmp_path / "unsupported").exists()


def test_sqlite_url_is_accepted_for_a_file_store(ctx: QaContext, tmp_path: Path) -> None:
    ctx.database_path = "sqlite:///" + str(ctx.database_path)
    assert _export(ctx, tmp_path / "url", limit=1)["receipt"]["exported"] == 1


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    path = tmp_path / "sealed-manifest.json"
    path.write_text(json.dumps({"run-blind": {"trace_id": "private-trace", "step_origin": [2, 9, 13], "reward": 0.375}}))
    return path


@pytest.mark.parametrize("trace", ("private-trace", "run-blind"))
def test_steps_round_trip_and_summary_never_expose_rewards_or_trace_ids(manifest: Path, trace: str) -> None:
    summary = steps(manifest, trace, original=None, blinded=None)
    assert summary == {"blind_id": "run-blind", "steps": 3}
    forward = steps(manifest, trace, original=9, blinded=None)
    assert forward == {**summary, "original": 9, "blinded": 2}
    reverse = steps(manifest, trace, original=None, blinded=forward["blinded"])
    assert reverse == {**summary, "blinded": 2, "original": 9}
    assert "reward" not in json.dumps(reverse) and "private-trace" not in json.dumps(reverse)


@pytest.mark.parametrize("original,blinded", ((5, None), (None, 0), (None, -1), (None, 4)))
def test_steps_absent_or_out_of_range_is_null(manifest: Path, original: int | None, blinded: int | None) -> None:
    result = steps(manifest, "run-blind", original=original, blinded=blinded)
    assert result["blinded" if original is not None else "original"] is None


def test_steps_unknown_trace_and_ambiguous_direction_are_clear_errors(manifest: Path) -> None:
    with pytest.raises(QaDataError, match="^not in this manifest$"):
        steps(manifest, "not-here", original=None, blinded=None)
    with pytest.raises(QaDataError, match="only one"):
        steps(manifest, "run-blind", original=2, blinded=1)


@pytest.mark.parametrize("content", ("not json", "[]", '{"run-blind": {"trace_id":"x", "step_origin":[true]}}'))
def test_steps_bad_manifests_are_errors_without_dumping_content(tmp_path: Path, content: str) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(content)
    with pytest.raises(QaDataError, match="sealed manifest"):
        steps(path, "run-blind", original=None, blinded=None)


def test_steps_cli_needs_no_store(manifest: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = cli_parser().parse_args([
        "qa-data", "steps", str(manifest), "run-blind", "--blinded", "2",
        "--cloud-database", str(tmp_path / "missing-parent" / "missing.sqlite"),
    ])
    assert args.func(args, tmp_path) == 0
    reply = json.loads(capsys.readouterr().out)
    assert reply["data"] == {"blind_id": "run-blind", "steps": 3, "blinded": 2, "original": 9}
    assert not (tmp_path / "missing-parent").exists()
    assert "reward" not in json.dumps(reply) and "private-trace" not in json.dumps(reply)


def test_an_existing_root_directory_is_a_qa_error_not_an_uncaught_path_error(ctx: QaContext) -> None:
    with pytest.raises(QaDataError, match="must both be new"):
        _export(ctx, Path("/"))


def test_missing_ids_file_refuses_without_creating_output(ctx: QaContext, tmp_path: Path) -> None:
    with pytest.raises(QaDataError, match="cannot read ids file"):
        _export(ctx, tmp_path / "no-output", facets={}, ids_file=tmp_path / "missing.txt")
    assert not (tmp_path / "no-output").exists()


@pytest.mark.parametrize("when", ("after_initial_check", "before_final_read"))
def test_an_id_excluded_by_limit_is_revalidated_in_the_final_export_snapshot(
    ctx: QaContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str,
) -> None:
    def reclassify() -> None:
        with CloudTraceDatabase(ctx.database_path, sqlite_fts=False) as database:
            database.upsert_task({
                "task_id": "synthetic/beta", "dataset_id": DATASET, "split": "test",
                "metadata_json": {"item_id": "beta-item"}, "instruction_text": "Reclassified task.",
            })

    if when == "after_initial_check":
        task_of_run = ctx.task_of_run

        def check_then_reclassify(trace_id: str) -> str:
            owner = task_of_run(trace_id)
            if trace_id == "trace-b":
                reclassify()
            return owner

        monkeypatch.setattr(ctx, "task_of_run", check_then_reclassify)
    else:
        export_runs = qa_data_export.export_runs

        def reclassify_then_export(*args: Any, **kwargs: Any) -> dict[str, Any]:
            reclassify()
            return export_runs(*args, **kwargs)

        monkeypatch.setattr(qa_data_export, "export_runs", reclassify_then_export)
    out = tmp_path / "new-parent" / "refused"
    with pytest.raises(QaDataError) as refused:
        _export(ctx, out, facets={}, trace_ids=["trace-z", "trace-b"], limit=1)
    assert str(refused.value) == UNAVAILABLE
    assert not out.parent.exists() and not _manifest(out).exists()


def test_explicit_limit_preserves_request_order_after_validating_all_ids(ctx: QaContext, tmp_path: Path) -> None:
    out = tmp_path / "limited"
    data = _export(ctx, out, facets={}, trace_ids=["trace-z", "trace-b"], limit=1)
    assert [run["trace_id"] for run in data["runs"]] == ["trace-z"]
    assert (data["receipt"]["admitted"], data["receipt"]["exported"], data["receipt"]["refused"]) == (2, 1, 0)
    assert {entry["trace_id"] for entry in json.loads(_manifest(out).read_text()).values()} == {"trace-z"}


@pytest.mark.parametrize("racing", ("output", "manifest"))
def test_qa_export_never_follows_a_final_component_symlink_created_during_path_resolution(
    ctx: QaContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, racing: str,
) -> None:
    out = tmp_path / "export"
    manifest = _manifest(out)
    destination = out if racing == "output" else manifest
    target = tmp_path / "must-not-be-created"
    resolve = Path.resolve
    raced = False

    def racing_resolve(path: Path, *args: Any, **kwargs: Any) -> Path:
        nonlocal raced
        if not raced and path in {destination, destination.parent}:
            destination.symlink_to(target)
            raced = True
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", racing_resolve)
    with pytest.raises(QaDataError):
        _export(ctx, out, limit=1)
    assert raced and destination.is_symlink() and destination.readlink() == target
    assert not target.exists()
    other = manifest if racing == "output" else out
    assert not other.exists() and not other.is_symlink()


def _served_run_ids(ctx: QaContext) -> set[str]:
    return {row["run_id"] for row in ctx.database.search_runs({}, registry=ctx.retriever.registry)}


def test_a_run_whose_facet_record_names_a_sealed_task_cites_no_evidence_like_an_unfaceted_run(
    ctx: QaContext, tmp_path: Path,
) -> None:
    with CloudTraceDatabase(ctx.database_path, sqlite_fts=False) as database:
        named = {**_facet("trace-b", 2), "approach": "Compared with synthetic/sealed"}
        assert database.upsert_run_facets([named], registry=ctx.retriever.registry)["stored"] == 1
    assert _served_run_ids(ctx) == {"trace-a", "trace-z"}  # `runs` withholds the record
    data = _export(ctx, tmp_path / "out", facets={}, trace_ids=["trace-a", "trace-b", "trace-unfaceted"])
    cited = {run["trace_id"]: run for run in data["runs"]}
    assert cited["trace-a"]["evidence_steps_original"] == [2, 5, 9, 99]
    for trace in ("trace-b", "trace-unfaceted"):
        assert cited[trace]["evidence_steps_original"] == cited[trace]["evidence_steps_blinded"] == []


def test_an_admitted_task_whose_id_contains_a_sealed_name_has_no_served_evidence(
    ctx: QaContext, tmp_path: Path,
) -> None:
    twin_task, twin_trace = "synthetic/sealed-twin", "trace-twin"
    ctx.retriever.registry._assignments[(DATASET, "twin-item")] = "dev"  # type: ignore[union-attr]
    with CloudTraceDatabase(ctx.database_path, sqlite_fts=False) as database:
        database.upsert_task({
            "task_id": twin_task, "dataset_id": DATASET, "split": "dev", "metadata_json": {"item_id": "twin-item"},
            "instruction_text": "Inspect the parser and report its output.",
        })
        _trace(database, twin_trace, twin_task)
        assert database.upsert_run_facets(
            [_facet(twin_trace, 3)], registry=ctx.retriever.registry,
        )["stored"] == 1
    assert twin_trace not in _served_run_ids(ctx)
    data = _export(ctx, tmp_path / "out", facets={}, trace_ids=[twin_trace, "trace-a"])
    cited = {run["trace_id"]: run["evidence_steps_original"] for run in data["runs"]}
    assert cited == {twin_trace: [], "trace-a": [2, 5, 9, 99]}
