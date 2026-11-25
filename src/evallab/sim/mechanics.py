from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from random import Random
from typing import Any

from evallab.package_anatomy import (
    _HOOK_CLASSES_BY_RUNNER,
    _test_sources,
    grader_script_mechanics,
    load_package,
)
from evallab.sim.engine import STEP_LIMIT, Simulation

_STEP_BUDGET = 20
_CONTEXT = {"workdir": "/app", "baked": {}, "layout_separate": False}
_EXIT_TRIGGERS = ("none", "dollar_q", "rc_assign", "and_echo")
_SAVED_STATUS = ("none", "unused", "gated")
_SAVED_LINE = "saved=$?"
_GATE_LINES = ('if [ "$saved" -ne 0 ]; then', "echo failed", "fi")
_ADDOPTS = ("none", "mention", "clear", "both")
_BARE = ("node", "npm", "cargo", "go", "ruby")
_WORKDIR = ("cwd", "cd", "none")
_ISOLATION = (
    "env_i",
    "python_isolated",
    "noconftest",
    "config_nulled",
    "plugin_autoload_disabled",
    "no_cacheprovider",
    "importlib_mode",
)
_DESELECT = ("pytest tests --deselect tests/a.py", "pytest tests --lf")
_PYPATH = ("export PYTHONPATH=/opt/lib1", "export PYTHONPATH=/opt/lib2")
_BARE_LINE = {
    "node": "node --version",
    "npm": "npm --version",
    "cargo": "cargo --version",
    "go": "go version",
    "ruby": "ruby --version",
}
_EXIT_LINE = {
    "dollar_q": "status=$?",
    "rc_assign": "RC=0",
    "and_echo": "pytest tests && echo 1",
}
_PIPE = "pytest tests -q | tee /tmp/pytest.out"
_PLUGIN = "export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1"
_REWARD_ZERO = "echo 0 > /logs/verifier/reward.txt"
_REWARD_ONE = "echo 1 > /logs/verifier/reward.txt"
_REWARD_EVIDENCE_LIMIT = 6
_REWARD_SOURCE = "def test_reward():\n    target = 'reward.txt'\n"


def _reset(plan: _Plan) -> dict[str, Any]:
    patterns = plan.plugin_autoload_disabled or plan.noconftest
    hooks = [] if patterns else list(_HOOK_CLASSES_BY_RUNNER["pytest"])
    assert len(hooks) <= len(_HOOK_CLASSES_BY_RUNNER["pytest"])
    return {
        "enumerated_files": [],
        "enumerated_count": 0,
        "files_touched_by_hidden_patch": [],
        "autoload_reset_by_pattern": patterns,
        "restores_from_outside_agent_tree": False,
        "autoload_hook_classes_left_writable": hooks,
        "helper_modules_not_shipped_by_patch": [],
    }


_Hit = tuple[int, str]


@dataclass(frozen=True, slots=True)
class _Plan:
    exit_trigger: str
    structured: bool
    log_grep: bool
    python_writes: bool
    unconditional_count: int
    zero_count: int
    mask_or_true: bool
    mask_set_plus_e: bool
    early_exit: bool
    reward_clear: bool
    env_i: bool
    python_isolated: bool
    noconftest: bool
    config_nulled: bool
    no_cacheprovider: bool
    importlib_mode: bool
    addopts: str
    path_usr: bool
    extra_bare: tuple[str, ...]
    pythonpath_count: int
    deselect_count: int
    workdir_mode: str
    pipefail: bool
    plugin_autoload_disabled: bool
    saved_status: str

    @property
    def unconditional(self) -> bool:
        return self.unconditional_count > 0


@dataclass(slots=True)
class _Lines:
    reward_clear: _Hit | None = None
    set_plus_e: _Hit | None = None
    or_true: _Hit | None = None
    early_exit: _Hit | None = None
    zeros: list[_Hit] = field(default_factory=list)
    echoes: list[_Hit] = field(default_factory=list)
    cd: _Hit | None = None
    addopts: list[_Hit] = field(default_factory=list)
    pythonpath: list[_Hit] = field(default_factory=list)
    deselect: list[_Hit] = field(default_factory=list)
    pipe: _Hit | None = None


class _Script:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, text: str) -> _Hit:
        assert text == text.strip() and len(text) <= 200
        self.lines.append(text)
        return (len(self.lines), text)

    def text(self) -> str:
        assert self.lines
        return "\n".join(self.lines) + "\n"


def _flag(rng: Random) -> bool:
    return rng.random() < 0.5


def _plan(rng: Random) -> _Plan:
    extra_count = rng.randint(0, 2)
    plan = _Plan(
        exit_trigger=rng.choice(_EXIT_TRIGGERS),
        structured=_flag(rng),
        log_grep=_flag(rng),
        python_writes=_flag(rng),
        unconditional_count=rng.randint(0, 2),
        zero_count=rng.randint(0, _REWARD_EVIDENCE_LIMIT),
        mask_or_true=_flag(rng),
        mask_set_plus_e=_flag(rng),
        early_exit=_flag(rng),
        reward_clear=_flag(rng),
        env_i=_flag(rng),
        python_isolated=_flag(rng),
        noconftest=_flag(rng),
        config_nulled=_flag(rng),
        no_cacheprovider=_flag(rng),
        importlib_mode=_flag(rng),
        addopts=rng.choice(_ADDOPTS),
        path_usr=_flag(rng),
        extra_bare=tuple(rng.sample(_BARE, extra_count)),
        pythonpath_count=rng.randint(0, 2),
        deselect_count=rng.randint(0, 2),
        workdir_mode=rng.choice(_WORKDIR),
        pipefail=_flag(rng),
        plugin_autoload_disabled=_flag(rng),
        saved_status=rng.choice(_SAVED_STATUS),
    )
    assert plan.exit_trigger in _EXIT_TRIGGERS and plan.saved_status in _SAVED_STATUS
    assert plan.addopts in _ADDOPTS and plan.workdir_mode in _WORKDIR
    assert len(plan.extra_bare) == extra_count <= 2
    assert 0 <= plan.pythonpath_count <= 2 and 0 <= plan.deselect_count <= 2
    assert 0 <= plan.unconditional_count <= 2 and 0 <= plan.zero_count <= _REWARD_EVIDENCE_LIMIT
    return plan


def _mechanisms(plan: _Plan) -> list[str]:
    found: list[str] = []
    if plan.exit_trigger != "none" or plan.saved_status != "none":
        found.append("exit_code")
    if plan.structured:
        found.append("structured_report")
    if plan.log_grep:
        found.append("log_grep")
    if plan.python_writes:
        found.append("python_writes_reward")
    if plan.unconditional and not found:
        found.append("exit_code")
    return found or ["none_detected"]


def _evidence(file: str, hits: list[_Hit]) -> list[dict[str, Any]]:
    return [{"file": file, "line": number, "text": text} for number, text in hits]


def _present(hits: tuple[_Hit | None, ...]) -> list[_Hit]:
    return [hit for hit in hits if hit is not None]


def _reward_lines(lines: _Lines) -> list[dict[str, Any]]:
    found = [*_present((lines.reward_clear,)), *lines.zeros, *lines.echoes]
    assert found == sorted(found)
    assert len(found) <= 1 + _REWARD_EVIDENCE_LIMIT + 2
    return _evidence("tests/test.sh", found[:_REWARD_EVIDENCE_LIMIT])


def _masks(lines: _Lines) -> list[dict[str, Any]]:
    found = _present((lines.or_true, lines.set_plus_e, lines.pipe))
    assert len(found) <= 6
    return _evidence("tests/test.sh", found)


def _isolation(plan: _Plan) -> dict[str, bool]:
    return {
        "env_i": plan.env_i,
        "python_isolated": plan.python_isolated,
        "noconftest": plan.noconftest,
        "config_nulled": plan.config_nulled,
        "plugin_autoload_disabled": plan.plugin_autoload_disabled,
        "no_cacheprovider": plan.no_cacheprovider,
        "importlib_mode": plan.importlib_mode,
    }


def _bare(plan: _Plan) -> list[str]:
    names = {"pytest"}
    if plan.python_isolated:
        names.add("python3")
    names.update(plan.extra_bare)
    found = sorted(names)
    assert found and "pytest" in found
    return found


def _workdir(plan: _Plan, lines: _Lines) -> str | list[_Hit] | None:
    if plan.workdir_mode == "cwd":
        return "/app"
    if plan.workdir_mode == "cd":
        assert lines.cd is not None
        return [lines.cd]
    assert plan.workdir_mode == "none"
    return None


def _render(plan: _Plan) -> tuple[str, _Lines]:
    script = _Script()
    recorded = _Lines()
    script.add("#!/bin/bash")
    if plan.pipefail:
        script.add("set -o pipefail")
    if plan.reward_clear:
        recorded.reward_clear = script.add("rm -f /logs/verifier/reward.txt")
    set_plus_e = script.add("set +e") if plan.mask_set_plus_e else None
    if plan.early_exit:
        recorded.early_exit = script.add("exit 1")
    if plan.mask_or_true:
        recorded.or_true = script.add("pytest tests || true")
    pipe = script.add(_PIPE)
    recorded.pipe = None if plan.pipefail else pipe
    if plan.saved_status != "none":
        script.add(_SAVED_LINE)
    if plan.saved_status == "gated":
        for text in _GATE_LINES:
            script.add(text)
    recorded.set_plus_e = None if plan.saved_status == "gated" else set_plus_e
    if plan.structured:
        script.add("jq . /tmp/report.json")
    if plan.log_grep:
        script.add("grep -q PASS /tmp/pytest.out")
    _render_env(script, recorded, plan)
    _render_tail(script, recorded, plan)
    text = script.text()
    assert _PIPE in text
    assert ("pipefail" in text) is plan.pipefail
    assert (_PLUGIN in text) is plan.plugin_autoload_disabled
    assert (recorded.pipe is not None) is (not plan.pipefail)
    return text, recorded


def _render_env(script: _Script, recorded: _Lines, plan: _Plan) -> None:
    if plan.env_i:
        script.add("env -i pytest tests")
    if plan.python_isolated:
        script.add("python3 -I -c 'import app'")
    if plan.noconftest:
        script.add("pytest tests --noconftest")
    if plan.config_nulled:
        script.add("pytest tests -c /dev/null")
    if plan.plugin_autoload_disabled:
        script.add(_PLUGIN)
    if plan.no_cacheprovider:
        script.add("pytest tests -p no:cacheprovider")
    if plan.importlib_mode:
        script.add("pytest tests --import-mode=importlib")
    if plan.addopts in ("mention", "both"):
        recorded.addopts.append(script.add("export PYTEST_ADDOPTS=-q"))
    if plan.addopts in ("clear", "both"):
        recorded.addopts.append(script.add("unset PYTEST_ADDOPTS"))
    if plan.path_usr:
        script.add("PATH=/usr/bin:/bin")
    for index in range(plan.pythonpath_count):
        recorded.pythonpath.append(script.add(_PYPATH[index]))
    for name in plan.extra_bare:
        script.add(_BARE_LINE[name])
    for index in range(plan.deselect_count):
        recorded.deselect.append(script.add(_DESELECT[index]))
    assert len(recorded.pythonpath) == plan.pythonpath_count
    assert len(recorded.deselect) == plan.deselect_count


def _render_tail(script: _Script, recorded: _Lines, plan: _Plan) -> None:
    if plan.workdir_mode == "cwd":
        script.add("CWD=/app")
    elif plan.workdir_mode == "cd":
        recorded.cd = script.add("cd /app")
    if plan.exit_trigger != "none":
        script.add(_EXIT_LINE[plan.exit_trigger])
    for _ in range(plan.zero_count):
        recorded.zeros.append(script.add(_REWARD_ZERO))
    for _ in range(plan.unconditional_count):
        recorded.echoes.append(script.add(_REWARD_ONE))
    assert len(recorded.zeros) == plan.zero_count
    assert len(recorded.echoes) == plan.unconditional_count
    assert (recorded.cd is not None) is (plan.workdir_mode == "cd")


class MechanicsSimulation:
    def __init__(self, sim: Simulation, root: Path) -> None:
        assert sim.step + _STEP_BUDGET <= STEP_LIMIT
        self.sim = sim
        self.root = root
        self.rng = sim.rng
        self.start_step = sim.step
        root.mkdir(parents=True, exist_ok=True)
        assert root.is_dir()

    def run(self) -> None:
        plan = _plan(self.rng)
        script, lines = _render(plan)
        self._write(script, plan)
        self.sim.begin("mechanics-script", clear=plan.reward_clear, early=plan.early_exit, saved=plan.saved_status)
        self._require_bash()
        package = load_package(self.root)
        sources, patch_scripts, _patches = _test_sources(package)
        result = grader_script_mechanics(package, sources, patch_scripts, _CONTEXT)
        self._check_reward(result, plan, lines)
        self._check_env(result, plan, lines)
        self._check_fields(result, plan, lines)
        self.sim.require(self.sim.step - self.start_step <= _STEP_BUDGET, "mechanics_step_budget")

    def _write(self, script: str, plan: _Plan) -> None:
        tests = self.root / "tests"
        tests.mkdir(parents=True, exist_ok=True)
        (tests / "test.sh").write_text(script, encoding="utf-8")
        if plan.python_writes:
            (tests / "test_reward.py").write_text(_REWARD_SOURCE, encoding="utf-8")
        assert (tests / "test.sh").is_file()

    def _require_bash(self) -> None:
        path = self.root / "tests" / "test.sh"
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True, check=False)
        self.sim.require(result.returncode == 0, "mechanics_bash_syntax")

    def _check_reward(self, result: dict[str, Any], plan: _Plan, lines: _Lines) -> None:
        sim = self.sim
        gate = result["reward_gate"]
        mechanisms = _mechanisms(plan)
        masks = _masks(lines)
        reward_lines = _reward_lines(lines)
        sim.begin("mechanics-reward", mechanisms=len(mechanisms))
        sim.require(gate["mechanisms"] == mechanisms, "mechanics_mechanisms")
        sim.require(
            gate["reward_one_unconditional"] == plan.unconditional,
            "mechanics_reward_one_unconditional",
        )
        sim.require(gate["failure_masks"] == masks, "mechanics_failure_masks")
        sim.require(len(gate["failure_masks"]) == len(masks), "mechanics_failure_mask_count")
        sim.require(gate["reward_write_lines"] == reward_lines, "mechanics_reward_write_lines")
        sim.require(len(gate["reward_write_lines"]) == len(reward_lines), "mechanics_reward_line_count")
        sim.require(
            gate["structured_report_count_check"] == ("structured_report" in mechanisms),
            "mechanics_structured_report_count_check",
        )

    def _check_env(self, result: dict[str, Any], plan: _Plan, lines: _Lines) -> None:
        sim = self.sim
        env = result["env_vectors"]
        expected = _isolation(plan)
        bare = _bare(plan)
        path_reset = plan.path_usr or plan.env_i
        sim.begin("mechanics-env", addopts=plan.addopts, bare=len(bare))
        for name in _ISOLATION:
            sim.require(env["isolation_flags"][name] == expected[name], f"mechanics_{name}")
        addopts = env["pytest_addopts"]
        sim.require(addopts["mentioned"] == (plan.addopts != "none"), "mechanics_addopts_mentioned")
        sim.require(
            addopts["cleared_or_overridden"] == (plan.addopts in ("clear", "both")),
            "mechanics_addopts_cleared",
        )
        sim.require(
            addopts["lines"] == _evidence("tests/test.sh", lines.addopts), "mechanics_addopts_lines"
        )
        hijack = env["path_hijack"]
        sim.require(hijack["bare_commands"] == bare, "mechanics_bare_commands")
        sim.require(hijack["path_reset"] == path_reset, "mechanics_path_reset")
        sim.require(hijack["exposed"] == (bool(bare) and not path_reset), "mechanics_path_exposed")
        sim.require(
            env["pythonpath"] == _evidence("tests/test.sh", lines.pythonpath), "mechanics_pythonpath"
        )
        sim.require(len(env["pythonpath"]) == plan.pythonpath_count, "mechanics_pythonpath_count")
        sim.require(
            env["deselect_or_ignore"]
            == _evidence("tests/test.sh or test_command.sh", lines.deselect),
            "mechanics_deselect",
        )
        sim.require(len(env["deselect_or_ignore"]) == plan.deselect_count, "mechanics_deselect_count")
        sim.require(env["workdir_in_script"] == _workdir(plan, lines), "mechanics_workdir_in_script")

    def _check_fields(self, result: dict[str, Any], plan: _Plan, lines: _Lines) -> None:
        sim = self.sim
        cwd = "/app" if plan.workdir_mode != "none" else None
        early = _evidence("tests/test.sh", _present((lines.early_exit,)))
        sim.begin("mechanics-fields", workdir=plan.workdir_mode)
        sim.require(result["entrypoint"] == "tests/test.sh", "mechanics_entrypoint")
        sim.require(result["runner"] == "pytest", "mechanics_runner")
        sim.require(result["runners_detected"] == ["pytest"], "mechanics_runners")
        sim.require(result["layout"] == "single", "mechanics_layout")
        sim.require(result["cwd"] == cwd, "mechanics_cwd")
        sim.require(
            result["runs_in_agent_workdir"] == (cwd is not None), "mechanics_runs_in_agent_workdir"
        )
        sim.require(result["reset_list"] == _reset(plan), "mechanics_reset_list")
        sim.require(result["executes_agent_writable_code"] == [], "mechanics_executes_agent_code")
        sim.require(result["baked_tests_executed"] == [], "mechanics_baked_tests_executed")
        sim.require(result["early_exit_paths"] == early, "mechanics_early_exit_paths")
        sim.require(len(result["early_exit_paths"]) == int(plan.early_exit), "mechanics_early_exit_count")
        sim.require(result["clears_reward_file_first"] == plan.reward_clear, "mechanics_clears_reward")
