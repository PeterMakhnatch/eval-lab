"""MiMo setup fingerprint gates, through the real dispatch validate path (HAR-149)."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from evallab.execution_contracts import RunRequest, validate_request
from evallab.schemas import ExperimentSpec
from evallab.setup_fingerprint import (
    build_intended_fingerprint,
    compare_fingerprint,
    load_reference_profile,
    render_spec_preflight,
    resolve_repo_root,
    trial_fingerprint,
)

MIMO_MODEL = "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
TASK_ID = "format-code-task-900001"

LEDGER_HEADER = (
    "task_id,split,project,image_mib,status,reason,run,run_digest,"
    "run_transform,run_variant_status,census_label,census_nop_job,"
    "census_evidence,leak_channel,evidence"
)


def _real_profile_source() -> Path:
    return resolve_repo_root(None, Path(__file__)) / "research" / "setup-profiles"


def make_repo_root(
    base: Path,
    *,
    with_parser: bool,
    task_id: str,
    task_name: str = "fixture",
    ledger_task_id: str | None = None,
) -> tuple[Path, str]:
    """Minimal fixture checkout: serve config, reference profile, ledger, task."""
    from evallab.registry import compute_task_digests

    root = base / "fixture-root"
    (root / "research" / "setup-profiles").mkdir(parents=True)
    shutil.copy(
        _real_profile_source() / "xiaomi-mimo-rl.yaml",
        root / "research" / "setup-profiles" / "xiaomi-mimo-rl.yaml",
    )
    serve_dir = root / "tools" / "modal-mimo-serve"
    serve_dir.mkdir(parents=True)
    shutil.copy(
        resolve_repo_root(None, Path(__file__)) / "tools" / "modal-mimo-serve" / "serve.py",
        serve_dir / "serve.py",
    )
    if with_parser:
        parser_dir = root / "src" / "evallab"
        parser_dir.mkdir(parents=True)
        (parser_dir / "mimo_tool_calls.py").write_text("# fixture presence marker\n")
    task_dir = root / "tasks" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text(f'[task]\nname = "{task_name}"\n')
    digest = compute_task_digests(task_dir).package
    ledger_dir = root / "research" / "experiments" / "python-task-ledger"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "ledger.csv").write_text(
        LEDGER_HEADER
        + f"\n{ledger_task_id or task_id},train,fixture,100,usable,fixture sound,original,"
        + f"{digest},,,sound,fp-test-job,fixture evidence,none_found,fixture-evidence\n",
        encoding="utf-8",
    )
    return root, digest


def make_spec(
    task_id: str,
    digest: str,
    *,
    egress_lock: bool | None = True,
    reference_profile: str | None = "xiaomi-mimo-rl",
    deviations: list[dict] | None = None,
) -> ExperimentSpec:
    return ExperimentSpec(
        name="fp-test-001",
        hypothesis="fingerprint gate behaviour test",
        purpose="comparison",
        task=f"tasks/{task_id}",
        task_path=f"tasks/{task_id}",
        task_id=task_id,
        task_package_digest=digest,
        agent="terminus-2",
        model=MIMO_MODEL,
        environment="daytona",
        egress_lock=egress_lock,
        reference_profile=reference_profile,
        deviations=deviations or [],
        timeout_seconds=3600,
        submitted_by="test",
        max_requests=500,
        max_input_tokens=2500000,
        max_output_tokens=131072,
        max_total_tokens=2631072,
        cost_limit_usd=0.01,
    )


def make_request(
    root: Path, task_id: str, spec: ExperimentSpec, *, egress_lock: bool | None = True
) -> RunRequest:
    return RunRequest(
        task=root / "tasks" / task_id,
        agent="terminus-2",
        name="fp-test-001",
        jobs_dir=root / "runs",
        environment="daytona",
        model=MIMO_MODEL,
        attempts=1,
        concurrency=1,
        timeout_seconds=3600,
        allow_billable=True,
        max_requests=spec.max_requests,
        max_input_tokens=spec.max_input_tokens,
        max_output_tokens=spec.max_output_tokens,
        max_total_tokens=spec.max_total_tokens,
        cost_limit_usd=spec.cost_limit_usd,
        egress_lock=egress_lock,
        experiment_spec=spec,
        harness_tree_path=root / spec.harness_tree_path if spec.harness_tree_path else None,
        harness_tree_sha256=spec.harness_tree_sha256,
    )


COVERING_DEVIATIONS = [
    {"field": "harness.id", "value": "terminus-2", "reason": "eval harness, not training harness"},
    {"field": "server.tool_call_parser", "value": "mimo-native", "reason": "lab normalizer"},
    {"field": "server.context_length", "value": 65536, "reason": "served context"},
    {"field": "sampling.temperature", "value": 0.6, "reason": "proxy-enforced"},
]


def test_unlocked_run_refused(tmp_path: Path) -> None:
    root, digest = make_repo_root(tmp_path, with_parser=True, task_id=TASK_ID)
    spec = make_spec(TASK_ID, digest, egress_lock=None, deviations=COVERING_DEVIATIONS)
    with pytest.raises(ValueError, match="egress_lock"):
        validate_request(make_request(root, TASK_ID, spec, egress_lock=None), repo_root=root)


def test_wrong_temperature_refused(tmp_path: Path) -> None:
    root, digest = make_repo_root(tmp_path, with_parser=True, task_id=TASK_ID)
    spec = make_spec(TASK_ID, digest, deviations=[])
    with pytest.raises(ValueError, match="sampling.temperature"):
        validate_request(make_request(root, TASK_ID, spec), repo_root=root)


def test_missing_parser_refused(tmp_path: Path) -> None:
    root, digest = make_repo_root(tmp_path, with_parser=False, task_id=TASK_ID)
    spec = make_spec(TASK_ID, digest, deviations=COVERING_DEVIATIONS)
    with pytest.raises(ValueError, match="tool_call_parser"):
        validate_request(make_request(root, TASK_ID, spec), repo_root=root)


def test_task_outside_ledger_refused(tmp_path: Path) -> None:
    root, digest = make_repo_root(tmp_path, with_parser=True, task_id="format-code-task-900002")
    spec = make_spec("format-code-task-900003", digest, deviations=COVERING_DEVIATIONS)
    request = make_request(root, "format-code-task-900002", spec)
    with pytest.raises(ValueError, match="outside the ledger"):
        validate_request(request, repo_root=root)


def test_exploit_probe_needs_no_reference_but_keeps_lock_and_ledger(tmp_path: Path) -> None:
    from evallab.exploit_probe import PREAMBLE

    root, digest = make_repo_root(tmp_path, with_parser=True, task_id=TASK_ID)
    (root / PREAMBLE).parent.mkdir(parents=True)
    (root / PREAMBLE).write_text("probe\n")
    probe = make_spec(TASK_ID, digest, reference_profile=None).model_copy(
        update={"extra_instruction_path": PREAMBLE}
    )
    validate_request(make_request(root, TASK_ID, probe), repo_root=root)
    with pytest.raises(ValueError, match="reference_profile"):
        validate_request(
            make_request(root, TASK_ID, make_spec(TASK_ID, digest, reference_profile=None)),
            repo_root=root,
        )
    unlocked = probe.model_copy(update={"egress_lock": None})
    with pytest.raises(ValueError, match="lock"):
        validate_request(make_request(root, TASK_ID, unlocked, egress_lock=False), repo_root=root)


def test_missing_reference_refused(tmp_path: Path) -> None:
    root, digest = make_repo_root(tmp_path, with_parser=True, task_id=TASK_ID)
    spec = make_spec(TASK_ID, digest, reference_profile=None, deviations=COVERING_DEVIATIONS)
    with pytest.raises(ValueError, match="reference_profile"):
        validate_request(make_request(root, TASK_ID, spec), repo_root=root)


def test_declared_deviations_pass(tmp_path: Path) -> None:
    root, digest = make_repo_root(tmp_path, with_parser=True, task_id=TASK_ID)
    spec = make_spec(TASK_ID, digest, deviations=COVERING_DEVIATIONS)
    validate_request(make_request(root, TASK_ID, spec), repo_root=root)


def test_stale_deviation_value_refused(tmp_path: Path) -> None:
    root, digest = make_repo_root(tmp_path, with_parser=True, task_id=TASK_ID)
    stale = [dict(item) for item in COVERING_DEVIATIONS]
    stale[-1] = {"field": "sampling.temperature", "value": 1.0, "reason": "stale pin"}
    spec = make_spec(TASK_ID, digest, deviations=stale)
    with pytest.raises(ValueError, match="sampling.temperature"):
        validate_request(make_request(root, TASK_ID, spec), repo_root=root)


def test_lock_waiver_deviation_refused(tmp_path: Path) -> None:
    root, digest = make_repo_root(tmp_path, with_parser=True, task_id=TASK_ID)
    spec = make_spec(
        TASK_ID,
        digest,
        egress_lock=None,
        deviations=[
            *COVERING_DEVIATIONS,
            {"field": "lock.mode", "value": "open", "reason": "trying to waive the lock"},
        ],
    )
    with pytest.raises(ValueError, match="egress_lock"):
        validate_request(make_request(root, TASK_ID, spec, egress_lock=None), repo_root=root)


def test_trial_fingerprint_observes_lock(tmp_path: Path) -> None:
    root, digest = make_repo_root(tmp_path, with_parser=True, task_id=TASK_ID)
    spec = make_spec(TASK_ID, digest, deviations=COVERING_DEVIATIONS)
    intended = build_intended_fingerprint(
        spec=spec,
        task_dir=root / "tasks" / TASK_ID,
        model=MIMO_MODEL,
        agent="terminus-2",
        environment="daytona",
        repo_root=root,
    )
    assert intended["lock"]["mode"] == "locked"
    assert intended["sampling"]["temperature"] == 0.6
    locked_trial = tmp_path / "trial__a"
    locked_trial.mkdir()
    (locked_trial / "egress-lock.json").write_text(json.dumps({"applied": True}))
    assert trial_fingerprint(intended, locked_trial)["lock"]["mode"] == "locked"
    open_trial = tmp_path / "trial__b"
    open_trial.mkdir()
    assert trial_fingerprint(intended, open_trial)["lock"]["mode"] == "open"


def test_compare_skips_unsourced_reference_fields(tmp_path: Path) -> None:
    root, _ = make_repo_root(tmp_path, with_parser=True, task_id=TASK_ID)
    profile = load_reference_profile(root, "xiaomi-mimo-rl")
    fingerprint = {
        "harness": {"id": "mimoagent-default"},
        "server": {
            "tool_call_parser": "qwen3_coder",
            "reasoning_parser": "whatever",
            "context_length": 262144,
            "model_revision": "other",
            "sglang_image": "other",
        },
        "sampling": {"temperature": 1.0, "top_p": 0.95, "top_k": 20},
        "lock": {"mode": "locked"},
        "budgets": {"step_limit": 500},
    }
    assert compare_fingerprint(fingerprint, profile) == []


MIMO_TASK_NAME = "mimo-v2.6-rl__format-code-task-900001"


def make_nop_spec(task_id: str, digest: str, *, egress_lock: bool | None = None) -> ExperimentSpec:
    """Census-nop shape: model-free, no reference, no deviations."""
    return ExperimentSpec(
        name="fp-nop-001",
        hypothesis="model-free census gate behaviour test",
        purpose="comparison",
        task=f"tasks/{task_id}",
        task_path=f"tasks/{task_id}",
        task_id=task_id,
        task_package_digest=digest,
        agent="nop",
        model=None,
        environment="daytona",
        egress_lock=egress_lock,
        timeout_seconds=3600,
        submitted_by="test",
    )


def make_nop_request(
    root: Path, task_id: str, spec: ExperimentSpec, *, egress_lock: bool | None = None
) -> RunRequest:
    return RunRequest(
        task=root / "tasks" / task_id,
        agent="nop",
        name="fp-nop-001",
        jobs_dir=root / "runs",
        environment="daytona",
        model=None,
        attempts=1,
        concurrency=1,
        timeout_seconds=3600,
        allow_billable=False,
        egress_lock=egress_lock,
        experiment_spec=spec,
    )


def test_modelfree_nop_passes_without_reference(tmp_path: Path) -> None:
    root, digest = make_repo_root(
        tmp_path, with_parser=True, task_id=TASK_ID, task_name=MIMO_TASK_NAME
    )
    spec = make_nop_spec(TASK_ID, digest)
    validate_request(make_nop_request(root, TASK_ID, spec), repo_root=root)


def test_modelfree_nop_outside_ledger_refused(tmp_path: Path) -> None:
    root, digest = make_repo_root(
        tmp_path,
        with_parser=True,
        task_id="format-code-task-900002",
        task_name=MIMO_TASK_NAME,
        ledger_task_id="format-code-task-900001",
    )
    spec = make_nop_spec("format-code-task-900002", digest)
    request = make_nop_request(root, "format-code-task-900002", spec)
    with pytest.raises(ValueError, match="outside the ledger"):
        validate_request(request, repo_root=root)


def test_reference_default_200_request_ceiling_refused(tmp_path: Path) -> None:
    root, digest = make_repo_root(tmp_path, with_parser=True, task_id=TASK_ID)
    spec = make_spec(TASK_ID, digest, deviations=COVERING_DEVIATIONS).model_copy(
        update={"max_requests": 200}
    )
    with pytest.raises(ValueError, match="budgets.max_requests"):
        validate_request(make_request(root, TASK_ID, spec), repo_root=root)
    path = root / "spec.json"
    path.write_text(spec.model_dump_json())
    text, ok = render_spec_preflight(path, root)
    assert not ok
    assert (
        "budgets.max_requests=200: reference budgets.step_limit=500; binds before reference" in text
    )


def test_declared_request_ceiling_deviation_passes_preflight(tmp_path: Path) -> None:
    root, digest = make_repo_root(tmp_path, with_parser=True, task_id=TASK_ID)
    spec = make_spec(
        TASK_ID,
        digest,
        deviations=[
            *COVERING_DEVIATIONS,
            {"field": "budgets.max_requests", "value": 200, "reason": "diagnostic only"},
        ],
    ).model_copy(update={"max_requests": 200})
    validate_request(make_request(root, TASK_ID, spec), repo_root=root)
    path = root / "spec.json"
    path.write_text(spec.model_dump_json())
    text, ok = render_spec_preflight(path, root)
    assert ok
    assert "budgets.max_requests=200 (diagnostic only)" in text


def test_reference_gate_checks_effective_request_not_spec_claim(tmp_path: Path) -> None:
    root, digest = make_repo_root(tmp_path, with_parser=True, task_id=TASK_ID)
    spec = make_spec(TASK_ID, digest, deviations=COVERING_DEVIATIONS)
    request = replace(make_request(root, TASK_ID, spec), max_requests=200)
    with pytest.raises(ValueError, match="budgets.max_requests"):
        validate_request(request, repo_root=root)


@pytest.mark.parametrize(
    "field", ["max_input_tokens", "max_output_tokens", "max_total_tokens", "cost_limit_usd"]
)
def test_reference_budget_refuses_lower_ceiling(tmp_path: Path, field: str) -> None:
    import yaml

    root, digest = make_repo_root(tmp_path, with_parser=True, task_id=TASK_ID)
    spec = make_spec(TASK_ID, digest, deviations=COVERING_DEVIATIONS)
    path = root / "research/setup-profiles/xiaomi-mimo-rl.yaml"
    profile = yaml.safe_load(path.read_text())
    profile["budgets"][field] = {"value": getattr(spec, field) + 1, "source": "fixture reference"}
    path.write_text(yaml.safe_dump(profile))
    with pytest.raises(ValueError, match=f"budgets.{field}"):
        validate_request(make_request(root, TASK_ID, spec), repo_root=root)


@pytest.mark.parametrize("additions", [{"loop_break": True}, {"output_cap_chars": 2000}])
def test_reference_harness_addition_refused(tmp_path: Path, additions: dict) -> None:
    from evallab.terminus_harness import load_harness_tree

    root, digest = make_repo_root(tmp_path, with_parser=True, task_id=TASK_ID)
    tree = root / "candidate"
    (tree / "terminus").mkdir(parents=True)
    (tree / "terminus/config.json").write_text(json.dumps(additions))
    pinned = load_harness_tree(tree)
    spec = make_spec(TASK_ID, digest, deviations=COVERING_DEVIATIONS).model_copy(
        update={"harness_tree_path": "candidate", "harness_tree_sha256": pinned.sha256}
    )
    with pytest.raises(ValueError, match="harness.additions"):
        validate_request(make_request(root, TASK_ID, spec), repo_root=root)
    path = root / "spec.json"
    path.write_text(spec.model_dump_json())
    text, ok = render_spec_preflight(path, root)
    assert not ok
    assert "harness.additions" in text


def test_declared_harness_addition_passes(tmp_path: Path) -> None:
    from evallab.terminus_harness import load_harness_tree

    root, digest = make_repo_root(tmp_path, with_parser=True, task_id=TASK_ID)
    tree = root / "candidate"
    (tree / "terminus").mkdir(parents=True)
    (tree / "terminus/config.json").write_text('{"loop_break": true}')
    pinned = load_harness_tree(tree)
    spec = make_spec(
        TASK_ID,
        digest,
        deviations=[
            *COVERING_DEVIATIONS,
            {
                "field": "harness.additions",
                "value": {"loop_break": True},
                "reason": "loop treatment",
            },
        ],
    ).model_copy(update={"harness_tree_path": "candidate", "harness_tree_sha256": pinned.sha256})
    validate_request(make_request(root, TASK_ID, spec), repo_root=root)


@pytest.mark.parametrize("step_limit", [500, 501])
def test_native_reference_gate_uses_native_setup(tmp_path: Path, step_limit: int) -> None:
    root, digest = make_repo_root(tmp_path, with_parser=True, task_id=TASK_ID)
    config = root / "tools/mimoagent-harbor/swe.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(f"agent:\n  step_limit: {step_limit}\n")
    spec = make_spec(
        TASK_ID,
        digest,
        deviations=[
            {"field": "server.tool_call_parser", "value": "mimo", "reason": "served parser"},
            {"field": "server.context_length", "value": 65536, "reason": "served context"},
        ],
    ).model_copy(
        update={
            "agent": "mimoagent",
            "max_requests": 500_000,
            "max_input_tokens": 32_768_000_000,
            "max_output_tokens": 32_768_000_000,
            "max_total_tokens": 65_536_000_000,
        }
    )
    request = replace(
        make_request(root, TASK_ID, spec),
        agent="mimoagent",
        max_requests=spec.max_requests,
        max_input_tokens=spec.max_input_tokens,
        max_output_tokens=spec.max_output_tokens,
        max_total_tokens=spec.max_total_tokens,
    )
    if step_limit == 500:
        validate_request(request, repo_root=root)
    else:
        with pytest.raises(ValueError, match="budgets.step_limit"):
            validate_request(request, repo_root=root)


def test_modelfree_nop_of_a_registered_variant_passes_before_the_ledger_runs_it(
    tmp_path: Path,
) -> None:
    root, digest = make_repo_root(
        tmp_path,
        with_parser=True,
        task_id="format-code-task-900002",
        task_name=MIMO_TASK_NAME,
        ledger_task_id="format-code-task-900001",
    )
    records = root / "library/task-variants/mimo-v2.6-rl__format-code-task-900002"
    records.mkdir(parents=True)
    record = {"task_name": "mimo-v2.6-rl/format-code-task-900002", "variant_digest": digest}
    (records / "abc.json").write_text(json.dumps(record))
    spec = make_nop_spec("format-code-task-900002", digest)
    validate_request(make_nop_request(root, "format-code-task-900002", spec), repo_root=root)
    # A variant of another task does not lift the refusal.
    record["task_name"] = "mimo-v2.6-rl/format-code-task-900003"
    (records / "abc.json").write_text(json.dumps(record))
    with pytest.raises(ValueError, match="outside the ledger"):
        validate_request(make_nop_request(root, "format-code-task-900002", spec), repo_root=root)
