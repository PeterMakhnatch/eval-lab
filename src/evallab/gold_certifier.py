"""Gold task certifier: behavioral synthesis, separate-verifier hardening and seven gates."""

from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from evallab.full_hardening import HardeningManifest, harden
from evallab.modal_cells import CellRunner, SandboxMeter, modal_cell_runner
from evallab.mutant_auditor import (
    Mutant,
    fresh_mutants,
    mutant_script,
    mutants_for_patch,
    parse_probe,
    probe_script,
    python_targets,
)

FORBIDDEN_SOURCE_INSPECTION = (
    "inspect.getsource",
    "inspect.getsourcelines",
    "ast.parse",
    "import ast",
    "from ast import",
    "import tokenize",
    "from tokenize import",
    "linecache",
    "__code__",
)

HOOK_ANCHOR = 'tail -c 12000 "$V/test_output.log"\n'
GOLD_CONTRACT_TEST_NAME = "test_gold_contract.py"
CONFTEST_TAMPER_PAYLOAD = (
    "def pytest_sessionfinish(session, exitstatus):\n"
    "    session.exitstatus = 0\n"
)


def wilson_interval(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """95% Wilson score interval for k successes in n trials; (nan, nan) when n == 0."""
    if n <= 0:
        return (math.nan, math.nan)
    p = k / n
    denom = 1.0 + (z * z) / n
    centre = (p + (z * z) / (2.0 * n)) / denom
    half = (z * math.sqrt(p * (1.0 - p) / n + (z * z) / (4.0 * n * n))) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def format_wilson(k: int, n: int) -> str:
    """Format 'k/n = x.x% (lo-hi)' with 95% Wilson interval."""
    if n == 0:
        return "0/0"
    lo, hi = wilson_interval(k, n)
    return f"{k}/{n} = {100.0 * k / n:.1f}% ({100.0 * lo:.1f}-{100.0 * hi:.1f})"


def split_mutants(mutants: list[Mutant]) -> tuple[list[Mutant], list[Mutant]]:
    """Deterministically partition mutants by sha256 ascending into train and strictly held-out eval.

    Train mutants: first half (shown to the synthesizer).
    Eval mutants: second half (never shown to the synthesizer, prompt, or self-repair revision).
    """
    ordered = sorted(mutants, key=lambda m: m.sha256)
    n = len(ordered)
    if n <= 1:
        return ordered, []
    half = (n + 1) // 2
    return ordered[:half], ordered[half:]


def mutant_diff(mutant: Mutant, original_source: str) -> str:
    """Unified diff representing the AST mutation against the original pre-mutated file."""
    lines_orig = original_source.splitlines(keepends=True)
    lines_mut = mutant.source.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            lines_orig,
            lines_mut,
            fromfile=f"a/{mutant.path}",
            tofile=f"b/{mutant.path}",
        )
    )


def extract_code_block(text: str) -> str | None:
    """Extract the last python code block from markdown fences."""
    blocks = re.findall(r"```(?:python)?\n(.*?)```", text, re.S)
    return blocks[-1] if blocks else None


def validate_contract_test(code: str) -> tuple[bool, str]:
    """Verify that the synthesized test file uses unittest and avoids source inspection."""
    if not code or not code.strip():
        return False, "empty code"
    if "unittest" not in code:
        return False, "missing unittest framework"
    for forbidden in FORBIDDEN_SOURCE_INSPECTION:
        if forbidden in code:
            return False, f"contains forbidden source inspection pattern: {forbidden!r}"
    return True, "valid"


def build_synthesis_prompt(
    *,
    instruction: str,
    hidden_tests: str,
    reference_patch: str,
    train_mutants: list[Mutant],
    train_diffs: list[str],
    interpreter_command: str,
    python_version: str = "3",
    existing_tests: str | None = None,
    feedback: str | None = None,
    previous_test: str | None = None,
    survivor_diffs: list[str] | None = None,
) -> str:
    """Build the behavioral synthesis prompt given train mutants only (strictly blind to eval mutants)."""
    parts = [
        "You are an expert software evaluation engineer writing a contract-complete behavioral unit-test "
        "suite for a software task grader.\n\n"
        "Earlier attempts at mutant repair overfitted to 1-2 shown diffs and failed on unseen semantic mutants. "
        "Your goal is NOT just to check the sample mutants shown below, but to write a thorough, contract-complete "
        "specification test suite covering EVERY behavioral contract, branch, guard, boundary condition, "
        "return value, keyword argument, and error handler introduced by the reference patch.\n\n"
        "## Task instruction (what the agent was asked to solve)\n",
        instruction.strip(),
        "\n\n## Existing hidden tests in the grader (reference test code)\n",
        hidden_tests.strip(),
        "\n\n## Reference implementation (unified diff of the correct solution)\n",
        reference_patch.strip(),
    ]

    if existing_tests and existing_tests.strip():
        parts += [
            "\n\n## Existing supplementary test file already in the grader (keep its style)\n",
            existing_tests.strip(),
        ]

    if previous_test and previous_test.strip():
        parts += [
            "\n\n## Your previous test file (an earlier round); the new file replaces it, so keep what it "
            "checks and add what it missed\n",
            previous_test.strip(),
        ]
    if survivor_diffs:
        parts += [
            "\n\n## Mutants your previous test file wrongly ACCEPTED (it still passed on every one of these)\n"
            "They were not shown to you before. Your new test file MUST fail on each of them, by testing "
            "behavior through the public API, and must still pass on the reference implementation:\n\n",
            "\n\n".join(survivor_diffs),
        ]

    if train_diffs:
        parts += [
            "\n\n## Sample semantic AST mutants of the reference implementation that naive graders wrongly ACCEPT\n"
            "Below are sample mutations. Your test suite MUST fail on every one of these, but must do so by testing "
            "actual behavior via the public API:\n\n",
            "\n\n".join(train_diffs),
        ]

    py_compat = "Python 2.7 and Python 3" if python_version == "2.7" else "Python 3"

    parts += [
        "\n\n## Requirements for your output\n",
        f"Write ONE Python `unittest` file. It will be saved as `/tests/{GOLD_CONTRACT_TEST_NAME}` and executed as:\n"
        f"  `cd /testbed && {interpreter_command} /tests/{GOLD_CONTRACT_TEST_NAME}`\n"
        "after the existing hidden tests pass. The repository checkout is at `/testbed`.\n\n"
        "Strict rules:\n"
        "1. BEHAVIORAL ONLY: Exercise the implementation strictly through the PUBLIC API of the package. "
        "NEVER inspect source code directly (no `inspect.getsource`, no `ast.parse`, no `tokenize`, no reading `.py` files, "
        "no regex over source files). Tests doing source inspection will be rejected.\n"
        "2. CONTRACT COMPLETENESS: Cover EVERY branch, boundary condition (<= vs <, off-by-one), keyword argument, "
        "None-checks, error raising/handling, and return values introduced by the reference patch.\n"
        "3. ACCURACY: Must PASS on the reference implementation with 0 errors and 0 failures, and FAIL on all mutants.\n"
        f"4. COMPATIBILITY: Must be compatible with {py_compat}. Add `import sys; sys.path.insert(0, '/testbed')` at top. "
        "End with `if __name__ == '__main__': unittest.main()`.\n"
        "5. MINIMAL OVERHEAD: Deterministic, no network, minimal mock usage (prefer real objects), finishes in under 30 seconds.\n"
        "6. CONCISE AND FOCUSED: Prefer 3-8 focused test methods covering the contracts; keep the file under 150 lines so it does not truncate.\n"
        "Output exactly ONE fenced ```python ``` block containing the complete test file and nothing else.\n",
    ]

    if feedback:
        parts += [
            "\n\n## Feedback from previous execution attempt (Self-repair revision)\n"
            "The previous version of your test file failed with the following execution output:\n\n",
            feedback.strip(),
            "\n\nPlease analyze the failure and fix your test suite so it passes on the reference and fails on all mutants.\n",
        ]

    return "".join(parts)


def format_supplementary_hook(hook_cmd: str, test_filename: str = GOLD_CONTRACT_TEST_NAME) -> str:
    """Format the supplementary hook recognized by separate_verifier._SUPPLEMENTARY_BLOCK (#1707)."""
    return (
        f'if [ "$RC" -eq 0 ]; then\n'
        f"  ( {hook_cmd} /tests/{test_filename} ) >> \"$V/test_output.log\" 2>&1\n"
        f"  RC=$?\n"
        f"fi\n"
    )


def extract_hook_command_from_test_sh(test_sh_text: str, default_hook: str | None = None) -> str:
    """Extract existing supplementary hook interpreter command from tests/test.sh if present."""
    match = re.search(r"\( (cd \"\$CWD\".*?) /tests/test_outputs\.py \)", test_sh_text)
    if match:
        return match.group(1).strip()
    match2 = re.search(r"\( (cd \"\$CWD\".*?) /tests/[a-zA-Z0-9_-]+\.py \)", test_sh_text)
    if match2:
        return match2.group(1).strip()
    if default_hook:
        return default_hook
    return 'cd "$CWD" && cd /testbed && PYTHONPATH=/testbed python3'


def stage_and_harden_package(
    base_package: Path,
    out_dir: Path,
    contract_test_code: str,
    hook_command: str,
    *,
    test_filename: str = GOLD_CONTRACT_TEST_NAME,
    single_container: bool = False,
    allow_degraded: bool = True,
    **harden_kwargs: Any,
) -> tuple[HardeningManifest, str]:
    """Copy base package, insert supplementary test & hook, and run evallab harden.

    Returns (manifest, hook_block).
    """
    base_package = base_package.resolve()
    out_dir = out_dir.resolve()
    if out_dir.exists():
        raise FileExistsError(f"output {out_dir} already exists")

    with tempfile.TemporaryDirectory(prefix="gold_stage_") as tmp:
        stage = Path(tmp) / "pkg"
        shutil.copytree(base_package, stage, symlinks=True)
        for p in (stage, *stage.rglob("*")):
            if not p.is_symlink():
                p.chmod(p.stat().st_mode | 0o200)

        tests_dir = stage / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / test_filename).write_text(contract_test_code, encoding="utf-8")

        test_sh = tests_dir / "test.sh"
        test_sh_text = test_sh.read_text(encoding="utf-8")
        hook_block = format_supplementary_hook(hook_command, test_filename=test_filename)

        if HOOK_ANCHOR in test_sh_text:
            test_sh.write_text(
                test_sh_text.replace(HOOK_ANCHOR, hook_block + HOOK_ANCHOR, 1),
                encoding="utf-8",
            )
        else:
            # If standard anchor is missing, append right before the final reward/echo logic
            pattern = re.compile(r'(if \[ "\$RC" -eq 0 \]; then echo 1 > "\$V/reward\.txt";|if \[ \$RC -eq 0 \]; then)')
            m = pattern.search(test_sh_text)
            if m:
                idx = m.start()
                test_sh.write_text(
                    test_sh_text[:idx] + hook_block + test_sh_text[idx:],
                    encoding="utf-8",
                )
            else:
                raise ValueError("cannot place supplementary test before the grader reward; unsupported tests/test.sh")

        manifest = harden(
            stage,
            out_dir,
            single_container=single_container,
            allow_degraded=allow_degraded,
            **harden_kwargs,
        )

    return manifest, hook_block


# --------------------------------------------------------------------------- Arm scripts


def _b64_write(path: str, body: str) -> str:
    encoded = base64.b64encode(body.encode()).decode()
    return f"echo {encoded} | base64 -d > {path}\n"


def reference_script(workdir: str, reference_patch: str) -> str:
    """Agent bash applying the reference patch."""
    patch_write = _b64_write("/tmp/reference.patch", reference_patch)
    return (
        f"set -u\ncd {workdir} || exit 1\n"
        f"{patch_write}"
        "git apply --whitespace=nowarn /tmp/reference.patch || patch -p1 < /tmp/reference.patch || exit 1\n"
        "echo REFERENCE_APPLIED\n"
    )


def nop_script(workdir: str) -> str:
    """Agent bash executing no modifications."""
    return f"set -u\ncd {workdir} || exit 1\necho NOP_APPLIED\n"


def witness_script(workdir: str, solve_sh: str | None = None) -> str:
    """Agent bash running a candidate witness solve.sh or dropping a sessionfinish conftest tamper hook."""
    if solve_sh:
        return f"set -u\ncd {workdir} || exit 1\n{solve_sh}\necho WITNESS_APPLIED solve_sh\n"
    conftest_write = _b64_write(f"{workdir}/conftest.py", CONFTEST_TAMPER_PAYLOAD)
    return f"set -u\ncd {workdir} || exit 1\n{conftest_write}echo WITNESS_APPLIED conftest\n"


def exit0_pth_script(workdir: str) -> str:
    """Agent bash installing an exit(0) startup hook into site-packages (L4 .pth startup forgery test)."""
    return (
        f"set -u\ncd {workdir} || exit 1\n"
        "n=0\n"
        "for PY in python python3; do\n"
        "  command -v $PY >/dev/null 2>&1 || continue\n"
        "  SP=$($PY -S -c 'import sysconfig; print(sysconfig.get_paths()[\"purelib\"])') || continue\n"
        '  [ -w "$SP" ] || continue\n'
        "  printf 'import os; os._exit(0)\\n' > \"$SP/zz_cert.pth\" && n=$((n+1))\n"
        "done\n"
        "[ $n -gt 0 ] || exit 1\n"
        "echo EXIT0_PTH_APPLIED $n\n"
    )


# --------------------------------------------------------------------------- Matrix results


@dataclass
class TaskCertificationResult:
    task_id: str
    kind: str
    upgraded_package: str
    test_lines: int
    train_mutants_total: int
    train_mutants_killed: int
    eval_mutants_total: int
    eval_mutants_killed: int
    eval_mutants_base_accepted: int
    eval_mutants_base_accepted_killed: int
    gate1_reference_passed: bool
    gate2_nop_passed: bool
    gate3_witness_passed: bool
    gate4_exit0_pth_passed: bool
    reference_reward: float | None = None
    nop_reward: float | None = None
    witness_reward: float | None = None
    exit0_pth_reward: float | None = None
    train_rewards: dict[str, float | None] = field(default_factory=dict)
    eval_rewards: dict[str, float | None] = field(default_factory=dict)
    eval_base_rewards: dict[str, float | None] = field(default_factory=dict)
    alternative_solutions_total: int | None = None
    alternative_solutions_accepted: int | None = None
    alternative_solution_rewards: dict[str, float | None] = field(default_factory=dict)
    witness_rewards: dict[str, float | None] = field(default_factory=dict)
    package_digest: str | None = None

    @property
    def gate7_alternatives_passed(self) -> bool | None:
        if self.alternative_solutions_total is None or self.alternative_solutions_accepted is None:
            return None
        return self.alternative_solutions_accepted == self.alternative_solutions_total

    @property
    def status(self) -> str:
        gates = (
            self.gate1_reference_passed, self.gate2_nop_passed,
            self.gate3_witness_passed, self.gate4_exit0_pth_passed,
            self.train_mutants_total > 0 and self.train_mutants_killed == self.train_mutants_total,
            self.eval_mutants_total > 0 and self.eval_mutants_killed == self.eval_mutants_total,
        )
        if (not all(gates) or self.gate7_alternatives_passed is False
                or (self.alternative_solutions_total is not None and self.alternative_solutions_accepted is None)):
            return "failed"
        if self.gate7_alternatives_passed and (self.alternative_solutions_total or 0) >= 2:
            return "certified"
        return "provisional"

    def summary(self) -> dict[str, Any]:
        return {**asdict(self), "gate7_alternatives_passed": self.gate7_alternatives_passed, "status": self.status}


@dataclass
class GoldMatrixSummary:
    tasks: list[TaskCertificationResult]
    total_tasks: int = 0
    gate1_valid_reference: int = 0
    gate2_valid_nop: int = 0
    gate3_witness_rejected: int = 0
    gate4_exit0_pth_rejected: int = 0
    gate5_train_killed: int = 0
    gate5_train_total: int = 0
    gate6_eval_base_accepted: int = 0
    gate6_eval_base_accepted_killed: int = 0
    gate6_eval_total_killed: int = 0
    gate6_eval_total: int = 0

    def calculate_totals(self) -> None:
        self.total_tasks = len(self.tasks)
        self.gate1_valid_reference = sum(1 for t in self.tasks if t.gate1_reference_passed)
        self.gate2_valid_nop = sum(1 for t in self.tasks if t.gate2_nop_passed)
        self.gate3_witness_rejected = sum(1 for t in self.tasks if t.gate3_witness_passed)
        self.gate4_exit0_pth_rejected = sum(1 for t in self.tasks if t.gate4_exit0_pth_passed)
        self.gate5_train_killed = sum(t.train_mutants_killed for t in self.tasks)
        self.gate5_train_total = sum(t.train_mutants_total for t in self.tasks)
        self.gate6_eval_base_accepted = sum(t.eval_mutants_base_accepted for t in self.tasks)
        self.gate6_eval_base_accepted_killed = sum(t.eval_mutants_base_accepted_killed for t in self.tasks)
        self.gate6_eval_total_killed = sum(t.eval_mutants_killed for t in self.tasks)
        self.gate6_eval_total = sum(t.eval_mutants_total for t in self.tasks)

    def to_report_dict(self) -> dict[str, Any]:
        self.calculate_totals()
        return {
            "total_tasks": self.total_tasks,
            "gate1_reference": {
                "passed": self.gate1_valid_reference,
                "total": self.total_tasks,
                "formatted": format_wilson(self.gate1_valid_reference, self.total_tasks),
            },
            "gate2_nop": {
                "passed": self.gate2_valid_nop,
                "total": self.total_tasks,
                "formatted": format_wilson(self.gate2_valid_nop, self.total_tasks),
            },
            "gate3_witness": {
                "passed": self.gate3_witness_rejected,
                "total": self.total_tasks,
                "formatted": format_wilson(self.gate3_witness_rejected, self.total_tasks),
            },
            "gate4_exit0_pth": {
                "passed": self.gate4_exit0_pth_rejected,
                "total": self.total_tasks,
                "formatted": format_wilson(self.gate4_exit0_pth_rejected, self.total_tasks),
            },
            "gate5_train_mutants": {
                "killed": self.gate5_train_killed,
                "total": self.gate5_train_total,
                "formatted": format_wilson(self.gate5_train_killed, self.gate5_train_total),
            },
            "gate6_eval_mutants_base_accepted": {
                "killed": self.gate6_eval_base_accepted_killed,
                "total": self.gate6_eval_base_accepted,
                "formatted": format_wilson(self.gate6_eval_base_accepted_killed, self.gate6_eval_base_accepted),
            },
            "gate6_eval_mutants_all": {
                "killed": self.gate6_eval_total_killed,
                "total": self.gate6_eval_total,
                "formatted": format_wilson(self.gate6_eval_total_killed, self.gate6_eval_total),
            },
            "gate7_alternative_solutions": {
                "measured_tasks": sum(t.alternative_solutions_accepted is not None for t in self.tasks),
                "accepted": sum(t.alternative_solutions_accepted or 0 for t in self.tasks),
                "total": sum(t.alternative_solutions_total or 0 for t in self.tasks),
            },
            "tasks": [t.summary() for t in self.tasks],
        }


# --------------------------------------------------------------------------- Feedback rounds

DEFAULT_ROUND_SEED = "gold-rounds-v1"
DEFAULT_PROBES_PER_ROUND = 2
DEFAULT_EVAL_EXTRA = 2


class LeakError(RuntimeError):
    """A held-out (eval) mutant reached the synthesizer's input, or the mutant sets overlap."""


@dataclass(frozen=True)
class FreshPools:
    """Mutants never used before: extra held-out eval mutants and the ordered probe pool."""

    eval_extra: tuple[Mutant, ...]
    probe_pool: tuple[Mutant, ...]


def _seeded_key(seed: str, label: str, mutant: Mutant) -> str:
    return hashlib.sha256(f"{seed}|{label}|{mutant.sha256}".encode()).hexdigest()


def build_fresh_pools(
    patch: str,
    sources: dict[str, str],
    *,
    train: list[Mutant],
    evals: list[Mutant],
    seed: str = DEFAULT_ROUND_SEED,
    eval_extra_limit: int = DEFAULT_EVAL_EXTRA,
    extra_operators: bool = True,
) -> FreshPools:
    """Fresh mutants of the reference patch, disjoint from ``train`` and ``evals`` by hash.

    The pool is ordered by a seeded hash. Its first ``min(eval_extra_limit, len // 2)`` mutants become
    extra held-out eval mutants; every other mutant on a line that no eval mutant (old or extra) touches
    is a probe candidate, so a probe survivor shown to the synthesizer never points at an eval line.
    """
    used = {m.sha256 for m in (*train, *evals)}
    pool: dict[str, Mutant] = {}
    for path, lines in sorted(python_targets(patch).items()):
        if path not in sources:
            continue
        for mutant in fresh_mutants(
            path, sources[path], lines, exclude_sha256=used, extra_operators=extra_operators
        ):
            pool.setdefault(mutant.sha256, mutant)
    ordered = sorted(pool.values(), key=lambda m: _seeded_key(seed, "pool", m))
    extra = ordered[: min(eval_extra_limit, len(ordered) // 2)]
    eval_lines = {(m.path, m.line) for m in (*evals, *extra)}
    probes = [m for m in ordered[len(extra) :] if (m.path, m.line) not in eval_lines]
    return FreshPools(tuple(extra), tuple(probes))


def assert_disjoint(train: list[Mutant], probes: list[Mutant], evals: list[Mutant]) -> None:
    """Raise :class:`LeakError` unless train, probe and eval are pairwise disjoint by hash and probes avoid eval lines."""
    sets = {
        "train": {m.sha256 for m in train},
        "probe": {m.sha256 for m in probes},
        "eval": {m.sha256 for m in evals},
    }
    for left, right in (("train", "probe"), ("train", "eval"), ("probe", "eval")):
        shared = sets[left] & sets[right]
        if shared:
            raise LeakError(f"{left} and {right} mutants overlap: {sorted(shared)[:3]}")
    eval_lines = {(m.path, m.line) for m in evals}
    touching = [m for m in probes if (m.path, m.line) in eval_lines]
    if touching:
        raise LeakError(f"probe mutants touch eval lines: {[(m.path, m.line) for m in touching][:3]}")


def assert_prompt_blind(prompt: str, eval_diffs: list[str]) -> None:
    """Raise :class:`LeakError` when any held-out mutant diff appears in a synthesizer prompt."""
    for diff in eval_diffs:
        if diff and diff in prompt:
            raise LeakError("an eval mutant diff is present in the synthesis prompt")


@dataclass
class RoundRecord:
    round: int
    test_sha256: str | None
    train_shas: list[str]
    probe_shas: list[str]
    grades: dict[str, Any]
    guard: str  # passed | repaired | reverted | unmeasured | synthesis_failed
    survivors: list[str]
    killed: list[str]
    unmeasured: list[str]


@dataclass
class RoundsOutcome:
    records: list[RoundRecord]
    accepted_tests: dict[int, str]
    stop_reason: str

    def final_round(self, arm_rounds: int) -> int | None:
        """Last accepted round no later than ``arm_rounds``: the test that a run of that many rounds ships."""
        accepted = [r for r in self.accepted_tests if r <= arm_rounds]
        return max(accepted) if accepted else None


#: ``build_prompt(round_index, train, survivors, previous_test, feedback) -> prompt``; ``train`` excludes
#: ``survivors`` (they are shown once, in their own section).
PromptBuilder = Callable[[int, list[Mutant], list[Mutant], str | None, str | None], str]
#: ``generate(round_index, prompt) -> code | None``; the only place a model is called, after the blindness check.
Generator = Callable[[int, str], str | None]
#: ``grade(round_index, code, probes) -> {"reference": r, "probe:<sha>": r, ...}`` (None = unmeasured); an optional
#: ``"reference_output"`` string (grader output tail) is shown to the self-repair attempt and never stored.
Grader = Callable[[int, str, list[Mutant]], dict[str, Any]]


def run_rounds(
    *,
    rounds: int,
    train: list[Mutant],
    probe_pool: list[Mutant],
    evals: list[Mutant],
    eval_diffs: list[str],
    build_prompt: PromptBuilder,
    generate: Generator,
    grade: Grader,
    probes_per_round: int = DEFAULT_PROBES_PER_ROUND,
) -> RoundsOutcome:
    """Feedback loop: round r synthesizes from every train mutant known so far, grades the reference and a
    fresh probe slice, and the probes the new test still accepts become training mutants of round r+1.

    ``evals`` and ``eval_diffs`` are used only for disjointness and blindness checks: every prompt is checked
    before ``generate`` is called, so a held-out mutant never reaches the model and an overlap raises
    :class:`LeakError`. The reference must pass after every accepted round. A reference that was measured and
    failed (reward other than 1.0) gets one self-repair attempt with the grader output, then the round is
    reverted (``guard_reverted``). A reference that could not be measured (reward ``None``: spend cap, Modal
    error) is not a failure: no second model call, the round is not accepted, the loop stops with
    ``reference_unmeasured`` and the earlier accepted tests stay, so the caller can regrade and resume. Stops
    early when every probe dies, when no probe could be measured, and after ``rounds``; round 1 always runs.
    """
    if rounds < 1:
        raise ValueError("rounds must be >= 1")
    if probes_per_round < 1:
        raise ValueError("probes_per_round must be >= 1")
    assert_disjoint(train, probe_pool, evals)
    known = list(train)
    survivors: list[Mutant] = []
    cursor = 0
    previous: str | None = None
    outcome = RoundsOutcome(records=[], accepted_tests={}, stop_reason="")

    def attempt(index: int, feedback: str | None) -> str | None:
        latest = {m.sha256 for m in survivors}
        prompt = build_prompt(
            index, [m for m in known if m.sha256 not in latest], list(survivors), previous, feedback
        )
        assert_prompt_blind(prompt, eval_diffs)
        return generate(index, prompt)

    for index in range(1, rounds + 1):
        probes = list(probe_pool[cursor : cursor + probes_per_round])
        cursor += len(probes)
        guard = "passed"
        grades: dict[str, Any] = {}
        code = attempt(index, None)
        if code is None:
            guard = "synthesis_failed"
        else:
            grades = grade(index, code, probes)
            reference = grades.get("reference")
            if reference is None:
                guard = "unmeasured"
            elif reference != 1.0:
                feedback = f"the reference solution scored {reference}, it must score 1.0"
                output = grades.get("reference_output")
                if isinstance(output, str) and output.strip():
                    feedback += "\n\nGrader output on the reference solution (tail):\n" + output.strip()
                code = attempt(index, feedback)
                guard = "repaired"
                grades = grade(index, code, probes) if code is not None else {}
                reference = grades.get("reference")
                if code is None or (reference is not None and reference != 1.0):
                    guard = "reverted"
                elif reference is None:
                    guard = "unmeasured"
        grades = {k: v for k, v in grades.items() if k != "reference_output"}
        probe_shas = [m.sha256 for m in probes]
        measured = {sha: grades.get(f"probe:{sha}") for sha in probe_shas}
        record = RoundRecord(
            round=index,
            test_sha256=hashlib.sha256(code.encode()).hexdigest() if code else None,
            train_shas=[m.sha256 for m in known],
            probe_shas=probe_shas,
            grades=grades,
            guard=guard,
            survivors=[sha for sha, r in measured.items() if r == 1.0],
            killed=[sha for sha, r in measured.items() if r == 0.0],
            unmeasured=[sha for sha, r in measured.items() if r not in (0.0, 1.0)],
        )
        outcome.records.append(record)
        if guard in ("reverted", "unmeasured", "synthesis_failed") or code is None:
            outcome.stop_reason = {
                "reverted": "guard_reverted",
                "unmeasured": "reference_unmeasured",
            }.get(guard, guard)
            return outcome
        outcome.accepted_tests[index] = code
        previous = code
        if index == rounds:
            outcome.stop_reason = "rounds_complete"
            return outcome
        if not probes:
            outcome.stop_reason = "probe_pool_exhausted"
            return outcome
        if not record.survivors:
            outcome.stop_reason = "all_probes_killed" if not record.unmeasured else "probes_unmeasured"
            return outcome
        by_sha = {m.sha256: m for m in probes}
        survivors = [by_sha[sha] for sha in record.survivors]
        known.extend(survivors)
    outcome.stop_reason = "rounds_complete"
    return outcome


@dataclass(frozen=True)
class CertificationCell:
    """One control, keyed by hash rather than potentially colliding line/operator."""

    name: str
    arm: str
    package: Path
    script: str
    marker: str
    key: str = ""


def plan_certification_cells(
    *, package: Path, upgraded: Path, reference_patch: str,
    train: list[Mutant], evals: list[Mutant],
    alternatives: list[str], witnesses: list[str],
) -> list[CertificationCell]:
    """Plan six research arms plus correct alternatives; evals never feed synthesis."""
    assert_disjoint(train, [], evals)
    wd = tomllib.loads((upgraded / "task.toml").read_text())["environment"]["workdir"]
    cells = [
        CertificationCell("reference", "reference", upgraded, reference_script(wd, reference_patch), "REFERENCE_APPLIED"),
        CertificationCell("nop", "nop", upgraded, nop_script(wd), "NOP_APPLIED"),
        CertificationCell("exit0_pth", "exit0_pth", upgraded, exit0_pth_script(wd), "EXIT0_PTH_APPLIED"),
    ]
    for index, witness in enumerate(witnesses or [None]):
        cells.append(CertificationCell(
            f"witness-{index}", "witness", upgraded, witness_script(wd, witness), "WITNESS_APPLIED",
        ))
    for arm, pool, target in (("train", train, upgraded), ("eval", evals, upgraded), ("evalbase", evals, package)):
        workdir = tomllib.loads((target / "task.toml").read_text())["environment"]["workdir"]
        for mutant in pool:
            cells.append(CertificationCell(
                f"{arm}-{mutant.sha256}", arm, target,
                mutant_script(workdir=workdir, reference_patch=reference_patch, mutant=mutant),
                f"MUTANT_APPLIED {mutant.operator} {mutant.path}:{mutant.line} {mutant.sha256}",
                mutant.sha256,
            ))
    for index, patch in enumerate(alternatives):
        cells.append(CertificationCell(
            f"alternative-{index}", "alternative", upgraded, reference_script(wd, patch),
            "REFERENCE_APPLIED", hashlib.sha256(patch.encode()).hexdigest(),
        ))
    return cells


def summarize_certification(
    *, task_id: str, upgraded: Path, test_lines: int,
    cells: list[CertificationCell], outcomes: dict[str, dict[str, Any]],
) -> TaskCertificationResult:
    """Missing, infra-failed or unapplied cells remain unmeasured, never killed."""
    arms: dict[str, dict[str, float | None]] = {}
    for cell in cells:
        raw = outcomes.get(cell.name, {})
        reward = raw.get("reward")
        measured = (
            raw.get("status") == "executed"
            and cell.marker in raw.get("agent_log", "")
            and type(reward) in (int, float) and reward in (0.0, 1.0)
        )
        arms.setdefault(cell.arm, {})[cell.key or cell.name] = (
            float(reward) if measured and isinstance(reward, int | float) else None
        )
    train, evals, base = (arms.get(arm, {}) for arm in ("train", "eval", "evalbase"))
    witnesses = arms.get("witness", {})
    alternatives = arms.get("alternative", {})
    ref = arms.get("reference", {}).get("reference")
    nop = arms.get("nop", {}).get("nop")
    pth = arms.get("exit0_pth", {}).get("exit0_pth")
    return TaskCertificationResult(
        task_id=task_id, kind="gold", upgraded_package=str(upgraded), test_lines=test_lines,
        train_mutants_total=len(train), train_mutants_killed=sum(r == 0.0 for r in train.values()),
        eval_mutants_total=len(evals), eval_mutants_killed=sum(r == 0.0 for r in evals.values()),
        eval_mutants_base_accepted=sum(r == 1.0 for r in base.values()),
        eval_mutants_base_accepted_killed=sum(r == 1.0 and evals.get(k) == 0.0 for k, r in base.items()),
        gate1_reference_passed=ref == 1.0, gate2_nop_passed=nop == 0.0,
        gate3_witness_passed=bool(witnesses) and all(r == 0.0 for r in witnesses.values()),
        gate4_exit0_pth_passed=pth == 0.0,
        reference_reward=ref, nop_reward=nop, exit0_pth_reward=pth,
        witness_reward=next(iter(witnesses.values())) if len(witnesses) == 1 else None,
        witness_rewards=witnesses, train_rewards=train, eval_rewards=evals, eval_base_rewards=base,
        alternative_solutions_total=len(alternatives) if alternatives else None,
        alternative_solutions_accepted=(
            sum(r == 1.0 for r in alternatives.values())
            if alternatives and all(r is not None for r in alternatives.values()) else None
        ),
        alternative_solution_rewards=alternatives,
    )


def synthesize_contract(prompt: str, out_dir: Path, *, model: str) -> str:
    """Use the existing OMP subscription route, without task-specific bridge paths."""
    proc = subprocess.run(
        ["omp", "-p", "--model", model, "--thinking", "high", "--no-tools", "--no-skills",
         "--session-dir", str(out_dir / "sessions"), prompt],
        capture_output=True, text=True, timeout=600, check=False,
    )
    (out_dir / "synthesis.stdout").write_text(proc.stdout)
    (out_dir / "synthesis.stderr").write_text(proc.stderr)
    code = extract_code_block(proc.stdout) if proc.returncode == 0 else None
    if code is None:
        raise RuntimeError(f"contract synthesis failed (exit {proc.returncode}); see {out_dir}")
    return code


def certify_task(
    *, task_id: str, package: Path, reference_patch: str, out_dir: Path,
    run_cell: CellRunner, synthesize: Callable[[str, Path], str],
    stage: Callable[..., tuple[HardeningManifest, str]] = stage_and_harden_package,
    limit: int = 8, alternative_patches: list[str] | None = None,
    witnesses: list[str] | None = None, contract_test: str | None = None,
    hook_command: str | None = None, python_version: str = "3",
    harden_options: dict[str, Any] | None = None,
) -> TaskCertificationResult:
    """Probe, synthesize from train only, harden, execute and persist gate evidence."""
    from evallab.registry import task_directory_digest

    package, out_dir = package.resolve(), out_dir.resolve()
    if out_dir == package or package in out_dir.parents:
        raise ValueError("output must be outside the source package")
    if limit < 2:
        raise ValueError("--limit must be >= 2 for train and held-out evaluation")
    alternatives = alternative_patches or []
    if reference_patch in alternatives or len(set(alternatives)) != len(alternatives):
        raise ValueError("alternative solutions must be distinct from each other and the reference")
    out_dir.mkdir(parents=True, exist_ok=False)
    provenance = {"task_id": task_id, "base_package_digest": task_directory_digest(package),
                  "reference_patch_sha256": hashlib.sha256(reference_patch.encode()).hexdigest(),
                  "alternative_patch_sha256": [hashlib.sha256(p.encode()).hexdigest() for p in alternatives]}
    _write_certification_json(out_dir / "inputs.json", provenance)
    wd = tomllib.loads((package / "task.toml").read_text())["environment"]["workdir"]
    probe_dir = out_dir / "cells" / "probe"
    probe = run_cell(probe_dir, package, probe_script(
        workdir=wd, reference_patch=reference_patch, targets=sorted(python_targets(reference_patch)),
    ))
    _write_certification_json(probe_dir / "result.json", probe)
    sources = parse_probe(probe.get("agent_log", ""))
    if probe.get("status") != "executed" or probe.get("reward") != 1.0 or not sources:
        raise RuntimeError("reference source probe failed; no certification")
    if set(python_targets(reference_patch)) - sources.keys():
        raise RuntimeError("reference source probe omitted Python targets")
    train, evals = split_mutants(mutants_for_patch(reference_patch, sources, limit=limit))
    if not train or not evals:
        raise ValueError("insufficient valid mutants for train and held-out evaluation")
    _write_certification_json(out_dir / "mutants.json", {
        "train": [m.summary() for m in train], "eval": [m.summary() for m in evals],
    })
    hook = hook_command or extract_hook_command_from_test_sh((package / "tests/test.sh").read_text())
    code = contract_test
    if code is None:
        hidden = "\n\n".join(
            f"{p.name}:\n{p.read_text(errors='replace')}"
            for p in sorted((package / "tests").rglob("*"))
            if p.is_file() and p.suffix in (".py", ".patch", ".sh")
        )
        prompt = build_synthesis_prompt(
            instruction=(package / "instruction.md").read_text(), hidden_tests=hidden,
            reference_patch=reference_patch, train_mutants=train,
            train_diffs=[mutant_diff(m, sources[m.path]) for m in train],
            interpreter_command=hook, python_version=python_version,
        )
        assert_prompt_blind(prompt, [mutant_diff(m, sources[m.path]) for m in evals])
        (out_dir / "prompt.txt").write_text(prompt)
        code = synthesize(prompt, out_dir)
    valid, reason = validate_contract_test(code)
    if not valid:
        raise ValueError(f"invalid contract test: {reason}")
    (out_dir / GOLD_CONTRACT_TEST_NAME).write_text(code)
    upgraded = out_dir / "package"
    manifest, _ = stage(package, upgraded, code, hook, single_container=False,
                        **(harden_options or {}))
    _write_certification_json(out_dir / "hardening.json", manifest.to_json())
    cells = plan_certification_cells(
        package=package, upgraded=upgraded, reference_patch=reference_patch,
        train=train, evals=evals, alternatives=alternatives, witnesses=witnesses or [],
    )
    _write_certification_json(out_dir / "plan.json", [
        {**asdict(c), "package": str(c.package)} for c in cells
    ])
    outcomes: dict[str, dict[str, Any]] = {}
    try:
        for cell in cells:
            cell_dir = out_dir / "cells" / cell.name
            cell_dir.mkdir(parents=True, exist_ok=True)
            (cell_dir / "agent-script.sh").write_text(cell.script)
            outcomes[cell.name] = run_cell(cell_dir, cell.package, cell.script)
            _write_certification_json(cell_dir / "result.json", outcomes[cell.name])
    finally:
        result = summarize_certification(
            task_id=task_id, upgraded=upgraded, test_lines=len(code.splitlines()),
            cells=cells, outcomes=outcomes,
        )
        result.package_digest = task_directory_digest(upgraded)
        _write_certification_json(out_dir / "certification.json", result.summary())
        _write_certification_json(out_dir / "summary.json", GoldMatrixSummary([result]).to_report_dict())
    return result


def _write_certification_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- CLI Parser


def build_certify_gold_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "certify-gold",
        help="Certify a task with behavioral tests, separate-verifier hardening and seven gates",
    )
    parser.add_argument("--package", type=Path, required=True, help="Base task package directory")
    parser.add_argument("--reference-patch", type=Path, required=True, help="Reference patch file")
    parser.add_argument("--out", type=Path, help="Output directory for hardened package")
    parser.add_argument("--task-id", type=str, help="Task identifier (e.g. format-code-task-000079)")
    parser.add_argument("--approval", help="Recorded approval text for Modal execution")
    parser.add_argument("--max-usd", type=float, help="Required explicit Modal cap in USD")
    parser.add_argument("--per-execution-usd", type=float, default=0.005)
    parser.add_argument("--ledger", type=Path, help="Persisted spend ledger (defaults beside --out)")
    parser.add_argument("--app-name", default="gold-certifier")
    parser.add_argument("--alt-solution", action="append", type=Path, default=[],
                        help="Independent correct patch; repeat for Gate 7")
    parser.add_argument("--witness", action="append", type=Path, default=[],
                        help="Existing tamper witness solve.sh (default: conftest control)")
    parser.add_argument("--contract-test", type=Path, help="Use an operator-supplied unittest instead of synthesis")
    parser.add_argument("--hook-command", help="Override supplementary test interpreter command")
    parser.add_argument("--python-version", default="3", choices=("3", "2.7"))
    parser.add_argument("--expected-tests", type=int, help="Reference test count for strict L3 hardening")
    parser.add_argument("--allow-degraded", action="store_true",
                        help="Explicitly allow at-least-one-test hardening instead of a calibrated count")
    parser.add_argument("--limit", type=int, default=8, help="Max mutants to enumerate")
    parser.add_argument(
        "--model",
        type=str,
        default="google-antigravity/gemini-3.8-flash",
        help="Model identifier for synthesis",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Offline planning: show parameters and Python targets without executing or synthesizing",
    )
    parser.add_argument("--json", action="store_true", help="Output summary as JSON")
    parser.add_argument(
        "--rounds",
        type=int,
        default=1,
        help="Synthesis rounds; round r+1 is fed the probe mutants that survived round r (default 1)",
    )
    parser.add_argument(
        "--probes-per-round",
        type=int,
        default=DEFAULT_PROBES_PER_ROUND,
        help="Fresh probe mutants graded per round (never train or eval mutants)",
    )
    parser.set_defaults(func=_certify_gold_command)


def _certify_gold_command(args: argparse.Namespace, root: Path, *, harbor: Any = None) -> int:
    patch = args.reference_patch.read_text(encoding="utf-8")
    targets = python_targets(patch)
    if not targets:
        print(f"refused: no Python targets in {args.reference_patch}", file=sys.stderr)
        return 1

    package = args.package.resolve()
    task_id = args.task_id or package.name
    if args.rounds < 1 or args.probes_per_round < 1:
        print("error: --rounds and --probes-per-round must be >= 1", file=sys.stderr)
        return 1

    if args.plan:
        plan_dict = {
            "task_id": task_id,
            "package": str(package),
            "targets": sorted(targets.keys()),
            "model": args.model,
            "metered_cap_usd": args.max_usd if args.max_usd is not None else 0.95,
            "mutant_limit": args.limit,
            "rounds": args.rounds,
            "probes_per_round": args.probes_per_round,
        }
        if args.json:
            print(json.dumps(plan_dict, indent=2))
        else:
            print(f"Plan for {task_id}:")
            print(f"  package: {package}")
            print(f"  targets: {sorted(targets.keys())}")
            print(f"  model: {args.model}")
            print(f"  metered cap: ${plan_dict['metered_cap_usd']:.2f}")
        return 0

    if not args.out:
        print("error: --out is required unless --plan", file=sys.stderr)
        return 1
    if args.rounds > 1:
        print(
            "error: --rounds > 1 is not implemented in the command (use evallab.gold_certifier.run_rounds); "
            "only --plan accepts it",
            file=sys.stderr,
        )
        return 2

    if not args.approval or not args.approval.strip():
        raise ValueError("Modal execution requires --approval (recorded approval text)")
    if args.max_usd is None:
        raise ValueError("Modal execution requires an explicit --max-usd")
    from evallab.regrade import CloudSpendGate

    gate = CloudSpendGate(
        approval=args.approval, cap_usd=args.max_usd,
        usd_per_execution=args.per_execution_usd,
        spend_ledger=args.ledger or args.out.resolve().with_suffix(".spend.json"),
        modal_app_name=args.app_name, billing_tag=args.app_name,
    )
    meter = SandboxMeter(args.max_usd)
    result = certify_task(
        task_id=task_id, package=package, reference_patch=patch, out_dir=args.out,
        run_cell=modal_cell_runner(repo_root=root, spend_gate=gate, meter=meter, timeout_seconds=240),
        synthesize=lambda prompt, output: synthesize_contract(prompt, output, model=args.model),
        limit=args.limit, alternative_patches=[p.read_text() for p in args.alt_solution],
        witnesses=[p.read_text() for p in args.witness],
        contract_test=args.contract_test.read_text() if args.contract_test else None,
        hook_command=args.hook_command, python_version=args.python_version,
        harden_options={"expected_tests": args.expected_tests, "allow_degraded": args.allow_degraded},
    )
    output = {**result.summary(), "estimated_usd": round(meter.spent, 5),
              "executions": meter.cells, "receipt": str(args.out / "certification.json")}
    if args.json:
        print(json.dumps(output, indent=2))
    else:
        print(f"{task_id}: {result.status}; receipt: {output['receipt']}")
        print(f"Gate 7 alternatives: {result.alternative_solutions_accepted}/{result.alternative_solutions_total}")
        for sha, reward in result.alternative_solution_rewards.items():
            if reward != 1.0:
                print(f"WRONG REJECTION / UNMEASURED: alternative {sha} reward={reward}")
    return 1 if result.status == "failed" else 0
