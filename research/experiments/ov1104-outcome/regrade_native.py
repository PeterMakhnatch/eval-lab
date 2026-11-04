"""Re-grade recorded MiMo passes from their final patch in a fresh pristine container.

The native MiMo grader runs inside the agent's own container (``verifier_environment_mode =
"shared"``), so a reward of 1 can come from container state the patch does not carry
(shadowed binaries, ``.pth`` hooks, stale ``.pyc``) or from test-infra edits. This harness
takes the recorded ``verifier/agent.diff`` (``git add -A && git diff --cached BASE``, written by
the native ``tests/test.sh`` before scoring), applies it on the host to the pristine base files
and grades it on a fresh snapshot (``evallab.ref_subset.Sandbox``: image + setup, one throwaway
container per grade, no network) under two graders:

* ``fresh_native`` (A): the task's own hidden tests, exit code, text patch only.
* ``pristine_kept`` (B): A plus the separate-verifier kept-change rules (test-infra, hidden-test,
  binary-artifact and new-infra paths dropped; runner-config hunks reverted; tamper signatures on
  added lines grade 0 without running), ``PYTHONDONTWRITEBYTECODE=1`` and a structured output
  check for the detected runner.

Per task the reference fix (``oracle``) and the empty submission (``nop``) run first on the same
harness; a task whose controls fail is ``harness-invalid`` and every run on it abstains.

A regrade reward is never the trial's reward: the receipt carries both, bound to the patch digest,
the image id and this script's digest.

Usage (from the eval-lab worktree, local Docker, $0)::

    .venv/bin/python research/experiments/ov1104-outcome/regrade_native.py \
        --runs /tmp/nov2-line/SecondBatch/rewarded-dev-runs.jsonl \
        --tasks-root derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks \
        --reference-index research/experiments/mimo-reference-fixes/index.csv \
        --out /tmp/ov1104-outcome/regrade --workers 2
"""

from __future__ import annotations

import argparse
import base64
import csv
import dataclasses
import hashlib
import io
import json
import re
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any

from evallab.ref_subset import (
    Package,
    Sandbox,
    _check,
    _docker,
    _tar,
    _verify,
    apply_reference,
)
from evallab.separate_verifier import (
    V3_TAMPER_SIGNATURES,
    detect_runner,
    drop_reason,
    evaluate_unittest,
    is_binary_artifact_path,
    is_v3_new_infra,
    resolve_command_text,
    v3_config_revert_reason,
)

SCHEMA = "ov1104-outcome.regrade/v1"
GRADE_TIMEOUT = 1_500.0
TAIL_CHARS = 12_000
_SECTION = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.M)
_PYTEST_SUMMARY = re.compile(r"=+ (.*?) =+\s*$", re.M)
_PYTEST_PASSED = re.compile(r"\b(\d+) passed\b")
_PYTEST_BAD = re.compile(r"\b[1-9]\d* (failed|errors?)\b|\berror\b", re.I)


@dataclasses.dataclass
class Section:
    path: str
    text: str
    binary: bool
    new_file: bool
    deleted: bool

    @property
    def added(self) -> str:
        return "\n".join(
            line[1:]
            for line in self.text.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )

    @property
    def changed(self) -> str:
        return "\n".join(
            line[1:]
            for line in self.text.splitlines()
            if (line.startswith("+") or line.startswith("-"))
            and not line.startswith("+++")
            and not line.startswith("---")
        )


def split_sections(patch: str) -> list[Section]:
    starts = [m.start() for m in _SECTION.finditer(patch)]
    out: list[Section] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(patch)
        text = patch[start:end]
        header = _SECTION.match(text)
        assert header is not None
        path = header.group(2)
        out.append(
            Section(
                path=path,
                text=text,
                binary="\nBinary files " in text or "\nGIT binary patch" in text,
                new_file="\nnew file mode" in text,
                deleted="\ndeleted file mode" in text,
            )
        )
    return out


def hidden_command_text(package: Package) -> str:
    tests = package.path / "tests"
    command = (tests / "test_command.sh").read_text(errors="replace")
    patch = (tests / "test.patch").read_text(errors="replace")
    text = resolve_command_text(command, None, patch)
    # Opaque build-env wrappers: the real command is inside the base64 tarball.
    match = re.search(r"mimo_build_env\.tar\.gz\.b64\n(?:\+\+\+ b/mimo_build_env\.tar\.gz\.b64\n)?@@[^\n]*\n((?:\+[^\n]*\n)+)", patch)
    if match:
        blob = "".join(line[1:] for line in match.group(1).splitlines(True))
        try:
            raw = base64.b64decode("".join(blob.split()))
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
                for member in archive.getmembers():
                    if member.name.endswith("test_command.sh"):
                        handle = archive.extractfile(member)
                        if handle is not None:
                            text += "\n" + handle.read().decode("utf-8", "replace")
        except (ValueError, tarfile.TarError, OSError):
            pass
    return text


def structured_grade(runner: str, output: str, rc: int) -> tuple[int, str]:
    """Structured pass/fail for the runner; exit code fallback for silent families."""
    if rc != 0:
        return 0, "rc!=0"
    if runner == "unittest":
        return evaluate_unittest(output, rc), "unittest markers"
    if runner == "pytest":
        summaries = _PYTEST_SUMMARY.findall(output)
        summary = next((s for s in reversed(summaries) if _PYTEST_PASSED.search(s)), None)
        if summary is None:
            return 0, "pytest: no 'N passed' summary"
        if _PYTEST_BAD.search(summary):
            return 0, f"pytest: summary reports failures: {summary[:120]}"
        return 1, f"pytest: {summary[:120]}"
    return 1, f"{runner}: exit-code fallback"


class Harness:
    def __init__(self, package: Package, image: str, *, memory: str, cpus: str) -> None:
        self.package = dataclasses.replace(package, image=image)
        self.sandbox = Sandbox(self.package, memory=memory, cpus=cpus)
        self.command_text = hidden_command_text(package)
        self.runner = detect_runner(self.command_text)
        self.hidden_paths = set(package.tests)
        self.image_id = _check(
            _docker("image", "inspect", "--format", "{{.Id}}", image), "image inspect"
        ).decode().strip()
        self.lock = threading.Lock()

    def prepare(self) -> None:
        self.sandbox.prepare()

    def close(self) -> None:
        self.sandbox.close()

    def base_kinds(self, paths: list[str]) -> dict[str, str]:
        """``file`` / ``symlink`` / ``absent`` per path in the pristine snapshot workdir."""
        if not paths:
            return {}
        name = f"{self.sandbox.name}-kinds"
        script = (
            'cd "$1" && while IFS= read -r p; do '
            'if [ -L "$p" ]; then printf "symlink\\t%s\\n" "$p"; '
            'elif [ -f "$p" ]; then printf "file\\t%s\\n" "$p"; fi; done'
        )
        self.sandbox._run(self.sandbox.snapshot, name)
        try:
            out = _check(
                _docker("exec", "-i", name, "sh", "-c", script, "sh", self.package.workdir, input="\n".join(paths).encode() + b"\n"),
                "list base kinds",
            ).decode(errors="replace")
        finally:
            _docker("rm", "-f", name)
        kinds: dict[str, str] = {}
        for line in out.splitlines():
            kind, _, path = line.partition("\t")
            if path:
                kinds[path] = kind
        return kinds

    def kept_sections(self, sections: list[Section], base: dict[str, bytes | None]) -> tuple[list[Section], list[dict[str, str]]]:
        kept: list[Section] = []
        drops: list[dict[str, str]] = []
        for section in sections:
            reason = drop_reason(section.path, self.hidden_paths)
            if reason is None and is_binary_artifact_path(section.path):
                reason = "binary-artifact"
            if reason is None and is_v3_new_infra(section.path, in_base=base.get(section.path) is not None):
                reason = "new-infra"
            if reason is None:
                revert = v3_config_revert_reason(section.path, section.changed)
                if revert is not None:
                    reason = f"config-revert: {revert}"
            if reason is None:
                kept.append(section)
            else:
                drops.append({"path": section.path, "reason": reason})
        return kept, drops

    def grade(self, label: str, files: dict[str, bytes | None], *, nobytecode: bool) -> dict[str, Any]:
        """One throwaway container from the snapshot; mirrors ``Sandbox.grade`` plus env control."""
        name = f"{self.sandbox.name}-{label[:20]}-{int(time.time() * 1000) % 100000}"
        started = time.monotonic()
        workdir = self.package.workdir
        try:
            self.sandbox._run(self.sandbox.snapshot, name)
            gone = [path for path, data in files.items() if data is None]
            present = {
                path: (data, self.sandbox.modes.get(path, 0o644))
                for path, data in files.items()
                if data is not None
            }
            if gone:
                _check(_docker("exec", name, "rm", "-f", "--", *[f"{workdir}/{p}" for p in gone]), "remove files")
            if present:
                _check(_docker("cp", "-", f"{name}:{workdir}", input=_tar(present)), "write files")
            if nobytecode:
                _docker("exec", name, "sh", "-c", f"find {workdir} -name __pycache__ -type d -prune -exec rm -rf {{}} + 2>/dev/null; true")
            else:
                stale = [p for p in files if p.endswith(".py")]
                if stale:
                    pycache = " ".join(
                        f"'{workdir}/{PurePosixPath(p).parent}/__pycache__/{PurePosixPath(p).stem}'.*.pyc"
                        for p in stale
                    )
                    _docker("exec", name, "sh", "-c", f"rm -f {pycache}")
            options = ["-e", "PYTHONDONTWRITEBYTECODE=1"] if nobytecode else []
            grade = _verify(name, label, [], GRADE_TIMEOUT, started, options, tail_chars=TAIL_CHARS)
            return {
                "reward": grade.reward,
                "rc": grade.rc,
                "status": grade.status,
                "seconds": round(grade.seconds, 1),
                "tail": grade.tail,
            }
        except (RuntimeError, OSError) as exc:
            return {"reward": None, "rc": None, "status": "error", "seconds": round(time.monotonic() - started, 1), "tail": str(exc)[-2000:]}
        finally:
            _docker("rm", "-f", name)

    def regrade_patch(self, label: str, patch: str) -> dict[str, Any]:
        """Both graders on one patch. Serialized per task (one snapshot, sequential containers)."""
        with self.lock:
            sections = split_sections(patch)
            binary_dropped = [s.path for s in sections if s.binary]
            text_sections = [s for s in sections if not s.binary]
            kinds = self.base_kinds([s.path for s in text_sections])
            # @4 rule: the agent delta is against the complete post-setup pristine workdir, so a
            # "new file" that already exists untracked in the image (baked .venv, symlinks) is
            # not an agent change. Identical content or a symlink target drops; different content
            # is kept as an overwrite.
            baked: list[dict[str, str]] = []
            kept_text: list[Section] = []
            for section in text_sections:
                kind = kinds.get(section.path, "absent")
                if section.new_file and kind == "symlink":
                    baked.append({"path": section.path, "reason": "baked-untracked symlink"})
                elif section.new_file and kind == "file":
                    baked.append({"path": section.path, "reason": "baked-untracked file (kept as overwrite)"})
                    kept_text.append(section)
                else:
                    kept_text.append(section)
            text_sections = kept_text
            paths = [s.path for s in text_sections]
            base = self.sandbox.read(paths) if paths else {}
            for section in text_sections:
                if section.new_file and base.get(section.path) is not None:
                    base[section.path] = None  # apply the new-file hunk; the grade overwrites the baked copy
            receipt: dict[str, Any] = {
                "label": label,
                "patch_sha256": hashlib.sha256(patch.encode("utf-8", "surrogateescape")).hexdigest(),
                "patch_files": len(sections),
                "binary_dropped": binary_dropped,
                "baked_untracked": baked,
                "runner": self.runner,
            }
            # Grader A: fresh native, text patch only.
            try:
                files_a = apply_reference("".join(s.text for s in text_sections), base) if text_sections else {}
            except ValueError as exc:
                receipt["fresh_native"] = {"reward": None, "status": "patch_unreconstructible", "tail": str(exc)[-1000:]}
                files_a = None
            if files_a is not None:
                receipt["fresh_native"] = self.grade(f"{label}-a", files_a, nobytecode=False)
            # Grader B: kept change + gates + structured grading.
            kept, drops = self.kept_sections(text_sections, base)
            receipt["kept_drops"] = drops
            added = "\n".join(s.added for s in kept)
            signature = next((sig for sig in V3_TAMPER_SIGNATURES if re.search(sig, added)), None)
            receipt["tamper_signature"] = signature
            if signature is not None:
                receipt["pristine_kept"] = {"reward": 0.0, "status": "scored", "rc": None, "seconds": 0.0, "tail": f"tamper signature gate: {signature}", "structured": "tamper gate"}
            else:
                try:
                    files_b = apply_reference("".join(s.text for s in kept), {p: base.get(p) for p in (s.path for s in kept)}) if kept else {}
                except ValueError as exc:
                    files_b = None
                    receipt["pristine_kept"] = {"reward": None, "status": "patch_unreconstructible", "tail": str(exc)[-1000:]}
                if files_b is not None:
                    grade = self.grade(f"{label}-b", files_b, nobytecode=True)
                    if grade["status"] == "scored":
                        structured, why = structured_grade(self.runner, grade["tail"], int(grade["rc"] or 0))
                        grade["native_reward"] = grade["reward"]
                        grade["reward"] = float(min(structured, int(grade["reward"] or 0)))
                        grade["structured"] = why
                    receipt["pristine_kept"] = grade
            return receipt


def load_reference(index: Path, task_id: str) -> Path | None:
    with index.open() as handle:
        for row in csv.DictReader(handle):
            if row["task_id"] == task_id and row.get("label") == "oracle:pass+nop:fail":
                return Path(row["patch_path"])
    return None


def run_task(task_id: str, runs: list[dict[str, Any]], args: argparse.Namespace, out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    package = Package.load(args.tasks_root / task_id)
    image = args.image_override.get(task_id) or package.image
    harness = Harness(package, image, memory=args.memory, cpus=args.cpus)
    record: dict[str, Any] = {
        "schema": SCHEMA,
        "task_id": task_id,
        "image": image,
        "image_id": harness.image_id,
        "declared_image": package.image,
        "runner": harness.runner,
        "hidden_paths": sorted(harness.hidden_paths),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "controls": {},
        "runs": [],
    }
    started = time.monotonic()
    try:
        harness.prepare()
        record["prepare_seconds"] = round(time.monotonic() - started, 1)
        reference = load_reference(args.reference_index, task_id)
        if reference is not None and reference.is_file():
            record["controls"]["oracle"] = harness.regrade_patch("oracle", reference.read_text(errors="replace"))
        else:
            record["controls"]["oracle"] = {"status": "no_reference"}
        record["controls"]["nop"] = harness.regrade_patch("nop", "")
        oracle, nop = record["controls"]["oracle"], record["controls"]["nop"]
        valid = all(
            (ctrl.get(g) or {}).get("reward") == expected
            for ctrl, expected in ((oracle, 1.0), (nop, 0.0))
            for g in ("fresh_native", "pristine_kept")
        )
        record["harness_valid"] = valid
        (out_dir / "task.json").write_text(json.dumps(record, indent=1))
        for run in runs:
            trial = Path(run["path"])
            patch = (trial / "verifier" / "agent.diff").read_text(errors="replace")
            original = (trial / "verifier" / "reward.txt").read_text().strip()
            receipt = harness.regrade_patch(run["id"][:8], patch)
            receipt.update({"run_id": run["id"], "model": run["model"], "original_reward": float(original) if original else None, "trial": str(trial)})
            if args.flake_guard and receipt["original_reward"] == 1.0 and (receipt.get("pristine_kept") or {}).get("reward") == 0.0:
                receipt["repeat"] = harness.regrade_patch(run["id"][:8] + "-r", patch)
            record["runs"].append(receipt)
            (out_dir / "task.json").write_text(json.dumps(record, indent=1))
    finally:
        harness.close()
    record["seconds"] = round(time.monotonic() - started, 1)
    (out_dir / "task.json").write_text(json.dumps(record, indent=1))
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--tasks-root", type=Path, required=True)
    parser.add_argument("--reference-index", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tasks", help="comma-separated task ids (default: all in --runs)")
    parser.add_argument("--only-rewarded", action="store_true", help="regrade only original reward 1 runs")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--memory", default="3g")
    parser.add_argument("--cpus", default="2")
    parser.add_argument("--no-flake-guard", dest="flake_guard", action="store_false")
    parser.add_argument("--image", action="append", default=[], help="task_id=image override")
    args = parser.parse_args()
    args.image_override = dict(item.split("=", 1) for item in args.image)
    rows = [json.loads(line) for line in args.runs.read_text().splitlines() if line.strip()]
    by_task: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if args.only_rewarded and (Path(row["path"]) / "verifier" / "reward.txt").read_text().strip() != "1":
            continue
        by_task.setdefault(row["task_id"], []).append(row)
    wanted = args.tasks.split(",") if args.tasks else sorted(by_task)
    args.out.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_task, task, by_task.get(task, []), args, args.out / task): task for task in wanted}
        for future, task in futures.items():
            try:
                record = future.result()
                print(json.dumps({"task": task, "valid": record.get("harness_valid"), "runs": len(record["runs"]), "seconds": record.get("seconds")}), flush=True)
            except Exception as exc:  # noqa: BLE001 - one task's failure must not hide the others
                (args.out / task / "error.txt").parent.mkdir(parents=True, exist_ok=True)
                (args.out / task / "error.txt").write_text(repr(exc))
                print(json.dumps({"task": task, "error": repr(exc)[:500]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
