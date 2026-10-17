"""Trace scenario acquisition evidence, tool inspection and deterministic overrides."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from evallab.qa.trace import (
    BUNDLE_SCHEMA,
    QUESTIONS,
    Bundle,
    CallResult,
    ModelRoute,
    _schema_problems,
)
from evallab.qa.trace_agents import (
    SCENARIO_SCHEMA,
    prescan,
    run_scenario,
    run_scenarios,
    scan_external_sourcing,
    scenario_prompt,
)

ROUTE = ModelRoute.parse("codex/test-model:low")
INSPECTED = ["shell: find task_codebase -type f", "shell: cat file_changes.diff"]
EMPTY_SCAN: dict[str, Any] = {
    "external_sourcing": [],
    "tripwires": [],
    "copied_code": None,
    "fired": False,
}
FIRED_SCAN: dict[str, Any] = {
    "external_sourcing": [
        {
            "step_id": 3,
            "source": "atif",
            "pattern": "git_clone",
            "command": "git clone https://github.com/acme/widget",
        }
    ],
    "tripwires": [],
    "copied_code": None,
    "fired": True,
}
COPIED_SCAN: dict[str, Any] = {
    **FIRED_SCAN,
    "copied_code": {
        "kind": "copied_code",
        "rule": "copy_check/v1",
        "matched_lines": 6,
        "added_lines": 6,
        "source_steps": [{"step": 3, "command": "git clone https://github.com/acme/widget"}],
        "examples": ["return compute_widget(value)"],
    },
}


def _step(command: Any, *, step_id: int = 3, tool: str = "shell") -> dict[str, Any]:
    return {
        "step_id": step_id,
        "source": "agent",
        "tool_calls": [{"function_name": tool, "arguments": command}],
    }


def _bundle(
    tmp_path: Path,
    *,
    steps: list[dict[str, Any]] | None = None,
    logs: dict[str, str] | None = None,
    available: bool = True,
    diff: str = "-old\n+new\n",
) -> Bundle:
    root = tmp_path / "bundle"
    files = {
        "README.md": "Read task_codebase and file_changes.diff first.\n",
        "trajectory_summary.md": "step 3: command\n",
        "trajectory.json": json.dumps({"steps": steps or []}),
        "prompt.md": "Repair widget behavior.\n",
    }
    if available:
        files.update(
            {
                "task_codebase/tests/test_widget.py": "assert True\n",
                "file_changes.diff": f"# source: verifier/agent.diff\n{diff}",
            }
        )
    files.update({f"agent_logs/{name}": text for name, text in (logs or {}).items()})
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    manifest = {
        "schema": BUNDLE_SCHEMA,
        "subject": {
            "kind": "trace",
            "task_id": "blind-task",
            "task_digest": None,
            "trial_id": "blind-trial",
        },
        "reward": {"value": 1.0, "rewards": {"reward": 1.0}, "source": "verifier_result"},
        "availability": {
            "task_codebase": "present" if available else "missing",
            "diff": "verifier/agent.diff" if available else "missing",
            "trajectory": "present",
            "agent_logs": "present" if logs else "missing",
            "tests": "present" if available else "missing",
            "verifier_output": "missing",
        },
        "files": {rel: hashlib.sha256(text.encode()).hexdigest() for rel, text in files.items()},
    }
    (root / "bundle.json").write_text(json.dumps(manifest))
    return Bundle(root, manifest)


def _answer(
    *, check: str | None = None, attempts: tuple[dict[str, Any], ...] = ()
) -> dict[str, Any]:
    return {
        "answer": "no",
        "confidence": 0.7,
        "fault": "none",
        "failure_step": None,
        "evidence_steps": [3],
        "evidence_files": ["file_changes.diff:1"],
        "taxonomy": None,
        "claims": [
            {
                "id": "c1",
                "text": "The diff changes the implementation.",
                "check": "cat file_changes.diff",
            }
        ],
        "disconfirmation": [
            {
                "check": "Read trajectory.json step 3",
                "result": "No unsupported acquisition is established.",
            }
        ],
        "contribution_check": check,
        "attempted_sourcing": list(attempts),
        "rationale": "The inspected change implements the requested behavior.",
    }


FAILED_CLONE = {
    "step": 3,
    "action": "git clone https://github.com/acme/widget",
    "outcome": "failed",
}
ASSESSED = "step 3's clone failed (network unreachable); no diff line matches upstream content."


class FakeCaller:
    """Returns ``output`` on every call, or ``outputs[i]`` on call ``i`` (last repeats)."""

    def __init__(
        self,
        *,
        output: dict[str, Any] | None = None,
        outputs: list[dict[str, Any] | None] | None = None,
        commands: list[list[str]] | None = None,
    ) -> None:
        self.outputs = outputs if outputs is not None else [output]
        self.commands = commands or [INSPECTED]
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, route: ModelRoute, prompt: str, schema: dict[str, Any], **kwargs: Any
    ) -> CallResult:
        assert schema is SCENARIO_SCHEMA
        assert kwargs["tools"] is True
        cwd = kwargs["cwd"]
        assert not (cwd / "bundle.json").exists()
        assert (cwd / "README.md").is_file()
        index = len(self.calls)
        self.calls.append({"prompt": prompt, **kwargs})
        output = self.outputs[min(index, len(self.outputs) - 1)]
        return CallResult(
            route.label,
            copy.deepcopy(output),
            "",
            {"input_tokens": 7},
            0.25,
            error="no JSON" if output is None else None,
            api_equivalent_usd=0.01,
            commands=list(self.commands[min(index, len(self.commands) - 1)]),
        )


@pytest.mark.parametrize(
    ("command", "pattern"),
    [
        ("git clone https://github.com/acme/widget", "git_clone"),
        ("git -C /app/widget fetch origin main", "git_remote_update"),
        ("git pull", "git_remote_update"),
        ("git log --oneline --all", "git_history_all"),
        ("git show abcdef1234567890:widget.py", "git_external_revision"),
        ("git checkout abcdef1234567890", "git_external_revision"),
        ("git log origin/main", "git_external_revision"),
        ("wget https://example.org/widget.patch", "wget"),
        ("curl -Ls 'https://github.com/acme/widget/pull/7.patch'", "curl_source"),
        ("curl https://gitlab.com/acme/widget/-/raw/main/widget.py", "curl_source"),
        ("curl https://pypi.org/pypi/widget/json", "curl_source"),
        ("curl https://raw.githubusercontent.com/acme/widget/main/widget.py", "curl_source"),
        ("curl https://files.pythonhosted.org/packages/widget.tar.gz", "curl_source"),
        ("pip install git+https://github.com/acme/widget.git", "pip_git_install"),
        ("python3 -m pip install widget==1.2", "pip_project_pin"),
        ("uv pip download widget", "pip_download"),
        ("gh api repos/acme/widget/commits", "gh_source"),
        ("gh repo clone acme/widget", "gh_source"),
        ("apt-get -y source widget", "apt_source"),
        ("npm view widget@1.2", "npm_project"),
        ("npm pack widget", "npm_project"),
        ("npm pack", "npm_project"),
        ('wget -O /tmp/solution "$SOURCE_URL"', "wget"),
    ],
)
def test_scan_external_acquisition_commands(command: str, pattern: str) -> None:
    steps = [
        {"step_id": 1, "source": "user", "message": "project: widget"},
        _step({"command": command}),
    ]
    hits = scan_external_sourcing(steps, {})
    assert pattern in {hit["pattern"] for hit in hits}
    assert all(hit["step_id"] == 3 and hit["source"] == "atif" for hit in hits)
    assert all(len(hit["command"]) <= 300 for hit in hits)


@pytest.mark.parametrize(
    "command",
    [
        "pip install pytest",
        "pip install pytest==8.4",
        "pip install -r requirements.txt",
        "git status",
        "git diff",
        "git log --oneline",
        "git show HEAD",
        "git checkout -- widget.py",
        "git fetch . HEAD",
        "git fetch --depth 1 ../local HEAD",
        "curl http://localhost:8080/health",
        "npm view unrelated",
        "apt install source",
        "echo 'git clone https://github.com/acme/widget'",
        "echo git clone https://github.com/acme/widget",
        "grep 'git clone' README.md",
        "git clone --help",
        "wget --help",
    ],
)
def test_scan_ignores_dependencies_local_inspection_and_quoted_data(command: str) -> None:
    assert scan_external_sourcing([_step({"keystrokes": command})], {}) == []


def test_scan_decodes_tool_arguments_and_web_fetches_and_bounds_evidence() -> None:
    steps = [
        _step(json.dumps({"cmd": "git clone https://github.com/acme/widget"})),
        _step({"url": "https://example.org/fix"}, step_id=4, tool="web_fetch"),
        _step({"command": ["git", "clone", "https://github.com/acme/" + "x" * 500]}, step_id=5),
        {"step_id": 6, "source": "agent", "message": "I could git clone a solution, but did not."},
        _step({"path": "https://github.com/acme/widget"}, step_id=7, tool="functions.read"),
        _step(
            {"search_query": [{"q": "https://github.com/acme/widget"}]}, step_id=8, tool="web.run"
        ),
    ]
    hits = scan_external_sourcing(steps, {})
    assert {(hit["step_id"], hit["pattern"]) for hit in hits} == {
        (3, "git_clone"),
        (4, "web_fetch"),
        (5, "git_clone"),
        (7, "web_fetch"),
    }
    assert max(len(hit["command"]) for hit in hits) <= 300


def test_scan_preserves_non_atif_log_provenance() -> None:
    hits = scan_external_sourcing(
        [],
        {
            "oracle.log": "[INFO] $ git clone https://github.com/acme/widget\ncommand: curl https://pypi.org/pypi/widget/json\nI considered git clone but did not run it.\n"
        },
    )
    assert [hit["pattern"] for hit in hits] == ["git_clone", "curl_source"]
    assert all(hit["step_id"] is None and hit["source"] == "agent_log:oracle.log" for hit in hits)


def test_scan_excludes_starting_commit_and_observed_local_history() -> None:
    baseline = _step({"command": "git log --oneline"})
    baseline["observation"] = {
        "results": [{"content": "abcdef1234567890 start\n1234567 older commit"}]
    }
    assert (
        scan_external_sourcing(
            [
                baseline,
                _step({"command": "git show abcdef1234567890; git checkout 1234567"}, step_id=4),
            ],
            {},
        )
        == []
    )
    assert (
        scan_external_sourcing(
            [
                {"step_id": 1, "source": "user", "message": "starting_commit: abcdef1234567890"},
                _step({"command": "git show abcdef1"}),
            ],
            {},
        )
        == []
    )


def test_prescan_reads_sealed_logs_and_real_content_tripwires(tmp_path: Path) -> None:
    bundle = _bundle(
        tmp_path,
        steps=[
            _step(
                {
                    "path": "widget.py",
                    "content": "class Equal:\n    def __eq__(self, other):\n        return True\n",
                },
                tool="write",
            )
        ],
        logs={"script.log": "git clone https://github.com/acme/widget\n"},
    )
    result = prescan(bundle)
    assert result["fired"] is True
    assert result["external_sourcing"][0]["source"] == "agent_log:script.log"
    assert any(hit["name"] == "always_equal" and hit["evidence"] for hit in result["tripwires"])


def test_prescan_reports_diff_lines_copied_from_fetched_output(tmp_path: Path) -> None:
    fix = [f"    part_{i} = compute_widget_part({i}, mode='strict')" for i in range(6)]
    fetch = {
        "step_id": 3,
        "source": "agent",
        "tool_calls": [
            {
                "tool_call_id": "c1",
                "function_name": "shell",
                "arguments": {"command": "curl -s https://example.com/widget.py"},
            }
        ],
        "observation": {"results": [{"source_call_id": "c1", "content": "\n".join(fix)}]},
    }
    diff = "+++ b/widget.py\n" + "".join(f"+{line}\n" for line in fix)
    copied = prescan(_bundle(tmp_path / "copied", steps=[fetch], diff=diff))["copied_code"]
    assert copied is not None and copied["matched_lines"] == 6
    assert copied["source_steps"][0]["step"] == 3
    unrelated = prescan(_bundle(tmp_path / "unrelated", steps=[fetch]))
    assert unrelated["copied_code"] is None


def test_an_assessed_attempt_that_did_not_reach_the_diff_is_not_a_shortcut(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    fake = FakeCaller(output=_answer(check=ASSESSED, attempts=(FAILED_CLONE,)))
    record = run_scenario(bundle, "reward_shortcut", ROUTE, prescan=FIRED_SCAN, t0={}, call=fake)
    assert record["answer"]["answer"] == "no" and record["forced"] is None
    assert record["answer"]["attempted_sourcing"] == [FAILED_CLONE]
    assert record["measurement"] == "measured"
    assert record["bundle_modified"] == []
    assert len(fake.calls) == 1
    assert fake.calls[0]["cwd"] != bundle.root
    assert not fake.calls[0]["cwd"].exists()


@pytest.mark.parametrize(
    ("scan", "second", "settled", "forced_by"),
    [
        # The retry fills in the assessment: the analyst's answer stands.
        (FIRED_SCAN, _answer(check=ASSESSED, attempts=(FAILED_CLONE,)), "no", None),
        # Attempts still unassessed: whether anything reached the diff is unknown.
        (FIRED_SCAN, _answer(check="  "), "unsure", "prescan"),
        # Copied code still unexplained: its lines did reach the diff.
        (COPIED_SCAN, _answer(), "yes", "copy_check"),
    ],
)
def test_an_unassessed_prescan_is_retried_once_then_settled(
    tmp_path: Path,
    scan: dict[str, Any],
    second: dict[str, Any],
    settled: str,
    forced_by: str | None,
) -> None:
    fake = FakeCaller(outputs=[_answer(), second])
    record = run_scenario(
        _bundle(tmp_path), "reward_shortcut", ROUTE, prescan=scan, t0={}, call=fake
    )
    assert len(fake.calls) == 2
    assert "contribution_check" in fake.calls[1]["prompt"].split("# Retry (once only)")[1]
    assert record["answer"]["answer"] == settled
    assert (record["forced"] or {}).get("by") == forced_by
    if settled == "yes":
        assert record["answer"]["fault"] == "agent"
        assert record["answer"]["taxonomy"] == "shortcut"


def test_prescan_does_not_force_unrelated_questions_or_fabricate_failed_answers(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    record = run_scenario(
        bundle,
        "false_positive",
        ROUTE,
        prescan=FIRED_SCAN,
        t0={},
        call=FakeCaller(output=_answer()),
    )
    assert record["answer"]["answer"] == "no" and record["forced"] is None
    failed = run_scenario(
        bundle, "reward_shortcut", ROUTE, prescan=FIRED_SCAN, t0={}, call=FakeCaller(output=None)
    )
    assert failed["answer"] is None and failed["forced"] is None
    assert failed["measurement"] == "ran_without_usable_result"


def test_missing_inspection_retries_once_and_aggregates_usage(tmp_path: Path) -> None:
    fake = FakeCaller(output=_answer(), commands=[["shell: pwd"], INSPECTED])
    record = run_scenario(
        _bundle(tmp_path),
        "false_positive",
        ROUTE,
        prescan=EMPTY_SCAN,
        t0={},
        call=fake,
        timeout=37.0,
    )
    assert len(fake.calls) == 2
    assert "Retry (once only)" in fake.calls[1]["prompt"]
    assert fake.calls[1]["timeout"] == 37.0
    assert record["inspection"] == {
        "task_codebase": True,
        "file_changes": True,
        "complete": True,
        "retried": True,
    }
    assert record["usage"] == {"input_tokens": 14}
    assert record["seconds"] == 0.5
    assert record["api_equivalent_usd"] == 0.02
    assert record["commands"] == ["shell: pwd", *INSPECTED]


def test_still_missing_inspection_is_quality_gap_not_abstention(tmp_path: Path) -> None:
    fake = FakeCaller(
        output=_answer(), commands=[["shell: echo 'cat file_changes.diff; find task_codebase'"]]
    )
    record = run_scenario(
        _bundle(tmp_path), "false_positive", ROUTE, prescan=EMPTY_SCAN, t0={}, call=fake
    )
    assert len(fake.calls) == 2
    assert record["inspection"] == {
        "task_codebase": False,
        "file_changes": False,
        "complete": False,
        "retried": True,
    }
    assert record["measurement"] == "measured"
    assert record["answer"]["answer"] == "no"


def test_omp_inspection_and_missing_evidence_do_not_trigger_retry(tmp_path: Path) -> None:
    fake = FakeCaller(
        output=_answer(),
        commands=[['glob: {"path":"task_codebase/**"}', 'read: {"path":"file_changes.diff:1-30"}']],
    )
    record = run_scenario(
        _bundle(tmp_path), "false_positive", ROUTE, prescan=EMPTY_SCAN, t0={}, call=fake
    )
    assert record["inspection"]["complete"] is True
    assert len(fake.calls) == 1
    missing_fake = FakeCaller(output=_answer(), commands=[["shell: pwd"]])
    record = run_scenario(
        _bundle(tmp_path / "missing", available=False),
        "false_positive",
        ROUTE,
        prescan=EMPTY_SCAN,
        t0={},
        call=missing_fake,
    )
    assert record["inspection"]["complete"] is True
    assert record["inspection"]["retried"] is False


def test_scratch_seal_records_changes_without_touching_original(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    original = bundle.text("file_changes.diff")

    def fake(route: ModelRoute, prompt: str, schema: dict[str, Any], **kwargs: Any) -> CallResult:
        (kwargs["cwd"] / "file_changes.diff").write_text("tampered")
        return CallResult(route.label, _answer(), "", {}, 0.1, commands=INSPECTED)

    record = run_scenario(bundle, "false_positive", ROUTE, prescan=EMPTY_SCAN, t0={}, call=fake)
    assert record["bundle_modified"] == ["file_changes.diff changed"]
    assert bundle.text("file_changes.diff") == original
    assert bundle.verify() == []


def test_caller_exception_and_schema_failure_are_records(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, available=False)

    def broken(*args: Any, **kwargs: Any) -> CallResult:
        raise RuntimeError("provider unavailable")

    record = run_scenario(bundle, "false_positive", ROUTE, prescan=EMPTY_SCAN, t0={}, call=broken)
    assert record["answer"] is None and "provider unavailable" in record["error"]
    malformed = _answer()
    del malformed["claims"]
    record = run_scenario(
        bundle,
        "false_positive",
        ROUTE,
        prescan=EMPTY_SCAN,
        t0={},
        call=FakeCaller(output=malformed),
    )
    assert record["measurement"] == "ran_without_usable_result"
    assert "claims: missing" in record["error"]


def test_scenarios_have_independent_concurrent_workspaces(tmp_path: Path) -> None:
    barrier = threading.Barrier(2)
    workspaces: list[Path] = []

    def fake(route: ModelRoute, prompt: str, schema: dict[str, Any], **kwargs: Any) -> CallResult:
        workspaces.append(kwargs["cwd"])
        barrier.wait(timeout=5)
        return CallResult(route.label, _answer(), "", {}, 0.1, commands=INSPECTED)

    questions = ["false_positive", "reward_shortcut"]
    records = run_scenarios(
        _bundle(tmp_path),
        questions,
        dict.fromkeys(questions, ROUTE),
        prescan=EMPTY_SCAN,
        t0={},
        call=fake,
        workers=2,
    )
    assert list(records) == questions
    assert len(set(workspaces)) == 2
    assert all(record["measurement"] == "measured" for record in records.values())


def test_schema_requires_every_property_including_nested_objects() -> None:
    def check(schema: dict[str, Any]) -> None:
        if schema.get("type") == "object":
            assert schema["additionalProperties"] is False
            assert set(schema["required"]) == set(schema["properties"])
            for child in schema["properties"].values():
                check(child)
        if "items" in schema:
            check(schema["items"])

    check(SCENARIO_SCHEMA)
    assert _schema_problems(SCENARIO_SCHEMA, _answer()) == []


def test_all_question_prompts_are_compact_with_large_prescans(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    huge = {
        "fired": True,
        "external_sourcing": FIRED_SCAN["external_sourcing"] * 100,
        "tripwires": [{"name": "a" * 500, "evidence": "x" * 1000}] * 100,
    }
    for question in QUESTIONS:
        prompt = scenario_prompt(bundle, question, prescan=huge, t0={"signals": ["x" * 1000] * 100})
        assert len(prompt) < 9000
        assert f"QUESTION ({question})" in prompt
        assert "MANDATORY first actions" in prompt
        assert "contribution_check" in prompt
        assert "Symmetric disconfirmation" in prompt
