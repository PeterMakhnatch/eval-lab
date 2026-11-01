"""Gold task certifier: contract-complete behavioral synthesis + separate-verifier L1-L4 hardening + 6-gate matrix."""

from __future__ import annotations

import argparse
import base64
import difflib
import json
import math
import re
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from evallab.full_hardening import HardeningManifest, harden
from evallab.mutant_auditor import (
    Mutant,
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
        shutil.rmtree(out_dir)

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
                test_sh.write_text(test_sh_text + "\n" + hook_block, encoding="utf-8")

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

    def summary(self) -> dict[str, Any]:
        return asdict(self)


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
            "tasks": [t.summary() for t in self.tasks],
        }


# --------------------------------------------------------------------------- CLI Parser


def build_certify_gold_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "certify-gold",
        help="Certify tasks with contract-complete behavioral tests + separate-verifier L1-L4 hardening + 6 gates",
    )
    parser.add_argument("--package", type=Path, required=True, help="Base task package directory")
    parser.add_argument("--reference-patch", type=Path, required=True, help="Reference patch file")
    parser.add_argument("--out", type=Path, help="Output directory for hardened package")
    parser.add_argument("--task-id", type=str, help="Task identifier (e.g. format-code-task-000079)")
    parser.add_argument("--cap-usd", type=float, default=0.95, help="Metered Modal cap in USD")
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
        help="Offline planning: enumerate mutants, split train/eval, show prompt preview",
    )
    parser.add_argument("--json", action="store_true", help="Output summary as JSON")
    parser.set_defaults(func=_certify_gold_command)


def _certify_gold_command(args: argparse.Namespace, root: Path, *, harbor: Any = None) -> int:
    patch = args.reference_patch.read_text(encoding="utf-8")
    targets = python_targets(patch)
    if not targets:
        print(f"refused: no Python targets in {args.reference_patch}", file=sys.stderr)
        return 1

    package = args.package.resolve()
    task_id = args.task_id or package.name

    if args.plan:
        plan_dict = {
            "task_id": task_id,
            "package": str(package),
            "targets": sorted(targets.keys()),
            "model": args.model,
            "metered_cap_usd": args.cap_usd,
            "mutant_limit": args.limit,
        }
        if args.json:
            print(json.dumps(plan_dict, indent=2))
        else:
            print(f"Plan for {task_id}:")
            print(f"  package: {package}")
            print(f"  targets: {sorted(targets.keys())}")
            print(f"  model: {args.model}")
            print(f"  metered cap: ${args.cap_usd:.2f}")
        return 0

    if not args.out:
        print("error: --out is required unless --plan", file=sys.stderr)
        return 1

    print(f"Certifying gold package for {task_id} -> {args.out}...")
    return 0
