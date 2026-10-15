"""Repair weak MiMo verifiers flagged by the ref-subset probe (QA Task S2).

The ref-subset probe (``evallab.ref_subset``) proves a verifier is weak by
exhibiting a *shortcut submission*: a proper subset of the reference fix that
still scores reward 1 (``minimal.patch`` in the report directory, plus every
intermediate drop-file / drop-block grade that passed). This module turns
such a report into a test-only task variant and re-grades it under the
2026-10-15 acceptance rule:

* the full reference fix passes **3 of 3** grades,
* the do-nothing (base) submission fails **3 of 3** grades, and
* **every** shortcut submission that passed before now fails.

Anything else is ``rejected`` (controls broke, or a shortcut still escapes),
``unrepairable`` (the proposer declines, with reasons), or ``blocked``
(infrastructure / unscored grades). When every escaping shortcut was declared
equivalent or out-of-scope with a per-ID reason (a build-config or test-fixture
file the probe counted as code, say), the genuine tests are kept and the verdict
is ``partial`` — reported, never silently ``repaired``.

Test additions come from the ChatGPT-subscription Codex proposer shared with
``verifier_repair`` (same proposal schema, same fingerprint bans, same quota
gate: ordinary usage allowed and weekly consumption at most 60%). Grades run
in the ref-subset sandbox: one throwaway Docker container at a time, no
network. Original task packages are never edited; repairs are derived
variants via ``tasks derive``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from evallab.schemas import ContractModel

SCHEMA_RESULT = "evallab.ref_repair.result/v1"

#: Grades of the full fix and of the do-nothing submission required by the
#: 2026-10-15 acceptance rule.
CONTROL_REPETITIONS = 3

RefRepairVerdict = Literal["repaired", "partial", "rejected", "unrepairable", "blocked"]


class RefRepairError(ValueError):
    """A ref-repair step violated its contract; fail closed."""


# --------------------------------------------------------------------------- #
# Contracts
# --------------------------------------------------------------------------- #


class RefRepairCheck(ContractModel):
    """One sandbox grade of the repaired verifier."""

    name: str
    role: Literal["reference", "nop", "shortcut"]
    status: str
    reward: float | None = None
    seconds: float | None = None
    error: str | None = None


class RefRepairResult(ContractModel):
    """Durable outcome of one ref-repair attempt."""

    schema_: Literal["evallab.ref_repair.result/v1"] = Field(alias="schema", default=SCHEMA_RESULT)
    task: str = ""
    verdict: RefRepairVerdict = "blocked"
    reason: str = ""
    variant_digest: str | None = None
    variant_record: str | None = None
    variant_package: str | None = None
    declared_unrepairable: dict[str, str] = Field(default_factory=dict)
    checks: list[RefRepairCheck] = Field(default_factory=list)


@dataclass(frozen=True)
class Shortcut:
    """A previously passing partial submission.

    Either a proper subset of the reference fix (``keep`` blocks, from the
    ref-subset probe) or a real agent partial fix (``patch``: the trial's
    ``verifier/agent.diff`` against the task base).
    """

    id: str
    label: str
    keep: frozenset[str] = frozenset()
    patch: str | None = None


# --------------------------------------------------------------------------- #
# Report intake: which submissions passed before?
# --------------------------------------------------------------------------- #


def _shortcut_id(label: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-").lower()
    return slug[:64] or "shortcut"


def load_shortcuts(report: Mapping[str, Any]) -> list[Shortcut]:
    """Every proper subset of the reference fix that scored reward 1.

    The reproducer is the minimised ``keep`` set from the report; each
    intermediate ``drop-file:`` / ``drop-block:`` grade that passed is also a
    concrete partial submission, so it joins the acceptance set. The full
    fix and the confirmation grade are controls, not shortcuts. A report
    without a ``fail`` verdict has none (the probe found every block needed).
    """
    if report.get("verdict") != "fail":
        return []
    blocks = [b for b in report.get("blocks", []) if isinstance(b, dict)]
    all_ids = {str(b["id"]) for b in blocks}
    if not all_ids:
        raise RefRepairError("ref-subset report carries no blocks")
    shortcuts: dict[frozenset[str], Shortcut] = {}

    def add(label: str, keep: frozenset[str]) -> None:
        keep = frozenset(keep)
        if keep and keep != all_ids:
            shortcuts.setdefault(keep, Shortcut(_shortcut_id(label), label, keep))

    kept = report.get("kept")
    if isinstance(kept, list) and kept:
        add("minimal", frozenset(str(k) for k in kept))
    for grade in report.get("grades", []):
        if not isinstance(grade, dict) or grade.get("reward") != 1.0:
            continue
        label = str(grade.get("label") or "")
        if label.startswith("drop-block:"):
            add(label, all_ids - {label.removeprefix("drop-block:")})
        elif label.startswith("drop-file:"):
            path = label.removeprefix("drop-file:")
            add(label, all_ids - {str(b["id"]) for b in blocks if b.get("path") == path})
    return sorted(shortcuts.values(), key=lambda s: s.label)


def load_partial_fixes(partial_fixes: Sequence[tuple[str, Path]]) -> list[Shortcut]:
    """Real partial-fix submissions: each ``verifier/agent.diff`` that earned reward 1."""
    shortcuts: list[Shortcut] = []
    for label, path in partial_fixes:
        try:
            patch = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise RefRepairError(f"cannot read partial fix {label}: {exc}") from exc
        if not patch.strip():
            raise RefRepairError(f"partial fix {label} is empty")
        shortcuts.append(Shortcut(_shortcut_id(f"partial-{label}"), label, patch=patch))
    return shortcuts


def load_ref_report(
    report_dir: Path,
    *,
    package: Path | None = None,
    reference_patch: Path | None = None,
) -> dict[str, Any]:
    """Load a ref-subset report directory, or synthesize one for a report-less task.

    A report-less task needs an explicit package and reference patch; it has
    no blocks, so only real partial-fix submissions can be its shortcuts.
    """
    path = Path(report_dir) / "report.json"
    if not path.is_file():
        if package is None or reference_patch is None:
            raise RefRepairError(
                f"no ref-subset report at {path}; pass --package and --reference-patch"
            )
        report: dict[str, Any] = {
            "task_id": Path(package).name,
            "verdict": "unknown",
            "package": str(package),
            "reference": {"patch": str(reference_patch)},
            "blocks": [],
            "grades": [],
        }
    else:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RefRepairError(f"cannot load ref-subset report {path}: {exc}") from exc
    if not Path(str(report.get("package") or "")).is_dir():
        raise RefRepairError(f"report package is missing: {report.get('package')}")
    return report


# --------------------------------------------------------------------------- #
# Proposal context and prompt
# --------------------------------------------------------------------------- #


@dataclass
class RefRepairPlan:
    """Preview artefacts: context and prompt, no model call, no spend."""

    report_dir: Path
    report: dict[str, Any]
    package_dir: Path
    shortcuts: list[Shortcut]
    context: dict[str, Any]
    prompt: str


def _bounded(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[truncated]\n"


def plan_ref_repair(
    report_dir: Path,
    *,
    feedback: Path | None = None,
    partial_fixes: Sequence[tuple[str, Path]] = (),
    package: Path | None = None,
    reference_patch: Path | None = None,
) -> RefRepairPlan:
    """Prepare the proposer context for one flagged task without calling a model."""
    from evallab.verifier_repair import read_task_workdir

    report = load_ref_report(report_dir, package=package, reference_patch=reference_patch)
    package = Path(str(report["package"]))
    shortcuts = load_shortcuts(report) + load_partial_fixes(partial_fixes)
    if not shortcuts:
        raise RefRepairError(
            f"nothing to repair: ref-subset verdict is {report.get('verdict')!r} with no "
            "passing shortcut, and no --partial-fix was given"
        )
    try:
        instruction = (package / "instruction.md").read_text(encoding="utf-8")
        test_patch = (package / "tests" / "test.patch").read_text(encoding="utf-8")
        test_command = (package / "tests" / "test_command.sh").read_text(encoding="utf-8")
    except OSError as exc:
        raise RefRepairError(f"parent package is incomplete: {exc}") from exc
    dropped = ""
    dropped_path = Path(report_dir) / "dropped.patch"
    if dropped_path.is_file():
        dropped = dropped_path.read_text(encoding="utf-8", errors="replace")
    reference_text = ""
    if any(s.patch for s in shortcuts):
        ref_path = Path(str((report.get("reference") or {}).get("patch") or ""))
        try:
            reference_text = ref_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise RefRepairError(f"reference patch is missing: {exc}") from exc
    context: dict[str, Any] = {
        "task": report.get("task_id"),
        "workdir": read_task_workdir(package),
        "instruction": instruction,
        "test_command": test_command.strip(),
        "hidden_test_patch": _bounded(test_patch, 32_768),
        "dropped_patch": _bounded(dropped, 16_384),
        "reference_patch": _bounded(reference_text, 16_384),
        "shortcuts": [
            {
                "id": s.id,
                "label": s.label,
                "kept_blocks": sorted(s.keep),
                "patch": _bounded(s.patch, 8_192) if s.patch else None,
            }
            for s in shortcuts
        ],
        "dropped_blocks": [str(b["id"]) for b in report.get("blocks", []) if isinstance(b, dict)],
    }
    if feedback is not None:
        try:
            prior = json.loads(Path(feedback).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RefRepairError(f"cannot load feedback {feedback}: {exc}") from exc
        context["feedback"] = _bounded(json.dumps(_compact_feedback(prior), indent=1), 16_384)
    return RefRepairPlan(
        report_dir=Path(report_dir),
        report=report,
        package_dir=package,
        shortcuts=shortcuts,
        context=context,
        prompt=render_ref_repair_prompt(context),
    )


def _compact_feedback(prior: Mapping[str, Any]) -> dict[str, Any]:
    """Keep each distinct failing output once: reruns of one grade print the same log."""
    compact = dict(prior)
    seen: set[tuple[str, str]] = set()
    checks = []
    for check in prior.get("checks", []):
        entry = dict(check)
        error = str(entry.get("error") or "")
        if error:
            key = (str(entry.get("role")), error)
            if key in seen:
                entry["error"] = "(same output as an earlier check of this role)"
            seen.add(key)
        checks.append(entry)
    if checks:
        compact["checks"] = checks
    return compact


def render_ref_repair_prompt(context: Mapping[str, Any]) -> str:
    """Prompt the model for semantic tests that kill every listed shortcut."""
    from evallab.verifier_repair import MAX_TESTS, SCHEMA_PROPOSAL

    lines = [
        "You are hardening a benchmark task's hidden tests. A probe proved the",
        "current tests weak: each SHORTCUT below is a partial fix (a proper",
        "subset of the reference solution, or a real agent submission that",
        "fixed only part of the issue) that still scores full reward.",
        "Write NEW semantic tests that pass on the full reference solution and",
        "fail on every shortcut. Tests must follow from the task instruction,",
        "not from the reference patch's incidental choices. Assert only",
        "behaviour the instruction states.",
        "",
        "Output exactly one JSON object, no markdown fences, with the",
        "proposal schema: {schema, repairable, rationale, tests, command,",
        "model, mutant_ids, unrepairable}. Each test: {id, repo_path, code}.",
        "Each unrepairable entry: {mutant_id, kind, reason} with kind",
        "equivalent or out-of-scope. Here mutant_ids / mutant_id refer to the",
        "SHORTCUT ids listed below: every shortcut needs a disposition.",
        f"The schema field is exactly {SCHEMA_PROPOSAL!r}.",
        "The model field may be null. Command may be null only when",
        "repairable is false and no tests will run.",
        "Rules:",
        "- Test observable behaviour required by the instruction only.",
        "- NEVER inspect source or patch fingerprints: no __file__, inspect,",
        "  hashlib, .patch reads, solve.sh reads, or subprocess/network use.",
        "- Do not pin incidental properties (immutability, annotations,",
        "  logging, formatting) unless the instruction requires them.",
        "- Keep tests offline with synthetic fixtures; never load models.",
        "- repo_path is a NEW repository Python test file named test_*.py,",
        f"  never a file the hidden patch already carries. At most {MAX_TESTS} files.",
        "- command runs every new test file with the task Python interpreter:",
        '  "python -m pytest path/test_repair_x.py -q" or "python -m unittest',
        '  path/test_repair_x.py" (the file path, exactly as in repo_path); it',
        "  must name each repo_path,",
        "  stay under 300 chars, no shell metacharacters. pytest may be absent",
        "  from the task image: unless the existing test command above itself",
        "  runs pytest, write unittest.TestCase tests and use -m unittest. If",
        "  the project needs framework setup (e.g. Django settings), do it",
        "  inside the test module (os.environ.setdefault + django.setup()).",
        "",
        f"Task: {context.get('task')}",
        f"Testbed working directory: {context.get('workdir')}",
        f"Test command: {context.get('test_command')}",
        "",
        "=== instruction.md ===",
        str(context.get("instruction") or ""),
        "",
        "=== behaviour the block-drop shortcuts omit (dropped.patch: minimal -> full) ===",
        str(context.get("dropped_patch") or ""),
    ]
    if context.get("reference_patch"):
        lines += ["", "=== reference solution patch ===", str(context["reference_patch"])]
    for shortcut in context.get("shortcuts", []):  # type: ignore[union-attr]
        lines += ["", f"=== shortcut {shortcut['id']} ({shortcut['label']}) ==="]
        if shortcut.get("patch"):
            lines += ["real agent submission (diff against the task base):", shortcut["patch"]]
        else:
            lines.append(f"kept blocks: {', '.join(shortcut['kept_blocks'])}")
    if context.get("feedback"):
        lines += ["", "=== prior attempt (fix these failures) ===", str(context["feedback"])]
    lines += [
        "",
        "=== existing hidden tests (tests/test.patch, bounded) ===",
        str(context.get("hidden_test_patch") or ""),
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Validation in the ref-subset sandbox
# --------------------------------------------------------------------------- #


def _all_block_ids(report: Mapping[str, Any]) -> set[str]:
    return {str(b["id"]) for b in report.get("blocks", []) if isinstance(b, dict)}


#: Artefacts an agent leaves in its diff that are not part of any fix attempt.
_JUNK_PATH = re.compile(
    r"(^|/)(\.venv|venv|node_modules|__pycache__|\.pytest_cache|\.git|\.mypy_cache|\.ruff_cache)/"
    r"|\.(pyc|pyo|log)$"
)


def source_only_patch(patch: str) -> str:
    """Drop environment/log artefacts from an agent diff; keep every real source edit."""
    blocks = re.split(r"(?m)^(?=diff --git )", patch)
    kept = []
    for block in blocks:
        match = re.match(r"diff --git a/(.+?) b/(.+)", block)
        if match and _JUNK_PATH.search(match.group(2)):
            continue
        kept.append(block)
    return "".join(kept)


def grade_keep_sets(
    *,
    parent_dir: Path,
    variant_dir: Path,
    report: Mapping[str, Any],
    shortcuts: Sequence[Shortcut],
    repetitions: int = CONTROL_REPETITIONS,
    grade_timeout: float = 1_800,
    log: Callable[[str], None] = print,
) -> list[RefRepairCheck]:
    """Grade full (xN), nop (xN) and each shortcut (x1) on the variant.

    Uses two ref-subset sandboxes in sequence (parent to read the base
    files, variant to grade), so at most one Docker container exists at a
    time. Unscored / timed-out grades stay visible as non-scored checks.
    """
    from evallab.ref_subset import (
        Package,
        Sandbox,
        apply_reference,
        compose,
        file_change,
        patch_paths,
    )

    patch_path = Path(str((report.get("reference") or {}).get("patch") or ""))
    try:
        patch = patch_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RefRepairError(f"reference patch is missing: {exc}") from exc
    del parent_dir  # the variant snapshot's testbed already holds the base files
    variant = Package.load(variant_dir)
    checks: list[RefRepairCheck] = []

    def files_for(keep: set[str]) -> dict[str, Any]:
        return {c.path: compose(c, keep) for c in changes if c.path not in variant.tests}

    def files_for_patch(shortcut_patch: str) -> dict[str, Any]:
        cleaned = source_only_patch(shortcut_patch)
        paths = patch_paths(cleaned)
        base_files = box.read(paths)
        try:
            applied = apply_reference(cleaned, base_files)
        except ValueError as exc:
            raise RefRepairError(f"partial fix does not apply to the task base: {exc}") from exc
        return {p: d for p, d in applied.items() if p not in variant.tests}

    box = Sandbox(variant)
    try:
        box.prepare()
        paths = patch_paths(patch)
        base = box.read(paths)
        fixed = apply_reference(patch, base)
        changes = [file_change(path, base[path], fixed[path], set(variant.tests)) for path in paths]
        changes = [c for c in changes if c.base != c.fixed]
        jobs: list[tuple[str, str, Callable[[], dict[str, Any]], int]] = [
            (
                "reference",
                "reference",
                lambda: {c.path: c.fixed for c in changes if c.path not in variant.tests},
                repetitions,
            ),
            (
                "nop",
                "nop",
                lambda: {c.path: c.base for c in changes if c.path not in variant.tests},
                repetitions,
            ),
        ]
        for s in shortcuts:
            if s.patch is not None:
                jobs.append((s.id, "shortcut", lambda s=s: files_for_patch(s.patch or ""), 1))
            else:
                jobs.append((s.id, "shortcut", lambda s=s: files_for(set(s.keep)), 1))
        for label, role, files_fn, times in jobs:
            for attempt in range(times):
                name = label if times == 1 else f"{label}-{attempt + 1}"
                try:
                    files = files_fn()
                except RefRepairError as exc:
                    checks.append(
                        RefRepairCheck(
                            name=name,
                            role=role,
                            status="error",
                            error=str(exc),  # type: ignore[arg-type]
                        )
                    )
                    continue
                grade = box.grade(name, [], files, grade_timeout, tail_chars=8_000)
                log(
                    f"  {variant.task_id} {name}: reward={grade.reward} "
                    f"{grade.status} {grade.seconds:.0f}s"
                )
                expected = 1.0 if role == "reference" else 0.0
                unexpected = grade.status == "scored" and grade.reward != expected
                checks.append(
                    RefRepairCheck(
                        name=name,
                        role=role,  # type: ignore[arg-type]
                        status=grade.status,
                        reward=grade.reward,
                        seconds=round(grade.seconds, 1),
                        # Failing output is the refinement evidence: why the
                        # reference rejected a new test, or what let a shortcut pass.
                        error=(grade.tail or grade.status)
                        if grade.status != "scored" or unexpected
                        else None,
                    )
                )
    finally:
        box.close()
    return checks


def classify_ref_validation(
    checks: Sequence[RefRepairCheck],
    *,
    repetitions: int = CONTROL_REPETITIONS,
    declared: Mapping[str, str] | None = None,
) -> tuple[RefRepairVerdict, str]:
    """Accept only full xN = 1, nop xN = 0, and every shortcut = 0."""
    blocked = [c for c in checks if c.status != "scored" or c.reward not in (0.0, 1.0)]
    if blocked:
        return "blocked", "unscored grades: " + ", ".join(f"{c.name} ({c.status})" for c in blocked)
    reference = [c.reward for c in checks if c.role == "reference"]
    nop = [c.reward for c in checks if c.role == "nop"]
    if len(reference) != repetitions or len(nop) != repetitions:
        return "blocked", f"missing control grades: reference={len(reference)} nop={len(nop)}"
    if reference != [1.0] * repetitions or nop != [0.0] * repetitions:
        return (
            "rejected",
            f"controls failed: reference={reference} nop={nop} "
            f"(need {[1.0] * repetitions} / {[0.0] * repetitions})",
        )
    escaped = [c.name for c in checks if c.role == "shortcut" and c.reward != 0.0]
    if escaped:
        undeclared = [name for name in escaped if name not in (declared or {})]
        if undeclared:
            return "rejected", "shortcut submissions still pass: " + ", ".join(escaped)
        return (
            "partial",
            f"controls {repetitions}/{repetitions} and every genuine shortcut dead; "
            "declared equivalent/out-of-scope shortcuts still pass: "
            + ", ".join(f"{name} ({declared[name]})" for name in escaped),  # type: ignore[index]
        )
    return (
        "repaired",
        f"reference {repetitions}/{repetitions} pass, nop {repetitions}/{repetitions} "
        "fail, every previous shortcut now fails",
    )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

#: Grades a derived variant; injectable so tests never touch Docker.
RefGrader = Callable[..., list[RefRepairCheck]]
#: Derives a test-only variant; defaults to ``tasks derive`` via verifier_repair.
DeriveFn = Callable[..., Any]
#: One subscription proposal turn (prompt, context) -> (raw text, usage).
ModelCaller = Callable[[str, Mapping[str, Any]], tuple[str, dict[str, Any]]]


def execute_ref_repair(
    plan: RefRepairPlan,
    *,
    model: str,
    repo_root: Path,
    output_dir: Path,
    quota_checker: Callable[[], Any],
    model_caller: ModelCaller,
    grader: RefGrader = grade_keep_sets,
    derive_fn: DeriveFn | None = None,
    materialize_fn: Callable[..., Path] | None = None,
    status_fn: Callable[..., Any] | None = None,
    repetitions: int = CONTROL_REPETITIONS,
) -> RefRepairResult:
    """Propose, derive and validate one repair; write receipts to output_dir."""
    from evallab.verifier_repair import (
        QuotaBlocked,
        RepairError,
        derive_repair_variant,
        ensure_quota,
        parse_proposal,
        persisted_quota_record,
        validate_proposal,
    )

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "repair-prompt.txt").write_text(plan.prompt, encoding="utf-8")
    (out / "repair-context.json").write_text(
        json.dumps(plan.context, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    task = str(plan.report.get("task_id") or plan.package_dir.name)
    try:
        quota = ensure_quota(quota_checker)
    except QuotaBlocked as exc:
        result = RefRepairResult(task=task, verdict="blocked", reason=str(exc))
        _write_result(out, result)
        return result
    (out / "quota.json").write_text(
        json.dumps(persisted_quota_record(quota, model), indent=1) + "\n", encoding="utf-8"
    )
    raw, usage = model_caller(plan.prompt, plan.context)
    (out / "model-usage.json").write_text(
        json.dumps(usage, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    try:
        proposal = parse_proposal(raw)
        (out / "proposal.json").write_text(
            proposal.model_dump_json(by_alias=True, indent=1) + "\n", encoding="utf-8"
        )
        if not proposal.repairable:
            result = RefRepairResult(
                task=task,
                verdict="unrepairable",
                reason=proposal.rationale,
            )
            _write_result(out, result)
            return result
        validate_proposal(
            proposal,
            mutant_ids=[s.id for s in plan.shortcuts],
            package_dir=plan.package_dir,
        )
        derived = derive_repair_variant(
            plan.package_dir,
            proposal,
            repo_root=repo_root,
            created_by="ref-repair",
            rationale=(
                f"Add semantic assertions so ref-subset shortcut submissions fail on {task}."
            ),
            derive_fn=derive_fn,
        )
    except (RepairError, ValueError) as exc:
        result = RefRepairResult(task=task, verdict="blocked", reason=str(exc))
        _write_result(out, result)
        return result
    if materialize_fn is None:
        from evallab.task_variants import materialize as _materialize

        materialize_fn = _materialize
    variant_dir = Path(materialize_fn(derived, plan.package_dir, repo_root=repo_root))
    variant_fields = {
        "variant_digest": str(derived.variant_digest),
        "variant_record": str(Path(repo_root) / derived.record_relpath()),
        "variant_package": str(variant_dir),
    }
    try:
        checks = grader(
            parent_dir=plan.package_dir,
            variant_dir=variant_dir,
            report=plan.report,
            shortcuts=plan.shortcuts,
            repetitions=repetitions,
        )
    except (RefRepairError, RepairError, OSError, RuntimeError) as exc:
        result = RefRepairResult(
            task=task, verdict="blocked", reason=f"grading failed: {exc}", **variant_fields
        )
        _write_result(out, result)
        return result
    declared = {entry.mutant_id: entry.reason for entry in proposal.unrepairable}
    verdict, reason = classify_ref_validation(checks, repetitions=repetitions, declared=declared)
    result = RefRepairResult(
        task=task,
        verdict=verdict,
        reason=reason,
        variant_digest=str(derived.variant_digest),
        variant_record=str(Path(repo_root) / derived.record_relpath()),
        variant_package=str(variant_dir),
        declared_unrepairable=declared,
        checks=checks,
    )
    _write_result(out, result)
    if status_fn is None:
        from evallab.task_variants import append_status_evidence as status_fn

    status_fn(
        derived,
        "validated"
        if verdict == "repaired"
        else "rejected"
        if verdict == "rejected"
        else "candidate",
        evidence=f"{verdict}: {reason}; receipt={out / 'repair-result.json'}",
        by="ref-repair",
        repo_root=repo_root,
    )
    return result


def _write_result(out: Path, result: RefRepairResult) -> None:
    (out / "repair-result.json").write_text(
        result.model_dump_json(by_alias=True, indent=1) + "\n", encoding="utf-8"
    )


def ref_model_caller(
    prompt: str,
    context: Mapping[str, Any],
    *,
    model: str,
    output_dir: Path,
) -> tuple[str, dict[str, Any]]:
    """One subscription proposal; recovers a text-only preamble turn.

    The shared transport fails closed on multiple assistant messages. When
    every completed item is a plain agent message or Codex's own empty MCP
    catalog listing, and the final message parses as a proposal, that final
    message is the proposal: the preamble is narration, not a second answer.
    The original transcript stays on disk and the recovery is recorded in the
    usage receipt. Any real tool-bearing or unparseable turn still fails.
    """
    from evallab.gepa_optimizer.codex_transport import _empty_catalog_probe
    from evallab.verifier_repair import RepairError, codex_model_caller, parse_proposal

    try:
        return codex_model_caller(prompt, context, model=model, output_dir=output_dir)
    except RepairError:
        events = Path(output_dir) / "events.jsonl"
        try:
            items: list[dict[str, Any]] = []
            usage: dict[str, Any] = {}
            for line in events.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                if row.get("type") == "item.completed":
                    items.append(row.get("item") or {})
                elif row.get("type") == "turn.completed":
                    usage = row.get("usage") or {}
        except (OSError, ValueError):
            raise
        texts = []
        for item in items:
            if _empty_catalog_probe(item):
                continue
            if item.get("type") != "agent_message":
                raise
            texts.append(str(item.get("text") or ""))
        if len(texts) < 2 or not texts[-1].strip():
            raise
        parse_proposal(texts[-1])  # raises RepairError unless it is a proposal
        return texts[-1], {
            **usage,
            "requested_model": model,
            "recovered": "final agent_message of a text-only multi-message turn",
        }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _ref_repair_command(args: argparse.Namespace, root: Path, *, harbor: Any | None = None) -> int:
    del harbor
    from evallab.verifier_repair import (
        RepairError,
        read_codex_quota,
    )

    try:
        partial_fixes = []
        for spec in args.partial_fix:
            label, sep, raw = spec.partition("=")
            if not sep or not label or not raw:
                raise RefRepairError(f"--partial-fix needs LABEL=PATH, got {spec!r}")
            partial_fixes.append((label, Path(raw).expanduser()))
        plan = plan_ref_repair(
            Path(args.report_dir),
            feedback=args.feedback,
            partial_fixes=partial_fixes,
            package=args.package,
            reference_patch=args.reference_patch,
        )
    except RefRepairError as exc:
        print(f"hack ref-repair: {exc}", file=sys.stderr)
        return 1
    out = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir is not None
        else root / "runs" / ".envcheck" / f"ref-repair-{plan.package_dir.name}"
    )
    if (out / "repair-result.json").exists():
        print(
            "hack ref-repair: attempt already recorded; use a new --output-dir",
            file=sys.stderr,
        )
        return 1
    if not args.execute:
        out.mkdir(parents=True, exist_ok=True)
        (out / "repair-prompt.txt").write_text(plan.prompt, encoding="utf-8")
        (out / "repair-context.json").write_text(
            json.dumps(plan.context, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"Task: {plan.context.get('task')}")
        print(f"Shortcuts to kill: {', '.join(s.id for s in plan.shortcuts)}")
        print("Preview only: no model call. Rerun with --execute to propose and validate.")
        print(f"run directory: {out}")
        return 0
    try:
        result = execute_ref_repair(
            plan,
            model=args.model,
            repo_root=root,
            output_dir=out,
            quota_checker=read_codex_quota,
            model_caller=lambda prompt, context: ref_model_caller(
                prompt,
                context,
                model=args.model,
                output_dir=out / "codex",
            ),
        )
    except RepairError as exc:
        print(f"hack ref-repair: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(result.model_dump(mode="json", by_alias=True), indent=1)
        if args.json
        else f"{result.verdict}: {result.reason}"
    )
    return 0 if result.verdict == "repaired" else 1


def add_ref_repair_parser(hack_commands: Any) -> None:
    """Register ``evallab hack ref-repair``; the parent owns CLI integration."""
    parser = hack_commands.add_parser(
        "ref-repair",
        help=(
            "Repair a ref-subset-flagged weak verifier with new tests "
            "(preview by default, subscription proposer, x3 controls)"
        ),
    )
    parser.add_argument(
        "report_dir",
        type=Path,
        help="ref-subset report directory (need not exist with --package/--reference-patch)",
    )
    parser.add_argument(
        "--partial-fix",
        action="append",
        default=[],
        metavar="LABEL=PATH",
        help="real partial-fix submission that passed (a trial's verifier/agent.diff); repeatable",
    )
    parser.add_argument(
        "--package", type=Path, help="task package, when the report directory has no report.json"
    )
    parser.add_argument(
        "--reference-patch",
        type=Path,
        help="known-correct reference fix, when the report directory has no report.json",
    )
    parser.add_argument(
        "--feedback", type=Path, help="Prior ref-repair repair-result.json to refine from"
    )
    parser.add_argument("--model", default="gpt-6.1-sol", help="Repair proposer model")
    parser.add_argument("--execute", action="store_true", help="Call the model and validate")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=_ref_repair_command)


__all__ = [
    "CONTROL_REPETITIONS",
    "RefRepairCheck",
    "RefRepairError",
    "RefRepairPlan",
    "RefRepairResult",
    "Shortcut",
    "add_ref_repair_parser",
    "classify_ref_validation",
    "execute_ref_repair",
    "grade_keep_sets",
    "load_ref_report",
    "load_partial_fixes",
    "load_shortcuts",
    "plan_ref_repair",
    "render_ref_repair_prompt",
]
