from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from random import Random
from typing import Any

from evallab.package_anatomy import (
    _MAX_EVIDENCE,
    _MAX_LIST,
    MECHANISM_CLASSES,
    Hypothesis,
    TestSource,
    _artifact_only,
    analyze_package,
    build_hypotheses,
    load_package,
)
from evallab.sim.engine import STEP_LIMIT, Simulation

_STEP_BUDGET = 20
_CASES = 8
_MAX_FILES = 16
_HEX = "0123456789abcdef"
_RULES: dict[str, tuple[str, int, str]] = {
    "GT-BAKED-TESTS-EXECUTED": ("grader_tamperable", 95, "static_certain"),
    "GT-VERIFIER-RUNS-AGENT-CODE": ("grader_tamperable", 90, "static_certain"),
    "GT-AUTOLOAD-WRITABLE": ("grader_tamperable", 80, "static_probable"),
    "EG-EXIT-CODE-ONLY-GATE": ("exploit_grader_gap", 50, "static_probable"),
    "EG-FAILURE-MASKED": ("exploit_grader_gap", 85, "static_certain"),
    "EG-REWARD-UNCONDITIONAL": ("exploit_grader_gap", 90, "static_certain"),
    "GT-REWARD-PREWRITTEN": ("grader_tamperable", 55, "needs_execution"),
    "GT-SITE-PACKAGES-HOOK": ("grader_tamperable", 40, "needs_execution"),
    "GT-PATH-HIJACK": ("grader_tamperable", 35, "needs_execution"),
    "GT-HELPER-NOT-RESET": ("grader_tamperable", 60, "static_probable"),
    "AR-TESTS-IN-IMAGE": ("answer_reachable", 85, "static_certain"),
    "AR-SOLUTION-ARTIFACT-IN-IMAGE": ("answer_reachable", 88, "static_probable"),
    "AR-GIT-HISTORY": ("answer_reachable", 65, "static_probable"),
    "AR-BAKED-OUTSIDE-WORKDIR": ("answer_reachable", 70, "static_probable"),
    "AR-PUBLIC-NETWORK-UPSTREAM": ("answer_reachable", 45, "needs_execution"),
    "AR-PUBLIC-NETWORK-BLOCKLIST-EDITABLE": ("answer_reachable", 30, "needs_execution"),
    "AR-LITERAL-PAIRS-CARVE": ("answer_reachable", 70, "static_probable"),
    "WC-LITERAL-PAIRS": ("weak_check", 40, "static_probable"),
    "WC-INSTRUCTION-SYMBOL-UNTESTED": ("weak_check", 40, "static_probable"),
    "WC-REFERENCE-SYMBOL-UNEXERCISED": ("weak_check", 50, "static_probable"),
    "WC-REFERENCE-BRANCH-UNASSERTED": ("weak_check", 45, "static_probable"),
    "WC-TEST-WITHOUT-ASSERTION": ("weak_check", 55, "static_certain"),
    "WC-SURFACE-ONLY": ("weak_check", 60, "static_probable"),
    "WC-SOURCE-TEXT-ASSERT": ("weak_check", 65, "static_probable"),
    "WC-MOCK-ONLY": ("weak_check", 55, "static_probable"),
    "WC-NO-NEGATIVE-CASES": ("weak_check", 50, "static_probable"),
    "WC-TINY-SUITE": ("weak_check", 40, "static_certain"),
    "WC-NO-TESTS-FOUND": ("weak_check", 45, "static_probable"),
    "WC-DESELECTED-TESTS": ("weak_check", 70, "static_certain"),
    "EG-ARTIFACT-ONLY-CHECK": ("exploit_grader_gap", 72, "static_probable"),
    "EG-INSTRUCTION-RULE-UNENFORCED": ("exploit_grader_gap", 60, "static_probable"),
}
_REFERENCE_RULES = ("WC-REFERENCE-SYMBOL-UNEXERCISED", "WC-REFERENCE-BRANCH-UNASSERTED")
_ROW_COUNTS = (0, 0, 1, 5, 6, 7, 9)
_LAYOUTS = ("single_container", "single_container", "separate_verifier", "")
_RUNNERS = ("pytest", "unittest", "jest", "custom_script", "unknown")
_RUNNER_COMMANDS = {
    "pytest": "pytest -q {target}",
    "unittest": "python3 -m unittest discover tests",
    "jest": "npx jest",
    "custom_script": "python3 tests/run_checks.py",
    "unknown": "true",
}
_ISOLATION = ("noconftest", "python_isolated", "env_i", "config_nulled")
_ISOLATION_OTHER = ("plugin_autoload_disabled", "no_cacheprovider", "importlib_mode")
_MECHANISMS = (
    ("exit_code",),
    ("exit_code",),
    ("exit_code", "structured_report"),
    ("python_writes_reward",),
    ("exit_code", "python_writes_reward"),
    ("log_grep",),
    ("none_detected",),
)
_OPERANDS = {
    "relative": "tests",
    "absent": "",
    "option_value": "--basetemp /tmp/pytest-base",
    "inside": "/app/tests",
    "outside": "/tests",
}
_TEST_STEMS = ("test_alpha", "test_beta", "test_gamma", "test_delta", "test_omega")
_NAMED_WORDS = ("harness", "leak", "expected", "golden", "answer", "solution", "reference", "verif", "grader", "oracle")
_PLAIN_WORDS = ("data", "cache", "vendor", "assets", "fonts", "locale")
_NETWORK_MODES = ("public", "public", "none", None)
_ENFORCE = (
    "sha256sum -c /opt/hashes.txt",
    "git diff --exit-code -- tests",
    "cmp /tests/test_a.py tests/test_a.py",
    "diff -r /tests tests",
    "chattr +i tests",
    "git checkout HEAD -- tests",
    "cp /tests/test_a.py tests/",
    "cp -r /tests/fixtures tests/",
)
_NEAR_ENFORCE = ("cp /opt/tests/test_a.py tests/", "echo diffstat", "ls /tests")
_FORBID_NEGATIONS = ("Do not", "You must not", "Never", "You should not", "You are not allowed to")
_FORBID_VERBS = ("modify", "edit", "delete", "read", "touch", "overwrite")
_FORBID_OBJECTS = ("the tests", "the grader", "the verifier script", "any test file", "the checker")
_FORBIDS = ("rule", "split", "none")
_ARTIFACT_KINDS = ("hit", "hit", "patch", "ext", "no_read", "no_loader", "runs_code", "few")
_PATH_DIRS = ("app", "testbed", "workspace", "repo", "work", "output", "home")
_OPEN_DIRS = ("app", "testbed", "workspace", "repo", "home")
_OTHER_DIRS = ("tmp", "data", "srv")
_RUNS_CODE = (
    "import subprocess",
    "from solution_pkg import build",
    'sys.path.insert(0, "/app")',
    'value = eval("1")',
    'spec = importlib.util.find_spec("json")',
    'con = sqlite3.connect("/app/db.sqlite"); con.execute("select 1")',
)
_CONSTANT_NAMES = "ABCDEF"
assert len(_RULES) == 31
assert all(rule[0] in MECHANISM_CLASSES for rule in _RULES.values())
assert _MAX_EVIDENCE == 6 and max(_ROW_COUNTS) > _MAX_EVIDENCE
_SOURCE_HIT_COUNTS = (0, 0, 1, 3, _MAX_LIST)


@dataclass(frozen=True, slots=True)
class _Artifact:
    path: str
    origin: str
    text: str
    rows: int


@dataclass(slots=True)
class _Plan:
    token: str
    layout: str = "single_container"
    runner: str = "pytest"
    isolation: dict[str, bool] = field(default_factory=dict)
    mechanisms: tuple[str, ...] = ("exit_code",)
    report_check: bool = False
    reward_lines: tuple[int | None, ...] = ()
    unconditional: bool = False
    masks: int = 0
    baked_executed: int = 0
    executes: int = 0
    hooks: bool = False
    workdir: bool = False
    early_exits: int = 0
    clears_reward: bool = False
    logs_writable: bool = False
    site_packages: bool = False
    path_exposed: bool = False
    helpers: int = 0
    deselect: int = 0
    overlaps: tuple[tuple[str, str, str], ...] = ()
    solution: tuple[str, ...] = ()
    solution_free: int = 0
    git: tuple[bool, bool, bool] = (False, False, False)
    git_audit: tuple[str, ...] = ()
    outside: tuple[str, ...] = ()
    outside_named: int = 0
    network: tuple[str | None, int, bool] = (None, 0, False)
    test_like: bool = False
    counts: dict[str, int] = field(default_factory=dict)
    error_words: bool = False
    weights: tuple[int, ...] = ()
    reference: tuple[int, int, int, int] | None = None
    test_files: int = 0
    source_hits: tuple[int, ...] = ()
    fragment: int = 0
    test_sh: bool = True
    enforce: str = ""
    forbid: str = "none"
    instruction: str = ""
    operand: str = "relative"
    artifacts: tuple[_Artifact, ...] = ()
    delegated: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Want:
    rule: str
    mechanism_class: str
    score: int
    confidence: str
    reference: bool
    evidence: int | None
    label: str | None


def _flag(rng: Random) -> bool:
    return rng.random() < 0.5


def _token(rng: Random) -> str:
    return "".join(rng.choice(_HEX) for _ in range(8))


def _ev_rows(count: int, file: str, prefix: str) -> list[dict[str, Any]]:
    assert 0 <= count <= max(_ROW_COUNTS)
    return [{"file": file, "line": index + 1, "text": f"{prefix} {index}"} for index in range(count)]


def _plan_grader(rng: Random, plan: _Plan) -> None:
    plan.layout = rng.choice(_LAYOUTS)
    plan.runner = rng.choice(_RUNNERS)
    broken = None if _flag(rng) else rng.choice(_ISOLATION)
    plan.isolation = {name: name == broken for name in _ISOLATION}
    plan.isolation.update({name: _flag(rng) for name in _ISOLATION_OTHER})
    plan.mechanisms = rng.choice(_MECHANISMS)
    plan.report_check = rng.random() < 0.3
    plan.reward_lines = tuple(rng.choice((None, 3 + index)) for index in range(rng.choice((0, 1, 2, 3))))
    plan.unconditional = rng.random() < 0.3
    plan.masks = rng.choice(_ROW_COUNTS)
    plan.baked_executed = rng.choice((0, 0, 0, 1, 6, 7, 9))
    plan.executes = rng.choice(_ROW_COUNTS)
    plan.hooks = _flag(rng)
    plan.workdir = rng.random() < 0.7
    plan.early_exits = rng.choice(_ROW_COUNTS)
    plan.clears_reward = rng.random() < 0.3
    plan.logs_writable = rng.random() < 0.7
    plan.site_packages = _flag(rng)
    plan.path_exposed = _flag(rng)
    plan.helpers = rng.choice(_ROW_COUNTS)
    plan.deselect = rng.choice(_ROW_COUNTS)
    plan.operand = rng.choice(tuple(_OPERANDS))


def _plan_overlaps(rng: Random, plan: _Plan) -> None:
    items: list[tuple[str, str, str]] = []
    stems = rng.sample(_TEST_STEMS, rng.choice((0, 0, 1, 2, 3)))
    for group, stem in enumerate(stems):
        tests_file = f"tests/{stem}_{plan.token}.py"
        for index in range(rng.randint(1, 7)):
            kind = rng.choice(("identical_bytes", "shared_lines"))
            member = f"environment/bundle_{plan.token}_{group}{index}.tar!{stem}.py"
            plain = f"environment/copy_{plan.token}_{group}{index}/{stem}.py"
            items.append((tests_file, kind, member if _flag(rng) else plain))
    rng.shuffle(items)
    plan.overlaps = tuple(items)
    solution: list[str] = []
    free = 0
    for index in range(rng.choice((0, 0, 1, 2, 4, 8))):
        kind = rng.choice(("free", "equal", "suffix")) if items else "free"
        if kind == "free":
            free += 1
            solution.append(f"environment/solution_{plan.token}_{index}.py")
            continue
        source = rng.choice(items)[2]
        solution.append(source if kind == "equal" else f"ctx/{source.split('!')[0]}")
    plan.solution = tuple(solution)
    plan.solution_free = free


def _plan_image(rng: Random, plan: _Plan) -> None:
    _plan_overlaps(rng, plan)
    plan.git = (_flag(rng), _flag(rng), _flag(rng))
    plan.git_audit = tuple(rng.choice(("git_clone", "git_fetch_all", "download")) for _ in range(rng.choice((0, 1, 2))))
    outside: list[str] = []
    named = 0
    for index in range(rng.choice((0, 0, 1, 3, 8))):
        is_named = _flag(rng)
        named += is_named
        word = rng.choice(_NAMED_WORDS if is_named else _PLAIN_WORDS)
        outside.append(f"/opt/{word}_{plan.token}_{index}.dat")
    plan.outside = tuple(outside)
    plan.outside_named = named
    plan.network = (rng.choice(_NETWORK_MODES), rng.choice((0, rng.randint(1, 40))), _flag(rng))
    plan.test_like = rng.random() < 0.3


def _plan_counts(rng: Random) -> dict[str, int]:
    tests = rng.choice((0, 0, 1, 2, 3, rng.randint(4, 12)))
    asserts = rng.choice((0, 1, 2, 3, 4, 10, rng.randint(5, 14)))
    seventy = (7 * asserts + 9) // 10
    half = asserts // 2
    halves = (0, max(0, half - 1), half, (asserts + 1) // 2, asserts)
    test_half = tests // 2
    test_halves = (0, max(0, test_half - 1), test_half, (tests + 1) // 2, tests)
    return {
        "files": rng.choice((0, 1, 2)),
        "tests": tests,
        "assertions": asserts,
        "negative_assertions": rng.choice((0, 0, 1, 2)),
        "negative_named_tests": rng.choice((0, 0, 1)),
        "literal_eq_assertions": max(0, min(asserts, rng.choice((0, 2, 3, seventy - 1, seventy, asserts)))),
        "surface_assertions": rng.choice(halves),
        "surface_only_tests": rng.choice(test_halves),
        "source_text_assertions": rng.choice((0, 0, 1, 3)),
        "mock_assertions": rng.choice(halves),
        "tests_without_assertion": rng.choice((0, rng.randint(0, tests))),
        "parametrized_cases": rng.choice((0, 0, 1, 4)),
        "randomized_or_property": rng.choice((0, 0, 0, 1)),
        "loops_with_assertions": rng.choice((0, 0, 0, 1)),
        "skipped_or_xfail": rng.choice((0, 1)),
        "mode_ast_files": rng.choice((0, 1)),
        "mode_regex_files": rng.choice((0, 1)),
    }


def _plan_rigor(rng: Random, plan: _Plan) -> None:
    plan.counts = _plan_counts(rng)
    plan.error_words = _flag(rng)
    plan.weights = tuple(rng.randint(1, 5) for _ in range(rng.choice((0, 0, 1, 2, 7, 9))))
    if _flag(rng):
        symbols = rng.choice((0, 1, 3, 9))
        branches = rng.choice((0, 1, 4, 9))
        plan.reference = (symbols, symbols + rng.choice((0, 1, 3)), branches, branches + rng.choice((0, 2, 3)))
    plan.test_files = rng.choice((0, 1, 2, 8))
    plan.fragment = rng.choice((0, 0, 1, 2))


def _plan_delegated(rng: Random, token: str) -> tuple[str, ...]:
    count = rng.choice((0, 0, 1, 2))
    delegated = tuple(f"tests/{stem}_{token}.py" for stem in rng.sample(("grader", "scoring", "model_for_tests"), count))
    assert len(delegated) == count <= 2
    return delegated


def _plan_source_hits(rng: Random, rows: int) -> tuple[int, ...]:
    hits = tuple(rng.choice(_SOURCE_HIT_COUNTS) for _ in range(rows))
    assert len(hits) == rows and all(0 <= count <= _MAX_LIST for count in hits)
    return hits


def _source_hit_rows(row: int, count: int) -> list[dict[str, Any]]:
    assert 0 <= count <= _MAX_LIST
    return [{"line": 20 * row + index + 1, "text": f'assert "token_{index}" in source_{row}'} for index in range(count)]


def _forbid_text(rng: Random, form: str) -> str:
    assert form in _FORBIDS
    if form == "none":
        return ""
    negation = rng.choice(_FORBID_NEGATIONS)
    verb = rng.choice(_FORBID_VERBS)
    target = rng.choice(_FORBID_OBJECTS)
    if form == "rule":
        return f"{negation} {verb} {target}.\n"
    return f"{negation} hurry.\n{verb.capitalize()} {target} as needed.\n"


def _plan_text(rng: Random, plan: _Plan) -> None:
    plan.test_sh = rng.random() < 0.75
    plan.enforce = rng.choice(("", "", *_ENFORCE, *_NEAR_ENFORCE))
    plan.forbid = rng.choice(_FORBIDS)
    lines = ["Implement the requested feature in /app.\n"]
    if plan.error_words:
        lines.append("Reject invalid input with an error.\n")
    lines.append(_forbid_text(rng, plan.forbid))
    plan.instruction = "".join(lines)


def _read_line(rng: Random, kind: str, token: str, index: int) -> str:
    target = f"out_{token}_{index}.json"
    if kind == "no_read":
        return f'    data_{index} = json.loads(Path("/{rng.choice(_OTHER_DIRS)}/{target}").read_text())'
    if kind == "no_loader":
        return f'    data_{index} = Path("/{rng.choice(_PATH_DIRS)}/{target}").stat().st_size'
    if _flag(rng):
        return f'    data_{index} = json.loads(Path("/{rng.choice(_PATH_DIRS)}/{target}").read_text())'
    return f'    data_{index} = json.load(open("/{rng.choice(_OPEN_DIRS)}/{target}"))'


def _artifact(rng: Random, token: str, index: int) -> _Artifact:
    kind = rng.choice(_ARTIFACT_KINDS)
    reads = rng.randint(1, 3)
    literals = rng.randint(0, 2) if kind == "few" else rng.randint(3, 6)
    lines = [] if kind == "no_loader" else ["import json"]
    lines.append("from pathlib import Path")
    if kind == "runs_code":
        lines.append(rng.choice(_RUNS_CODE))
    constants = rng.randint(0, literals)
    lines += [f'EXPECTED_{_CONSTANT_NAMES[slot]} = {{"k": {slot}}}' for slot in range(constants)]
    lines += ["", f"def test_artifact_{index}():"]
    lines += [_read_line(rng, kind, token, slot) for slot in range(reads)]
    lines += [f'    assert data_0["k{slot}"] == {slot}' for slot in range(literals - constants)]
    lines.append("    assert data_0 is not None")
    suffix = "txt" if kind == "ext" else "py"
    rows = min(reads, 2) + 1 if kind == "hit" else 0
    return _Artifact(f"tests/artifact_{token}_{index}.{suffix}", "patch" if kind == "patch" else "file", "\n".join(lines) + "\n", rows)


def _plan(rng: Random) -> _Plan:
    plan = _Plan(token=_token(rng))
    _plan_grader(rng, plan)
    _plan_image(rng, plan)
    _plan_rigor(rng, plan)
    _plan_text(rng, plan)
    plan.artifacts = tuple(_artifact(rng, plan.token, index) for index in range(rng.choice((0, 0, 1, 2, 3))))
    return plan


def _files(plan: _Plan) -> dict[str, str]:
    files = {
        "instruction.md": plan.instruction,
        "environment/Dockerfile": "FROM ubuntu:24.04\nWORKDIR /app\n",
    }
    if plan.test_sh:
        command = " ".join(_RUNNER_COMMANDS[plan.runner].format(target=_OPERANDS[plan.operand]).split())
        lines = ["#!/bin/bash", "cd /app", command]
        lines += [plan.enforce] if plan.enforce else []
        lines.append("echo 1 > /logs/verifier/reward.txt")
        files["tests/test.sh"] = "\n".join(lines) + "\n"
    files.update({artifact.path: artifact.text for artifact in plan.artifacts})
    assert len(files) <= _MAX_FILES
    return files


def _put(section: dict[str, Any], path: str, value: object) -> None:
    *head, last = path.split(".")
    node = section
    for key in head:
        node = node[key]
    assert isinstance(node, dict) and last in node, path
    node[last] = value


def _boundary(card: dict[str, Any], plan: _Plan) -> dict[str, Any]:
    boundary = copy.deepcopy(card["container_and_boundary"])
    copies, clones, removed = plan.git
    overlaps = [
        {"tests_file": tests_file, "kind": kind, "source": source, "image_path": f"/app/{index}.py", "dockerfile_line": index + 2}
        for index, (tests_file, kind, source) in enumerate(plan.overlaps)
    ]
    audit = [{"line": index + 3, "tags": [tag], "text": f"RUN {tag}"} for index, tag in enumerate(plan.git_audit)]
    mode, domains, github = plan.network
    for path, value in (
        ("layout", plan.layout),
        ("agent_image.baked_test_like_files", ["/app/tests/test_visible.py"] if plan.test_like else []),
        ("agent_image.writable_system_site_packages", plan.site_packages),
        ("agent_image.tests_baked_identical_or_overlapping", overlaps),
        ("agent_image.baked_solution_like_files", list(plan.solution)),
        ("agent_image.git.copies_git_history", copies),
        ("agent_image.git.clone_or_fetch_in_run", clones),
        ("agent_image.git.history_removed_in_run", removed),
        ("agent_image.run_audit", audit),
        ("agent_image.baked_outside_workdir", list(plan.outside)),
        ("root_posture.logs_verifier_world_writable", plan.logs_writable),
        ("network.network_mode", mode),
        ("network.blocklist_domains", domains),
        ("network.github_hosts_blocklisted", github),
    ):
        _put(boundary, path, value)
    return boundary


def _grader(card: dict[str, Any], plan: _Plan) -> dict[str, Any]:
    grader = copy.deepcopy(card["grader_script_mechanics"])
    hooks = grader["reset_list"]["autoload_hook_classes_left_writable"] if plan.hooks else []
    reward = [{"file": "tests/test.sh", "line": line, "text": "echo 1 > /logs/verifier/reward.txt"} for line in plan.reward_lines]
    executes = [
        {"kind": "test_sh_runs_baked_script", "script": f"/opt/check_{index}.py", **row}
        for index, row in enumerate(_ev_rows(plan.executes, "tests/test.sh", "python3 /opt/check"))
    ]
    helpers = [{"module": f"helper_{index}", **row} for index, row in enumerate(_ev_rows(plan.helpers, "tests/h.patch:tests/t.py", "import helper"))]
    for path, value in (
        ("runner", plan.runner),
        ("runs_in_agent_workdir", plan.workdir),
        ("baked_tests_executed", [f"/app/tests/test_{index}.py" for index in range(plan.baked_executed)]),
        ("executes_agent_writable_code", executes),
        ("early_exit_paths", _ev_rows(plan.early_exits, "tests/test.sh", "exit 1")),
        ("clears_reward_file_first", plan.clears_reward),
        ("reset_list.autoload_hook_classes_left_writable", hooks),
        ("reset_list.helper_modules_not_shipped_by_patch", helpers),
        ("reward_gate.mechanisms", list(plan.mechanisms)),
        ("reward_gate.structured_report_count_check", plan.report_check),
        ("reward_gate.reward_write_lines", reward),
        ("reward_gate.reward_one_unconditional", plan.unconditional),
        ("reward_gate.failure_masks", _ev_rows(plan.masks, "tests/test.sh", "pytest || true")),
        ("env_vectors.path_hijack.exposed", plan.path_exposed),
        ("env_vectors.path_hijack.bare_commands", ["pytest", "python3"] if plan.path_exposed else []),
        ("env_vectors.deselect_or_ignore", _ev_rows(plan.deselect, "tests/test.sh", "pytest --deselect")),
    ):
        _put(grader, path, value)
    for name, value in plan.isolation.items():
        _put(grader, f"env_vectors.isolation_flags.{name}", value)
    return grader


def _reference(plan: _Plan) -> dict[str, Any] | None:
    if plan.reference is None:
        return None
    symbols, symbol_count, branches, branch_count = plan.reference
    assert symbols <= symbol_count and branches <= branch_count
    return {
        "changed_symbols": symbol_count + 2,
        "exercised_symbols": 2,
        "unexercised": [{"file": "src/core.py", "symbol": f"func_{index}", "edited_lines": index + 1} for index in range(symbols)],
        "unexercised_count": symbol_count,
        "branch_lines": branch_count + 1,
        "branches_unreferenced": [{"file": "src/core.py", "patch_line": index + 4, "text": f"if flag_{index}:"} for index in range(branches)],
        "branches_unreferenced_count": branch_count,
    }


def _rigor(card: dict[str, Any], plan: _Plan) -> dict[str, Any]:
    rigor = copy.deepcopy(card["test_suite_rigor"])
    asserts = plan.counts["assertions"]
    share = round(plan.counts["literal_eq_assertions"] / asserts, 3) if asserts else None
    symbols = [
        {"kind": "function", "symbol": f"sym_{index}", "weight": weight, "constraint": index % 2 == 0, "sentence": f"Call sym_{index}."}
        for index, weight in enumerate(plan.weights)
    ]
    test_files = [
        {"file": f"tests/test_{index}.py", "mode": "ast", "tests": index, "assertions": index,
         "source_text_hits": _source_hit_rows(index, plan.source_hits[index])}
        for index in range(plan.test_files)
    ]
    for name, value in plan.counts.items():
        _put(rigor, f"counts.{name}", value)
    for path, value in (
        ("ratios.literal_eq_share", share),
        ("instruction_error_words", plan.error_words),
        ("instruction_named_symbols_without_test_reference", symbols),
        ("reference_patch", _reference(plan)),
        ("test_files", test_files),
        ("fragment_files", plan.fragment),
    ):
        _put(rigor, path, value)
    rigor["delegated_grader_modules"] = list(plan.delegated)
    return rigor


def _want(out: list[_Want], rule: str, *, score: int | None = None, evidence: int | None = None, label: str | None = None) -> None:
    mechanism_class, base, confidence = _RULES[rule]
    capped = None if evidence is None else min(evidence, _MAX_EVIDENCE)
    out.append(_Want(rule, mechanism_class, base if score is None else score, confidence, rule in _REFERENCE_RULES, capped, label))


def _expect_grader(plan: _Plan, out: list[_Want]) -> None:
    single = plan.layout == "single_container"
    iso = plan.isolation
    if plan.baked_executed:
        _want(out, "GT-BAKED-TESTS-EXECUTED")
    if plan.executes:
        _want(out, "GT-VERIFIER-RUNS-AGENT-CODE", evidence=plan.executes)
    loaded = plan.test_sh and (plan.runner == "jest" or (plan.runner == "pytest" and plan.operand != "outside"))
    if single and plan.hooks and loaded and plan.workdir and not any(iso[name] for name in _ISOLATION):
        _want(out, "GT-AUTOLOAD-WRITABLE")
    if single and plan.mechanisms == ("exit_code",) and not plan.report_check and plan.runner != "unknown":
        _want(out, "EG-EXIT-CODE-ONLY-GATE", evidence=sum(1 for line in plan.reward_lines if line))
    if plan.masks:
        _want(out, "EG-FAILURE-MASKED", evidence=plan.masks)
    if plan.unconditional:
        _want(out, "EG-REWARD-UNCONDITIONAL", evidence=len(plan.reward_lines))
    if single and plan.early_exits and not plan.clears_reward and plan.logs_writable:
        _want(out, "GT-REWARD-PREWRITTEN", evidence=plan.early_exits)
    python_runner = plan.runner in ("pytest", "unittest")
    if single and python_runner and plan.site_packages and not iso["python_isolated"] and not iso["env_i"]:
        _want(out, "GT-SITE-PACKAGES-HOOK", evidence=1)
    if plan.path_exposed and single:
        _want(out, "GT-PATH-HIJACK", evidence=1)
    if plan.helpers:
        _want(out, "GT-HELPER-NOT-RESET", evidence=plan.helpers)
    if plan.deselect:
        _want(out, "WC-DESELECTED-TESTS", evidence=plan.deselect)


def _expect_image(plan: _Plan, out: list[_Want]) -> None:
    groups: dict[str, list[str]] = {}
    for tests_file, kind, _source in plan.overlaps:
        groups.setdefault(tests_file, []).append(kind)
    for tests_file in sorted(groups):
        kinds = groups[tests_file]
        score = 92 if "identical_bytes" in kinds else 85
        _want(out, "AR-TESTS-IN-IMAGE", score=score, evidence=len(kinds) + 1, label=tests_file)
    if plan.solution_free:
        _want(out, "AR-SOLUTION-ARTIFACT-IN-IMAGE", evidence=plan.solution_free)
    copies, clones, removed = plan.git
    if copies or (clones and not removed):
        _want(out, "AR-GIT-HISTORY")
    if plan.outside_named:
        _want(out, "AR-BAKED-OUTSIDE-WORKDIR", evidence=plan.outside_named)
    mode, domains, github = plan.network
    if mode == "public" and domains and not github:
        _want(out, "AR-PUBLIC-NETWORK-UPSTREAM", evidence=1)
    elif mode == "public" and domains:
        _want(out, "AR-PUBLIC-NETWORK-BLOCKLIST-EDITABLE", evidence=1)


def _expect_literals(plan: _Plan, out: list[_Want]) -> None:
    counts = plan.counts
    asserts = counts["assertions"]
    share = round(counts["literal_eq_assertions"] / asserts, 3) if asserts else None
    plain = not counts["randomized_or_property"] and not counts["loops_with_assertions"]
    if counts["literal_eq_assertions"] >= 3 and share is not None and share >= 0.7 and plain and plan.layout:
        visible = plan.test_like or plan.baked_executed > 0
        _want(out, "AR-LITERAL-PAIRS-CARVE" if visible else "WC-LITERAL-PAIRS", evidence=1)
    strong = sum(1 for weight in plan.weights if weight >= 3)
    if strong:
        _want(out, "WC-INSTRUCTION-SYMBOL-UNTESTED", score=min(75, 40 + 5 * strong), evidence=strong)
    if plan.reference is not None:
        symbols, symbol_count, branches, branch_count = plan.reference
        if symbols:
            _want(out, "WC-REFERENCE-SYMBOL-UNEXERCISED", score=min(85, 50 + 6 * symbol_count), evidence=symbols)
        if branches:
            _want(out, "WC-REFERENCE-BRANCH-UNASSERTED", score=min(80, 45 + 3 * branch_count), evidence=branches)


def _expect_counts(plan: _Plan, out: list[_Want]) -> None:
    counts = plan.counts
    tests, asserts, rows = counts["tests"], counts["assertions"], plan.test_files
    if tests and counts["tests_without_assertion"]:
        _want(out, "WC-TEST-WITHOUT-ASSERTION", evidence=rows)
    if tests and 2 * counts["surface_only_tests"] >= tests:
        _want(out, "WC-SURFACE-ONLY", evidence=rows)
    if counts["source_text_assertions"]:
        _want(out, "WC-SOURCE-TEXT-ASSERT", evidence=sum(plan.source_hits))
    if counts["mock_assertions"] and 2 * counts["mock_assertions"] >= asserts:
        _want(out, "WC-MOCK-ONLY", evidence=rows)
    if plan.error_words and tests and not counts["negative_assertions"] and not counts["negative_named_tests"]:
        _want(out, "WC-NO-NEGATIVE-CASES")
    if tests and tests <= 2 and asserts <= 3 and not counts["parametrized_cases"] and not plan.delegated:
        _want(out, "WC-TINY-SUITE", evidence=rows)
    scripted = "python_writes_reward" in plan.mechanisms
    if tests == 0 and not plan.fragment and (counts["files"] or plan.test_sh) and not scripted:
        _want(out, "WC-NO-TESTS-FOUND", evidence=1)
    artifact_rows = sum(artifact.rows for artifact in plan.artifacts)
    if artifact_rows:
        _want(out, "EG-ARTIFACT-ONLY-CHECK", evidence=artifact_rows)
    if plan.forbid == "rule" and not (plan.test_sh and plan.enforce in _ENFORCE):
        _want(out, "EG-INSTRUCTION-RULE-UNENFORCED")


def _expected(plan: _Plan) -> list[_Want]:
    out: list[_Want] = []
    _expect_grader(plan, out)
    _expect_image(plan, out)
    _expect_literals(plan, out)
    _expect_counts(plan, out)
    out.sort(key=lambda want: (-want.score, want.rule))
    assert len(out) <= len(_RULES) + len(_TEST_STEMS)
    return out


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert (root / "instruction.md").is_file()


class HypothesisSimulation:
    def __init__(self, sim: Simulation, root: Path) -> None:
        assert sim.step + _STEP_BUDGET <= STEP_LIMIT
        self.sim = sim
        self.root = root
        self.rng = sim.rng
        self.start_step = sim.step
        root.mkdir(parents=True, exist_ok=True)
        assert root.is_dir()

    def run(self) -> None:
        for index in range(_CASES):
            self._case(index)
        self.sim.require(self.sim.step - self.start_step <= _STEP_BUDGET, "hypotheses_step_budget")

    def _case(self, index: int) -> None:
        sim = self.sim
        plan = _plan(self.rng)
        plan.delegated = _plan_delegated(Random(f"hypotheses-delegation:{sim.seed}:{index}"), plan.token)
        plan.source_hits = _plan_source_hits(Random(f"hypotheses-source-hits:{sim.seed}:{index}"), plan.test_files)
        root = self.root / f"case-{index}"
        files = _files(plan)
        _write(root, files)
        sim.begin("hypotheses-package", case=index, files=len(files), layout=plan.layout, runner=plan.runner,
                  delegated=len(plan.delegated))
        package = load_package(root)
        sim.require(sorted(package.files) == sorted(files), "hypotheses_package_files")
        card = analyze_package(root)
        sources = [
            TestSource(artifact.path, artifact.origin, list(enumerate(package.files[artifact.path].splitlines(), 1)), True)
            for artifact in plan.artifacts
        ]
        rows = _artifact_only(sources)
        sim.require(len(rows) == sum(artifact.rows for artifact in plan.artifacts), "hypotheses_artifact_rows")
        boundary, grader, rigor = _boundary(card, plan), _grader(card, plan), _rigor(card, plan)
        expected = _expected(plan)
        sim.begin("hypotheses-rules", case=index, expected=[want.rule for want in expected])
        result = build_hypotheses(package, boundary, grader, rigor, sources)
        self._check(result, expected)
        self._check_source_evidence(result, plan)

    def _check_source_evidence(self, result: Sequence[Hypothesis], plan: _Plan) -> None:
        found = [hypothesis for hypothesis in result if hypothesis.id == "WC-SOURCE-TEXT-ASSERT"]
        assert len(found) <= 1
        if not found:
            return
        planned = [
            {"file": f"tests/test_{row}.py", **hit}
            for row, count in enumerate(plan.source_hits)
            for hit in _source_hit_rows(row, count)
        ]
        self.sim.require(list(found[0].evidence) == planned[:_MAX_EVIDENCE], "hypothesis_source_text_evidence_rows")

    def _check(self, result: Sequence[Hypothesis], expected: Sequence[_Want]) -> None:
        sim = self.sim
        bases = [hypothesis.id.split("#", 1)[0] for hypothesis in result]
        wanted = [want.rule for want in expected]
        for rule in _RULES:
            sim.require((rule in bases) == (rule in wanted), f"hypothesis_fires_{rule}")
        sim.require(len(result) == len(expected), "hypotheses_count")
        sim.require(bases == wanted, "hypotheses_order")
        keys = [(-hypothesis.score, base) for hypothesis, base in zip(result, bases, strict=True)]
        sim.require(keys == sorted(keys), "hypotheses_sorted_by_score_then_id")
        seen: dict[str, int] = {}
        for hypothesis, want, base in zip(result, expected, bases, strict=True):
            seen[base] = seen.get(base, 0) + 1
            suffixed = base if seen[base] == 1 else f"{base}#{seen[base]}"
            sim.require(hypothesis.id == suffixed, "hypotheses_repeat_suffix")
            self._check_one(hypothesis, want)

    def _check_one(self, hypothesis: Hypothesis, want: _Want) -> None:
        sim = self.sim
        rule = want.rule
        sim.require(hypothesis.score == want.score, f"hypothesis_score_{rule}")
        sim.require(hypothesis.mechanism_class == want.mechanism_class, f"hypothesis_class_{rule}")
        sim.require(hypothesis.mechanism_class in MECHANISM_CLASSES, "hypotheses_known_class")
        sim.require(hypothesis.confidence == want.confidence, f"hypothesis_confidence_{rule}")
        sim.require(hypothesis.requires_reference == want.reference, f"hypothesis_reference_{rule}")
        sim.require(len(hypothesis.evidence) <= _MAX_EVIDENCE, "hypotheses_evidence_bound")
        sim.require(len(hypothesis.as_dict()["evidence"]) <= _MAX_EVIDENCE, "hypotheses_card_evidence_bound")
        if want.evidence is not None:
            sim.require(len(hypothesis.evidence) == want.evidence, f"hypothesis_evidence_{rule}")
        if want.label is not None:
            sim.require(want.label in hypothesis.title, f"hypothesis_label_{rule}")
        sim.require(bool(hypothesis.title) and bool(hypothesis.witness), "hypotheses_text_present")
