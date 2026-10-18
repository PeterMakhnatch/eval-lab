"""Hash custody and actor/evaluator separation at the CheatBench intake boundary."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from evallab.cheatbench_fidelity import blind_atif
from evallab.qa import trace_workspace as workspace

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "prepare_cheat_set_v7", REPO / "research/experiments/qa-trace/prepare_cheat_set_v7.py"
)
assert _spec is not None and _spec.loader is not None
intake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(intake)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _blind_document(item_id: str) -> dict[str, Any]:
    return blind_atif(
        {
            "schema_version": "ATIF-v1.8",
            "session_id": "native-session",
            "agent": {"name": "mimoagent", "version": "fixture"},
            "metadata": {"task_id": "PRIVATE_TASK", "reward": 1, "model": "PRIVATE_MODEL"},
            "steps": [
                {"step_id": 12, "source": "user", "message": "Implement the documented API.\n"},
                {
                    "step_id": 13,
                    "source": "agent",
                    "message": "I will inspect the implementation.",
                    "tool_calls": [
                        {
                            "tool_call_id": "call-1",
                            "function_name": "write_file",
                            "arguments": {
                                "path": "answer.py",
                                "content": "answer = 42\n",
                                "label": "a legitimate task argument",
                                "reward": 0,
                            },
                        }
                    ],
                    "observation": {
                        "results": [{"source_call_id": "call-1", "content": "Wrote answer.py.\n"}]
                    },
                    "extra": {"source_job": "PRIVATE_JOB"},
                },
            ],
        },
        item_id=item_id,
    )


def _row(
    exports: Path,
    item_id: str = "opaque-attempt-1",
    *,
    document: dict[str, Any] | None = None,
    trajectory_bytes: bytes | None = None,
    diff_path: str | None = None,
    diff_bytes: bytes | None = None,
) -> tuple[dict[str, Any], Path, bytes]:
    if trajectory_bytes is None:
        doc = _blind_document(item_id) if document is None else document
        # This is the producer's actual phase_export serialization, not a raw trial.
        trajectory_bytes = (json.dumps(doc, sort_keys=True) + "\n").encode()
    trajectory = exports / "blind" / f"{item_id}.json"
    trajectory.parent.mkdir(parents=True, exist_ok=True)
    trajectory.write_bytes(trajectory_bytes)
    row = {
        "schema": "mimo-cheat-set/blind-index/v1",
        "item_id": item_id,
        "blind_trajectory": {
            "file": trajectory.relative_to(exports).as_posix(),
            "sha256": "sha256:" + _sha256(trajectory_bytes),
            "bytes": len(trajectory_bytes),
        },
        "trial_ref": {
            "job_name": "PRIVATE_JOB",
            "trial_dir": "runs/PRIVATE_NATIVE_TRIAL",
            "agent_diff_path": diff_path,
            "agent_diff_sha256": "sha256:" + _sha256(diff_bytes)
            if diff_bytes is not None
            else None,
        },
        "generation": {
            "agent": "PRIVATE_GENERATION_AGENT",
            "model": "PRIVATE_GENERATION_MODEL",
            "prompt_condition": "PRIVATE_PROMPT_CONDITION",
            "prompt_sha256": "sha256:" + "f" * 64,
            "attempt": 3,
        },
    }
    return row, trajectory, trajectory_bytes


def _index(path: Path, rows: list[Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows).encode())
    return path


def test_preparation_hydrates_only_bound_evidence_not_native_or_evaluator_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    producer = tmp_path / "producer"
    exports = producer / "runs/solver/exports"
    diff_path = producer / "runs/native/verifier/agent.kept.diff"
    diff_path.parent.mkdir(parents=True)
    diff_bytes = b"diff --git a/answer.py b/answer.py\r\n+answer = 42\n+binary = '\xff'\n"
    diff_path.write_bytes(diff_bytes)
    first, first_trajectory, first_bytes = _row(
        exports,
        "opaque-attempt-1",
        diff_path=diff_path.relative_to(producer).as_posix(),
        diff_bytes=diff_bytes,
    )
    second, second_trajectory, second_bytes = _row(exports, "opaque-attempt-2")
    index = _index(exports / "cheat-set-v7.jsonl", [first, second])
    (exports / "cheat-set-v7.labels.jsonl").write_text("EVALUATOR_ONLY: do not read\n")
    native = producer / first["trial_ref"]["trial_dir"]
    native.mkdir(parents=True)
    (native / "result.json").write_text('{"reward": 1, "task_id": "PRIVATE_TASK"}')
    (native / "config.json").write_text('{"prompt": "PRIVATE_PROMPT"}')
    original_read_bytes = Path.read_bytes
    allowed = {p.resolve() for p in (index, first_trajectory, second_trajectory, diff_path)}
    opened: set[Path] = set()

    def guarded_read(path: Path) -> bytes:
        resolved = path.resolve()
        assert resolved in allowed, f"preparation hydrated an unapproved source: {path}"
        opened.add(resolved)
        return original_read_bytes(path)

    out = tmp_path / "prepared"
    with monkeypatch.context() as custody:
        custody.setattr(Path, "read_bytes", guarded_read)
        manifest = intake.prepare(index, out, producer_root=producer)
    assert opened == allowed
    subjects = [json.loads(line) for line in (out / "subjects.jsonl").read_bytes().splitlines()]
    assert all(set(row) == {"item_id", "trial_path"} for row in subjects)
    projected = [path for path in out.rglob("*") if path.is_file()]
    assert b"PRIVATE_" not in b"\n".join(path.read_bytes() for path in projected)
    for item in manifest["items"]:
        trial = out / item["trial_path"]
        allowed_files = {"agent/trajectory.json"}
        if item["diff"] is not None:
            allowed_files.add("verifier/agent.diff")
        assert {
            path.relative_to(trial).as_posix() for path in trial.rglob("*") if path.is_file()
        } == allowed_files


def test_projected_trial_preserves_ordinary_words_through_b0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Synthetic fidelity regression, not a native solver or accuracy control."""
    lookup = tmp_path / "lookup"
    lookup.mkdir()
    monkeypatch.setattr(workspace, "_repo_root", lambda: lookup)
    monkeypatch.setattr(workspace, "_prior_archive_root", lambda: tmp_path / "archived-priors")
    document = _blind_document("fidelity-probe")
    document["steps"][0]["message"] = "Use inputs to produce outputs.\n"
    code = "inputs = [1, 2]\nanswer = sum(inputs)\n"
    document["steps"][1]["tool_calls"][0]["arguments"]["content"] = code
    exports = tmp_path / "exports"
    row, _, _ = _row(exports, "fidelity-probe", document=document)
    index = _index(exports / "cheat-set-v7.jsonl", [row])
    out = tmp_path / "prepared"
    manifest = intake.prepare(index, out)
    bundle = workspace.build_workspace(
        out / manifest["items"][0]["trial_path"], tmp_path / "bundle"
    )
    steps = json.loads(bundle.text("trajectory.json"))["steps"]
    assert steps[0]["message"] == document["steps"][0]["message"]
    assert steps[1]["tool_calls"][0]["arguments"]["content"] == code
    assert document["steps"][0]["message"].strip() in bundle.text("prompt.md")


def test_missing_diff_stays_missing_despite_recorded_write_actions(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    row, _, payload = _row(exports)
    index = _index(exports / "cheat-set-v7.jsonl", [row])
    out = tmp_path / "out"
    manifest = intake.prepare(index, out)
    item = manifest["items"][0]
    assert item["diff"] is None
    assert not (out / item["trial_path"] / "verifier").exists()
    assert not (out / item["trial_path"] / "answer.py").exists()
    assert (out / item["trajectory"]["path"]).read_bytes() == payload
    copied = json.loads(payload)
    assert copied["steps"][0]["message"] == "Implement the documented API.\n"
    assert copied["steps"][1]["tool_calls"][0]["arguments"]["content"] == "answer = 42\n"
    assert not (out / item["trial_path"] / "result.json").exists()


def test_absolute_empty_diff_is_present_without_a_producer_root(tmp_path: Path) -> None:
    diff = tmp_path / "native-agent.diff"
    diff.write_bytes(b"")
    exports = tmp_path / "exports"
    row, _, _ = _row(exports, diff_path=str(diff), diff_bytes=b"")
    index = _index(exports / "cheat-set-v7.jsonl", [row])
    out = tmp_path / "out"
    out.mkdir()
    item = intake.prepare(index, out)["items"][0]
    assert item["diff"] is not None
    assert item["diff"]["bytes"] == 0
    assert item["diff"]["sha256"] == _sha256(b"")
    assert (out / item["diff"]["path"]).read_bytes() == b""


def test_relative_diff_requires_explicit_producer_root(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    row, _, _ = _row(exports, diff_path="runs/native/verifier/agent.diff", diff_bytes=b"diff\n")
    index = _index(exports / "cheat-set-v7.jsonl", [row])
    out = tmp_path / "out"
    with pytest.raises(SystemExit, match="explicit --producer-root"):
        intake.prepare(index, out)
    assert not out.exists()


def test_explicit_exports_root_is_used_instead_of_index_directory(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    row, _, payload = _row(exports)
    index = _index(tmp_path / "handoff/cheat-set-v7.jsonl", [row])
    out = tmp_path / "out"
    item = intake.prepare(index, out, exports_root=exports)["items"][0]
    assert (out / item["trajectory"]["path"]).read_bytes() == payload


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("sha256", "sha256:" + "0" * 64, "size/hash mismatch"),
        ("sha256", "0" * 64, "expected sha256"),
        ("sha256", "sha256:not-a-digest", "expected sha256"),
        ("bytes", 0, "size/hash mismatch"),
        ("bytes", True, "nonnegative integer"),
        ("bytes", -1, "nonnegative integer"),
        ("bytes", 1.0, "nonnegative integer"),
    ],
)
def test_trajectory_descriptor_corruption_refuses_before_writing(
    tmp_path: Path, field: str, value: Any, error: str
) -> None:
    exports = tmp_path / "exports"
    row, _, _ = _row(exports)
    row["blind_trajectory"][field] = value
    index = _index(exports / "cheat-set-v7.jsonl", [row])
    out = tmp_path / "out"
    with pytest.raises(SystemExit, match=error):
        intake.prepare(index, out)
    assert not out.exists()


def test_later_corrupt_record_does_not_leave_an_earlier_projected_trial(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    first, _, _ = _row(exports, "first")
    second, trajectory, _ = _row(exports, "second")
    trajectory.write_bytes(b"tampered after export\n")
    index = _index(exports / "cheat-set-v7.jsonl", [first, second])
    out = tmp_path / "out"
    with pytest.raises(SystemExit, match="size/hash mismatch"):
        intake.prepare(index, out)
    assert not out.exists()


@pytest.mark.parametrize("case", ["changed", "missing", "no_digest", "no_path", "empty_path"])
def test_ambiguous_or_unbound_diff_is_not_silently_dropped(tmp_path: Path, case: str) -> None:
    exports = tmp_path / "exports"
    diff = tmp_path / "agent.diff"
    diff.write_bytes(b"original\n")
    row, _, _ = _row(exports, diff_path=str(diff), diff_bytes=b"original\n")
    if case == "changed":
        diff.write_bytes(b"changed\n")
    elif case == "missing":
        diff.unlink()
    elif case == "no_digest":
        row["trial_ref"]["agent_diff_sha256"] = None
    elif case == "no_path":
        row["trial_ref"]["agent_diff_path"] = None
    else:
        row["trial_ref"]["agent_diff_path"] = ""
    index = _index(exports / "cheat-set-v7.jsonl", [row])
    out = tmp_path / "out"
    with pytest.raises(SystemExit, match="diff"):
        intake.prepare(index, out)
    assert not out.exists()


@pytest.mark.parametrize("reference", ["../outside.json", "absolute", "symlink"])
def test_trajectory_reference_cannot_escape_exports(tmp_path: Path, reference: str) -> None:
    exports = tmp_path / "exports"
    row, _, payload = _row(exports)
    outside = tmp_path / "outside.json"
    outside.write_bytes(payload)
    if reference == "absolute":
        row["blind_trajectory"]["file"] = str(outside)
    elif reference == "symlink":
        link = exports / "blind/escape.json"
        link.symlink_to(outside)
        row["blind_trajectory"]["file"] = "blind/escape.json"
    else:
        row["blind_trajectory"]["file"] = reference
    index = _index(exports / "cheat-set-v7.jsonl", [row])
    out = tmp_path / "out"
    with pytest.raises(SystemExit, match="absolute file reference|escapes its root"):
        intake.prepare(index, out)
    assert not out.exists()


def test_relative_diff_cannot_escape_explicit_producer_root(tmp_path: Path) -> None:
    producer = tmp_path / "producer"
    exports = producer / "exports"
    outside = tmp_path / "agent.diff"
    outside.write_bytes(b"diff\n")
    row, _, _ = _row(exports, diff_path="../agent.diff", diff_bytes=b"diff\n")
    index = _index(exports / "cheat-set-v7.jsonl", [row])
    out = tmp_path / "out"
    with pytest.raises(SystemExit, match="escapes its root"):
        intake.prepare(index, out, producer_root=producer)
    assert not out.exists()


@pytest.mark.parametrize("payload", [b"", b"\n \t\n"])
def test_empty_index_refuses(tmp_path: Path, payload: bytes) -> None:
    index = tmp_path / "index.jsonl"
    index.write_bytes(payload)
    out = tmp_path / "out"
    with pytest.raises(SystemExit, match="empty input"):
        intake.prepare(index, out)
    assert not out.exists()


@pytest.mark.parametrize("item_id", [None, 1, "", "  ", "\ud800"])
def test_item_ids_are_nonempty_utf8_strings(tmp_path: Path, item_id: Any) -> None:
    exports = tmp_path / "exports"
    row, _, _ = _row(exports)
    row["item_id"] = item_id
    index = _index(exports / "cheat-set-v7.jsonl", [row])
    out = tmp_path / "out"
    with pytest.raises(SystemExit, match="item_id"):
        intake.prepare(index, out)
    assert not out.exists()


def test_duplicate_item_ids_refuse_without_output(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    row, _, _ = _row(exports)
    index = _index(exports / "cheat-set-v7.jsonl", [row, row])
    out = tmp_path / "out"
    with pytest.raises(SystemExit, match="duplicate item_id"):
        intake.prepare(index, out)
    assert not out.exists()


@pytest.mark.parametrize("row", [{"schema": "unknown"}, [], None])
def test_wrong_schema_or_nonobject_index_rows_refuse(tmp_path: Path, row: Any) -> None:
    index = _index(tmp_path / "index.jsonl", [row])
    out = tmp_path / "out"
    with pytest.raises(SystemExit, match="expected schema|expected a JSON object"):
        intake.prepare(index, out)
    assert not out.exists()


def test_duplicate_index_json_keys_are_ambiguous(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    row, _, _ = _row(exports)
    text = json.dumps(row)
    index = exports / "cheat-set-v7.jsonl"
    index.write_text('{"item_id":"different",' + text[1:] + "\n")
    out = tmp_path / "out"
    with pytest.raises(SystemExit, match="duplicate JSON key"):
        intake.prepare(index, out)
    assert not out.exists()


@pytest.mark.parametrize(
    "payload",
    [
        b"{broken\n",
        b"[]\n",
        b'{"steps":[],"steps":[]}\n',
        b'{"steps":[],"number":NaN}\n',
        b"\xff\n",
    ],
)
def test_hash_bound_but_invalid_atif_is_not_accepted(tmp_path: Path, payload: bytes) -> None:
    exports = tmp_path / "exports"
    row, _, _ = _row(exports, trajectory_bytes=payload)
    index = _index(exports / "cheat-set-v7.jsonl", [row])
    out = tmp_path / "out"
    with pytest.raises(SystemExit, match="invalid JSON|expected a JSON object"):
        intake.prepare(index, out)
    assert not out.exists()


@pytest.mark.parametrize(
    ("location", "field"),
    [
        ("root", "v7_reward"),
        ("root", "disposition"),
        ("root", "wrong_pass"),
        ("root", "reward"),
        ("root", "task_id"),
        ("root", "generation"),
        ("root", "label"),
        ("root", "cheatbench_label"),
        ("metadata", "prompt_condition"),
        ("step", "v6_reward"),
        ("tool", "v7_grader_kind"),
        ("observation", "wrong_pass"),
        ("observation_result", "disposition"),
    ],
)
def test_hash_bound_evaluator_or_generation_annotations_fail_closed(
    tmp_path: Path, location: str, field: str
) -> None:
    exports = tmp_path / "exports"
    doc = _blind_document("opaque-attempt-1")
    targets = {
        "root": doc,
        "metadata": doc["metadata"],
        "step": doc["steps"][1],
        "tool": doc["steps"][1]["tool_calls"][0],
        "observation": doc["steps"][1]["observation"],
        "observation_result": doc["steps"][1]["observation"]["results"][0],
    }
    targets[location][field] = "EVALUATOR_ONLY"
    row, _, _ = _row(exports, document=doc)
    index = _index(exports / "cheat-set-v7.jsonl", [row])
    out = tmp_path / "out"
    with pytest.raises(SystemExit, match="evaluator|blind_atif"):
        intake.prepare(index, out)
    assert not out.exists()


def test_structured_user_and_tool_evidence_are_not_reinterpreted_as_labels(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    doc = _blind_document("opaque-attempt-1")
    doc["steps"][0]["message"] = {
        "content": "Implement a label/reward parser.",
        "example": {"reward": 1, "label": "a task input, not an evaluator label"},
    }
    doc["steps"][1]["observation"]["results"][0]["content"] = {
        "label": "observed tool output",
        "reward": 0,
    }
    row, _, payload = _row(exports, document=doc)
    index = _index(exports / "cheat-set-v7.jsonl", [row])
    out = tmp_path / "out"
    item = intake.prepare(index, out)["items"][0]
    assert (out / item["trajectory"]["path"]).read_bytes() == payload


@pytest.mark.parametrize("case", ["item_id", "annotation", "extra", "step_id", "schema"])
def test_atif_must_already_be_the_bound_items_blind_projection(tmp_path: Path, case: str) -> None:
    exports = tmp_path / "exports"
    doc = _blind_document("opaque-attempt-1")
    if case == "item_id":
        doc["metadata"]["item_id"] = "another-item"
    elif case == "annotation":
        doc["steps"].append(
            {"step_id": 2, "source": "user", "message": "[CheatBench annotation] cheating"}
        )
    elif case == "extra":
        doc["steps"][1]["extra"] = {"source_job": "PRIVATE_JOB"}
    elif case == "step_id":
        doc["steps"][1]["step_id"] = 13
    else:
        del doc["schema_version"]
    row, _, _ = _row(exports, document=doc)
    index = _index(exports / "cheat-set-v7.jsonl", [row])
    out = tmp_path / "out"
    with pytest.raises(SystemExit, match="blind_atif|annotation|schema_version|step_id"):
        intake.prepare(index, out)
    assert not out.exists()


def test_existing_output_is_never_overwritten(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    row, _, _ = _row(exports)
    index = _index(exports / "cheat-set-v7.jsonl", [row])
    out = tmp_path / "out"
    intake.prepare(index, out)
    before = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    with pytest.raises(SystemExit, match="must not contain existing data"):
        intake.prepare(index, out)
    assert {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()} == before


def test_nonempty_or_symlink_output_refuses_before_reading_index(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    sentinel = real / "keep"
    sentinel.write_bytes(b"existing user data")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    for out, error in ((real, "existing data"), (link, "symlink")):
        with pytest.raises(SystemExit, match=error):
            intake.prepare(tmp_path / "does-not-exist.jsonl", out)
    assert sentinel.read_bytes() == b"existing user data"
