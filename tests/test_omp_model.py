"""Subscription transport identity, data-loss and concurrency boundaries (offline)."""

import asyncio
import fcntl
import importlib.util
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab.readers import omp_model


def end_event():
    return {
        "type": "agent_end",
        "messages": [{
            "role": "assistant",
            "provider": "kimi-code",
            "model": "k3.1-pro-highspeed",
            "stopReason": "stop",
            "responseId": "response-fixture",
            "content": [
                {"type": "thinking", "thinking": "private reasoning is not the answer"},
                {"type": "text", "text": '{"score":7,'},
                {"type": "text", "text": '"reason":"fixture","quotes":[]}'},
            ],
            "usage": {"input": 10, "output": 5, "cacheRead": 20, "cacheWrite": 3, "reasoningTokens": 2},
        }],
    }


def stream(event=None):
    return "diagnostic\n[]\n" + json.dumps(end_event() if event is None else event) + "\n"


def test_final_text_excludes_reasoning_and_preserves_native_usage():
    reply = omp_model.parse_omp_stream(stream())
    assert json.loads(reply.text)["score"] == 7
    assert "private reasoning" not in reply.text
    assert reply.served_model == omp_model.KIMI_MODEL
    assert reply.response_id == "response-fixture"
    assert (reply.input_tokens, reply.output_tokens, reply.cache_read_tokens) == (10, 5, 20)
    assert (reply.cache_write_tokens, reply.reasoning_tokens, reply.total_tokens) == (3, 2, 38)


@pytest.mark.parametrize("event", [
    {"type": "model_changed"},
    {"type": "model_fallback"},
    {"type": "FallbackAttempt"},
    {"type": "session", "resolvedModelIsFallback": True},
])
def test_any_fallback_event_rejects_even_matching_final_model(event):
    with pytest.raises(omp_model.OmpRouteError, match="fallback or change"):
        omp_model.parse_omp_stream(json.dumps(event) + "\n" + stream())


@pytest.mark.parametrize("field,value", [
    ("provider", "paid-provider"),
    ("model", "different-model"),
    ("model", None),
])
def test_identity_must_be_explicit_and_exact(field, value):
    event = end_event()
    event["messages"][0][field] = value
    with pytest.raises(omp_model.OmpRouteError, match="served-model mismatch"):
        omp_model.parse_omp_stream(stream(event))


@pytest.mark.parametrize("field,value", [
    ("stopReason", "error"),
    ("stopReason", "length"),
    ("stopReason", "aborted"),
    ("refusal", "policy"),
    ("errorMessage", "cyber_policy"),
    ("content", [{"type": "refusal", "refusal": "no"}]),
    ("content", [{"type": "toolCall", "name": "bash"}]),
    ("content", [{"type": "text", "text": "I cannot assist with that request."}]),
    ("content", [{"type": "text", "text": "I'm sorry, I can't help with that."}]),
    ("content", [{"type": "text", "text": "I’m sorry, but I can’t assist with that request."}]),
    ("content", [{"type": "text", "text": "Sorry, but I cannot provide that."}]),
    ("content", [{"type": "text", "text": ""}]),
    ("content", [{"type": "thinking", "thinking": "only reasoning"}]),
    ("content", [{"type": "text", "text": None}]),
])
def test_refusal_incomplete_and_nontext_responses_are_not_completions(field, value):
    event = end_event()
    event["messages"][0][field] = value
    with pytest.raises(omp_model.OmpRouteError):
        omp_model.parse_omp_stream(stream(event))


@pytest.mark.parametrize("stdout", ["", "not json\n", '{"type":"agent_end"}', stream() + stream()])
def test_truncated_or_ambiguous_stream_fails_closed(stdout):
    with pytest.raises(omp_model.OmpRouteError):
        omp_model.parse_omp_stream(stdout)


@pytest.mark.parametrize("messages", [[], [end_event()["messages"][0]] * 2])
def test_exactly_one_final_assistant_turn(messages):
    with pytest.raises(omp_model.OmpRouteError, match="exactly one"):
        omp_model.parse_omp_stream(stream({"type": "agent_end", "messages": messages}))


@pytest.mark.parametrize("usage", [None, {}, {"input": True, "output": 5}, {"input": 1, "output": -1}])
def test_usage_cannot_be_missing_or_invented(usage):
    event = end_event()
    event["messages"][0]["usage"] = usage
    with pytest.raises(omp_model.OmpRouteError, match="usage"):
        omp_model.parse_omp_stream(stream(event))


def test_environment_keeps_oauth_home_but_removes_inherited_credentials():
    env = {
        "HOME": "/fixture/home", "PATH": "/bin", "OMP_PROFILE": "fixture",
        "OPENAI_API_KEY": "not-a-real-secret", "API_KEY": "fixture",
        "ANTHROPIC_OAUTH_TOKEN": "fixture", "KIMI_AUTH_TOKEN": "fixture",
        "CURSOR_ACCESS_TOKEN": "fixture", "A_KEYWORD": "not a credential",
    }
    assert omp_model.subscription_env(env) == {
        "HOME": "/fixture/home", "PATH": "/bin", "OMP_PROFILE": "fixture",
        "A_KEYWORD": "not a credential",
    }
    assert "OPENAI_API_KEY" in env  # Never mutate the parent environment.


def test_render_single_user_preserves_full_text_and_separates_system():
    text = "a" * 200_000
    assert omp_model.render_prompt([
        {"role": "system", "content": "first"},
        {"role": "system", "content": [{"type": "text", "text": "second"}]},
        {"role": "user", "content": text},
    ]) == ("first\n\nsecond", text)


def test_render_multiturn_keeps_role_boundaries():
    assert omp_model.render_prompt([
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "followup"},
    ]) == ("", "[user]\nquestion\n\n[assistant]\nanswer\n\n[user]\nfollowup")


@pytest.mark.parametrize("messages", [
    [], [{"role": "tool", "content": "result"}],
    [{"role": "user", "content": [{"type": "image", "image": "fixture"}]}],
    [{"role": "user", "content": None}],
    [{"role": "assistant", "content": "", "tool_calls": [{"function": "bash"}]}],
    [{"role": "user", "content": "x"}, {"role": "system", "content": "late"}],
])
def test_render_rejects_inputs_that_cannot_be_preserved(messages):
    with pytest.raises(ValueError):
        omp_model.render_prompt(messages)


def test_transport_uses_isolated_cm9_flags_and_writes_only_attestation(monkeypatch, tmp_path):
    def run(argv, **kwargs):
        assert argv[:5] == ["omp", "-p", "--mode", "json", "--no-session"]
        for flag in ("--no-tools", "--no-lsp", "--no-extensions", "--no-skills", "--no-rules", "--no-title", "--no-pty"):
            assert flag in argv
        assert argv[argv.index("--model") + 1] == omp_model.KIMI_MODEL
        assert argv[argv.index("--system-prompt") + 1] == "scanner system"
        overlay = Path(argv[argv.index("--config") + 1])
        assert overlay.read_text() == omp_model.NO_FALLBACK_OVERLAY
        assert overlay.parent == Path(kwargs["cwd"])
        assert kwargs["env"] == {"HOME": "/fixture/home"}
        assert kwargs["input"] == "private transcript"
        return SimpleNamespace(returncode=0, stdout=stream())

    monkeypatch.setattr(omp_model.os, "environ", {"HOME": "/fixture/home", "ZAI_API_KEY": "fixture"})
    monkeypatch.setattr(omp_model.subprocess, "run", run)
    path = tmp_path / "calls.jsonl"
    reply = omp_model.call_omp("scanner system", "private transcript", receipt_path=path)
    receipt = json.loads(path.read_text())
    assert receipt["ok"] and receipt["served_model"] == reply.served_model
    assert receipt["usd"] == 0 and receipt["fallback"] is False
    assert "private transcript" not in path.read_text()
    assert "scanner system" not in path.read_text()


def test_failed_child_does_not_parse_success_shaped_stdout(monkeypatch, tmp_path):
    monkeypatch.setattr(omp_model.os, "environ", {})
    monkeypatch.setattr(omp_model.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=stream()))
    path = tmp_path / "calls.jsonl"
    with pytest.raises(omp_model.OmpRouteError, match="status 1"):
        omp_model.call_omp("system", "user", receipt_path=path)
    assert json.loads(path.read_text())["ok"] is False


def test_process_bound_is_shared_across_concurrent_calls(monkeypatch):
    lock = threading.Lock()
    four_started = threading.Event()
    release = threading.Event()
    active = maximum = 0

    def run(*args, **kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active == 4:
                four_started.set()
        assert release.wait(10)
        with lock:
            active -= 1
        return SimpleNamespace(returncode=0, stdout=stream())

    monkeypatch.setattr(omp_model.os, "environ", {})
    monkeypatch.setattr(omp_model.subprocess, "run", run)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(omp_model.call_omp, "system", "user") for _ in range(8)]
        try:
            assert four_started.wait(10)
            assert maximum == 4
        finally:
            release.set()
        assert len([future.result() for future in futures]) == 8
    assert maximum == 4


def test_unqualified_routes_are_rejected_before_execution():
    with pytest.raises(ValueError, match="explicit provider/model"):
        omp_model.call_omp("system", "user", model="ambiguous-model")

@pytest.mark.parametrize("halt_while_queued", [False, True])
def test_quota_halt_prevents_even_previously_queued_subprocesses(monkeypatch, tmp_path, halt_while_queued):
    halt = tmp_path / "halt.json"

    class Slot:
        def __enter__(self):
            if halt_while_queued:
                halt.write_text("{}")

        def __exit__(self, *args):
            return False

    if not halt_while_queued:
        halt.write_text("{}")
    monkeypatch.setattr(omp_model, "_PROCESS_SLOTS", Slot())
    monkeypatch.setattr(omp_model.subprocess, "run", lambda *args, **kwargs: pytest.fail("halted subprocess started"))
    receipt = tmp_path / "calls.jsonl"
    with pytest.raises(omp_model.OmpRouteError, match="halted before subprocess"):
        omp_model.call_omp("system", "user", halt_path=halt, receipt_path=receipt)
    assert not receipt.exists()  # No hosted call was made.


def test_generic_route_attestation_includes_provider_and_full_model_id():
    event = end_event()
    event["messages"][0].update(provider="xai-oauth", model="grok-4.3")
    reply = omp_model.parse_omp_stream(stream(event), "xai-oauth/grok-4.3")
    assert reply.served_model == "xai-oauth/grok-4.3"
    with pytest.raises(omp_model.OmpRouteError, match="served-model mismatch"):
        omp_model.parse_omp_stream(stream(event), "kimi-code/k3.1-pro-highspeed")



def test_inspect_provider_schema_and_usage_without_model_call(monkeypatch):
    inspect_model = pytest.importorskip("inspect_ai.model")
    captured = {}

    def call(system, prompt, **kwargs):
        captured.update(system=system, prompt=prompt, **kwargs)
        return omp_model.parse_omp_stream(stream())

    monkeypatch.setattr(omp_model, "call_omp", call)
    assert omp_model.register_omp_provider() is omp_model.register_omp_provider()
    model = inspect_model.get_model(omp_model.INSPECT_MODEL)
    result = asyncio.run(model.api.generate(
        [inspect_model.ChatMessageUser(content="unchanged scanner prompt")], [], "none",
        inspect_model.GenerateConfig(response_schema=inspect_model.ResponseSchema(
            name="RewardHacking", json_schema={"type": "object", "properties": {"score": {"type": "number"}}},
        )),
    ))
    assert captured["prompt"] == "unchanged scanner prompt"
    assert json.loads(captured["system"].split("schema:\n", 1)[1])["type"] == "object"
    assert result.completion == omp_model.parse_omp_stream(stream()).text
    assert result.usage.total_tokens == 38
    assert result.usage.total_cost == 0
    assert result.metadata["served_model"] == omp_model.KIMI_MODEL


def load_runner(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = (
        Path(__file__).resolve().parents[1]
        / "research/evidence/runs/trace-tools-tt4-20261027/run.py"
    )
    spec = importlib.util.spec_from_file_location("tt4_runner_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seeded_round_robin_keeps_prefixes_balanced_without_exposing_strata(monkeypatch, tmp_path):
    runner = load_runner(monkeypatch, tmp_path)
    sample = [
        {"trace_id": f"{stratum}-{index}", "stratum": stratum}
        for stratum, count in zip(runner.STRATA, (8, 7, 5), strict=True)
        for index in range(count)
    ]
    ordered = runner.round_robin(sample)
    assert ordered == runner.round_robin(list(reversed(sample)))
    assert ordered != runner.round_robin(sample, seed=42)
    assert set(ordered) == {row["trace_id"] for row in sample}
    assert len(ordered) == len(sample)
    strata = {row["trace_id"]: row["stratum"] for row in sample}
    for offset in range(0, 15, 3):
        assert {strata[trace] for trace in ordered[offset:offset + 3]} == set(runner.STRATA)
    assert all(isinstance(trace, str) for trace in ordered)


def test_resumption_never_reuses_other_models_threshold_rows_or_missing_scores(monkeypatch, tmp_path):
    runner = load_runner(monkeypatch, tmp_path)
    base = {"tool": "scout_metr", "detector": runner.DETECTOR, "detector_version": runner.DETECTOR_VERSION, "flag": None}
    rows = [
        dict(base, run_id="zero", value=0),
        dict(base, run_id="ten", value=10),
        dict(base, run_id="missing", value=None),
        dict(base, run_id="bool", value=True),
        dict(base, run_id="nan", value=float("nan")),
        dict(base, run_id="outside", value=11),
        dict(base, run_id="grok", value=9, detector_version="omp-xai-oauth-grok-4.3"),
        dict(base, run_id="threshold-only", value=9, detector="reward_hacking_scanner@ge7"),
    ]
    assert runner.scores(rows) == {"zero": 0, "ten": 10}


def test_spend_recovery_is_idempotent_across_partial_dual_ledger_writes(monkeypatch, tmp_path):
    runner = load_runner(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "CAMPAIGN", tmp_path / "campaign")
    monkeypatch.setattr(runner, "RECEIPTS", tmp_path / "campaign/receipts")
    monkeypatch.setattr(runner, "STATE", tmp_path / "trace-tools")
    monkeypatch.setattr(runner, "SPEND", tmp_path / "canonical/spend.jsonl")
    opaque = "run-fixture"
    unrelated = {"tool": "other-lane", "basis": {"authorization": "separate"}}
    runner.append(runner.SPEND, [unrelated])
    runner.append(runner.CAMPAIGN / f"{opaque}-calls.jsonl", [
        {"ok": True, "input_tokens": 10, "cache_read_tokens": 20, "output_tokens": 3},
        {"ok": False, "error": "fixture"},
    ])
    runner.flush_spend(opaque)
    # Simulate interruption after the canonical append but before the other
    # ledger/cursor were durable. Replaying must not append a second charge.
    (runner.CAMPAIGN / f"{opaque}-spend-cursor.json").unlink()
    (runner.STATE / "spend.jsonl").unlink()
    runner.flush_spend(opaque)
    runner.flush_spend(opaque)
    assert len(runner.jsonl(runner.SPEND)) == 2
    assert runner.jsonl(runner.SPEND)[0] == unrelated
    assert len(runner.jsonl(runner.STATE / "spend.jsonl")) == 1
    assert len(runner.jsonl(runner.RECEIPTS / "spend.jsonl")) == 1
    record = runner.jsonl(runner.SPEND)[1]
    assert (record["input_tokens"], record["output_tokens"], record["usd"]) == (30, 3, 0)


@pytest.fixture
def kimi_quota_report():
    return {"reports": [{"provider": "kimi-code", "limits": [
        {
            "label": name, "status": "ok",
            "amount": {"usedFraction": 0.2, "remainingFraction": 0.8},
            "window": {"resetsAt": (index + 1) * 1000},
        }
        for index, name in enumerate(("5h", "Monthly total", "Monthly code"))
    ]}]}


@pytest.mark.parametrize("window_index", [0, 1, 2])
@pytest.mark.parametrize("used,available", [(0.899999, True), (0.9, False), (1, False)])
def test_quota_reserves_coding_headroom_in_every_window(
    monkeypatch, tmp_path, kimi_quota_report, window_index, used, available,
):
    runner = load_runner(monkeypatch, tmp_path)
    limit = kimi_quota_report["reports"][0]["limits"][window_index]
    limit["amount"] = {"usedFraction": used, "remainingFraction": 1 - used}
    calls = []

    def usage(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=json.dumps(kimi_quota_report))

    monkeypatch.setattr(runner.subprocess, "run", usage)
    result = runner.quota()
    assert result["available"] is available
    assert result["stop_used_fraction"] == 0.9
    assert len(result["reset_at"]) == (not available)
    assert calls == [
        ["omp", "usage", "invalidate", "--provider", "kimi-code"],
        ["omp", "usage", "--json", "--provider", "kimi-code", "--redact"],
    ]


@pytest.mark.parametrize("field", ["usedFraction", "remainingFraction"])
@pytest.mark.parametrize("value", [None, True, "0.1", float("nan"), float("inf"), -0.1, 1.1])
def test_quota_unknown_amount_is_fail_closed(
    monkeypatch, tmp_path, kimi_quota_report, field, value,
):
    runner = load_runner(monkeypatch, tmp_path)
    kimi_quota_report["reports"][0]["limits"][0]["amount"][field] = value
    monkeypatch.setattr(
        runner.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(kimi_quota_report)),
    )
    with pytest.raises(RuntimeError, match="unknown subscription allowance"):
        runner.quota()


@pytest.mark.parametrize("status", [None, "unknown"])
def test_quota_unknown_status_is_fail_closed(monkeypatch, tmp_path, kimi_quota_report, status):
    runner = load_runner(monkeypatch, tmp_path)
    kimi_quota_report["reports"][0]["limits"][0]["status"] = status
    monkeypatch.setattr(
        runner.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(kimi_quota_report)),
    )
    with pytest.raises(RuntimeError, match="unknown subscription allowance"):
        runner.quota()


BUDGET_PATH = (
    Path(__file__).resolve().parents[1]
    / "research/evidence/runs/trace-tools-tt4-20261027/openrouter_budget.py"
)
BUDGET_NOW = datetime(2026, 10, 28, 16, 0, 0, tzinfo=UTC)
MISSING = object()
BUDGET_WORKER = """
import importlib.util, json, sys
from datetime import UTC, datetime
path, state, worker, count = sys.argv[1:5]
spec = importlib.util.spec_from_file_location("budget_worker", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
now = datetime(2026, 10, 28, 16, 0, 0, tzinfo=UTC)
cash = lambda: {"available_usd": "1000.00", "fetched_at": now.isoformat(), "source": "fixture-credits"}
budget = module.Budget(state, "tt4", clock=lambda: now)
codes = []
for index in range(int(count)):
    try:
        budget.reserve(f"proc-{worker}-{index}", "1.00", cash)
        codes.append("ok")
    except module.BudgetError as error:
        codes.append(error.code)
print(json.dumps(codes))
"""


@pytest.fixture
def budget_module():
    spec = importlib.util.spec_from_file_location("tt4_openrouter_budget_fixture", BUDGET_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def budget_dir(tmp_path):
    return tmp_path / "openrouter-budget"


@pytest.fixture
def make_budget(budget_module, budget_dir):
    def make(arm="tt4", now=BUDGET_NOW):
        return budget_module.Budget(budget_dir, arm, clock=lambda: now)

    return make


def cash_reading(**changes):
    reading = {
        "available_usd": "1000.00",
        "fetched_at": BUDGET_NOW.isoformat(),
        "source": "fixture-credits",
    }
    for key, value in changes.items():
        if value is MISSING:
            del reading[key]
        else:
            reading[key] = value
    return reading


def budget_cash(amount, *, age=0.0):
    def fetch():
        fetch.calls += 1
        return cash_reading(
            available_usd=amount, fetched_at=(BUDGET_NOW - timedelta(seconds=age)).isoformat()
        )

    fetch.calls = 0
    return fetch


def refused(module, code, call, *args, **kwargs):
    with pytest.raises(module.BudgetError) as caught:
        call(*args, **kwargs)
    assert caught.value.code == code


def usd(text):
    return Decimal(text)


def value_id(value):
    return f"{type(value).__name__}:{str(value)[:16]}"


def test_cash_is_fetched_under_the_state_lock_and_the_hold_is_durable_before_return(
    budget_module,
    make_budget,
    budget_dir,
):
    seen = []

    def fetch():
        descriptor = os.open(budget_dir / "state.lock", os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            seen.append("locked")
        else:
            seen.append("unlocked")
        finally:
            os.close(descriptor)
        return cash_reading()

    reservation = make_budget().reserve("durable-1", "0.11", fetch, {"stage": "smoke"})
    assert seen == ["locked"]
    assert reservation.bound_usd == usd("0.11")
    assert reservation.required_cash_usd == usd("35.11")  # bound + unspent TT7 earmark
    assert json.loads(json.dumps(reservation.to_dict()))["attempt_id"] == "durable-1"
    snapshot = make_budget().snapshot()  # a new process-equivalent view, no shared memory
    assert [(a["attempt_id"], a["status"], a["charge_usd"]) for a in snapshot["attempts"]] == [
        ("durable-1", "pending", "0.11"),
    ]
    assert set(snapshot["attempts"][0]) == {
        "attempt_id",
        "arm",
        "status",
        "bound_usd",
        "actual_usd",
        "charge_usd",
        "created_at",
        "updated_at",
    }
    assert "stage" not in json.dumps(snapshot, allow_nan=False)
    state = json.loads((budget_dir / "state.json").read_text())
    assert state["attempts"]["durable-1"]["metadata"] == {"stage": "smoke"}


def test_state_replacement_is_fsync_then_replace_then_directory_fsync(
    budget_module,
    make_budget,
    budget_dir,
    monkeypatch,
):
    calls = []
    real_fsync, real_replace = os.fsync, os.replace
    lock_path = budget_dir / "state.lock"
    markers_at_replace = []

    def fsync(descriptor):
        is_lock = os.fstat(descriptor).st_ino == lock_path.stat().st_ino
        calls.append("lock_fsync" if is_lock else "fsync")
        if is_lock:
            assert (budget_dir / "state.json").exists()
            assert lock_path.read_bytes()
        real_fsync(descriptor)

    def replace(source, destination):
        calls.append("replace")
        markers_at_replace.append(lock_path.read_bytes())
        real_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(budget_module.os, "fsync", fsync)
        patch.setattr(budget_module.os, "replace", replace)
        make_budget().reserve("order-1", "0.11", budget_cash("1000.00"))
        assert calls == ["fsync", "replace", "fsync", "lock_fsync"]
        marker = lock_path.read_bytes()
        assert markers_at_replace == [b""]  # no initialized marker before the first commit
        calls.clear()
        make_budget().reserve("order-2", "0.11", budget_cash("1000.00"))
        assert calls == ["fsync", "replace", "fsync", "lock_fsync"]
        assert markers_at_replace == [b"", marker]
        assert lock_path.read_bytes() == marker  # never append or replace the stable marker


def test_failed_state_write_returns_no_reservation_and_consumes_no_attempt_id(
    budget_module,
    make_budget,
    budget_dir,
    monkeypatch,
):
    def boom(*args):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(budget_module.os, "replace", boom)
        refused(
            budget_module,
            "state_write_failed",
            make_budget().reserve,
            "write-1",
            "0.11",
            budget_cash("1000.00"),
        )
    assert not (budget_dir / "state.json").exists()
    assert (budget_dir / "state.lock").read_bytes() == b""
    assert list(budget_dir.glob("*.tmp")) == []
    assert make_budget().reserve("write-1", "0.11", budget_cash("1000.00")).attempt_id == "write-1"


def test_initialized_lock_refuses_deleted_state_for_every_operation(
    budget_module,
    make_budget,
    budget_dir,
):
    cash = budget_cash("1000.00")
    make_budget().reserve("deleted-state-1", "0.11", cash)
    lock_path = budget_dir / "state.lock"
    lock_inode = lock_path.stat().st_ino
    marker = lock_path.read_bytes()
    assert marker
    (budget_dir / "state.json").unlink()
    for arm in ("tt4", "tt7"):
        budget = make_budget(arm)  # no instance-local memory of the original reservation
        for call, args in (
            (budget.snapshot, ()),
            (budget.reserve, ("deleted-state-2", "0.11", cash)),
            (budget.settle, ("deleted-state-1", "0.01", "generation:deleted-state")),
            (budget.fail, ("deleted-state-1", "unknown outcome")),
            (budget.close_arm, ("stop after missing state",)),
        ):
            refused(budget_module, "missing_state", call, *args)
            assert not (budget_dir / "state.json").exists()
    assert cash.calls == 1  # never fetch cash or make a new dispatch reservation
    assert lock_path.stat().st_ino == lock_inode
    assert lock_path.read_bytes() == marker


@pytest.mark.parametrize("preexisting_lock", [False, True])
def test_blank_lock_remains_uninitialized_until_the_first_successful_state_commit(
    budget_module,
    make_budget,
    budget_dir,
    preexisting_lock,
):
    lock_path = budget_dir / "state.lock"
    if preexisting_lock:
        budget_dir.mkdir()
        lock_path.write_bytes(b"")
    refused(
        budget_module,
        "cash_low",
        make_budget().reserve,
        "first-success",
        "0.11",
        budget_cash("35.10"),
    )
    lock_inode = lock_path.stat().st_ino
    assert lock_path.read_bytes() == b""
    assert not (budget_dir / "state.json").exists()
    for arm in ("tt4", "tt7"):
        snapshot = make_budget(arm).snapshot()
        assert snapshot["attempts"] == []
        assert usd(snapshot["arms"]["tt4"]["used_usd"]) == 0
        assert lock_path.read_bytes() == b""
        assert not (budget_dir / "state.json").exists()
    reservation = make_budget().reserve("first-success", "0.11", budget_cash("1000.00"))
    assert reservation.attempt_id == "first-success"
    assert lock_path.read_bytes()
    assert lock_path.stat().st_ino == lock_inode
    assert make_budget().snapshot()["attempts"][0]["status"] == "pending"


def test_threaded_reservations_never_exceed_the_arm_cap(budget_module, budget_dir):
    def reserve(index):
        budget = budget_module.Budget(budget_dir, "tt4", clock=lambda: BUDGET_NOW)
        try:
            budget.reserve(f"thread-{index}", "1.00", budget_cash("1000.00"))
        except budget_module.BudgetError as error:
            return error.code
        return "ok"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(reserve, range(24)))
    assert outcomes.count("ok") == 14
    assert outcomes.count("arm_cap") == 10
    arm = budget_module.Budget(budget_dir, "tt4", clock=lambda: BUDGET_NOW).snapshot()["arms"][
        "tt4"
    ]
    assert usd(arm["used_usd"]) == usd("14.00") and arm["attempts"]["pending"] == 14


def test_separate_processes_share_one_cap(budget_module, budget_dir):
    workers = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                BUDGET_WORKER,
                str(BUDGET_PATH),
                str(budget_dir),
                str(number),
                "5",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for number in range(4)
    ]
    codes = []
    for worker in workers:
        stdout, stderr = worker.communicate(timeout=120)
        assert worker.returncode == 0, stderr
        codes.extend(json.loads(stdout))
    assert codes.count("ok") == 14
    assert codes.count("arm_cap") == 6
    snapshot = budget_module.Budget(budget_dir, "tt4", clock=lambda: BUDGET_NOW).snapshot()
    assert usd(snapshot["arms"]["tt4"]["used_usd"]) == usd("14.00")
    assert len({attempt["attempt_id"] for attempt in snapshot["attempts"]}) == 14


def test_pending_and_failed_attempts_retain_their_whole_bound(budget_module, make_budget):
    budget = make_budget()
    cash = budget_cash("1000.00")
    budget.reserve("hold-pending", "0.11", cash)
    budget.reserve("hold-failed", "0.11", cash)
    failed = budget.fail("hold-failed", RuntimeError("HTTP 504 after the request was sent"))
    assert failed["status"] == "failed" and usd(failed["retained_usd"]) == usd("0.11")
    arm = budget.snapshot()["arms"]["tt4"]
    assert (
        usd(arm["held_usd"]) == usd("0.22")
        and usd(arm["settled_usd"]) == 0
        and usd(arm["used_usd"]) == usd("0.22")
    )
    assert arm["attempts"] == {"pending": 1, "settled": 0, "failed": 1}
    refused(
        budget_module, "bad_transition", budget.settle, "hold-failed", "0.01", "generation:gen-1"
    )
    refused(budget_module, "bad_transition", budget.fail, "hold-failed", "again")
    refused(budget_module, "duplicate_attempt", budget.reserve, "hold-failed", "0.11", cash)
    assert usd(make_budget().snapshot()["arms"]["tt4"]["used_usd"]) == usd("0.22")


def test_verified_actual_cost_releases_only_the_unused_remainder(budget_module, make_budget):
    budget = make_budget()
    cash = budget_cash("1000.00")
    budget.reserve("whole-cap", "14.85", cash)
    refused(budget_module, "arm_cap", budget.reserve, "no-room", "0.01", cash)
    settled = budget.settle(
        "whole-cap", "14.00", {"generation_id": "gen-1", "usage": {"cost": 14.0}}
    )
    assert usd(settled["released_usd"]) == usd("0.85")
    assert usd(settled["arm_used_usd"]) == usd("14.00") and usd(
        settled["arm_remaining_usd"]
    ) == usd("0.85")
    assert budget.reserve("room", "0.85", cash).arm_remaining_usd == 0
    refused(budget_module, "arm_cap", budget.reserve, "too-much", "0.01", cash)
    budget_free = make_budget()
    refused(budget_module, "arm_cap", budget_free.reserve, "still-full", "0.01", cash)


def test_zero_provider_cost_is_a_valid_settlement(make_budget):
    budget = make_budget()
    budget.reserve("free-1", "0.11", budget_cash("1000.00"))
    settled = budget.settle("free-1", 0, "generation:gen-free")
    assert usd(settled["released_usd"]) == usd("0.11") and usd(settled["arm_used_usd"]) == 0


@pytest.mark.parametrize(
    "proof", ["", "   ", {}, None, 0, ["generation"], b"generation"], ids=value_id
)
def test_settlement_requires_nonempty_provider_proof(budget_module, make_budget, proof):
    budget = make_budget()
    budget.reserve("proof-1", "0.11", budget_cash("1000.00"))
    refused(budget_module, "invalid_proof", budget.settle, "proof-1", "0.05", proof)
    assert budget.snapshot()["attempts"][0]["status"] == "pending"


def test_over_bound_cost_fails_closed_without_releasing_the_hold(
    budget_module, make_budget, budget_dir
):
    budget = make_budget()
    budget.reserve("over-1", "0.11", budget_cash("1000.00"))
    refused(budget_module, "over_bound", budget.settle, "over-1", "0.12", "generation:gen-2")
    snapshot = budget.snapshot()
    attempt = snapshot["attempts"][0]
    assert (attempt["status"], attempt["actual_usd"], usd(attempt["charge_usd"])) == (
        "pending",
        None,
        usd("0.12"),
    )
    assert usd(snapshot["arms"]["tt4"]["used_usd"]) == usd("0.12")
    refused(budget_module, "bad_transition", budget.settle, "over-1", "0.01", "generation:gen-2")
    failed = budget.fail("over-1", "billed above the reservation")
    assert usd(failed["retained_usd"]) == usd("0.12")
    assert usd(budget.snapshot()["arms"]["tt4"]["used_usd"]) == usd("0.12")
    events = json.loads((budget_dir / "state.json").read_text())["attempts"]["over-1"]["history"]
    assert [event["event"] for event in events] == [
        "reserved",
        "settle_rejected_over_bound",
        "failed",
    ]


def test_duplicate_attempts_are_always_refused_without_fetching_cash(budget_module, make_budget):
    budget = make_budget()
    cash = budget_cash("1000.00")
    budget.reserve("dup-pending", "0.11", cash)
    budget.reserve("dup-settled", "0.11", cash)
    budget.settle("dup-settled", "0.05", "generation:gen-3")
    budget.reserve("dup-failed", "0.11", cash)
    budget.fail("dup-failed", "timeout")
    assert cash.calls == 3
    for attempt in ("dup-pending", "dup-settled", "dup-failed"):
        refused(budget_module, "duplicate_attempt", budget.reserve, attempt, "0.11", cash)
        refused(budget_module, "duplicate_attempt", make_budget().reserve, attempt, "0.01", cash)
    assert cash.calls == 3
    refused(
        budget_module, "bad_transition", budget.settle, "dup-settled", "0.05", "generation:gen-3"
    )
    refused(budget_module, "bad_transition", budget.fail, "dup-settled", "late error")
    assert len(budget.snapshot()["attempts"]) == 3


def test_settle_and_fail_are_scoped_to_known_attempts_of_the_same_arm(budget_module, make_budget):
    budget = make_budget()
    budget.reserve("scoped-1", "0.11", budget_cash("1000.00"))
    refused(
        budget_module, "unknown_attempt", budget.settle, "never-reserved", "0.01", "generation:g"
    )
    refused(budget_module, "unknown_attempt", budget.fail, "never-reserved", "error")
    refused(
        budget_module, "wrong_arm", make_budget("tt7").settle, "scoped-1", "0.01", "generation:g"
    )
    refused(budget_module, "wrong_arm", make_budget("tt7").fail, "scoped-1", "error")
    assert budget.snapshot()["attempts"][0]["status"] == "pending"


@pytest.mark.parametrize(
    "age,code",
    [
        (0.0, None),
        (15.0, None),
        (-2.0, None),
        (15.001, "cash_stale"),
        (3600.0, "cash_stale"),
        (-2.001, "cash_future"),
        (-3600.0, "cash_future"),
    ],
)
def test_cash_must_be_at_most_15s_old_and_not_more_than_2s_in_the_future(
    budget_module,
    make_budget,
    budget_dir,
    age,
    code,
):
    budget = make_budget()
    cash = budget_cash("1000.00", age=age)
    if code is None:
        assert budget.reserve("age-1", "0.11", cash).attempt_id == "age-1"
    else:
        refused(budget_module, code, budget.reserve, "age-1", "0.11", cash)
        assert not (budget_dir / "state.json").exists()


@pytest.mark.parametrize(
    "reading",
    [
        "not-a-mapping",
        None,
        [],
        cash_reading(available_usd=1000.0),
        cash_reading(available_usd=Decimal("1000")),
        cash_reading(available_usd=True),
        cash_reading(available_usd=None),
        cash_reading(available_usd="-1"),
        cash_reading(available_usd="nan"),
        cash_reading(available_usd="inf"),
        cash_reading(available_usd="1e3"),
        cash_reading(available_usd=MISSING),
        cash_reading(fetched_at="2026-10-28T16:00:00"),
        cash_reading(fetched_at="2026-10-28T17:00:00+01:00"),
        cash_reading(fetched_at="yesterday"),
        cash_reading(fetched_at=1.0),
        cash_reading(fetched_at=MISSING),
        cash_reading(source=""),
        cash_reading(source="   "),
        cash_reading(source=None),
        cash_reading(source=MISSING),
    ],
    ids=lambda reading: str(reading)[:48],
)
def test_malformed_cash_readings_fail_closed(budget_module, make_budget, budget_dir, reading):
    refused(
        budget_module, "cash_invalid", make_budget().reserve, "bad-cash", "0.11", lambda: reading
    )
    assert not (budget_dir / "state.json").exists()


def test_cash_fetch_failure_reserves_nothing(budget_module, make_budget, budget_dir):
    def boom():
        raise TimeoutError("credits endpoint timed out")

    refused(budget_module, "cash_unavailable", make_budget().reserve, "boom-1", "0.11", boom)
    assert not (budget_dir / "state.json").exists()


def test_tt7_is_reserved_but_denied_and_nothing_can_approve_it(
    budget_module, make_budget, budget_dir
):
    cash = budget_cash("1000.00")
    refused(budget_module, "arm_not_approved", make_budget("tt7").reserve, "tt7-1", "1.00", cash)
    assert cash.calls == 0 and not (budget_dir / "state.json").exists()
    snapshot = make_budget("tt7").snapshot()
    assert snapshot["caps"] == {"tt4": "14.85", "tt7": "35.00"}
    assert snapshot["arms"]["tt4"]["dispatch_approved"] is True
    assert snapshot["arms"]["tt7"]["dispatch_approved"] is False
    assert snapshot["attempts"] == []
    assert {name for name in vars(budget_module.Budget) if not name.startswith("_")} == {
        "reserve",
        "settle",
        "fail",
        "snapshot",
        "close_arm",
    }
    for constants in (budget_module.CAPS, budget_module.DISPATCH_APPROVED):
        with pytest.raises(TypeError):
            constants["tt7"] = "35.00"
    for arm in ("tt5", None, 4):
        refused(budget_module, "unknown_arm", budget_module.Budget, budget_dir, arm)


def edit_budget_state(budget_module, budget_dir, edit, reseal):
    path = budget_dir / "state.json"
    state = json.loads(path.read_text())
    edit(state)
    if reseal:
        state["integrity"] = {"sha256": budget_module.state_digest(state)}
    path.write_text(json.dumps(state))


def raise_both_caps(state):
    state["caps"]["tt4"] = "99.00"
    state["arms"]["tt4"]["cap_usd"] = "99.00"


def raise_arm_cap(state):
    state["arms"]["tt4"]["cap_usd"] = "99.00"


def approve_tt7(state):
    state["arms"]["tt7"]["dispatch_approved"] = True


def lower_bound(state):
    state["attempts"]["tamper-1"]["bound_usd"] = "0.01"


def drop_attempt(state):
    del state["attempts"]["tamper-1"]


def forge_settlement(state):
    state["attempts"]["tamper-1"].update(status="settled", actual_usd="0")


def drop_history(state):
    state["attempts"]["tamper-1"]["history"] = []


@pytest.mark.parametrize(
    "edit,reseal,code",
    [
        (raise_both_caps, True, "tampered_caps"),
        (raise_both_caps, False, "tampered_caps"),
        (raise_arm_cap, True, "tampered_caps"),
        (approve_tt7, True, "tampered_approval"),
        (lower_bound, False, "tampered_state"),
        (drop_attempt, False, "tampered_state"),
        (forge_settlement, True, "tampered_state"),
        (drop_history, True, "tampered_state"),
    ],
    ids=lambda value: getattr(value, "__name__", str(value)),
)
def test_tampered_state_fails_every_operation_closed_and_is_never_repaired(
    budget_module,
    make_budget,
    budget_dir,
    edit,
    reseal,
    code,
):
    budget = make_budget()
    cash = budget_cash("1000.00")
    budget.reserve("tamper-1", "0.11", cash)
    edit_budget_state(budget_module, budget_dir, edit, reseal)
    tampered = (budget_dir / "state.json").read_bytes()
    fetches = cash.calls
    for call, args in (
        (budget.snapshot, ()),
        (make_budget("tt7").snapshot, ()),
        (budget.reserve, ("tamper-2", "0.11", cash)),
        (budget.settle, ("tamper-1", "0.01", "generation:g")),
        (budget.fail, ("tamper-1", "error")),
        (budget.close_arm, ("stop",)),
    ):
        refused(budget_module, code, call, *args)
    assert cash.calls == fetches
    assert (budget_dir / "state.json").read_bytes() == tampered


@pytest.mark.parametrize("text", ["{not json", '{"seq": NaN}', "[]", ""])
def test_corrupt_state_files_fail_closed(budget_module, make_budget, budget_dir, text):
    budget_dir.mkdir()
    (budget_dir / "state.json").write_text(text)
    refused(budget_module, "tampered_state", make_budget().snapshot)
    refused(
        budget_module,
        "tampered_state",
        make_budget().reserve,
        "corrupt-1",
        "0.11",
        budget_cash("1000.00"),
    )
    assert (budget_dir / "state.json").read_text() == text


def test_low_cash_and_other_open_arm_earmark_block_reservations(
    budget_module, make_budget, budget_dir
):
    budget = make_budget()
    # TT4 must leave TT7's whole unspent 35.00 earmark untouched: 0.11 + 35.00.
    refused(budget_module, "cash_low", budget.reserve, "low-1", "0.11", budget_cash("35.10"))
    assert not (budget_dir / "state.json").exists()
    assert budget.reserve("low-1", "0.11", budget_cash("35.11")).required_cash_usd == usd("35.11")
    # A refusal never consumed the id; an existing pending hold is subtracted from cash too.
    refused(budget_module, "cash_low", budget.reserve, "low-2", "0.11", budget_cash("35.21"))
    assert budget.reserve("low-2", "0.11", budget_cash("35.22")).required_cash_usd == usd("35.22")
    budget.fail("low-2", "timeout")  # a failed hold keeps counting against cash
    refused(budget_module, "cash_low", budget.reserve, "low-3", "0.11", budget_cash("35.32"))
    assert budget.reserve("low-3", "0.11", budget_cash("35.33")).required_cash_usd == usd("35.33")


def test_settled_debits_stay_subtracted_without_reflection_inference(budget_module, make_budget):
    budget = make_budget()
    budget.reserve("shadow-1", "0.11", budget_cash("1000.00"))
    budget.settle("shadow-1", "0.10", "generation:gen-4")
    # Even if the balance already fell by 0.10, the debit is shadowed: 0.11 + 0.10 + 35.00.
    refused(budget_module, "cash_low", budget.reserve, "shadow-2", "0.11", budget_cash("35.20"))
    assert budget.reserve("shadow-2", "0.11", budget_cash("35.21")).required_cash_usd == usd(
        "35.21"
    )
    budget.settle("shadow-2", "0.11", "generation:gen-5")
    refused(budget_module, "cash_low", budget.reserve, "shadow-3", "0.11", budget_cash("35.31"))
    assert budget.reserve("shadow-3", "0.11", budget_cash("35.32")).required_cash_usd == usd(
        "35.32"
    )


def test_closing_tt7_releases_only_its_unused_earmark(budget_module, make_budget):
    budget = make_budget()
    budget.reserve("close-hold", "0.11", budget_cash("1000.00"))
    budget.reserve("close-set", "0.11", budget_cash("1000.00"))
    budget.settle("close-set", "0.05", "generation:gen-6")
    before = budget.snapshot()
    assert usd(before["global"]["open_earmark_usd"]) == usd("49.69")  # 14.85 - 0.16 + 35.00
    closed = make_budget("tt7").close_arm("TT7 was never dispatched")
    assert usd(closed["released_earmark_usd"]) == usd("35.00") and usd(closed["used_usd"]) == 0
    after = budget.snapshot()
    assert usd(after["global"]["open_earmark_usd"]) == usd("14.69")
    for key in ("settled_usd", "held_usd", "used_usd"):
        assert usd(after["arms"]["tt4"][key]) == usd(before["arms"]["tt4"][key])
    # Cash guard now keeps only own holds and settled shadow: 0.11 + 0.11 + 0.05.
    refused(budget_module, "cash_low", budget.reserve, "after-close", "0.11", budget_cash("0.26"))
    assert budget.reserve("after-close", "0.11", budget_cash("0.27")).required_cash_usd == usd(
        "0.27"
    )
    refused(
        budget_module,
        "arm_not_approved",
        make_budget("tt7").reserve,
        "tt7-late",
        "1.00",
        budget_cash("1000.00"),
    )


def test_closing_an_arm_stops_dispatch_but_keeps_every_charge(budget_module, make_budget):
    budget = make_budget()
    cash = budget_cash("1000.00")
    budget.reserve("c-pending", "0.11", cash)
    budget.reserve("c-failed", "0.11", cash)
    budget.fail("c-failed", "unknown outcome")
    budget.reserve("c-settled", "0.11", cash)
    budget.settle("c-settled", "0.04", "generation:gen-7")
    before = budget.snapshot()["arms"]["tt4"]
    closed = budget.close_arm("TT4 complete")
    assert closed["already_closed"] is False and usd(closed["released_earmark_usd"]) == usd("14.59")
    after = budget.snapshot()["arms"]["tt4"]
    assert after["open"] is False and usd(after["earmark_usd"]) == 0
    for key in ("settled_usd", "held_usd", "used_usd"):
        assert usd(after[key]) == usd(before[key])
    assert usd(after["held_usd"]) == usd("0.22") and usd(after["settled_usd"]) == usd("0.04")
    refused(budget_module, "arm_closed", budget.reserve, "c-new", "0.11", cash)
    refused(budget_module, "invalid_reason", budget.close_arm, "  ")
    # In-flight work can still resolve; only its own remainder is released.
    assert usd(budget.settle("c-pending", "0.02", "generation:gen-8")["released_usd"]) == usd(
        "0.09"
    )
    later = make_budget(now=BUDGET_NOW + timedelta(hours=1)).close_arm("different reason")
    assert later["already_closed"] is True
    assert (later["closed_reason"], later["closed_at"]) == ("TT4 complete", closed["closed_at"])


def test_attempt_records_are_append_only_across_later_transitions(make_budget, budget_dir):
    path = budget_dir / "state.json"
    budget = make_budget()
    cash = budget_cash("1000.00")
    budget.reserve("history-1", "0.11", cash, {"stage": "smoke"})
    original = json.loads(path.read_text())["attempts"]["history-1"]
    budget.reserve("history-2", "0.11", cash)
    budget.settle("history-1", "0.05", "generation:gen-9")
    budget.fail("history-2", "boom")
    attempts = json.loads(path.read_text())["attempts"]
    assert set(attempts) == {"history-1", "history-2"}
    settled = attempts["history-1"]
    for key in ("arm", "bound_usd", "cash", "metadata", "created_at", "seq"):
        assert settled[key] == original[key]
    assert settled["history"][:1] == original["history"]
    assert [event["event"] for event in settled["history"]] == ["reserved", "settled"]
    assert [event["event"] for event in attempts["history-2"]["history"]] == ["reserved", "failed"]
    sequence = [event["seq"] for attempt in attempts.values() for event in attempt["history"]]
    assert len(set(sequence)) == len(sequence) == 4


BAD_AMOUNTS = [
    True,
    False,
    None,
    "",
    " 1",
    "1 ",
    "nan",
    "NaN",
    "inf",
    "Infinity",
    "-0.01",
    "-0",
    "1e3",
    "1_0",
    "0x1",
    "abc",
    "0.0000000000001",
    "12345678901",
    [],
    {},
    SimpleNamespace(),
    float("nan"),
    float("inf"),
    float("-inf"),
    -1,
    -0.5,
    Decimal("NaN"),
    Decimal("Infinity"),
    Decimal("-1"),
    Decimal("-0"),
]


@pytest.mark.parametrize("value", BAD_AMOUNTS + ["0", 0, Decimal(0)], ids=value_id)
def test_reservation_bound_must_be_a_finite_positive_decimal(
    budget_module, make_budget, budget_dir, value
):
    cash = budget_cash("1000.00")
    refused(budget_module, "invalid_decimal", make_budget().reserve, "bad-bound", value, cash)
    assert cash.calls == 0 and not (budget_dir / "state.json").exists()


@pytest.mark.parametrize("value", BAD_AMOUNTS, ids=value_id)
def test_settled_actual_must_be_a_finite_nonnegative_decimal(budget_module, make_budget, value):
    budget = make_budget()
    budget.reserve("bad-actual", "0.11", budget_cash("1000.00"))
    refused(budget_module, "invalid_decimal", budget.settle, "bad-actual", value, "generation:g")
    assert budget.snapshot()["attempts"][0]["status"] == "pending"


@pytest.mark.parametrize("value", ["0.11", Decimal("0.11"), 0.11], ids=value_id)
def test_equivalent_decimal_inputs_reserve_the_same_bound(make_budget, value):
    assert make_budget().reserve("form-1", value, budget_cash("1000.00")).bound_usd == usd("0.11")


@pytest.mark.parametrize(
    "attempt_id",
    ["", " a", "a b", "../x", "a/b", "-lead", "x" * 129, None, 7, b"a"],
    ids=value_id,
)
def test_attempt_ids_must_be_safe_stable_strings(
    budget_module, make_budget, budget_dir, attempt_id
):
    budget = make_budget()
    cash = budget_cash("1000.00")
    refused(budget_module, "invalid_attempt_id", budget.reserve, attempt_id, "0.11", cash)
    refused(budget_module, "invalid_attempt_id", budget.settle, attempt_id, "0.11", "generation:g")
    refused(budget_module, "invalid_attempt_id", budget.fail, attempt_id, "error")
    assert cash.calls == 0 and not (budget_dir / "state.json").exists()


@pytest.mark.parametrize(
    "metadata",
    [
        "text",
        ["x"],
        {1: "x"},
        {"nan": float("nan")},
        {"big": "x" * 5000},
        {"object": SimpleNamespace()},
    ],
    ids=value_id,
)
def test_metadata_must_be_small_finite_json(budget_module, make_budget, metadata):
    refused(
        budget_module,
        "invalid_metadata",
        make_budget().reserve,
        "meta-1",
        "0.11",
        budget_cash("1000.00"),
        metadata,
    )
