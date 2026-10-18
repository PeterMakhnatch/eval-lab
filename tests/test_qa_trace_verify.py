"""Claim isolation, evidence-backed revision and controlled counterfactual precedence."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tarfile
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evallab import runner
from evallab.qa import counterfactual as cf
from evallab.qa import trace, trace_workspace
from evallab.qa import trace_verify as tv
from evallab.qa.trace import Bundle, CallResult, ModelRoute

TASK = "mimo-v2.6-rl/format-code-task-000001"
AGENT_PATCH = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n"
REFERENCE_PATCH = AGENT_PATCH.replace("+new", "+reference")
TEST_PATCH = (
    "diff --git a/tests/test_case.py b/tests/test_case.py\n"
    "--- a/tests/test_case.py\n+++ b/tests/test_case.py\n"
    "@@ -1 +1 @@\n-assert value == 3\n+assert value == 2\n"
)
CHECK = "cat file_changes.diff"
COUNTER = "cat prompt.md"
NEW_CHECK = "cat task_codebase/tests/test_case.py"
NEW_EVIDENCE = f"task_codebase/tests/test_case.py:1 shows the required value; command: {NEW_CHECK}"
VERIFY_ROUTE = ModelRoute.parse("codex/test-verifier")
ANALYST_ROUTE = ModelRoute.parse("claude/test-analyst")


def _bundle(tmp: Path, *, reward: float = 0.0) -> Bundle:
    package = tmp / "source-package"
    (package / "tests").mkdir(parents=True)
    (package / "tests/test_case.py").write_text("assert value == 3\n")
    (package / "instruction.md").write_text("Produce value 2.\n")
    (package / "task.toml").write_text('[environment]\nworkdir = "/testbed"\n')
    root = tmp / "bundle"
    content = {
        "prompt.md": "Produce value 2.\n",
        "steps.txt": "[step 2] agent\nChanged app.py.\n",
        "trajectory.json": '{"steps": []}',
        "file_changes.diff": "# source: verifier/agent.diff\n" + AGENT_PATCH,
        "task_codebase/tests/test_case.py": "assert value == 3\n",
        "verifier/test-stdout.txt": "Expected 3, got 2.\n",
    }
    for rel, text in content.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    manifest = {
        "schema": trace.BUNDLE_SCHEMA,
        "subject": {"kind": "trace", "task_id": TASK, "trial_id": "blind-1"},
        "reward": {"value": reward},
        "availability": {
            "diff": "verifier/agent.diff",
            "tests": "present",
            "verifier_output": "present",
        },
        "resolution": {"package": str(package)},
        "files": {rel: hashlib.sha256((root / rel).read_bytes()).hexdigest() for rel in content},
    }
    (root / "bundle.json").write_text(json.dumps({**manifest, "private_label": "SECRET_LABEL"}))
    return Bundle(root, manifest)


def _answer() -> dict[str, Any]:
    return {
        "answer": "yes",
        "fault": "eval",
        "rationale": "The grader expects the wrong value.",
        "claims": [{"id": "c1", "text": "The recorded edit implements value 2.", "check": CHECK}],
        "private_chain": "DO_NOT_TRANSMIT_ANALYST_THOUGHTS",
    }


def _verification(*, overall: str = "CONFIRMED", claim_status: str = "VERIFIED") -> dict[str, Any]:
    return {
        "claims": [
            {
                "id": "c1",
                "text": "claim",
                "status": claim_status,
                "command": CHECK,
                "observed": "+new",
            }
        ],
        "counter_check": {
            "command": COUNTER,
            "observed": "Produce value 2.",
            "supports_conclusion": overall == "CONFIRMED",
        },
        "overall": overall,
    }


class FakeCall:
    def __init__(self, bundle: Bundle, outputs: list[tuple[dict[str, Any], list[str]]]):
        self.bundle = bundle
        self.outputs = iter(outputs)
        self.calls: list[tuple[ModelRoute, str, dict[str, Any], bool]] = []
        self.scratch_paths: list[Path] = []

    def __call__(
        self,
        route: ModelRoute,
        prompt: str,
        schema: dict[str, Any],
        *,
        cwd: Path,
        tools: bool = False,
        timeout: float = 600.0,
    ) -> CallResult:
        assert cwd != self.bundle.root
        assert cwd.is_dir() and not (cwd / "bundle.json").exists()
        assert "SECRET_LABEL" not in prompt
        assert "DO_NOT_TRANSMIT_ANALYST_THOUGHTS" not in prompt
        self.scratch_paths.append(cwd)
        self.calls.append((route, prompt, schema, tools))
        output, commands = next(self.outputs)
        return CallResult(
            route.label,
            output,
            json.dumps(output),
            {"input_tokens": 10},
            0.1,
            commands=commands,
        )


def test_budget_overflow_keeps_in_budget_checks_and_ignores_later_evidence(
    tmp_path: Path,
) -> None:
    bundle, answer = _bundle(tmp_path), _answer()
    answer["claims"].append(
        {"id": "c2", "text": "The grader rejects it.", "check": "cat verifier/test-stdout.txt"}
    )
    output = _verification()
    output["claims"].append(
        {
            "id": "c2",
            "text": "claim",
            "status": "VERIFIED",
            "command": "",
            "observed": "allegedly checked",
        }
    )
    # The counter-check is the 16th call: past the budget, so it proves nothing.
    commands = (
        [f"shell: {CHECK}"] + [f"shell: true # {n}" for n in range(14)] + [f"shell: {COUNTER}"]
    )
    call = FakeCall(bundle, [(output, commands)])

    result = tv.verify_claims(bundle, "false_negative", answer, VERIFY_ROUTE, call=call)

    assert result["tool_calls"] == 16 and result["budget_exceeded"] is True
    assert [row["status"] for row in result["claims"]] == ["VERIFIED", "UNVERIFIED"]
    assert result["counter_check"]["supports_conclusion"] is None
    assert result["overall"] == "INCONCLUSIVE"
    assert call.calls[0][2] is tv.VERIFY_SCHEMA and call.calls[0][3] is True
    assert "15 tool calls TOTAL" in call.calls[0][1]
    assert "ONE independent counter-check" in call.calls[0][1]
    assert all(not path.exists() for path in call.scratch_paths)
    assert bundle.verify() == []


def test_claims_and_counter_check_cannot_cite_unexecuted_commands(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    call = FakeCall(bundle, [(_verification(), [])])
    result = tv.verify_claims(bundle, "false_negative", _answer(), VERIFY_ROUTE, call=call)
    assert result["claims"][0]["status"] == "UNVERIFIED"
    assert result["counter_check"]["supports_conclusion"] is None
    assert result["overall"] == "INCONCLUSIVE"


def test_omp_tool_calls_match_their_natural_spelling(tmp_path: Path) -> None:
    # OMP records `read: {"path": ...}` / `bash: {"command": ..., "cwd": ...}`;
    # verifiers report `read file_changes.diff` or the bare shell command.
    bundle = _bundle(tmp_path)
    output = _verification()
    output["claims"][0]["command"] = "read file_changes.diff"
    commands = [
        'read: {"path": "file_changes.diff"}',
        f'bash: {{"command": "{COUNTER}", "cwd": "/tmp/scratch"}}',
    ]
    call = FakeCall(bundle, [(output, commands)])
    result = tv.verify_claims(bundle, "false_negative", _answer(), VERIFY_ROUTE, call=call)
    assert result["claims"][0]["status"] == "VERIFIED"
    assert result["overall"] == "CONFIRMED"


def test_claim_and_counter_check_require_separate_tool_calls(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    output = _verification()
    output["counter_check"]["command"] = CHECK
    call = FakeCall(bundle, [(output, [f"shell: {CHECK}"])])
    result = tv.verify_claims(bundle, "false_negative", _answer(), VERIFY_ROUTE, call=call)
    assert result["claims"][0]["status"] == "VERIFIED"
    assert result["counter_check"]["supports_conclusion"] is None
    assert result["overall"] == "INCONCLUSIVE"


@pytest.mark.parametrize(
    ("overall", "claim_status", "action", "evidence", "commands", "revised", "expected"),
    [
        ("CONFIRMED", "VERIFIED", None, "", [], "yes", "verified"),
        ("REJECTED", "VERIFIED", None, "", [], "yes", "refuted"),
        ("CONFIRMED", "REFUTED", "retract", "I agree with the check.", [], "no", "refuted"),
        # An unsupported rebuttal becomes a retraction: the claim is withdrawn,
        # the conclusion is no longer verified, but it is not refuted.
        ("CONFIRMED", "REFUTED", "rebut", "", [f"shell: {NEW_CHECK}"], "yes", "unverified"),
        ("CONFIRMED", "REFUTED", "rebut", NEW_EVIDENCE, [], "yes", "unverified"),
        ("CONFIRMED", "REFUTED", "rebut", NEW_EVIDENCE, [f"shell: {NEW_CHECK}"], "yes", "verified"),
        (
            "CONFIRMED",
            "REFUTED",
            "rebut",
            NEW_EVIDENCE,
            [f"shell: {NEW_CHECK}"],
            "unsure",
            "unverified",
        ),
        (
            "INCONCLUSIVE",
            "REFUTED",
            "rebut",
            NEW_EVIDENCE,
            [f"shell: {NEW_CHECK}"],
            "yes",
            "unverified",
        ),
        ("REJECTED", "REFUTED", "rebut", NEW_EVIDENCE, [f"shell: {NEW_CHECK}"], "yes", "refuted"),
    ],
)
def test_status_fusion_and_evidence_backed_revision(
    tmp_path: Path,
    overall: str,
    claim_status: str,
    action: str | None,
    evidence: str,
    commands: list[str],
    revised: str,
    expected: str,
) -> None:
    bundle = _bundle(tmp_path)
    outputs = [
        (
            _verification(overall=overall, claim_status=claim_status),
            [f"shell: {CHECK}", f"shell: {COUNTER}"],
        )
    ]
    if action:
        outputs.append(
            (
                {
                    "answer": revised,
                    "responses": [{"claim_id": "c1", "action": action, "evidence": evidence}],
                },
                commands,
            )
        )
    call = FakeCall(bundle, outputs)

    record = tv.refute(
        bundle,
        "false_negative",
        _answer(),
        routes={"verification": VERIFY_ROUTE, "analyst": ANALYST_ROUTE},
        trial_dir=None,
        config_like={"regrade": False},
        call=call,
    )

    assert record["stage"] == "T3" and record["status"] == expected
    assert record["usage"] == {"input_tokens": 10 * (2 if action else 1)}
    if action:
        assert call.calls[1][0] == ANALYST_ROUTE
        assert call.calls[1][2] is tv.ADDRESS_SCHEMA and call.calls[1][3] is True
        if action == "rebut" and (not evidence or not commands):
            assert record["revision"]["responses"][0]["action"] == "retract"
    else:
        assert record["revision"] is None


def test_every_refuted_claim_requires_a_response(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    verification = _verification(claim_status="REFUTED")
    verification["claims"].append({**verification["claims"][0], "id": "c2"})
    call = FakeCall(
        bundle,
        [
            (
                {
                    "answer": "yes",
                    "responses": [{"claim_id": "c1", "action": "rebut", "evidence": NEW_EVIDENCE}],
                },
                [f"shell: {NEW_CHECK}"],
            )
        ],
    )
    revision = tv.address_refuted(
        bundle, "false_negative", _answer(), verification, ANALYST_ROUTE, call=call
    )
    assert revision is not None
    assert [(row["claim_id"], row["action"]) for row in revision["responses"]] == [
        ("c1", "rebut"),
        ("c2", "retract"),
    ]


@pytest.mark.parametrize(
    ("score", "controls", "overall", "expected"),
    [
        (0.0, {"nop": 0.0, "reference": 1.0, "valid": True}, "CONFIRMED", "refuted"),
        (1.0, {"nop": 0.0, "reference": 1.0, "valid": True}, "REJECTED", "verified"),
        (0.0, {"nop": 1.0, "reference": 1.0, "valid": True}, "CONFIRMED", "verified"),
    ],
)
def test_only_valid_executed_counterfactuals_override_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    score: float,
    controls: dict[str, Any],
    overall: str,
    expected: str,
) -> None:
    bundle = _bundle(tmp_path)
    proposer = ModelRoute.parse("google-antigravity/test-proposer")
    proposed_routes: list[ModelRoute] = []

    def counterfactual(bundle, trial_dir, claim, route, **kwargs):
        proposed_routes.append(route)
        return {
            "kind": "minimal_grader_change",
            "ran": True,
            "patch": TEST_PATCH,
            "controls": controls,
            "regraded": score,
            "verdict": "verified" if score == 1.0 else "refuted",
            "reason": "executed evidence",
        }

    monkeypatch.setattr(tv, "minimal_grader_change", counterfactual)
    call = FakeCall(
        bundle, [(_verification(overall=overall), [f"shell: {CHECK}", f"shell: {COUNTER}"])]
    )
    record = tv.refute(
        bundle,
        "false_negative",
        _answer(),
        routes={"verification": VERIFY_ROUTE, "analyst": ANALYST_ROUTE, "grader_patch": proposer},
        trial_dir=tmp_path / "trial",
        config_like={"minimal_grader_change": True},
        call=call,
    )
    assert record["status"] == expected
    assert (record["by"] == "minimal_grader_change") is (controls["nop"] == 0.0)
    assert proposed_routes == [proposer]


def test_regrade_wrapper_preserves_missing_results_and_backend_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    seen: dict[str, Any] = {}
    gate = object()

    def regrade(trial_dir, task_name, **kwargs):
        seen.update(kwargs)
        assert task_name == TASK
        return {"ran": False, "reason": "no valid grader produced a reward", "graders": []}

    monkeypatch.setattr(trace, "regrade_counterfactual", regrade)
    result = tv.executed_regrade(
        bundle,
        tmp_path / "trial",
        repairs=(),
        grader_manifest=None,
        environment="modal",
        spend_gate=gate,
    )
    assert result["kind"] == "regrade" and result["ran"] is False
    assert result["recorded"] == 0.0 and result["lowest"] is None and result["highest"] is None
    assert seen["environment"] == "modal" and seen["spend_gate"] is gate


@pytest.mark.parametrize(
    ("question", "score", "expected"),
    [
        ("false_positive", 0.0, ("verified", "regrade")),
        ("false_positive", 1.0, ("unverified", "verification")),
        ("false_negative", 0.0, ("unverified", "verification")),
        ("false_negative", 1.0, ("verified", "regrade")),
    ],
)
def test_only_a_newer_grader_flip_in_the_claimed_direction_decides(
    question: str, score: float, expected: tuple[str, str]
) -> None:
    # A newer grader that agrees with the recorded reward may share the claimed
    # defect, so it leaves the claim to the verifier instead of refuting it.
    executed = {
        "kind": "regrade",
        "ran": True,
        "graders": [
            {
                "valid": True,
                "controls": {"nop": 0.0, "reference": 1.0},
                "regraded": score,
                "refusals": [],
            }
        ],
    }
    status, by, _ = tv.fuse(question, {"overall": "INCONCLUSIVE"}, None, executed)
    assert (status, by) == expected


@pytest.mark.parametrize(
    "patch",
    [
        AGENT_PATCH,
        TEST_PATCH.replace("tests/test_case.py", "tests/../../app.py"),
        TEST_PATCH.replace("+assert value == 2", "+pass"),
        TEST_PATCH.replace("b/tests/test_case.py", "b/task.toml"),
    ],
)
def test_minimal_grader_change_rejects_outside_tests_and_assertion_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patch: str,
) -> None:
    bundle = _bundle(tmp_path)
    call = FakeCall(
        bundle, [({"patch": patch, "justification": "Correct the expected value."}, [])]
    )

    def no_execution(*args, **kwargs):
        pytest.fail("rejected patch reached execution")

    monkeypatch.setattr(cf, "replay", no_execution)
    result = tv.minimal_grader_change(
        bundle,
        tmp_path / "trial",
        {**_answer(), "question": "false_negative"},
        VERIFY_ROUTE,
        call=call,
    )
    assert result["ran"] is False and result["verdict"] == "unverified"
    assert result["reason"] and result["controls"]["valid"] is False
    assert call.calls[0][3] is False
    assert (
        Path(bundle.manifest["resolution"]["package"]) / "tests/test_case.py"
    ).read_text() == "assert value == 3\n"


@pytest.mark.parametrize(
    ("nop", "reference", "replayed", "expected", "executions"),
    [
        (0.0, 1.0, 1.0, "verified", 3),
        (0.0, 1.0, 0.0, "refuted", 3),
        (1.0, 1.0, 1.0, "unverified", 2),
    ],
)
def test_minimal_grader_change_uses_same_variant_for_controls_and_recorded_diff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nop: float,
    reference: float,
    replayed: float,
    expected: str,
    executions: int,
) -> None:
    bundle = _bundle(tmp_path)
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "result.json").write_text('{"verifier_result": {"rewards": {"reward": 0.0}}}')
    package = Path(bundle.manifest["resolution"]["package"])
    source_before = {
        p.relative_to(package).as_posix(): p.read_bytes() for p in package.rglob("*") if p.is_file()
    }
    seen: list[tuple[Path, str]] = []

    def replay(
        trial_dir, grader, work, *, diff_text, repo_root, environment, spend_gate, runner=None,
        matrix_runner=None,
    ):
        assert grader.package != package
        assert (grader.package / "tests/test_case.py").read_text() == "assert value == 2\n"
        assert environment == "docker" and spend_gate is None
        seen.append((grader.package, diff_text))
        score = nop if diff_text == "" else reference if diff_text == REFERENCE_PATCH else replayed
        return {
            "regraded": score,
            "recorded": 0.0,
            "refusals": [],
            "outcome": "loosened" if score > 0 else "not_a_pass",
        }

    monkeypatch.setattr(cf, "replay", replay)
    monkeypatch.setattr(cf, "reference_patch", lambda task, root: REFERENCE_PATCH)
    monkeypatch.setattr(trace, "counterfactual_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(trace, "_repo_root", lambda: tmp_path)
    call = FakeCall(
        bundle,
        [({"patch": TEST_PATCH, "justification": "Use the value required by the task."}, [])],
    )

    result = tv.minimal_grader_change(
        bundle,
        trial,
        {**_answer(), "question": "false_negative"},
        VERIFY_ROUTE,
        call=call,
    )

    assert result["verdict"] == expected and result["ran"] is True
    assert result["controls"]["valid"] is (nop == 0.0 and reference == 1.0)
    assert len(seen) == executions and seen[0][1] == "" and seen[1][1] == REFERENCE_PATCH
    if executions == 3:
        assert seen[2][1] == bundle.text("file_changes.diff") and result["regraded"] == replayed
    assert len({path for path, _ in seen}) == 1
    assert all(not path.exists() for path, _ in seen)
    assert source_before == {
        p.relative_to(package).as_posix(): p.read_bytes() for p in package.rglob("*") if p.is_file()
    }


@pytest.mark.parametrize(
    "patch_source",
    [
        "verifier/agent.full.diff",
        "verifier/agent.diff",
        "verifier/agent.kept.diff",
        "artifacts/submission.patch",
        "snapshot",
    ],
)
def test_minimal_grader_change_replays_literal_patch_bytes_from_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch_source: str
) -> None:
    import evallab.docker_lock

    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setattr(evallab.docker_lock, "docker_slot", lambda **_: nullcontext())
    workspace = tmp_path / "workspace with spaces"
    workspace.mkdir()
    subprocess.run(["git", "init", "--quiet", str(workspace)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(workspace), "config", "core.autocrlf", "false"],
        check=True,
        capture_output=True,
    )
    target = workspace / "app.py"
    target.write_bytes(b"old\n")
    subprocess.run(
        ["git", "-C", str(workspace), "add", "app.py"], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git", "-C", str(workspace), "-c", "user.name=Fixture",
            "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=/dev/null",
            "commit", "--quiet", "-m", "base",
        ],
        check=True,
        capture_output=True,
    )
    postimage = b"new\r\nstill LF\nlast\r\n"
    target.write_bytes(postimage)
    patch = subprocess.run(
        ["git", "-C", str(workspace), "diff", "--no-ext-diff", "--no-textconv", "HEAD", "--"],
        check=True,
        capture_output=True,
    ).stdout
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "result.json").write_text(
        json.dumps({"task_name": TASK, "verifier_result": {"rewards": {"reward": 0.0}}})
    )
    untracked = workspace / "new.txt"
    if patch_source == "snapshot":
        untracked.write_bytes(b"untracked\r\n")
        archive = trial / "artifacts/workspace.tgz"
        archive.parent.mkdir()
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(workspace, arcname="workspace")
    else:
        retained = trial / patch_source
        retained.parent.mkdir(parents=True)
        retained.write_bytes(patch)
    source_before = {
        p.relative_to(trial): p.read_bytes() for p in trial.rglob("*") if p.is_file()
    }
    package = tmp_path / "package"
    (package / "tests").mkdir(parents=True)
    (package / "tests/test_case.py").write_text("assert value == 3\n")
    (package / "instruction.md").write_text("Preserve the requested literal file bytes.\n")
    (package / "task.toml").write_text(
        f"[environment]\nworkdir = {json.dumps(str(workspace))}\n"
    )
    monkeypatch.setattr(trace_workspace, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(trace_workspace, "_prior_archive_root", lambda: tmp_path / "priors")
    bundle = trace_workspace.build_workspace(
        trial, tmp_path / "bundle", task_dir=package, task_name=TASK
    )
    if patch_source != "snapshot":
        assert (bundle.root / "file_changes.diff").read_bytes() == (
            f"# source: {patch_source}\n".encode() + patch
        )
    monkeypatch.setattr(cf, "reference_patch", lambda *_: patch.decode("utf-8"))
    monkeypatch.setattr(trace, "counterfactual_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(trace, "_repo_root", lambda: tmp_path)
    observed: list[bytes] = []

    def execute(matrix, *, root):
        target.write_bytes(b"old\n")
        untracked.unlink(missing_ok=True)
        applied = subprocess.run(
            ["bash", str(root / matrix.runs[0].solution)], capture_output=True, text=True
        )
        assert applied.returncode == 0, applied.stderr
        observed.append(target.read_bytes())
        return SimpleNamespace(
            results=[{"status": "ok", "rewards": [float(observed[-1] == postimage)], "error": None}],
            receipt_path=root / f"{matrix.name}.json",
        )

    monkeypatch.setattr(runner, "run_matrix", execute)
    result = tv.minimal_grader_change(
        bundle,
        trial,
        {**_answer(), "question": "false_negative"},
        VERIFY_ROUTE,
        call=FakeCall(bundle, [({"patch": TEST_PATCH, "justification": "Correct the test."}, [])]),
    )
    assert result["verdict"] == "verified", result
    assert result["controls"] == {"nop": 0.0, "reference": 1.0, "valid": True}
    assert observed == [b"old\n", postimage, postimage]
    if patch_source == "snapshot":
        assert untracked.read_bytes() == b"untracked\r\n"
    assert bundle.verify() == []
    assert source_before == {
        p.relative_to(trial): p.read_bytes() for p in trial.rglob("*") if p.is_file()
    }


def test_standalone_eval_fault_claim_reaches_patch_proposer(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    call = FakeCall(
        bundle, [({"patch": AGENT_PATCH, "justification": "Rejected outside tests."}, [])]
    )
    tv.minimal_grader_change(
        bundle,
        tmp_path / "trial",
        {
            "id": "defect",
            "text": "Expected-value typo in grader",
            "check": NEW_CHECK,
            "fault": "eval",
        },
        VERIFY_ROUTE,
        call=call,
    )
    assert "Expected-value typo in grader" in call.calls[0][1]


def test_paid_minimal_grader_change_is_refused_before_proposing_or_replaying(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)

    def no_call(*args, **kwargs):
        pytest.fail("paid execution without a gate reached the model")

    result = tv.minimal_grader_change(
        bundle,
        tmp_path / "trial",
        {**_answer(), "question": "false_negative"},
        VERIFY_ROUTE,
        call=no_call,
        environment="modal",
    )
    assert result["ran"] is False and result["verdict"] == "unverified"
    assert "CloudSpendGate" in result["reason"]
