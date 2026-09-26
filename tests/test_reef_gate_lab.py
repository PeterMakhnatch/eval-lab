"""Deterministic behavioral tests for the HAR-73 Lab CLI evaluator.

Everything runs against a fabricated Lab checkout in ``tmp_path`` and a fake
CLI runner: no shared queue is drained, no shared database is written, no
real model or sandbox is touched. The scenarios are the acceptance surface:

* committed-split validation (disjointness, strict keys, schema) and the
  registry allow-list contract (registered, measurement+training, never
  heldout) refusing BEFORE any CLI submit/execution;
* ``prepare()`` submitting every task-repeat-side spec and returning the
  exact approve commands without ticking, executing, or waiting;
* approval timeout / rejection aborts yielding positional ``None`` scores and
  ``metadata["complete"] is False`` (never a fabricated zero);
* infrastructure-failure episodes scoring ``None`` with a grounded failure
  observation, the opposite side staying scoreable, and ``evaluation_sides``
  excluding the incompletely covered side so settlement cannot clear a
  failure manifest for missing evidence;
* candidate-id idempotency: retry reuses submitted specs, the crash window
  (spec submitted, manifest not yet updated) recovers by deterministic job
  name, and conflicting content for one id refuses.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[1]
        / "library" / "adapters" / "reef_gate" / "src",
    ),
)

from evallab_reef_gate.lab import (  # noqa: E402
    LabConfig,
    LabEvaluator,
    LabGateError,
    SubprocessRunner,
)

MODEL = "zai-coding-plan/glm-5.3"
TASK_A = "event-summary"
TASK_B = "travel-lisbon-002"
HELD_TASK = "heldout-external-001"
HELD_PATH = "~/Developer/elsewhere/heldout-external-001"

CURRENT_FILES = {"terminus/AGENTS.md": "# current rules\n"}
CANDIDATE_FILES = {"terminus/AGENTS.md": "# candidate rules\nmore care\n"}

DEV_A_DIGEST = "sha256:" + "a" * 64
DEV_B_DIGEST = "sha256:" + "b" * 64
HELD_DIGEST = "sha256:" + "c" * 64


def _sha64(seed: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


class FakeLab:
    """A fake Eval Lab CLI: prepare/submit/tick against tmp directories."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.submits: list[str] = []
        self.ticks: list[str] = []
        self.prepared: list[str] = []
        self.counter = 0
        self.outcomes: dict[str, tuple[str, float | None]] = {}
        for state in ("pending", "waiting", "approved", "running", "done", "failed", "rejected"):
            (root / "queue" / state).mkdir(parents=True, exist_ok=True)

    # -- fake CLI entry ----------------------------------------------------

    def cli(self, cmd: list[str]) -> tuple[int, str, str]:
        sub = cmd[1] if len(cmd) > 1 else ""
        if sub == "tasks" and "prepare" in cmd:
            return self._prepare(cmd)
        if sub == "submit":
            return self._submit(cmd)
        if sub == "tick":
            return self._tick(cmd)
        return 2, "", f"unknown command: {cmd}"

    def _flag(self, cmd: list[str], name: str) -> str | None:
        for index, token in enumerate(cmd):
            if token == name and index + 1 < len(cmd):
                return cmd[index + 1]
        return None

    def _prepare(self, cmd: list[str]) -> tuple[int, str, str]:
        job = self._flag(cmd, "--name")
        task_path = cmd[cmd.index("prepare") + 1]
        spec = {
            "name": job,
            "agent": self._flag(cmd, "--agent"),
            "model": self._flag(cmd, "--model"),
            "environment": self._flag(cmd, "--environment"),
            "task": task_path,
            "task_path": task_path,
            "task_package_digest": None,
            "harness_tree_sha256": self._flag(cmd, "--harness-tree-sha256"),
        }
        path = self.root / "derived" / "prepared" / f"{job}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(spec, indent=2) + "\n")
        self.prepared.append(job)
        return 0, json.dumps({"spec": spec, "spec_path": str(path)}) + "\n", ""

    def _submit(self, cmd: list[str]) -> tuple[int, str, str]:
        spec_path = Path(cmd[2])
        spec = json.loads(spec_path.read_text())
        existing = self._find_by_name(spec["name"])
        if existing is not None:
            state, path = existing
            return 0, f"spec_id: {json.loads(path.read_text())['spec_id']}\nstate: {state}\n", ""
        self.counter += 1
        spec_id = f"01JTEST{self.counter:06d}"
        spec["spec_id"] = spec_id
        path = self.root / "queue" / "waiting" / f"{spec['agent']}-{spec_id}.json"
        path.write_text(json.dumps(spec, indent=2) + "\n")
        self.submits.append(spec_id)
        return 0, f"spec_id: {spec_id}\nstate: waiting\npath: {path}\n", ""

    def _tick(self, cmd: list[str]) -> tuple[int, str, str]:
        spec_id = self._flag(cmd, "--spec-id")
        self.ticks.append(spec_id)
        located = self._find(spec_id)
        if located is None:
            return 2, "", f"spec {spec_id} not found\n"
        state, path = located
        if state != "approved":
            return 1, f"spec: {spec_id} state: {state}\n", f"{spec_id} is {state}, not approved\n"
        spec = json.loads(path.read_text())
        self._run_job(spec["name"])
        (self.root / "queue" / "done" / path.name).write_text(
            json.dumps(spec, indent=2) + "\n"
        )
        path.unlink()
        return 0, f"dispatched 1 experiment(s)\nspec: {spec_id} state: done\n", ""

    def _run_job(self, job: str) -> None:
        from datetime import UTC, datetime

        kind, value = self.outcomes.get(job, ("score", 1.0))
        job_dir = self.root / "runs" / job
        trial_dir = job_dir / f"{job}-trial1"
        trial_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC).isoformat()
        if kind == "score":
            trial = {"finished_at": now, "verifier_result": {"rewards": {"reward": value}}}
        elif kind == "infra":
            trial = {
                "finished_at": now,
                "exception_info": {"message": str(value)},
                "verifier_result": {"rewards": {"reward": 0.0}},
            }
        elif kind == "unscored":
            trial = {"finished_at": now, "verifier_result": {"rewards": {}}}
        elif kind == "incomplete":
            trial = None
        else:  # pragma: no cover - unknown script
            raise AssertionError(kind)
        if trial is not None:
            (trial_dir / "result.json").write_text(json.dumps(trial, indent=2) + "\n")
        result = {"finished_at": now if kind != "incomplete" else None, "n_total_trials": 1}
        (job_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")

    def _find(self, spec_id: str) -> tuple[str, Path] | None:
        for state in ("pending", "waiting", "approved", "running", "done", "failed", "rejected"):
            for path in (self.root / "queue" / state).glob(f"*-{spec_id}.json"):
                return state, path
        return None

    def _find_by_name(self, name: str) -> tuple[str, Path] | None:
        for state in ("pending", "waiting", "approved", "running", "done", "failed", "rejected"):
            for path in sorted((self.root / "queue" / state).glob("*.json")):
                payload = json.loads(path.read_text())
                if payload.get("name") == name:
                    return state, path
        return None


class FakeRunner:
    """Runs the FakeLab CLI; records every invocation for assertions."""

    def __init__(self, lab: FakeLab) -> None:
        self.lab = lab
        self.calls: list[tuple[str, list[str]]] = []

    def run(self, cmd, *, cwd, timeout, python):
        self.calls.append((str(cwd), list(cmd)))
        return self.lab.cli(cmd)


def _write_split(root: Path, *, dev=None, held=None, extra_root=None, version=1) -> Path:
    payload = {
        "schema_version": version,
        "name": "har73-split",
        "status": "committed",
        "authority": "peter",
        "rationale": "HAR-73 dev/heldout split",
        "dev": dev if dev is not None else [
            {"task_id": TASK_A, "task_path": f"library/tasks/{TASK_A}", "package_digest": DEV_A_DIGEST},
            {"task_id": TASK_B, "task_path": f"library/tasks/{TASK_B}", "package_digest": DEV_B_DIGEST},
        ],
        "held_out": held if held is not None else [
            {"task_id": HELD_TASK, "task_path": HELD_PATH, "package_digest": HELD_DIGEST},
        ],
        "limits": {"max_dev_tasks": 2},
    }
    if extra_root:
        payload.update(extra_root)
    path = root / "split.json"
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def _write_registry(root: Path, task_id: str, *, uses=None, package=None, state="registered") -> None:
    record = {
        "task_id": task_id,
        "task_path": f"library/tasks/{task_id}",
        "state": state,
        "allowed_uses": uses if uses is not None else ["measurement", "training"],
        "digests": {
            "task_toml": _sha64(task_id + ":toml"),
            "instruction": _sha64(task_id + ":instruction"),
            "environment": _sha64(task_id + ":environment"),
            "verifier": _sha64(task_id + ":verifier"),
            "package": package if package is not None else _sha64(task_id + ":package"),
        },
    }
    if task_id == TASK_A:
        record["digests"]["package"] = DEV_A_DIGEST
    if task_id == TASK_B:
        record["digests"]["package"] = DEV_B_DIGEST
    directory = root / "library" / "registry"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{task_id}.json").write_text(json.dumps(record, indent=2) + "\n")


def _lab(tmp_path: Path, **config_overrides) -> tuple[LabEvaluator, FakeLab, FakeRunner, Path]:
    lab_root = tmp_path / "lab"
    (lab_root / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (lab_root / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
    split = _write_split(lab_root)
    _write_registry(lab_root, TASK_A)
    _write_registry(lab_root, TASK_B)
    lab = FakeLab(lab_root)
    runner = FakeRunner(lab)
    config = LabConfig.from_dict({
        "lab_root": str(lab_root),
        "split_path": str(split),
        "model": MODEL,
        "record_dir": str(tmp_path / "records"),
        "tick_timeout_seconds": 1.0,
        "tick_poll_seconds": 0.01,
        **config_overrides,
    })
    return LabEvaluator(config, runner=runner), lab, runner, lab_root


def _approve_all(lab: FakeLab) -> None:
    for spec_id in list(lab.submits):
        located = lab._find(spec_id)
        assert located is not None
        state, path = located
        if state == "waiting":
            target = lab.root / "queue" / "approved" / path.name
            path.rename(target)


def _job_names(evaluator: LabEvaluator, candidate_id: str) -> list[str]:
    import hashlib

    prefix = "rg-" + hashlib.sha256(candidate_id.encode()).hexdigest()[:12]
    names = []
    for task in (TASK_A, TASK_B):
        slug = task.replace("-", "-")
        for side in ("candidate", "current"):
            names.append(f"{prefix}-{slug}-0-{side}")
    return names


# ---------------------------------------------------------------------------
# prepare(): submit everything, execute nothing
# ---------------------------------------------------------------------------

def test_prepare_submits_every_side_and_never_ticks(tmp_path: Path) -> None:
    evaluator, lab, _runner, _root = _lab(tmp_path)
    result = evaluator.prepare(
        "cand-1", CURRENT_FILES, CANDIDATE_FILES, (TASK_A, TASK_B),
    )
    assert len(lab.submits) == 4  # 2 dev tasks x 1 repeat x 2 sides
    assert lab.ticks == []
    assert not any((tmp_path / "lab" / "runs").rglob("result.json"))
    assert [detail["side"] for detail in result["spec_details"]] == [
        "candidate", "current", "candidate", "current",
    ]
    assert result["approve_commands"] == [
        f"uv run evallab approve {detail['spec_id']} --actor <you>"
        for detail in result["spec_details"]
    ]
    assert all(command.startswith("uv run evallab approve ") for command in result["approve_commands"])
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    assert manifest["candidate_id"] == "cand-1"
    assert manifest["outcome"] == "awaiting_approval"
    assert manifest["content_hash"] == result["content_hash"]
    assert manifest["reef_commit"] is None
    assert manifest["model"] == MODEL


def test_prepare_is_idempotent_for_the_same_candidate_id(tmp_path: Path) -> None:
    evaluator, lab, _runner, _root = _lab(tmp_path)
    first = evaluator.prepare(
        "cand-1", CURRENT_FILES, CANDIDATE_FILES, (TASK_A, TASK_B),
    )
    second = evaluator.prepare(
        "cand-1", CURRENT_FILES, CANDIDATE_FILES, (TASK_A, TASK_B),
    )
    assert len(lab.submits) == 4
    assert second["recovered"] is False
    assert [d["spec_id"] for d in second["spec_details"]] == [
        d["spec_id"] for d in first["spec_details"]
    ]


def test_conflicting_content_for_one_candidate_id_refuses(tmp_path: Path) -> None:
    evaluator, _lab, _runner, _root = _lab(tmp_path)
    evaluator.prepare("cand-1", CURRENT_FILES, CANDIDATE_FILES, (TASK_A, TASK_B))
    with pytest.raises(LabGateError, match="conflicting content"):
        evaluator.prepare(
            "cand-1", CURRENT_FILES, {"terminus/AGENTS.md": "# different\n"}, (TASK_A, TASK_B),
        )


def test_crash_window_spec_recovered_by_deterministic_job_name(tmp_path: Path) -> None:
    evaluator, lab, _runner, _root = _lab(tmp_path)
    # Submit through prepare, then wipe the manifest: the queue still holds
    # the specs, and recovery must not submit anything new.
    result = evaluator.prepare(
        "cand-1", CURRENT_FILES, CANDIDATE_FILES, (TASK_A, TASK_B),
    )
    Path(result["manifest_path"]).unlink()
    recovered = evaluator.prepare(
        "cand-1", CURRENT_FILES, CANDIDATE_FILES, (TASK_A, TASK_B),
    )
    assert len(lab.submits) == 4
    assert recovered["recovered"] is True
    assert [d["spec_id"] for d in recovered["spec_details"]] == [
        d["spec_id"] for d in result["spec_details"]
    ]


# ---------------------------------------------------------------------------
# fail-closed validation before any CLI submit/execution
# ---------------------------------------------------------------------------

def test_heldout_allowlisted_task_refuses_before_submission(tmp_path: Path) -> None:
    evaluator, lab, _runner, root = _lab(tmp_path)
    _write_registry(root, TASK_B, uses=["measurement", "training", "heldout"])
    with pytest.raises(LabGateError, match="heldout-allow-listed"):
        evaluator.prepare("cand-1", CURRENT_FILES, CANDIDATE_FILES, (TASK_A, TASK_B))
    assert lab.submits == []


def test_missing_registration_fails_closed(tmp_path: Path) -> None:
    evaluator, lab, _runner, root = _lab(tmp_path)
    (root / "library" / "registry" / f"{TASK_B}.json").unlink()
    with pytest.raises(LabGateError, match="no registry record"):
        evaluator.prepare("cand-1", CURRENT_FILES, CANDIDATE_FILES, (TASK_A, TASK_B))
    assert lab.submits == []


def test_corrupt_registration_fails_closed(tmp_path: Path) -> None:
    evaluator, lab, _runner, root = _lab(tmp_path)
    (root / "library" / "registry" / f"{TASK_B}.json").write_text("{not json")
    with pytest.raises(LabGateError, match="corrupt"):
        evaluator.prepare("cand-1", CURRENT_FILES, CANDIDATE_FILES, (TASK_A, TASK_B))
    assert lab.submits == []


def test_dev_digest_drift_against_registry_refuses(tmp_path: Path) -> None:
    evaluator, lab, _runner, root = _lab(tmp_path)
    _write_registry(root, TASK_B, package="sha256:" + "d" * 64)
    with pytest.raises(LabGateError, match="digest drift"):
        evaluator.prepare("cand-1", CURRENT_FILES, CANDIDATE_FILES, (TASK_A, TASK_B))
    assert lab.submits == []


def test_task_mismatch_against_dev_set_refuses(tmp_path: Path) -> None:
    evaluator, lab, _runner, _root = _lab(tmp_path)
    with pytest.raises(LabGateError, match="match the committed dev set exactly"):
        evaluator.prepare("cand-1", CURRENT_FILES, CANDIDATE_FILES, (TASK_A,))
    assert lab.submits == []


@pytest.mark.parametrize(
    "mutation",
    [
        {"dev": [{"task_id": TASK_A, "task_path": "library/tasks/a", "package_digest": DEV_A_DIGEST},
                 {"task_id": TASK_A, "task_path": "library/tasks/a2", "package_digest": DEV_B_DIGEST}]},
        {"held": [{"task_id": TASK_A, "task_path": "~/x", "package_digest": DEV_A_DIGEST}]},
        {"extra_root": {"sneaky": True}},
        {"version": 2},
    ],
    ids=["duplicate-dev-id", "dev-held-digest-overlap", "unknown-root-key", "schema-version-2"],
)
def test_invalid_splits_refuse(tmp_path: Path, mutation: dict) -> None:
    evaluator, lab, _runner, root = _lab(tmp_path)
    held = [{"task_id": TASK_A, "task_path": "~/x", "package_digest": DEV_A_DIGEST}] \
        if "held" in mutation else None
    dev = mutation.get("dev")
    _write_split(
        root,
        dev=dev,
        held=held,
        extra_root=mutation.get("extra_root"),
        version=mutation.get("version", 1),
    )
    with pytest.raises(LabGateError):
        evaluator.prepare("cand-1", CURRENT_FILES, CANDIDATE_FILES, (TASK_A, TASK_B))
    assert lab.submits == []


# ---------------------------------------------------------------------------
# evaluation outcomes: timeouts, infra failures, idempotent retries
# ---------------------------------------------------------------------------

def test_approval_timeout_yields_positional_none_and_incomplete(tmp_path: Path, capsys) -> None:
    evaluator, lab, _runner, _root = _lab(tmp_path, tick_timeout_seconds=0.05)
    lab.outcomes = {}  # nothing will run: no approval ever arrives
    out = evaluator.evaluate(
        "cand-timeout", CURRENT_FILES, CANDIDATE_FILES, (TASK_A, TASK_B),
    )
    assert out["metadata"]["complete"] is False
    assert out["metadata"]["incomplete_reason"] == "approval_timeout"
    assert out["metrics"]["candidate_scores"] == (None, None)
    assert out["metrics"]["current_scores"] == (None, None)
    assert out["metrics"]["episode_failures"] == 4
    assert out["metrics"]["evaluation_sides"] == []
    assert lab.ticks == []
    stdout = capsys.readouterr().out
    assert "uv run evallab approve " in stdout
    assert "candidate manifest:" in stdout


def test_rejected_spec_reports_withdrawn(tmp_path: Path) -> None:
    evaluator, lab, _runner, _root = _lab(tmp_path)
    evaluator.prepare("cand-reject", CURRENT_FILES, CANDIDATE_FILES, (TASK_A, TASK_B))
    for spec_id in list(lab.submits):
        state, path = lab._find(spec_id)
        (lab.root / "queue" / "rejected" / path.name).write_text(path.read_text())
        path.unlink()
    out = evaluator.evaluate(
        "cand-reject", CURRENT_FILES, CANDIDATE_FILES, (TASK_A, TASK_B),
    )
    assert out["metadata"]["incomplete_reason"] == "withdrawn"
    assert out["metadata"]["complete"] is False
    assert lab.ticks == []


def test_infra_failure_scores_none_with_grounded_cause(tmp_path: Path) -> None:
    evaluator, lab, _runner, _root = _lab(tmp_path)
    names = _job_names(evaluator, "cand-infra")
    for name in names:
        side = "candidate" if name.endswith("-candidate") else "current"
        if side == "candidate" and TASK_A in name:
            lab.outcomes[name] = ("infra", "provider http 500")
        else:
            lab.outcomes[name] = ("score", 1.0)
    _approve_all(lab)
    out = evaluator.evaluate(
        "cand-infra", CURRENT_FILES, CANDIDATE_FILES, (TASK_A, TASK_B),
    )
    metrics = out["metrics"]
    assert metrics["candidate_scores"] == (None, 1.0)
    assert metrics["current_scores"] == (1.0, 1.0)
    assert metrics["episode_failures"] == 1
    assert metrics["evaluation_sides"] == ["current"]
    failure = metrics["candidate_failures"][0]
    assert failure["task"] == TASK_A
    assert failure["stage"] == "trial"
    assert "provider http 500" in failure["cause"]
    # Unobserved native quantities stay unknown, never invented.
    assert out["metadata"]["residue_unknown"] is True
    assert out["metadata"]["agents_unknown"] is True
    assert metrics["candidate_agents"] == {}
    assert metrics["candidate_residue"] == 0


def test_unscored_valid_trial_preserves_none(tmp_path: Path) -> None:
    evaluator, lab, _runner, _root = _lab(tmp_path)
    names = _job_names(evaluator, "cand-unscored")
    for name in names:
        if name.endswith("-current") and TASK_B in name:
            lab.outcomes[name] = ("unscored", None)
        else:
            lab.outcomes[name] = ("score", 0.0)
    _approve_all(lab)
    out = evaluator.evaluate(
        "cand-unscored", CURRENT_FILES, CANDIDATE_FILES, (TASK_A, TASK_B),
    )
    metrics = out["metrics"]
    assert metrics["candidate_scores"] == (0.0, 0.0)
    assert metrics["current_scores"] == (0.0, None)
    assert metrics["evaluation_sides"] == ["candidate"]
    assert out["metadata"]["complete"] is False
    assert out["metadata"]["incomplete_reason"] == "incomplete_evidence"


def test_complete_campaign_has_full_settlement_shape(tmp_path: Path) -> None:
    evaluator, lab, _runner, _root = _lab(tmp_path)
    names = _job_names(evaluator, "cand-ok")
    for name in names:
        reward = 1.0 if name.endswith("-candidate") else 0.0
        lab.outcomes[name] = ("score", reward)
    _approve_all(lab)
    out = evaluator.evaluate(
        "cand-ok", CURRENT_FILES, CANDIDATE_FILES, (TASK_A, TASK_B),
    )
    metrics = out["metrics"]
    assert metrics["candidate_scores"] == (1.0, 1.0)
    assert metrics["current_scores"] == (0.0, 0.0)
    assert metrics["episode_failures"] == 0
    assert metrics["episode_repeats"] == 1
    assert metrics["candidate_score"] == 2.0
    assert metrics["current_score"] == 0.0
    assert metrics["candidate_failures"] == ()
    assert "evaluation_sides" not in metrics  # both sides fully covered
    assert all(isinstance(p, str) and "runs/" in p for p in metrics["candidate_paths"])
    meta = out["metadata"]
    assert meta["complete"] is True
    assert meta["incomplete_reason"] is None
    assert meta["observed_pairs"] == 2
    assert meta["split_digest"].startswith("sha256:")
    assert meta["candidate_tree_sha256"] != meta["current_tree_sha256"]
    assert meta["reef_commit"] == "2a1864d4158de8a24e00ae777e9ff0501f49a97f"
    assert meta["model"] == MODEL
    assert len(meta["spec_ids"]) == 4
    assert meta["evaluation_seconds"] >= 0.0
    assert {row["task_id"] for row in meta["evidence"]} == {TASK_A, TASK_B}
    manifest = json.loads(Path(meta["manifest_path"]).read_text())
    assert manifest["outcome"] == "evaluated"
    assert len(manifest["approve_commands"]) == 8  # approve + tick per spec


def test_evaluate_retry_reuses_completed_specs_without_resubmitting(tmp_path: Path) -> None:
    evaluator, lab, _runner, _root = _lab(tmp_path)
    names = _job_names(evaluator, "cand-retry")
    for name in names:
        lab.outcomes[name] = ("score", 1.0)
    _approve_all(lab)
    first = evaluator.evaluate(
        "cand-retry", CURRENT_FILES, CANDIDATE_FILES, (TASK_A, TASK_B),
    )
    submits_after_first = len(lab.submits)
    ticks_after_first = len(lab.ticks)
    second = evaluator.evaluate(
        "cand-retry", CURRENT_FILES, CANDIDATE_FILES, (TASK_A, TASK_B),
    )
    assert len(lab.submits) == submits_after_first
    # Completed specs are terminal; nothing new is ticked.
    assert len(lab.ticks) == ticks_after_first
    assert second["metrics"]["candidate_scores"] == first["metrics"]["candidate_scores"]
    assert second["metadata"]["complete"] is True


def test_model_binding_in_candidate_tree_refuses(tmp_path: Path) -> None:
    evaluator, lab, _runner, _root = _lab(tmp_path)
    poisoned = {
        "terminus/AGENTS.md": "# rules\n",
        "terminus/config.json": json.dumps({"model_name": "gpt-9", "temperature": 0.2}),
    }
    with pytest.raises(LabGateError, match="model/transport binding"):
        evaluator.prepare("cand-poison", poisoned, CURRENT_FILES, (TASK_A, TASK_B))
    assert lab.submits == []


# ---------------------------------------------------------------------------
# environment isolation
# ---------------------------------------------------------------------------

def test_child_environment_excludes_reef_runtime_but_keeps_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    venv_bin = tmp_path / "lab" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    python = venv_bin / "python"
    monkeypatch.setenv("PYTHONPATH", "/Users/x/Developer/reef")
    monkeypatch.setenv("VIRTUAL_ENV", "/Users/x/Developer/reef/.venv")
    monkeypatch.setenv("ZAI_OPENAPI_API_KEY", "sk-secret")
    env = SubprocessRunner()._env(python)
    assert "PYTHONPATH" not in env
    assert "VIRTUAL_ENV" not in env
    assert env["ZAI_OPENAPI_API_KEY"] == "sk-secret"
    assert env["PATH"].startswith(str(venv_bin))


def test_config_rejects_unknown_keys_and_wrong_agent() -> None:
    with pytest.raises(LabGateError, match="unknown lab config keys"):
        LabConfig.from_dict({
            "lab_root": "/lab", "split_path": "split.json",
            "model": MODEL, "record_dir": "/rec", "surprise": 1,
        })
    with pytest.raises(LabGateError, match="terminus-2"):
        LabConfig.from_dict({
            "lab_root": "/lab", "split_path": "s.json",
            "model": MODEL, "record_dir": "/rec", "agent": "oracle",
        })
    with pytest.raises(LabGateError, match="complete lowercase commit"):
        LabConfig.from_dict({
            "lab_root": "/lab", "split_path": "s.json",
            "model": MODEL, "record_dir": "/rec", "reef_commit": "HEAD",
        })
