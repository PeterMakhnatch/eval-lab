"""Local/temp-only FW-4 contracts; these tests never dispatch a model or Harbor run."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

from evallab.failure_library.bank import optimize as opt
from evallab.production.run_atlas import Completion


def _examples() -> list[dict]:
    examples = []
    for prompt in opt.PROMPTS:
        for number in range(90):
            # Two different tasks connected by overlapping family units, not just
            # exact task-name duplicates. The connection must be transitive.
            units = [f"family-{number}"]
            if number == 0:
                units.append("family-1")
            examples.append(
                {
                    "id": f"{prompt}-{number}",
                    "prompt": prompt,
                    "task": f"task-{number}",
                    "units": units,
                    "messages": [
                        [
                            {"role": "system", "content": f"Frozen {prompt} rules."},
                            {"role": "user", "content": "Quoted source without labels."},
                        ]
                    ],
                }
            )
    return examples


def _registration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict]:
    monkeypatch.setattr(
        opt,
        "engine_metadata",
        lambda: {
            "required_version": opt.GEPA_VERSION,
            "version": None,
            "source_sha256": None,
        },
    )
    # Deliberately not JSON: promotion bundles are opaque hashes at proposal time.
    trace = tmp_path / "trace.bundle"
    taskqa = tmp_path / "taskqa.bundle"
    trace.write_bytes(b"opaque retained manifest; do not decode labels\xff")
    taskqa.write_bytes(b"opaque retained manifest; no result inspection\xfe")
    source = tmp_path / "source.txt"
    source.write_text("Immutable scientific input.")
    run_dir = tmp_path / "experiment"
    examples = _examples()
    registration = opt.preregister(
        run_dir,
        examples=examples,
        truth={e["id"]: {"scorer_only_key": "SECRET-KEY-TEXT"} for e in examples},
        baselines={p: f"Frozen {p} rules." for p in opt.PROMPTS},
        references=[opt.file_ref(source)],
        cross_test_manifests={"trace": trace, "taskqa": taskqa},
        seed=17,
        limits=opt.Limits(train=4, validation=3),
    )
    return run_dir, registration


def test_family_connected_split_is_global_and_order_independent() -> None:
    examples = _examples()
    split = opt.grouped_split(examples, seed=17, train=8, validation=5)
    assert split == opt.grouped_split(list(reversed(examples)), seed=17, train=8, validation=5)
    groups = split["groups"]
    assert groups["trace-0"] == groups["trace-1"] == groups["taskqa-1"]
    train, validation = set(), set()
    for reader in split["readers"].values():
        train.update(groups[i] for i in reader["train"])
        validation.update(groups[i] for i in reader["validation"])
    assert train.isdisjoint(validation)
    assert len(split["readers"]["trace"]["train"]) == 8
    assert len(split["readers"]["taskqa"]["validation"]) == 5


def test_missing_family_authority_and_single_group_never_fall_back_to_rows() -> None:
    examples = _examples()
    examples[0]["units"] = []
    with pytest.raises(opt.PrerequisiteError, match="ledger units are missing"):
        opt.grouped_split(examples, seed=17, train=1, validation=1)
    for example in examples:
        example["units"] = ["one-family"]
    with pytest.raises(opt.PrerequisiteError, match="no row-level fallback"):
        opt.grouped_split(examples, seed=17, train=1, validation=1)


def test_tasks_outside_the_ledger_group_by_task_and_are_counted() -> None:
    population: Counter = Counter()
    ledger = {"known": frozenset({"family-a"})}
    assert opt._units("known", ledger, population) == ["family-a"]
    assert opt._units("goodfire-task", ledger, population) == ["task-only:goodfire-task"]
    assert population == Counter({"task_only_grouping": 1})
    # Two runs of one unledgered task can never straddle train and validation.
    examples = [
        {
            "id": f"{prompt}-{i}",
            "prompt": prompt,
            "task": task,
            "units": opt._units(task, ledger, population),
        }
        for prompt in opt.PROMPTS
        for i, task in enumerate(["goodfire-task", "goodfire-task", "other", "known"])
    ]
    groups = opt.grouped_split(examples, seed=1, train=1, validation=1)["groups"]
    assert groups["trace-0"] == groups["trace-1"] == groups["taskqa-0"]


def test_plan_is_opaque_to_cross_labels_and_separates_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, registration = _registration(tmp_path, monkeypatch)
    assert registration["status"] == "BLOCKED"
    assert registration["model_calls"] == 0
    plan = opt.checked_plan(run_dir, registration["plan_digest"])
    assert plan["limits"]["max_metric_calls"] == 100
    assert plan["promotion_gate"]["taskqa"]["eligibility"] == {"verified": 0.5, "unverified": 0.8}
    assert "SECRET-KEY-TEXT" not in (run_dir / "inputs.json").read_text()
    assert "SECRET-KEY-TEXT" in (run_dir / "scorer-only.json").read_text()
    assert not (run_dir / "started.json").exists()
    with pytest.raises(FileExistsError):
        opt.preregister(
            run_dir,
            examples=_examples(),
            truth={e["id"]: {} for e in _examples()},
            baselines={p: f"Frozen {p} rules." for p in opt.PROMPTS},
            references=[],
            cross_test_manifests={p: tmp_path / f"{p}.bundle" for p in opt.PROMPTS},
            seed=17,
            limits=opt.Limits(train=4, validation=3),
        )


def test_digest_and_changed_scorer_truth_block_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, registration = _registration(tmp_path, monkeypatch)
    with pytest.raises(opt.PrerequisiteError, match="--plan-digest"):
        opt.run_experiment(run_dir, plan_digest="0" * 64, execute=True)
    with pytest.raises(opt.PrerequisiteError, match="explicit --execute"):
        opt.run_experiment(run_dir, plan_digest=registration["plan_digest"])
    (run_dir / "scorer-only.json").write_text("{}")
    with pytest.raises(opt.PrerequisiteError, match="scorer-only.json"):
        opt.checked_plan(run_dir, registration["plan_digest"])
    assert not (run_dir / "started.json").exists()


def test_source_and_opaque_bundle_drift_are_not_new_candidate_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, registration = _registration(tmp_path, monkeypatch)
    source = tmp_path / "source.txt"
    source.write_text("Changed scientific input.")
    with pytest.raises(opt.PrerequisiteError, match="source/input drift"):
        opt.checked_plan(run_dir, registration["plan_digest"])
    source.write_text("Immutable scientific input.")
    (tmp_path / "trace.bundle").write_bytes(b"different bundle")
    with pytest.raises(opt.PrerequisiteError, match="trace.bundle"):
        opt.checked_plan(run_dir, registration["plan_digest"])


def test_missing_gepa_fails_actionably_without_substitute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(opt, "engine_metadata", lambda: {"version": None})
    with pytest.raises(opt.PrerequisiteError, match=r"gepa==0\.1\.4.*NEW run directory"):
        opt.load_gepa()


class _Transport:
    def __init__(self, reply: Completion | Exception) -> None:
        self.reply = reply
        self.calls = 0

    def complete(self, *args, **kwargs) -> Completion:
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _ledger(
    tmp_path: Path, transport: _Transport, limits: opt.Limits | None = None
) -> opt.CallLedger:
    return opt.CallLedger(
        tmp_path,
        {
            "reader": opt.READER,
            "reflection_model": opt.REFLECTION_MODEL,
            "fixed_scorer": opt.SCORER_MODEL,
            "limits": (limits or opt.Limits()).__dict__,
        },
        transport,
    )


MESSAGES = [{"role": "user", "content": "Quoted diagnostic source."}]


def test_reader_mismatch_is_terminal_and_accounts_observed_tokens(tmp_path: Path) -> None:
    transport = _Transport(Completion("{}", opt.SCORER_MODEL, 5, 2, 0.25))
    ledger = _ledger(tmp_path, transport)
    with pytest.raises(opt.PrerequisiteError, match="served-model mismatch"):
        ledger.complete(opt.READER, MESSAGES, purpose="audit")
    with pytest.raises(opt.BudgetExceeded):
        ledger.complete(opt.READER, MESSAGES, purpose="audit")
    receipt = json.loads((tmp_path / "calls/000001/receipt.json").read_text())
    assert transport.calls == 1
    assert ledger.tokens == 7
    assert receipt["status"] == "unanswered"
    assert receipt["requested_model"] != receipt["served_model"]
    assert receipt["paid_spend_usd"] is None


def test_offline_retest_cannot_swap_candidate_or_import_historical_calls(tmp_path: Path) -> None:
    transport = _Transport(Completion("{}", opt.READER, 5, 2, 0.1))
    ledger = _ledger(tmp_path, transport)
    _, receipt = ledger.complete(opt.READER, MESSAGES, purpose="retest:candidate")
    archive = opt.ArchivedCalls(tmp_path, ledger.plan, [receipt])
    with pytest.raises(opt.PrerequisiteError, match="exact selected candidate"):
        archive.complete(
            opt.READER,
            [{"role": "user", "content": "a different candidate input"}],
            purpose="retest:candidate",
        )
    other_run = tmp_path / "other-experiment"
    other_run.mkdir()
    with pytest.raises(opt.PrerequisiteError, match="another experiment"):
        opt.ArchivedCalls(other_run, ledger.plan, [receipt]).complete(
            opt.READER, MESSAGES, purpose="retest:candidate"
        )
    with pytest.raises(opt.PrerequisiteError, match="purpose attestation"):
        archive.complete(opt.READER, MESSAGES, purpose="retest:baseline")
    retained, _ = archive.complete(opt.READER, MESSAGES, purpose="retest:candidate")
    archive.finish()
    with pytest.raises(opt.PrerequisiteError, match="response missing"):
        archive.complete(opt.READER, MESSAGES, purpose="retest:candidate")
    assert retained == "{}"
    assert transport.calls == 1  # offline parser/scorer replay cannot issue another call


def test_quota_stops_without_retry_or_fallback_and_retains_unknown_usage(tmp_path: Path) -> None:
    class QuotaError(RuntimeError):
        quota = True

    transport = _Transport(QuotaError("subscription quota exhausted"))
    ledger = _ledger(tmp_path, transport)
    with pytest.raises(QuotaError):
        ledger.complete(opt.READER, MESSAGES, purpose="audit")
    with pytest.raises(opt.BudgetExceeded):
        ledger.complete(opt.REFLECTION_MODEL, MESSAGES, purpose="reflection")
    receipt = json.loads((tmp_path / "calls/000001/receipt.json").read_text())
    assert transport.calls == 1
    assert receipt["quota"] is True
    assert receipt["input_tokens"] is None and receipt["output_tokens"] is None


def test_unreported_tokens_never_look_like_a_zero_cost_answer(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, _Transport(Completion("{}", opt.READER, 0, 0, 0.1)))
    with pytest.raises(opt.PrerequisiteError, match="token accounting"):
        ledger.complete(opt.READER, MESSAGES, purpose="audit")
    receipt = json.loads((tmp_path / "calls/000001/receipt.json").read_text())
    assert receipt["status"] == "unanswered"
    assert ledger.stopped


def test_token_admission_and_tools_are_rejected_before_dispatch(tmp_path: Path) -> None:
    transport = _Transport(Completion("{}", opt.READER, 5, 2, 0.1))
    cap = len(MESSAGES[0]["content"].encode()) + 512 + opt.Limits().max_output_tokens - 1
    ledger = _ledger(tmp_path, transport, replace(opt.Limits(), max_total_tokens=cap))
    with pytest.raises(opt.BudgetExceeded):
        ledger.complete(opt.READER, MESSAGES, purpose="audit")
    with pytest.raises(opt.PrerequisiteError, match="no tools"):
        ledger.complete(
            opt.READER, [{"role": "user", "content": "x", "tool_calls": []}], purpose="audit"
        )
    assert transport.calls == 0
    assert not (tmp_path / "calls").exists()


def _fake_omp(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fallback: bool,
    used: float,
    windows: dict[str, tuple[str, ...]] | None = None,
) -> list[tuple[str, ...]]:
    seen: list[tuple[str, ...]] = []

    class _Result:
        def __init__(self, body: dict) -> None:
            self.stdout = json.dumps(body)

    def run(argv, **_kwargs):
        seen.append(tuple(argv))
        if "config" in argv:
            key = argv[argv.index("get") + 1]
            return _Result({"key": key, "value": fallback and key == "retry.modelFallback"})
        provider = argv[argv.index("--provider") + 1]
        provider_windows = (windows or {}).get(provider, ("5h", "7d"))
        return _Result(
            {
                "reports": [
                    {
                        "limits": [
                            {
                                "scope": {"provider": provider},
                                "window": {"id": window},
                                "amount": {"usedFraction": used},
                            }
                            for window in provider_windows
                        ]
                    }
                ]
            }
        )

    monkeypatch.setattr(opt.subprocess, "run", run)
    return seen


def test_gateway_refuses_enabled_fallback_or_high_quota_before_any_model_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, registration = _registration(tmp_path, monkeypatch)
    plan = opt.checked_plan(run_dir, registration["plan_digest"])
    assert "enabled: false" in (run_dir / "gateway.overlay.yml").read_text()
    _fake_omp(monkeypatch, fallback=True, used=0.1)
    with pytest.raises(opt.PrerequisiteError, match="retry.modelFallback"):
        opt._gateway(run_dir, plan)
    _fake_omp(monkeypatch, fallback=False, used=0.95)
    with pytest.raises(opt.PrerequisiteError, match="quota"):
        opt._gateway(run_dir, plan)
    admission = json.loads((run_dir / "gateway-admission.json").read_text())
    assert admission["status"] == "BLOCKED_QUOTA" and admission["model_calls"] == 0


def test_admitted_gateway_inherits_only_the_planned_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, registration = _registration(tmp_path, monkeypatch)
    plan = opt.checked_plan(run_dir, registration["plan_digest"])
    monkeypatch.delenv("PI_CONFIG_FILES", raising=False)
    seen = _fake_omp(monkeypatch, fallback=False, used=0.2)
    transport = opt._gateway(run_dir, plan)
    expected = f"PI_CONFIG_FILES={run_dir / 'gateway.overlay.yml'}"
    assert all(argv[:2] == ("/usr/bin/env", expected) for argv in seen)
    assert transport._argv == ("/usr/bin/env", expected, "omp", "auth-gateway", "stdio")
    assert transport._process is None  # admission alone sends no model request


def test_quota_admission_uses_each_providers_own_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, registration = _registration(tmp_path, monkeypatch)
    plan = opt.checked_plan(run_dir, registration["plan_digest"])
    assert plan["reflection_model"].startswith("openai-codex/")
    # Codex reports only a weekly window: that alone admits it.
    _fake_omp(monkeypatch, fallback=False, used=0.2, windows={"openai-codex": ("7d",)})
    opt._gateway(run_dir, plan)
    (run_dir / "gateway-admission.json").unlink()
    # Anthropic still needs its 5-hour window; a missing reading closes admission.
    _fake_omp(monkeypatch, fallback=False, used=0.2, windows={"anthropic": ("7d",)})
    with pytest.raises(opt.PrerequisiteError, match="anthropic numeric"):
        opt._gateway(run_dir, plan)


def test_guidance_keeps_original_schema_safety_and_citation_rules() -> None:
    baseline = "Defensive review only. Cite verbatim evidence. Return the fixed JSON schema."
    assert opt.with_guidance(baseline, "") == baseline
    candidate = opt.with_guidance(
        baseline, "Check the stated contract before drawing a conclusion."
    )
    assert candidate.startswith(baseline)
    assert "does not override the rules above" in candidate


@pytest.fixture
def facet() -> ModuleType:
    # Actual retained helper imports, no admit(), plan(), old scores or models.
    path = Path(__file__).resolve().parents[1] / opt.TRACE_TOOL
    return opt._module(path, opt.sha(path.read_bytes()))


def _window_messages() -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "Frozen defensive reader."},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "flagged_step": 7,
                    "actions": [
                        {"step": 6, "text": "Validation summary is available."},
                        {"step": 7, "text": "Validation summary: expected one record, found zero."},
                    ],
                }
            ),
        },
    ]


def test_actual_trace_scorer_requires_flagged_step_verbatim_citation(facet: ModuleType) -> None:
    correct = {
        "label": "defect-fail",
        "mechanism": "The reported record count contradicts the contract.",
        "evidence": [{"step": 7, "quote": "expected one record, found zero"}],
    }
    result = opt.trace_prediction(_window_messages(), json.dumps(correct), facet)
    assert result["confirmed"] is True and result["citation_valid"] is True
    correct["evidence"] = [{"step": 6, "quote": "Validation summary is available."}]
    wrong_step = opt.trace_prediction(_window_messages(), json.dumps(correct), facet)
    assert wrong_step["confirmed"] is False
    assert wrong_step["citation_valid"] is False
    assert wrong_step["reason"] == "flagged_step_not_cited"
    correct["evidence"] = [{"step": 7, "quote": "a quotation absent from the window"}]
    invented = opt.trace_prediction(_window_messages(), json.dumps(correct), facet)
    assert invented["confirmed"] is False and invented["reason"] == "quote_not_verbatim"


def test_trace_malformed_is_unknown_not_clear(facet: ModuleType) -> None:
    malformed = opt.trace_prediction(_window_messages(), "No JSON answer", facet)
    negative = opt.trace_prediction(
        _window_messages(), '{"label":"neither","mechanism":null,"evidence":[]}', facet
    )
    assert malformed["complete"] is False and malformed["confirmed"] is None
    assert negative["complete"] is True and negative["confirmed"] is False
    with pytest.raises(opt.PrerequisiteError, match="selected trace window is unmeasured"):
        opt.trace_measurement([_window_messages()], [], positive=False, facet=facet)


def test_strict_key_objective_uses_new_complete_one_to_one_scores() -> None:
    measured = {
        "complete": True,
        "key_count": 3,
        "gates": {opt.GATE: {"eligible_findings": 2, "matched_keys": 2}},
    }
    assert opt.task_objective(measured) == pytest.approx(0.8)
    measured["gates"][opt.GATE] = {"eligible_findings": 0, "matched_keys": 0}
    assert opt.task_objective(measured) == 0.0  # explicit measured empty output, not missing
    with pytest.raises(opt.PrerequisiteError, match="missing/invalid"):
        opt.task_objective({**measured, "complete": False})
    with pytest.raises(opt.PrerequisiteError, match="not negative"):
        opt.task_objective({**measured, "key_count": 0})


def test_tiny_dev_improvement_and_missing_denominators_cannot_promote() -> None:
    baseline = {"complete": True, "recall": 0.2, "key_backed_precision": 0.5}
    candidate = {"complete": True, "recall": 0.4, "key_backed_precision": 0.75}
    decision = opt.diagnostic_decision(baseline, candidate, prompt="taskqa", changed=True)
    assert decision["decision"] == "UNMEASURED"
    assert any("unfair-fail" in reason for reason in decision["reasons"])
    assert (
        opt.diagnostic_decision(
            baseline, {**candidate, "complete": False}, prompt="taskqa", changed=True
        )["decision"]
        == "UNMEASURED"
    )
    assert (
        opt.diagnostic_decision(baseline, baseline, prompt="taskqa", changed=True)["decision"]
        == "NOT_PROMOTED"
    )
    assert (
        opt.diagnostic_decision(baseline, candidate, prompt="taskqa", changed=False)["decision"]
        == "NOT_PROMOTED"
    )


def test_trace_citation_regression_is_rejected_despite_recall_gain() -> None:
    baseline = {"complete": True, "recall": 0.2, "citation_invalid": 0}
    candidate = {"complete": True, "recall": 0.6, "citation_invalid": 1}
    decision = opt.diagnostic_decision(baseline, candidate, prompt="trace", changed=True)
    assert decision == {
        "decision": "NOT_PROMOTED",
        "reasons": ["DEV evidence/citation validity regressed"],
    }
