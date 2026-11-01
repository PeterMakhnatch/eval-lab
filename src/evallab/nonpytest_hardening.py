"""``nonpytest-hardening@1``: close the wrong-pass routes Stream A left open on non-pytest MiMo runners.

``grader-hardening@1`` gates pytest packages on a JUnit report (L3). It cannot cover a package whose runner is
vitest, mocha or unittest, and it cannot cover a pytest package whose wrapper clears ``PYTEST_ADDOPTS``. Stream A
measured those eight strictly-DEV ``format-code`` packages as **L1-only**, and K2's surface map showed why that is not
enough: the upstream ``tests/test.sh`` resets the files the hidden patch touches plus ``mimo_test_command.sh``, and
stops. A JS runner reads ``package.json`` and its own config, and unittest reads discovery and site hooks, so the
agent can still change what "the tests" means without touching a file the grader restores.

This transform adds two layers on top of the upstream reset:

* **R1 — runner-config reset.** Restore or delete ``package.json``, ``vitest.config.*``, ``vite.config.*``,
  ``.mocharc.*``, ``jest.config.*`` and ``.npmrc`` from the base commit by pattern, delete untracked ones outside
  dependency directories, and scrub the environment variables that redirect a runner (``NODE_OPTIONS``,
  ``PYTEST_ADDOPTS``, ``VITEST_*``, ``MOCHA_*``, ``NODE_PATH``) before the hidden tests run.
* **R2 — summary gate.** The reward is 1 only when the command exits 0 **and** the runner's own summary line reports
  at least the reference number of passing tests, no failures and no extra skips. The expected count is calibrated by
  running the package's reference solution once; a package without it is refused unless ``allow_degraded`` is set, so
  "one passing test" can never be the gate by accident.

Known limits, stated rather than hidden: the gate parses the runner's summary text (vitest/mocha/unittest print one;
pytest packages that clear ``PYTEST_ADDOPTS`` get the same treatment instead of a JUnit file), so a submission that
prints a forged summary *after* the runner's own line is not caught by this layer alone — combine it with the
dependency-directory exclusion and, where the runner allows it, prefer a JSON/JUnit reporter. Code running in the
grader's own interpreter still needs a separate verifier (unchanged from ``grader-hardening@1``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from evallab.grader_hardening import _FINAL_ECHO, _build_env_command
from evallab.separate_verifier import resolve_command_text
from evallab.task_variants import VariantInvalid, VariantRecord, derive_task

TRANSFORM_ID = "nonpytest-hardening@1"
TEST_SH_REL = "tests/test.sh"
EXPECTED_REL = "tests/expected_runner_report.json"

RUNNER_CONFIG_PATTERN = (
    "package.json|*/package.json|vitest.config.*|*/vitest.config.*|vite.config.*|*/vite.config.*|"
    ".mocharc.*|*/.mocharc.*|jest.config.*|*/jest.config.*|.npmrc|*/.npmrc|"
    "sitecustomize.py|*/sitecustomize.py|usercustomize.py|*/usercustomize.py|"
    "pytest.ini|*/pytest.ini|tox.ini|*/tox.ini|conftest.py|*/conftest.py|*.pth"
)
ENV_SCRUB = "NODE_OPTIONS PYTEST_ADDOPTS NODE_PATH VITEST_POOL_ID VITEST_MAX_THREADS MOCHA_TIMEOUT"

_RESET_END = '); then echo "could not reset the test files (testbed problem, not scored)" >&2; exit 1; fi\n'
_RUN_LINE = 'timeout 1800 sh -c "$(cat /tests/test_command.sh)" > "$V/test_output.log" 2>&1\n'
_REWARD_LINE = 'if [ $RC -eq 0 ]; then echo 1 > "$V/reward.txt"; else echo 0 > "$V/reward.txt"; fi\n'

_FAMILY_MARKERS = (
    ("vitest", ("vitest",)),
    ("mocha", ("mocha", "mocharc")),
    ("jest", ("jest",)),
    ("unittest", ("unittest",)),
    ("pytest", ("pytest",)),
)


def runner_family(text: str) -> str | None:
    """Best-effort runner family from a package's command text; ``None`` when nothing matches."""
    low = text.lower()
    for family, markers in _FAMILY_MARKERS:
        if any(marker in low for marker in markers):
            return family
    return None


def parse_runner_summary(family: str, text: str) -> dict[str, int] | None:
    """Counts from the runner's own summary line, or ``None`` when no summary is present.

    vitest:  ``Tests  12 passed (12)`` / ``Tests  2 failed | 10 passed``
    mocha:   ``12 passing (34ms)`` / ``3 failing`` / ``1 pending``
    unittest: ``Ran 12 tests`` plus ``OK`` or ``FAILED (failures=1)``
    pytest:  ``12 passed`` / ``2 failed, 10 passed``
    """
    if "Ran " in text and re.search(r"^Ran \d+ tests? in ", text, re.MULTILINE):
        ran = re.search(r"^Ran (\d+) tests? in ", text, re.MULTILINE)
        if ran is None:
            return None
        failed = 1 if re.search(r"^FAILED\b", text, re.MULTILINE) else 0
        return {
            "passed": max(int(ran.group(1)) - failed, 0),
            "failed": failed,
            "skipped": len(re.findall(r"\.\.\. skipped", text)),
        }
    if family in {"vitest", "mocha", "jest", "pytest"}:
        passed = failed = skipped = 0
        found = False
        for pattern, key in (
            (r"(\d+) (?:passed|passing)", "passed"),
            (r"(\d+) (?:failed|failing)", "failed"),
            (r"(\d+) (?:skipped|pending|todo)", "skipped"),
        ):
            # runners print their own summary last; "Test Files 1 passed" precedes "Tests 12 passed"
            matches = re.findall(pattern, text)
            if matches:
                found = True
                value = int(matches[-1])
                if key == "passed":
                    passed = value
                elif key == "failed":
                    failed = value
                else:
                    skipped = value
        if not found:
            return None
        return {"passed": passed, "failed": failed, "skipped": skipped}
    return None


def _gate_python(family: str) -> str:
    """Inline python that turns the captured log into GATE_OK / GATE_NO."""
    return f"""python3 -I - "$V/test_output.log" "$EXPECTED_JSON" "{family}" <<'PYEOF' 2>/dev/null
import json, re, sys
log_path, expected_path, family = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    text = open(log_path, encoding="utf-8", errors="replace").read()
    want = json.load(open(expected_path))
except Exception:
    print("GATE_NO"); raise SystemExit(0)
counts = None
if re.search(r"^Ran \d+ tests? in ", text, re.MULTILINE):
    ran = re.search(r"^Ran (\\d+) tests? in ", text, re.MULTILINE)
    if ran:
        bad = 1 if re.search(r"^FAILED\\b", text, re.MULTILINE) else 0
        counts = {{"passed": max(int(ran.group(1)) - bad, 0), "failed": bad,
                  "skipped": len(re.findall(r"\\.\\.\\. skipped", text))}}
else:
    p = re.search(r"(\\d+) (?:passed|passing)", text)
    if p:
        counts = {{"passed": int(p.group(1))}}
        for key, pat in (("failed", r"(\\d+) (?:failed|failing)"),
                         ("skipped", r"(\\d+) (?:skipped|pending|todo)")):
            m = re.search(pat, text)
            counts[key] = int(m.group(1)) if m else 0
ok = (
    counts is not None
    and counts["passed"] >= max(int(want.get("passed", 0)), 1)
    and counts["failed"] == 0
    and counts["skipped"] <= int(want.get("skipped", 0))
)
print("GATE_OK" if ok else "GATE_NO")
PYEOF"""


def build_reset_block() -> str:
    """R1: restore or delete runner config by pattern, then scrub the variables that redirect a runner."""
    return f"""\
# nonpytest-hardening@1 R1: reset runner config by pattern, then scrub runner-redirecting environment variables
np_is_runner_config() {{ case "$1" in {RUNNER_CONFIG_PATTERN}) return 0 ;; esac; return 1; }}
np_in_dependency_dir() {{
  case "/$1" in */node_modules/*|*/.venv/*|*/venv/*|*/site-packages/*|*/.tox/*|*/.eggs/*|*/__pypackages__/*) return 0 ;; esac
  return 1
}}
: > "$V/r1-removed.txt"
if ! (
  git ls-tree -r --name-only "$BASE" | while IFS= read -r f; do
    np_is_runner_config "$f" || continue
    git checkout "$BASE" -- "$f" || exit 1
  done
  {{ git ls-files --others --exclude-standard; git ls-files --others --ignored --exclude-standard; }} | sort -u | while IFS= read -r f; do
    np_is_runner_config "$f" || continue
    np_in_dependency_dir "$f" && continue
    git cat-file -e "$BASE:$f" 2>/dev/null && continue
    rm -f -- "$f" && echo "$f" >> "$V/r1-removed.txt"
  done
); then echo "could not reset the runner config (testbed problem, not scored)" >&2; exit 1; fi
unset {ENV_SCRUB}
"""


def _gate_after(family: str) -> str:
    return f"""\
GATE=GATE_NO
GATE=$(
{_gate_python(family)}
)
[ "$GATE" = GATE_OK ] || RC=1
"""


def build_test_sh(parent_text: str, *, family: str) -> str:
    """Parent ``tests/test.sh`` plus R1 and R2."""
    if "nonpytest-hardening@1" in parent_text:
        raise VariantInvalid("parent is already nonpytest-hardened")
    if parent_text.count(_RESET_END) != 1:
        raise VariantInvalid("parent tests/test.sh does not have the expected reset-failure line exactly once")
    if parent_text.count(_REWARD_LINE) != 1:
        raise VariantInvalid("parent tests/test.sh does not have the expected reward line exactly once")
    text = parent_text.replace(_RESET_END, _RESET_END + build_reset_block(), 1)
    text = text.replace(
        _REWARD_LINE,
        "EXPECTED_JSON=/tests/expected_runner_report.json\n" + _gate_after(family) + _REWARD_LINE,
        1,
    )
    return text.replace(_FINAL_ECHO, 'echo "test command exited $RC (gate $GATE)"\n', 1)


def expected_report(passed: int, skipped: int) -> bytes:
    if passed < 1 or skipped < 0:
        raise VariantInvalid(f"implausible reference summary: passed={passed} skipped={skipped}")
    return (json.dumps({"passed": passed, "skipped": skipped}, sort_keys=True) + "\n").encode()


def build_changes(
    parent_dir: Path | str,
    *,
    family: str | None = None,
    expected_passed: int | None,
    expected_skipped: int = 0,
    allow_degraded: bool = False,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """The ``derive_task`` changes mapping plus lineage inputs for one non-pytest package."""
    parent = Path(parent_dir)
    try:
        parent_text = (parent / TEST_SH_REL).read_text(encoding="utf-8")
    except OSError as exc:
        raise VariantInvalid(f"parent has no readable {TEST_SH_REL}: {exc}") from exc
    command = (parent / "tests" / "test_command.sh")
    command_text = command.read_text(encoding="utf-8", errors="replace") if command.is_file() else ""
    patch = (parent / "tests" / "test.patch")
    patch_text = patch.read_text(encoding="utf-8", errors="replace") if patch.is_file() else ""
    resolved = resolve_command_text(command_text, None, patch_text) + _build_env_command(patch_text)
    family = family or runner_family(parent_text + command_text + resolved)
    if family is None:
        raise VariantInvalid(
            "cannot identify the runner family from the package files (the runner command is generated at image "
            "build); pass family='vitest'|'mocha'|'jest'|'unittest'|'pytest'"
        )
    if expected_passed is None and not allow_degraded:
        raise VariantInvalid(
            "the reference pass count is required (run the package's oracle once); "
            "pass allow_degraded=True to accept 'at least one passing test'"
        )
    changes: dict[str, bytes | None] = {
        TEST_SH_REL: build_test_sh(parent_text, family=family).encode("utf-8"),
        EXPECTED_REL: (
            expected_report(expected_passed, expected_skipped)
            if expected_passed is not None
            else (json.dumps({"passed": 1, "skipped": 0}, sort_keys=True) + "\n").encode()
        ),
    }
    inputs: dict[str, Any] = {
        "layers": ["R1", "R2"],
        "runner_family": family,
        "runner_config_pattern": RUNNER_CONFIG_PATTERN,
        "environment_scrubbed": ENV_SCRUB.split(),
        "expected_passed": expected_passed,
        "expected_skipped": expected_skipped if expected_passed is not None else None,
        "degraded_count_gate": expected_passed is None,
        "gate_basis": "runner summary line (passed/failed/skipped)",
        "known_open": "a forged summary printed after the runner's own line is not caught; prefer a JSON reporter where the runner supports one",
    }
    return changes, inputs


def derive_nonpytest_hardening(
    parent_dir: Path | str,
    *,
    family: str | None = None,
    expected_passed: int | None,
    expected_skipped: int = 0,
    allow_degraded: bool = False,
    rationale: str = (
        "Reset runner configuration and scrub runner-redirecting environment variables, then gate the reward on the "
        "runner's own pass/fail/skip summary so a wrong submission cannot pass by changing what the runner runs."
    ),
    created_by: str = "nonpytest-hardening",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive the ``nonpytest-hardening@1`` variant of one package whose runner is not plain pytest."""
    parent = Path(parent_dir)
    changes, inputs = build_changes(
        parent,
        family=family,
        expected_passed=expected_passed,
        expected_skipped=expected_skipped,
        allow_degraded=allow_degraded,
    )
    kwargs: dict[str, Any] = {}
    if repo_root is not None:
        kwargs["repo_root"] = repo_root
    if parent_source is not None:
        kwargs["parent_source"] = parent_source
    if variants_root is not None:
        kwargs["variants_root"] = variants_root
    return derive_task(
        parent,
        changes=changes,
        transform=TRANSFORM_ID,
        rationale=rationale,
        created_by=created_by,
        inputs=inputs,
        **kwargs,
    )


__all__ = [
    "TRANSFORM_ID",
    "ENV_SCRUB",
    "RUNNER_CONFIG_PATTERN",
    "EXPECTED_REL",
    "derive_nonpytest_hardening",
    "parse_runner_summary",
    "runner_family",
]
