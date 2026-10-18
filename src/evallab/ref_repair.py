"""Repair weak MiMo verifiers flagged by the ref-subset probe (QA Task S2).

The ref-subset probe (``evallab.ref_subset``) flags a possible weak verifier by
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
import hashlib
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
    expected_shortcuts: list[str] = Field(default_factory=list)
    checks: list[RefRepairCheck] = Field(default_factory=list)
    lineage_error: str | None = None


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
    """Replay the probe's greedy removals to recover the actual passing submissions."""
    mutants = report.get("mutants")
    if mutants is not None:
        if not isinstance(mutants, Mapping):
            raise RefRepairError("invalid line-level mutation summary")
        if mutants.get("survivors") or mutants.get("verdict") == "fail":
            raise RefRepairError(
                "line-level mutation survivors are not replayed by block repair"
            )
    if report.get("verdict") != "fail":
        return []
    blocks = [b for b in report.get("blocks", []) if isinstance(b, dict)]
    all_ids = {str(b["id"]) for b in blocks if b.get("kind") != "test"}
    code_ids = {str(b["id"]) for b in blocks if b.get("kind") == "code"}
    if not all_ids or not code_ids:
        raise RefRepairError("ref-subset report carries no code blocks")
    keep = set(all_ids)
    passed: list[tuple[str, frozenset[str]]] = []
    for grade in report.get("grades", []):
        label = str(grade.get("label") or "")
        if label == "drop-docs-comments":
            removed = {str(b["id"]) for b in blocks if b.get("kind") in {"comment", "noncode"}}
        elif label.startswith("drop-block:"):
            removed = {label.removeprefix("drop-block:")}
            if not removed <= keep:
                raise RefRepairError(f"unknown or already removed block in {label}")
        elif label.startswith("drop-file:"):
            path = label.removeprefix("drop-file:")
            removed = {
                str(b["id"]) for b in blocks if b.get("path") == path and b.get("kind") == "code"
            }
            if not removed:
                raise RefRepairError(f"unknown source file in {label}")
        elif label in {"full", "nop", "confirm"}:
            continue
        else:
            raise RefRepairError(f"unknown probe grade: {label}")
        candidate = keep - removed
        if grade.get("kept") != len(candidate):
            raise RefRepairError(f"cannot reconstruct the scored keep set for {label}")
        if grade.get("status") == "scored" and grade.get("reward") == 1.0:
            keep = candidate
            if code_ids - keep:
                passed.append((label, frozenset(keep)))
    final = report.get("kept")
    if not isinstance(final, list) or set(final) != keep:
        raise RefRepairError("reported minimal keep set disagrees with the grade history")
    if not passed:
        return []
    shortcuts = {frozenset(keep): Shortcut("minimal", "minimal", frozenset(keep))}
    for label, candidate in passed:
        shortcuts.setdefault(candidate, Shortcut(_shortcut_id(label), label, candidate))
    return sorted(shortcuts.values(), key=lambda s: s.label)


def load_partial_fixes(partial_fixes: Sequence[tuple[str, Path]]) -> list[Shortcut]:
    """Real partial-fix submissions: each ``verifier/agent.diff`` that earned reward 1."""
    shortcuts: list[Shortcut] = []
    for label, path in partial_fixes:
        try:
            patch = source_only_patch(Path(path).read_text(encoding="utf-8", errors="strict"))
        except OSError as exc:
            raise RefRepairError(f"cannot read partial fix {label}: {exc}") from exc
        if not patch.strip():
            raise RefRepairError(f"partial fix {label} is empty")
        shortcuts.append(Shortcut(_shortcut_id(f"partial-{label}"), label, patch=patch))
    return shortcuts


def _supported_shortcuts(
    report: Mapping[str, Any],
    *,
    report_dir: Path,
    findings_path: Path,
    findings_root: Path,
) -> tuple[list[Shortcut], dict[str, Any]]:
    """Bind canonical adjudications to recorded complete submissions, not edits."""
    from evallab.qa.models import Finding

    root = Path(findings_root).resolve()
    report_dir = Path(report_dir).resolve()
    findings_path = Path(findings_path).resolve()
    try:
        findings_bytes = findings_path.read_bytes()
        findings = [
            Finding.model_validate_json(line)
            for line in findings_bytes.splitlines()
            if line.strip()
        ]
    except (OSError, ValueError) as exc:
        raise RefRepairError(f"cannot load supported findings: {exc}") from exc

    def usable(grade: Mapping[str, Any], expected: float) -> bool:
        reward = grade.get("reward")
        return (
            grade.get("status") == "scored"
            and type(grade.get("rc")) is int
            and grade["rc"] == 0
            and not isinstance(reward, bool)
            and isinstance(reward, int | float)
            and reward == expected
        )

    recorded = report.get("grades")
    if not isinstance(recorded, list):
        raise RefRepairError("original report has no recorded controls")
    mutants = report.get("mutants") or {}
    if not isinstance(mutants, Mapping):
        raise RefRepairError("invalid original line-level mutation summary")
    survivors = mutants.get("survivors") or []
    originals: dict[tuple[str, str], list[tuple[Path, str]]] = {}
    survivor_hashes: dict[str, str] = {}
    minimal = report_dir / "minimal.patch"
    if minimal.is_file():
        digest = hashlib.sha256(minimal.read_bytes()).hexdigest()
        originals.setdefault(("S2.ref_subset", digest), []).append((minimal, "confirm"))
    for survivor in survivors:
        identifier = str(survivor["id"])
        if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
            raise RefRepairError("invalid original survivor identifier")
        path = report_dir / "mutants" / f"{identifier}.patch"
        if not path.is_file():
            raise RefRepairError(f"original complete survivor patch is missing: {identifier}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        survivor_hashes[identifier] = digest
        originals.setdefault(("S2.ref_mutants", digest), []).append((path, f"mutant:{identifier}"))

    log_path = report_dir / "grades.log"
    log_bytes = log_path.read_bytes() if log_path.is_file() else b""
    log_grades: dict[str, list[dict[str, Any]]] = {}
    for match in re.finditer(
        rb"^== (full|nop|confirm|mutant:[A-Za-z0-9_-]+) reward=(\S+) rc=(-?\d+) (\S+) \S+s$",
        log_bytes,
        re.MULTILINE,
    ):
        label, reward, rc, status = (part.decode("ascii") for part in match.groups())
        try:
            observation = {"label": label, "reward": float(reward), "rc": int(rc), "status": status}
        except ValueError:
            continue
        log_grades.setdefault(label, []).append(observation)

    def observations(label: str) -> Sequence[Mapping[str, Any]]:
        summary = [g for g in recorded if isinstance(g, Mapping) and g.get("label") == label]
        native = log_grades.get(label, [])
        if len(summary) > 1 or len(native) > 1:
            raise RefRepairError(f"ambiguous recorded observation: {label}")
        if summary and native and any(
            summary[0].get(field) != native[0].get(field)
            for field in ("reward", "rc", "status")
        ):
            raise RefRepairError(f"report and native grade log disagree: {label}")
        return summary or native

    controls: dict[str, Mapping[str, Any]] = {}
    for label, expected in (("full", 1.0), ("nop", 0.0)):
        matches = observations(label)
        if len(matches) != 1 or not usable(matches[0], expected):
            raise RefRepairError(f"original {label} control is not a usable recorded observation")
        controls[label] = matches[0]

    task = str(report.get("task_id") or "")
    shortcuts: dict[str, Shortcut] = {}
    admitted_hashes: set[str] = set()
    bindings: list[dict[str, Any]] = []
    for finding in findings:
        name = finding.subject.task_id.split("@", 1)[0].rsplit("/", 1)[-1]
        if name != task:
            continue
        if (
            finding.subject.kind != "task"
            or finding.subject.task_digest != report.get("package_digest")
            or finding.stage not in {"S2.ref_subset", "S2.ref_mutants"}
            or finding.verdict != "fail"
            or finding.category != "weak"
            or finding.fault != "eval"
            or finding.measurement != "measured"
            or finding.refutation.status != "verified"
            or finding.reproducer is None
            or finding.reproducer.kind != "patch"
            or finding.reproducer.expected_reward != 1.0
        ):
            raise RefRepairError("finding is not a verified passing prompt-level witness for this package")
        supplied = Path(finding.reproducer.path)
        if ".." in supplied.parts:
            raise RefRepairError("supported reproducer escapes its evidence root")
        path = (supplied if supplied.is_absolute() else root / supplied).resolve()
        if not path.is_relative_to(root) and not path.is_relative_to(report_dir):
            raise RefRepairError("supported reproducer escapes its evidence root")
        try:
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            patch = data.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise RefRepairError(f"cannot load supported witness patch: {exc}") from exc
        matches = originals.get((finding.stage, digest), [])
        if len(matches) != 1:
            raise RefRepairError("supported witness differs from its original complete submission")
        original, label = matches[0]
        grades = observations(label)
        if len(grades) != 1 or not usable(grades[0], 1.0):
            raise RefRepairError("supported witness has no unambiguous scored passing observation")
        if finding.id is None:
            raise RefRepairError("supported witness has no explicit finding identity")
        _checked_patch_paths(patch)
        identifier = f"finding-{digest[:16]}"
        shortcuts.setdefault(identifier, Shortcut(identifier, finding.id, patch=patch))
        bindings.append({
            "finding_id": finding.id, "reproducer": str(path), "original_patch": str(original),
            "sha256": "sha256:" + digest, "recorded_grade": grades[0],
        })
        if finding.stage == "S2.ref_mutants":
            admitted_hashes.add(digest)
    if not shortcuts:
        raise RefRepairError("no supported witnesses for this report's task")
    provenance = {
        "path": str(findings_path),
        "sha256": "sha256:" + hashlib.sha256(findings_bytes).hexdigest(),
        "repository": str(root),
        "finding_ids": sorted({binding["finding_id"] for binding in bindings}),
        "controls": controls,
        "witnesses": bindings,
        "grades_log": str(log_path) if log_bytes else None,
        "grades_log_sha256": "sha256:" + hashlib.sha256(log_bytes).hexdigest() if log_bytes else None,
        "unadmitted_line_survivors": sorted(
            identifier for identifier, digest in survivor_hashes.items() if digest not in admitted_hashes
        ),
        "scope": "Only accepted complete witnesses are graded; the full original report is retained.",
    }
    return list(shortcuts.values()), provenance


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
    from evallab.ref_subset import Package
    from evallab.registry import compute_task_digests

    path = Path(report_dir) / "report.json"
    if not path.is_file():
        if package is None or reference_patch is None:
            raise RefRepairError(
                f"no ref-subset report at {path}; pass --package and --reference-patch"
            )
        report: dict[str, Any] = {
            "schema": "evallab.ref_subset.report/v1",
            "task_id": Package.load(Path(package)).task_id,
            "verdict": "unknown",
            "package": str(package),
            "package_digest": compute_task_digests(Path(package)).package,
            "reference": {
                "patch": str(reference_patch),
                "sha256": hashlib.sha256(Path(reference_patch).read_bytes()).hexdigest(),
            },
            "blocks": [],
            "grades": [],
        }
    else:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RefRepairError(f"cannot load ref-subset report {path}: {exc}") from exc
    if not isinstance(report, dict) or report.get("schema") != "evallab.ref_subset.report/v1":
        raise RefRepairError("not a ref-subset report")
    bound_package = Path(str(report.get("package") or ""))
    bound_reference = Path(str((report.get("reference") or {}).get("patch") or ""))
    if package is not None and Path(package).resolve() != bound_package.resolve():
        raise RefRepairError("--package conflicts with the recorded probe package")
    if reference_patch is not None and Path(reference_patch).resolve() != bound_reference.resolve():
        raise RefRepairError("--reference-patch conflicts with the recorded probe reference")
    if not bound_package.is_dir() or not bound_reference.is_file():
        raise RefRepairError("report package or reference patch is missing")
    if report.get("task_id") != Package.load(bound_package).task_id:
        raise RefRepairError("report task identity does not match the package")
    if report.get("package_digest") != compute_task_digests(bound_package).package:
        raise RefRepairError("report package digest does not match current bytes")
    if (report.get("reference") or {}).get("sha256") != hashlib.sha256(
        bound_reference.read_bytes()
    ).hexdigest():
        raise RefRepairError("report reference digest does not match current bytes")
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
    allow_test_edits: bool = False


def _bounded(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[truncated]\n"


def plan_ref_repair(
    report_dir: Path,
    *,
    feedback: Path | None = None,
    partial_fixes: Sequence[tuple[str, Path]] = (),
    package: Path | None = None,
    reference_patch: Path | None = None,
    allow_test_edits: bool = False,
    supported_findings: Path | None = None,
    findings_root: Path | None = None,
) -> RefRepairPlan:
    """Prepare the proposer context for one flagged task without calling a model."""
    from evallab.verifier_repair import MAX_HIDDEN_TEST_BYTES, read_task_workdir

    report = load_ref_report(report_dir, package=package, reference_patch=reference_patch)
    package = Path(str(report["package"]))
    if supported_findings is None:
        if findings_root is not None:
            raise RefRepairError("--findings-root requires --supported-findings")
        shortcuts = load_shortcuts(report)
        admission = None
    else:
        if findings_root is None:
            raise RefRepairError("--supported-findings requires its --findings-root repository")
        shortcuts, admission = _supported_shortcuts(
            report,
            report_dir=Path(report_dir),
            findings_path=supported_findings,
            findings_root=findings_root,
        )
    shortcuts += load_partial_fixes(partial_fixes)
    if not shortcuts:
        raise RefRepairError(
            f"nothing to repair: ref-subset verdict is {report.get('verdict')!r} with no "
            "passing shortcut, and no --partial-fix was given"
        )
    if len({s.id for s in shortcuts}) != len(shortcuts):
        raise RefRepairError("shortcut labels resolve to duplicate IDs; use distinct labels")
    try:
        instruction = (package / "instruction.md").read_text(encoding="utf-8")
        test_patch = (package / "tests" / "test.patch").read_bytes().decode("utf-8")
        test_command = (package / "tests" / "test_command.sh").read_text(encoding="utf-8")
    except OSError as exc:
        raise RefRepairError(f"parent package is incomplete: {exc}") from exc
    if allow_test_edits and len(test_patch.encode()) > MAX_HIDDEN_TEST_BYTES:
        raise RefRepairError("hidden-test patch exceeds the existing-test edit context bound")
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
        "hidden_test_patch": test_patch if allow_test_edits else _bounded(test_patch, 32_768),
        "test_patch_sha256": "sha256:" + hashlib.sha256(test_patch.encode()).hexdigest(),
        "allow_test_edits": allow_test_edits,
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
        "dropped_blocks": list(report.get("dropped_code") or []),
    }
    if admission is not None:
        context["supported_findings"] = admission
    if feedback is not None:
        try:
            prior = json.loads(Path(feedback).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RefRepairError(f"cannot load feedback {feedback}: {exc}") from exc
        if (
            not isinstance(prior, dict)
            or prior.get("schema") != SCHEMA_RESULT
            or prior.get("task") != report.get("task_id")
        ):
            raise RefRepairError("feedback is not a repair result for this task")
        context["feedback"] = _bounded(json.dumps(_compact_feedback(prior), indent=1), 16_384)
    return RefRepairPlan(
        report_dir=Path(report_dir),
        report=report,
        package_dir=package,
        shortcuts=shortcuts,
        context=context,
        prompt=render_ref_repair_prompt(context),
        allow_test_edits=allow_test_edits,
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
    from evallab.verifier_repair import (
        MAX_COMMAND_BYTES,
        MAX_TEST_BYTES,
        MAX_TESTS,
        MAX_TOTAL_TEST_BYTES,
        SCHEMA_PROPOSAL,
        render_test_edit_rules,
    )

    lines = [
        "You are reviewing a benchmark task's hidden tests. Each SHORTCUT below",
        "is a reference subset or a real submission that scored full reward.",
        "First decide whether its missing behaviour violates the instruction:",
        "passing a smaller or different solution is not itself a defect.",
        "For real gaps, write NEW semantic tests that pass on the reference",
        "and fail on the incorrect shortcuts. Declare equivalent/out-of-scope",
        "submissions explicitly rather than inventing requirements to kill them.",
        "Assert only behaviour required by the instruction, not the reference's",
        "incidental implementation choices.",
        "",
        "Output exactly one JSON object, no markdown fences, with the",
        "proposal schema: {schema, repairable, rationale, tests, command,",
        "model, mutant_ids, unrepairable, edits, test_patch_sha256}. Each test: {id, repo_path, code}.",
        "Each unrepairable entry: {mutant_id, kind, reason} with kind",
        "equivalent or out-of-scope. Here mutant_ids / mutant_id refer to the",
        "SHORTCUT ids listed below: every shortcut needs a disposition.",
        f"The schema field is exactly {SCHEMA_PROPOSAL!r}.",
        "The model field may be null. Command is null when no NEW files run;",
        "existing tests always run under the unchanged original command.",
        "Rules:",
        "- Test observable behaviour required by the instruction only.",
        "- Return text only; do not call tools, including MCP resource discovery.",
        "- NEVER inspect source or patch fingerprints: no __file__, inspect,",
        "  hashlib, .patch reads, solve.sh reads, or subprocess/network use.",
        "- Do not pin incidental properties (immutability, annotations,",
        "  logging, formatting) unless the instruction requires them.",
        "- Keep tests offline with synthetic fixtures; never load models.",
        "- Exercise the API named by the instruction; do not infer interfaces or invent mock attributes.",
        "- Synthetic revision names must not be long hexadecimal IDs or reference fingerprints.",
        f"- Each test file is at most {MAX_TEST_BYTES} UTF-8 bytes; total at most {MAX_TOTAL_TEST_BYTES}.",
        "- Test code must end in a newline. Use new paths not reset by the hidden harness.",
        "- repo_path is a NEW repository Python test file named test_*.py,",
        f"  never a file the hidden patch already carries. At most {MAX_TESTS} files.",
        "- command runs every new test file with the task Python interpreter:",
        '  "python -m pytest path/test_repair_x.py -q" or "python -m unittest',
        '  path/test_repair_x.py" (the file path, exactly as in repo_path); it',
        "  must name each repo_path,",
        f"  stay under {MAX_COMMAND_BYTES} bytes, no shell metacharacters. pytest may be absent",
        "  from the task image: unless the existing test command above itself",
        "  runs pytest, write unittest.TestCase tests and use -m unittest. If",
        "  the project needs framework setup (e.g. Django settings), do it",
        "  inside the test module (os.environ.setdefault + django.setup()).",
        *render_test_edit_rules(context),
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


def _checked_patch_paths(patch: str) -> list[str]:
    """Reject patch features the byte-only replay cannot preserve safely."""
    paths: list[str] = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            match = re.fullmatch(r"diff --git a/([A-Za-z0-9_./-]+) b/([A-Za-z0-9_./-]+)", line)
            if match is None or match[1] != match[2]:
                raise RefRepairError(
                    "quoted, renamed or unsupported patch paths cannot be replayed"
                )
            path = Path(match[1])
            if path.is_absolute() or any(part in {"..", ".git"} for part in path.parts):
                raise RefRepairError("patch path must stay inside the task source tree")
            if match[1] not in paths:
                paths.append(match[1])
        elif line.startswith(
            ("old mode ", "new mode ", "rename ", "copy ", "GIT binary patch", "Binary files ")
        ):
            raise RefRepairError("mode, rename, copy or binary patches cannot be replayed")
        elif line.startswith(("new file mode ", "deleted file mode ")):
            if not line.endswith(" 100644"):
                raise RefRepairError("only regular non-executable file changes can be replayed")
        elif line.startswith("index ") and line.endswith((" 120000", " 160000")):
            raise RefRepairError("symlink or gitlink patches cannot be replayed")
    if not paths:
        raise RefRepairError("patch contains no supported source changes")
    return paths


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

    Uses the existing ref-subset sandbox, with at most one Docker container
    at a time. Unscored / timed-out grades stay visible as non-scored checks.
    """
    from evallab.ref_subset import (
        Package,
        Sandbox,
        apply_reference,
        compose,
        file_change,
    )

    patch_path = Path(str((report.get("reference") or {}).get("patch") or ""))
    try:
        patch = patch_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RefRepairError(f"reference patch is missing: {exc}") from exc
    parent = Package.load(parent_dir)
    variant = Package.load(variant_dir)
    if (parent.task_id, parent.image, parent.workdir, parent.tests) != (
        variant.task_id,
        variant.image,
        variant.workdir,
        variant.tests,
    ):
        raise RefRepairError("variant does not preserve the parent task environment")
    if report.get("task_id") != parent.task_id:
        raise RefRepairError("report task differs from the grading package")
    paths = _checked_patch_paths(patch)
    checks: list[RefRepairCheck] = []

    def files_for(keep: set[str]) -> dict[str, Any]:
        return {c.path: compose(c, keep) for c in changes if c.path not in variant.tests}

    def files_for_patch(shortcut_patch: str) -> dict[str, Any]:
        cleaned = source_only_patch(shortcut_patch)
        paths = _checked_patch_paths(cleaned)
        base_files = box.read(paths)
        try:
            applied = apply_reference(cleaned, base_files)
        except ValueError as exc:
            raise RefRepairError(f"partial fix does not apply to the task base: {exc}") from exc
        return {p: d for p, d in applied.items() if p not in variant.tests}

    box = Sandbox(variant)
    try:
        box.prepare()
        base = box.read(paths)
        fixed = apply_reference(patch, base)
        changes = [file_change(path, base[path], fixed[path], set(variant.tests)) for path in paths]
        changes = [c for c in changes if c.base != c.fixed]
        block_ids = {
            block.id for change in changes for block in change.blocks if block.kind != "test"
        }
        recorded_ids = {
            str(block["id"]) for block in report.get("blocks", []) if block.get("kind") != "test"
        }
        if recorded_ids and recorded_ids != block_ids:
            raise RefRepairError("reference blocks no longer match the recorded probe")
        for shortcut in shortcuts:
            if shortcut.patch is None and (not shortcut.keep or not shortcut.keep <= block_ids):
                raise RefRepairError(f"shortcut {shortcut.id} does not match the reference blocks")
        jobs: list[
            tuple[str, Literal["reference", "nop", "shortcut"], Callable[[], dict[str, Any]], int]
        ] = [
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
                            error=str(exc),
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
                        role=role,
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
    shortcut_ids: Sequence[str],
    repetitions: int = CONTROL_REPETITIONS,
    declared: Mapping[str, str] | None = None,
    unadmitted: Sequence[str] = (),
) -> tuple[RefRepairVerdict, str]:
    """Require x3 controls and every admitted shortcut dead; preserve unknown scope."""
    if repetitions < CONTROL_REPETITIONS:
        return "blocked", "at least three reference and nop grades are required"
    expected = set(shortcut_ids)
    observed = [c.name for c in checks if c.role == "shortcut"]
    if not expected or len(expected) != len(shortcut_ids):
        return "blocked", "expected shortcut IDs must be nonempty and unique"
    if set(observed) != expected or len(observed) != len(expected):
        return "blocked", "missing, duplicate or unexpected shortcut grades"
    names = [(c.role, c.name) for c in checks]
    if len(set(names)) != len(names):
        return "blocked", "duplicate control or shortcut grades"
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
    reasons = declared or {}
    if escaped:
        undeclared = [name for name in escaped if name not in reasons]
        if undeclared:
            return "rejected", "shortcut submissions still pass: " + ", ".join(escaped)
        return (
            "partial",
            f"controls {repetitions}/{repetitions} pass; declared survivors require scope review: "
            + ", ".join(f"{name} ({reasons[name]})" for name in escaped),
        )
    if unadmitted:
        return (
            "partial",
            "admitted witnesses rejected; original line survivors remain unmeasured, "
            "not refuted or equivalent: " + ", ".join(unadmitted),
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
    proposal_path: Path | None = None,
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
    if (out / "repair-result.json").exists():
        raise RefRepairError("attempt already recorded; use a new output directory")
    out.mkdir(parents=True, exist_ok=True)
    (out / "repair-prompt.txt").write_text(plan.prompt, encoding="utf-8")
    (out / "repair-context.json").write_text(
        json.dumps(plan.context, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    task = str(plan.report.get("task_id") or plan.package_dir.name)
    try:
        if proposal_path is None:
            quota = ensure_quota(quota_checker)
            (out / "quota.json").write_text(
                json.dumps(persisted_quota_record(quota, model), indent=1) + "\n", encoding="utf-8"
            )
            raw, usage = model_caller(plan.prompt, plan.context)
        else:
            data = Path(proposal_path).read_bytes()
            raw = data.decode("utf-8")
            usage = {
                "source": "retained_proposal",
                "proposal_path": str(Path(proposal_path).resolve()),
                "proposal_sha256": hashlib.sha256(data).hexdigest(),
                "new_model_call": False,
            }
        (out / "model-usage.json").write_text(
            json.dumps(usage, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (QuotaBlocked, RepairError, OSError, ValueError) as exc:
        result = RefRepairResult(task=task, verdict="blocked", reason=str(exc))
        _write_result(out, result)
        return result
    try:
        proposal = parse_proposal(raw)
        (out / "proposal.json").write_text(
            proposal.model_dump_json(by_alias=True, indent=1) + "\n", encoding="utf-8"
        )
        validate_proposal(
            proposal,
            mutant_ids=[s.id for s in plan.shortcuts],
            package_dir=plan.package_dir,
            allow_test_edits=plan.allow_test_edits,
        )
        if not proposal.repairable:
            result = RefRepairResult(
                task=task,
                verdict="unrepairable",
                reason=proposal.rationale,
            )
            _write_result(out, result)
            return result
        derived = derive_repair_variant(
            plan.package_dir,
            proposal,
            repo_root=repo_root,
            created_by="ref-repair",
            rationale=(
                f"Add semantic assertions so ref-subset shortcut submissions fail on {task}."
            ),
            derive_fn=derive_fn,
            allow_test_edits=plan.allow_test_edits,
        )
    except (RepairError, ValueError) as exc:
        result = RefRepairResult(task=task, verdict="blocked", reason=str(exc))
        _write_result(out, result)
        return result
    if materialize_fn is None:
        from evallab.task_variants import materialize as _materialize

        materialize_fn = _materialize
    variant_dir = Path(materialize_fn(derived, plan.package_dir, repo_root=repo_root))
    try:
        from evallab.registry import compute_task_digests

        if compute_task_digests(variant_dir).package != str(derived.variant_digest):
            raise RefRepairError("materialized variant bytes do not match the derived digest")
        checks = grader(
            parent_dir=plan.package_dir,
            variant_dir=variant_dir,
            report=plan.report,
            shortcuts=plan.shortcuts,
            repetitions=repetitions,
        )
    except (RefRepairError, RepairError, OSError, RuntimeError) as exc:
        result = RefRepairResult(
            task=task,
            verdict="blocked",
            reason=f"grading failed: {exc}",
            variant_digest=str(derived.variant_digest),
            variant_record=str(Path(repo_root) / derived.record_relpath()),
            variant_package=str(variant_dir),
        )
        _write_result(out, result)
        return result
    declared = {entry.mutant_id: entry.reason for entry in proposal.unrepairable}
    admission = plan.context.get("supported_findings") or {}
    verdict, reason = classify_ref_validation(
        checks,
        shortcut_ids=[s.id for s in plan.shortcuts],
        repetitions=repetitions,
        declared=declared,
        unadmitted=admission.get("unadmitted_line_survivors") or (),
    )
    result = RefRepairResult(
        task=task,
        verdict=verdict,
        reason=reason,
        variant_digest=str(derived.variant_digest),
        variant_record=str(Path(repo_root) / derived.record_relpath()),
        variant_package=str(variant_dir),
        declared_unrepairable=declared,
        expected_shortcuts=[s.id for s in plan.shortcuts],
        checks=checks,
    )
    if status_fn is None:
        from evallab.task_variants import append_status_evidence as status_fn

    try:
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
    except (OSError, ValueError, RuntimeError) as exc:
        result.lineage_error = f"{type(exc).__name__}: {exc}"
    _write_result(out, result)
    return result


def _write_result(out: Path, result: RefRepairResult) -> None:
    (out / "repair-result.json").write_text(
        result.model_dump_json(by_alias=True, indent=1) + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _ref_repair_command(args: argparse.Namespace, root: Path, *, harbor: Any | None = None) -> int:
    del harbor
    from evallab.verifier_repair import (
        RepairError,
        codex_model_caller,
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
            allow_test_edits=args.allow_test_edits,
            supported_findings=args.supported_findings,
            findings_root=args.findings_root,
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
        action = "grade the retained proposal" if args.proposal else "propose and validate"
        print(f"Preview only: no model call. Rerun with --execute to {action}.")
        print(f"run directory: {out}")
        return 0
    try:
        result = execute_ref_repair(
            plan,
            model=args.model,
            repo_root=root,
            output_dir=out,
            proposal_path=args.proposal,
            quota_checker=read_codex_quota,
            model_caller=lambda prompt, context: codex_model_caller(
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
    parser.add_argument(
        "--allow-test-edits",
        action="store_true",
        help="Opt in to preimage-bound replacement of prompt-overconstrained hidden tests",
    )
    parser.add_argument(
        "--supported-findings",
        type=Path,
        help="Canonical verified qa.finding/v1 JSONL with sibling atlas.jsonl; original report retained",
    )
    parser.add_argument(
        "--findings-root",
        type=Path,
        help="Repository root for the supported findings' relative witness paths",
    )
    parser.add_argument("--model", default="gpt-6.1-sol", help="Repair proposer model")
    parser.add_argument(
        "--execute", action="store_true", help="Propose and validate, or grade --proposal"
    )
    parser.add_argument(
        "--proposal",
        type=Path,
        help="Grade a retained JSON proposal without a model call or a quota override",
    )
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
