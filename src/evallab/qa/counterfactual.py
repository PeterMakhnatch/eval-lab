"""Counterfactual regrade for QA Trace T3: replay a stored pass against newer graders.

A *newer grader* is a lineage-recorded package for the same task: the
``separate-verifier@10`` variant of the task's MiMo source package (derived on
demand with the existing transform, :func:`v10_grader`) and any
``verifier-repair@*`` variant already recorded for it (:func:`repaired_graders`).

The stored pass replays through native Harbor ``trial regrade``
(:func:`evallab.regrade.regrade_trial`) from a scratch copy of the trial
(:func:`stage_replay`); the source trial is never touched:

- **workspace**: the recorded separate-verifier snapshot when the trial has
  one; otherwise a snapshot rebuilt from ``verifier/agent.diff``: the task
  image, the task's own setup, ``git apply`` of the recorded diff, then
  ``tar`` of the workdir without ``.git``. That archive is exactly the bytes a
  separate verifier takes from an agent; the verifier re-derives the base,
  drops test infrastructure and applies the hidden tests itself.
- **trajectory**: the recorded ``agent/trajectory.json`` as the declared
  ``/logs/agent/trajectory.json`` artifact, else a marked empty placeholder.
  Only the integrity dimensions read it; this module reads ``reward`` only.

A grader is **valid** when an empty diff scores 0 and the task's reference fix
(``research/experiments/mimo-reference-fixes``), when one exists, scores 1
through the same replay (:func:`grader_controls`). Per pass the outcome is
``stays`` (reward unchanged), ``flips`` (lower), ``loosened`` (higher),
``refused`` (typed reason) or ``not_a_pass``. Each replay holds one of
:data:`DOCKER_SLOTS` for its snapshot rebuild and verifier container, so at
most two containers run at once.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
import tempfile
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

V10 = "separate-verifier@10"
REPAIR_PREFIX = "verifier-repair@"
MIMO_DATASET = "mimo-v2.6-rl"
SNAPSHOT_ARCHIVE = "workspace.tgz"
TRAJECTORY_PLACEHOLDER = {
    "schema_version": "ATIF-v1.6",
    "session_id": "absent",
    "agent": {"name": "absent", "version": "0"},
    "steps": [],
    "extra": {"qa_trace_placeholder": "source trial recorded no trajectory"},
}
#: Replays in flight (one container each at any moment), across threads.
DOCKER_SLOTS = threading.BoundedSemaphore(2)
REBUILD_TIMEOUT_SEC = 900
PULL_TIMEOUT_SEC = 3600


@dataclass(frozen=True)
class Grader:
    """A newer, lineage-recorded grader package for one task."""

    task_name: str
    transform: str
    variant_digest: str
    package: Path
    record: str

    @property
    def label(self) -> str:
        return f"{self.transform}:{self.variant_digest.removeprefix('sha256:')[:12]}"

    def to_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "task_name": self.task_name,
            "transform": self.transform,
            "variant_digest": self.variant_digest,
            "package": str(self.package),
            "record": self.record,
        }


def _slug(task_name: str) -> str:
    return task_name.replace("/", "__")


def mimo_source(task_name: str, repo_root: Path) -> Path | None:
    """The MiMo HF source package for ``mimo-v2.6-rl/<task>`` code tasks, if on disk."""
    from evallab.mimo_clean import SNAPSHOT_REL
    from evallab.storage.paths import shared_checkout_root

    dataset, _, base = task_name.partition("/")
    if dataset != MIMO_DATASET or not base:
        return None
    package = shared_checkout_root(repo_root) / SNAPSHOT_REL / "tasks" / base
    has_tests = (package / "tests" / "test.patch").is_file()
    return package if has_tests and (package / "tests" / "test_command.sh").is_file() else None


def _records(task_name: str, repo_root: Path) -> list[tuple[Path, Any]]:
    from evallab.task_variants import RECORDS_DIRNAME, VariantRecord

    folder = repo_root / RECORDS_DIRNAME / _slug(task_name)
    return [
        (path, VariantRecord.model_validate_json(path.read_text(encoding="utf-8")))
        for path in sorted(folder.glob("*.json"))
    ]


def _grader(task_name: str, record: Any, path: Path, repo_root: Path) -> Grader | None:
    from evallab.task_variants import default_variants_root

    package = (
        default_variants_root(repo_root)
        / _slug(task_name)
        / record.variant_digest.removeprefix("sha256:")[:12]
    )
    if not (package / "task.toml").is_file():
        return None
    return Grader(
        task_name=task_name,
        transform=record.transform,
        variant_digest=record.variant_digest,
        package=package,
        record=str(path.relative_to(repo_root)),
    )


def v10_grader(task_name: str, repo_root: Path) -> Grader | None:
    """Find or derive the ``separate-verifier@10`` package of a MiMo code task.

    Derived from the HF source package with the hidden-test marker mimo-clean
    uses; the reference fix is not bundled (controls replay it instead).
    Generated lineage records are never committed (``.gitignore``), so a fresh
    checkout may hold the shared package without its record: the derivation is
    deterministic, so a throwaway derive names the digest and the shared
    package is reused when it hashes to it.
    """
    from evallab.mimo_clean import derive_marker
    from evallab.separate_verifier import derive_separate_verifier_v10
    from evallab.task_variants import (
        RECORDS_DIRNAME,
        VariantExistsError,
        default_variants_root,
        task_directory_digest,
    )

    parent = mimo_source(task_name, repo_root)
    if parent is None:
        return None
    marker = derive_marker(
        (parent / "tests" / "test.patch").read_text(errors="replace"),
        task_name.partition("/")[2],
    )
    parent_digest = task_directory_digest(parent)

    def lookup() -> Grader | None:
        for path, record in _records(task_name, repo_root):
            if (
                record.transform == V10
                and record.parent.digest == parent_digest
                and record.inputs.get("marker") == marker
                and record.inputs.get("solution") == "absent"
            ):
                return _grader(task_name, record, path, repo_root)
        return None

    found = lookup()
    if found is not None:
        return found
    with tempfile.TemporaryDirectory(prefix="qa-trace-v10-") as scratch:
        planned = derive_separate_verifier_v10(
            parent, marker=marker, repo_root=Path(scratch), variants_root=Path(scratch) / "store"
        )
    digest12 = planned.variant_digest.removeprefix("sha256:")[:12]
    package = default_variants_root(repo_root) / _slug(task_name) / digest12
    if (package / "task.toml").is_file():
        if task_directory_digest(package) != planned.variant_digest:
            return None  # a different package under this digest prefix: never grade with it
        return Grader(
            task_name=task_name,
            transform=V10,
            variant_digest=planned.variant_digest,
            package=package,
            record=str(RECORDS_DIRNAME / _slug(task_name) / f"{digest12}.json"),
        )
    # Another worker may have derived the same package first; re-read either way.
    with contextlib.suppress(VariantExistsError):
        derive_separate_verifier_v10(parent, marker=marker, repo_root=repo_root)
    return lookup()


def repaired_graders(task_name: str, repo_root: Path) -> list[Grader]:
    """Materialized ``verifier-repair@*`` variants recorded for the task."""
    out = []
    for path, record in _records(task_name, repo_root):
        if record.transform.startswith(REPAIR_PREFIX):
            grader = _grader(task_name, record, path, repo_root)
            if grader is not None:
                out.append(grader)
    return out


def newer_graders(task_name: str, repo_root: Path) -> list[Grader]:
    v10 = v10_grader(task_name, repo_root)
    return ([v10] if v10 else []) + repaired_graders(task_name, repo_root)


def _task_env(package: Path) -> tuple[str, str]:
    env = tomllib.loads((package / "task.toml").read_text(encoding="utf-8"))["environment"]
    return env["docker_image"], env["workdir"]


def ensure_image(image: str, *, runner: Any = subprocess.run) -> str | None:
    """Pull ``image`` when it is not cached (no container runs); returns a refusal or None."""
    cached = runner(["docker", "image", "inspect", image], capture_output=True, text=True)
    if cached.returncode == 0:
        return None
    try:
        pulled = runner(
            ["docker", "pull", "--platform", "linux/amd64", image],
            capture_output=True,
            text=True,
            timeout=PULL_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return f"image_unavailable: pull exceeded {PULL_TIMEOUT_SEC}s"
    return None if pulled.returncode == 0 else f"image_unavailable: {(pulled.stderr or '')[-300:]}"


def _rebuild_snapshot(
    diff_path: Path | None, grader: Grader, snap: Path, *, runner: Any
) -> str | None:
    """Post-setup repo + recorded diff as ``workspace.tgz``; returns a refusal or None."""
    image, workdir = _task_env(grader.package)
    setup = grader.package / "environment" / "setup"
    if not (setup / "setup.sh").is_file():
        return "setup_missing: grader package has no environment/setup/setup.sh"
    pulled = ensure_image(image, runner=runner)
    if pulled:
        return pulled
    snap.mkdir(parents=True, exist_ok=True)
    script = (
        "set -e; mkdir -p /var/lib/mimo && cp -r /in/setup/. /var/lib/mimo/ "
        "&& bash /var/lib/mimo/setup.sh >/dev/null; "
        f"cd {workdir}; "
        "if [ -s /in/agent.diff ]; then git apply --whitespace=nowarn /in/agent.diff "
        "|| { echo PATCH_DOES_NOT_APPLY >&2; exit 3; }; fi; "
        f"tar --exclude=.git -czf /out/{SNAPSHOT_ARCHIVE} -C {workdir} ."
    )
    mounts = ["-v", f"{setup}:/in/setup:ro", "-v", f"{snap}:/out"]
    if diff_path is not None:
        mounts += ["-v", f"{diff_path}:/in/agent.diff:ro"]
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--platform",
        "linux/amd64",
        *mounts,
        "--entrypoint",
        "bash",
        image,
        "-c",
        script,
    ]
    try:
        done = runner(command, capture_output=True, text=True, timeout=REBUILD_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return f"snapshot_rebuild_timeout: over {REBUILD_TIMEOUT_SEC}s"
    if done.returncode == 3:
        return "patch_does_not_apply: recorded agent.diff does not apply to the task base"
    if done.returncode != 0 or not (snap / SNAPSHOT_ARCHIVE).is_file():
        return f"snapshot_rebuild_failed: {(done.stderr or '')[-300:]}"
    return None


def strip_binary_artifacts(diff: str) -> tuple[str, list[str]]:
    """Drop whole per-file stanzas for paths ``separate-verifier@10`` drops from a
    kept change (``__pycache__``, compiled objects); returns (kept diff, dropped paths).
    """
    from evallab.separate_verifier import is_binary_artifact_path

    kept: list[str] = []
    dropped: list[str] = []
    for stanza in re.split(r"(?m)^(?=diff --git )", diff):
        header = re.match(r"diff --git a/(\S+) b/(\S+)", stanza)
        if header and any(is_binary_artifact_path(p) for p in header.groups()):
            dropped.append(header.group(2))
        else:
            kept.append(stanza)
    return "".join(kept), dropped


def stage_replay(
    trial_dir: Path,
    grader: Grader,
    out: Path,
    *,
    diff_text: str | None = None,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Scratch copy of ``trial_dir`` ready for a verifier-only regrade.

    ``diff_text`` replaces the recorded change (controls: empty diff, reference
    fix). Binary artifacts are stripped first, as ``@10`` strips them. Returns
    ``{"trial", "snapshot", "trajectory", ...}`` or ``{"refused"}``.
    """
    from evallab.heldout_verifier import scan_agent_diff
    from evallab.separate_verifier import SNAP_DIR, TRAJECTORY_ARTIFACT

    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(trial_dir, out, ignore=shutil.ignore_patterns("regrade-receipt.json"))
    snap = out / "artifacts" / SNAP_DIR.lstrip("/")
    recorded_snapshot = (snap / SNAPSHOT_ARCHIVE).is_file()
    diff_path = out / "verifier" / "agent.diff"
    if diff_text is not None:
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_path.write_text(diff_text, encoding="utf-8")
    dropped: list[str] = []
    if diff_text is None and recorded_snapshot:
        snapshot = "recorded"
    elif diff_path.is_file():
        kept, dropped = strip_binary_artifacts(diff_path.read_text(errors="replace"))
        replay_diff = out / "verifier" / "agent.replay.diff"
        replay_diff.write_text(kept, encoding="utf-8")
        problems, _ = scan_agent_diff(str(replay_diff))
        if problems:
            return {"refused": f"diff_unreplayable: {problems[0]}"}
        shutil.rmtree(snap, ignore_errors=True)
        refusal = _rebuild_snapshot(replay_diff if kept else None, grader, snap, runner=runner)
        if refusal:
            return {"refused": refusal}
        snapshot = "rebuilt_from_agent_diff"
    else:
        return {"refused": "source_patch_unavailable: no recorded snapshot and no agent.diff"}
    trajectory_out = out / "artifacts" / TRAJECTORY_ARTIFACT.lstrip("/")
    trajectory_out.parent.mkdir(parents=True, exist_ok=True)
    recorded_trajectory = trial_dir / "agent" / "trajectory.json"
    if recorded_trajectory.is_file() and diff_text is None:
        shutil.copyfile(recorded_trajectory, trajectory_out)
        trajectory = "recorded"
    else:
        trajectory_out.write_text(json.dumps(TRAJECTORY_PLACEHOLDER), encoding="utf-8")
        trajectory = "placeholder"
    manifest_path = out / "artifacts" / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else []
    manifest = [m for m in manifest if m.get("source") not in (SNAP_DIR, TRAJECTORY_ARTIFACT)]
    # Publishing drops empty directories; Harbor then calls the trial incomplete.
    # The graders read only the snapshot and trajectory, so recreate those dirs
    # empty (recorded in the row); a missing collected *file* stays a refusal.
    recreated = []
    for entry in manifest:
        target = out / str(entry.get("destination", ""))
        if entry.get("status") != "ok" or target.exists():
            continue
        if entry.get("type") != "directory":
            return {"refused": f"source_artifact_missing: {entry.get('source')}"}
        target.mkdir(parents=True)
        recreated.append(entry.get("source"))
    for source, kind in ((SNAP_DIR, "directory"), (TRAJECTORY_ARTIFACT, "file")):
        manifest.append(
            {
                "source": source,
                "destination": f"artifacts{source}",
                "type": kind,
                "status": "ok",
                "service": None,
                "exclude": [],
            }
        )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return {
        "trial": out,
        "snapshot": snapshot,
        "trajectory": trajectory,
        "recreated_empty_dirs": recreated,
        "dropped_binary_artifacts": dropped,
    }


def _reward(observation: Any) -> float | None:
    if observation is None:
        return None
    value = (observation.rewards or {}).get("reward")
    return float(value) if isinstance(value, int | float) else None


def replay(
    trial_dir: Path,
    grader: Grader,
    work: Path,
    *,
    diff_text: str | None = None,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Stage and regrade one trial against ``grader``; ``reward`` dimension only."""
    from evallab.regrade import regrade_trial

    row: dict[str, Any] = {"grader": grader.label, "trial": str(trial_dir)}
    with DOCKER_SLOTS:
        staged = stage_replay(
            trial_dir, grader, work / "source" / trial_dir.name, diff_text=diff_text, runner=runner
        )
        if "refused" in staged:
            return {**row, "outcome": "refused", "refusals": [staged["refused"]]}
        trials_dir = work / "regrades"
        shutil.rmtree(trials_dir, ignore_errors=True)
        try:
            receipt = regrade_trial(
                trial_dir=staged["trial"], task_dir=grader.package, trials_dir=trials_dir
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return {**row, "outcome": "refused", "refusals": [f"regrade_error: {str(exc)[:300]}"]}
    recorded, regraded = _reward(receipt.recorded), _reward(receipt.regraded)
    row.update(
        snapshot=staged["snapshot"],
        trajectory=staged["trajectory"],
        verdict=str(receipt.verdict.value),
        recorded=recorded,
        regraded=regraded,
        refusals=[str(code.value) for code in receipt.refusals],
        regrade_dir=receipt.regrade_trial_dir,
        recreated_empty_dirs=staged["recreated_empty_dirs"],
        dropped_binary_artifacts=staged["dropped_binary_artifacts"],
        harbor_output_tail=(
            (receipt.execution.stdout_tail + receipt.execution.stderr_tail)[-600:]
            if receipt.execution and receipt.refusals
            else None
        ),
    )
    if receipt.refusals or regraded is None or recorded is None:
        row["outcome"] = "refused"
    elif diff_text is None and recorded < 1.0:
        row["outcome"] = "not_a_pass"
    elif regraded == recorded:
        row["outcome"] = "stays"
    else:
        row["outcome"] = "flips" if regraded < recorded else "loosened"
    return row


def reference_patch(task_name: str, repo_root: Path) -> str | None:
    """The validated reference fix (oracle pass + nop fail) for a MiMo task, if any."""
    from evallab.mimo_clean import REFERENCE_INDEX_REL, load_reference_index, oracle_info_from_index

    index_path = repo_root / REFERENCE_INDEX_REL
    if not index_path.is_file() or not task_name.startswith(f"{MIMO_DATASET}/"):
        return None
    info = oracle_info_from_index(
        task_name.partition("/")[2], load_reference_index(index_path), repo_root=repo_root
    )
    return info.patch_bytes.decode("utf-8", "replace") if info else None


def grader_controls(
    grader: Grader,
    shell_trial: Path,
    work: Path,
    *,
    repo_root: Path,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Empty-diff and reference-fix replays through ``grader`` (cached in ``work``).

    ``shell_trial`` is any recorded trial of the task; only its harness files
    are reused, its change is replaced. Valid = nop scores 0 and the reference
    fix, when one exists, scores 1.
    """
    cache = work / "controls.json"
    if cache.is_file():
        cached = json.loads(cache.read_text())
        if cached.get("grader") == grader.label:
            return cached
    nop = replay(shell_trial, grader, work / "nop", diff_text="", runner=runner)
    patch = reference_patch(grader.task_name, repo_root)
    reference = (
        replay(shell_trial, grader, work / "reference", diff_text=patch, runner=runner)
        if patch is not None
        else None
    )
    nop_score = nop.get("regraded")
    reference_score = reference.get("regraded") if reference else None
    controls = {
        "grader": grader.label,
        "nop": nop_score,
        "reference": reference_score,
        "reference_available": patch is not None,
        "valid": nop_score == 0.0 and (reference is None or reference_score == 1.0),
        "rows": {"nop": nop, "reference": reference},
    }
    work.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(controls, indent=1, default=str))
    return controls
