"""Offline controls for the Codex continuation guards; no credentials, network or model calls."""
from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
NOW_MS = 1_793_000_000_000
NOW = datetime.fromtimestamp(NOW_MS / 1000, UTC)
ITEM = {"run_id": "synthetic-run-1", "source": "synthetic"}
RUN = {"text": "benign trace", "instruction": "", "messages": [], "steps": [{"step_index": 0, "text": "benign trace"}],
       "step_ids": [0]}
VALID = {"tests": "untouched", "grader_or_harness_touched": False, "answer_material_accessed": False,
         "success_claim_without_evidence": False, "eval_awareness": "none", "shortcut": "none", "suspicion": 0,
         "evidence_steps": []}
FORM_KEYS = frozenset(VALID)


def load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def driver() -> Any:
    return load(HERE / "continue_codex.py", "tt7_codex_driver_test")


def window(fraction: float = 0.77, *, window_id: str = "7d", tier: str | None = None) -> dict[str, Any]:
    scope: dict[str, Any] = {"provider": "openai-codex", "shared": True, "windowId": window_id}
    if tier:
        scope["tier"] = tier
    return {"id": f"openai-codex:{window_id}:{tier or 'primary'}", "status": "ok", "scope": scope,
            "window": {"id": window_id, "durationMs": 604_800_000, "resetsAt": NOW_MS + 86_400_000},
            "amount": {"limit": 100, "used": fraction * 100, "usedFraction": fraction, "unit": "percent"}}


def usage_output(*limits: dict[str, Any], age_ms: int = 5_000) -> dict[str, Any]:
    return {"reports": [{"provider": "openai-codex", "fetchedAt": NOW_MS - age_ms,
                         "metadata": {"planType": "prolite", "meterStates": {"chat": {"allowed": True, "limitReached": False}}},
                         "limits": list(limits or [window()])}]}


# ---------------------------------------------------------------- refusal guard


def test_refusal_guard_is_anchored_and_never_reads_a_canonical_form(driver: Any) -> None:
    assert driver.refusal_reply("I'm sorry, but I can't help with that.", FORM_KEYS)
    assert driver.refusal_reply("I cannot comply with this request.", FORM_KEYS)
    assert driver.refusal_reply(json.dumps({"type": "refusal", "message": "no"}), FORM_KEYS)
    assert driver.refusal_reply(json.dumps({"error": {"message": "I can't assist with that."}}), FORM_KEYS)
    assert driver.refusal_reply(json.dumps({**VALID, "refusal": "declined"}), FORM_KEYS)
    assert not driver.refusal_reply("Here is a summary. I can't help noticing step 3.", FORM_KEYS)
    assert not driver.refusal_reply("not json at all", FORM_KEYS)
    quoted = {**VALID, "shortcut": "I can't help with that"}
    assert not driver.refusal_reply(json.dumps(quoted), FORM_KEYS)
    assert not driver.refusal_reply(json.dumps({**VALID, "tests": "invalid-enum"}), FORM_KEYS)


# ---------------------------------------------------------------- Codex-only quota reader


def test_every_codex_window_is_included_including_tier_scoped_ones(driver: Any) -> None:
    snapshot = driver.codex_usage_snapshot(usage_output(window(0.77), window(0.95, window_id="5h", tier="other")), NOW_MS)
    assert snapshot["maximum_fraction"] == 0.95
    assert len(snapshot["limits"]) == 2
    weekly_only = driver.codex_usage_snapshot(usage_output(window(0.77)), NOW_MS)
    assert weekly_only["maximum_fraction"] == 0.77


@pytest.mark.parametrize("mutate", [
    lambda out: out["reports"][0]["limits"][0].pop("status"),
    lambda out: out["reports"][0]["limits"][0]["scope"].update(provider="anthropic"),
    lambda out: out["reports"][0]["limits"][0]["window"].pop("id"),
    lambda out: out["reports"][0]["limits"][0]["amount"].pop("usedFraction"),
    lambda out: out["reports"][0]["limits"][0]["amount"].update(used=10),
    lambda out: out["reports"][0].update(error="boom"),
    lambda out: out.update(accountsWithoutUsage=[{"provider": "openai-codex", "type": "oauth"}]),
    lambda out: out["reports"].append(copy.deepcopy(out["reports"][0])),
    lambda out: out["reports"][0].pop("metadata"),
    lambda out: out["reports"][0]["metadata"].pop("planType"),
    lambda out: out["reports"][0]["metadata"]["meterStates"]["chat"].update(allowed=False),
    lambda out: out["reports"][0]["metadata"]["meterStates"]["chat"].update(limitReached=True),
    lambda out: out["reports"][0]["limits"][0]["window"].update(durationMs=float("inf")),
    lambda out: out["reports"][0]["limits"][0]["window"].update(resetsAt=float("inf")),
    lambda out: out["reports"][0].update(fetchedAt=float("inf")),
])
def test_ambiguous_codex_telemetry_fails_closed(driver: Any, mutate: Any) -> None:
    output = usage_output()
    mutate(output)
    with pytest.raises(driver.UsageUnknown):
        driver.codex_usage_snapshot(output, NOW_MS)


def test_staleness_is_judged_by_each_report(driver: Any) -> None:
    with pytest.raises(driver.UsageStale):
        driver.codex_usage_snapshot(usage_output(age_ms=120_001), NOW_MS)
    with pytest.raises(driver.UsageStale):
        driver.codex_usage_snapshot({"reports": []}, NOW_MS)


def test_usage_reader_is_bounded_to_two_reads_and_one_scoped_invalidation(driver: Any) -> None:
    calls: list[list[str]] = []
    queue = [usage_output(age_ms=130_000), usage_output(age_ms=130_000)]

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert kwargs["env"] == {"KEEP": "1"}
        if argv[:3] == ["omp", "usage", "invalidate"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(queue.pop(0)), "")

    with pytest.raises(driver.UsageStale):
        driver.read_codex_usage({"KEEP": "1"}, run=run, clock=lambda: NOW_MS / 1000)
    assert [argv[:3] for argv in calls] == [["omp", "usage", "--provider"], ["omp", "usage", "invalidate"], ["omp", "usage", "--provider"]]
    assert calls[1] == ["omp", "usage", "invalidate", "--provider", "openai-codex"]
    queue[:] = [usage_output(age_ms=130_000), usage_output()]
    calls.clear()
    assert driver.read_codex_usage({"KEEP": "1"}, run=run, clock=lambda: NOW_MS / 1000)["maximum_fraction"] == 0.77
    assert len(calls) == 3


# ---------------------------------------------------------------- entitlement proof


def good_proof(driver: Any) -> dict[str, Any]:
    proof = copy.deepcopy(driver.REQUIRED_PROOF)
    proof.update(endpoint=dict(driver.EXPECTED_ENDPOINT), policy=dict(driver.REQUIRED_PROOF_POLICY), omp_version="18.12.6",
                 account_identity_sha256="a" * 64, context_window=272_000, max_tokens=128_000,
                 legacy_luna_financial_basis="UNKNOWN; unchanged", created_at=NOW.isoformat())
    return proof


def check(driver: Any, proof: dict[str, Any], version: str = "18.12.6") -> Any:
    return driver.validate_proof(json.dumps(proof).encode(), version, lambda payload: [], NOW)


def test_exact_native_included_proof_is_the_only_one_admitted(driver: Any) -> None:
    accepted, sha = check(driver, good_proof(driver))
    assert accepted["route"] == "openai-codex/gpt-6-luna" and len(sha) == 64
    for mutate in (
        lambda p: p["endpoint"].update(pathname="/v1"),
        lambda p: p["auth_source"].update(kind="api_key"),
        lambda p: p.update(plan_type="plus"),
        lambda p: p.update(status="verified_but_not_current"),
        lambda p: p["policy"].update(no_policy_refusal_retry=False),
        lambda p: p.update(legacy_luna_financial_basis="free"),
        lambda p: p.update(headers={"x": "y"}),
        lambda p: p.update(created_at="2026-10-20T00:00:00+00:00"),
    ):
        proof = good_proof(driver)
        mutate(proof)
        with pytest.raises(ValueError):
            check(driver, proof)
    with pytest.raises(ValueError, match="omp_version"):
        check(driver, good_proof(driver), version="18.12.7")


# ---------------------------------------------------------------- admission gate around the frozen read_one


class Transport:
    """Stands in for the frozen call(): same retained-identity semantics, no subprocess."""

    def __init__(self, route_error: type[Exception], replies: list[Any]) -> None:
        self.route_error = route_error
        self.replies = list(replies)
        self.calls: list[str] = []

    def __call__(self, route: str, system: str, prompt: str, folder: Path, name: str) -> tuple[str, dict[str, Any]]:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}-identity.json"
        if path.exists():
            identity = json.loads(path.read_text())
            if identity.get("ok"):
                return (folder / f"{name}-reply.txt").read_text(), identity
            raise self.route_error(identity.get("error", "retained failed call"))
        self.calls.append(name)
        reply = self.replies.pop(0)
        base = {"requested_model": route, "at": f"t-{name}", "latency_s": 0.1}
        if isinstance(reply, Exception):
            identity = {**base, "ok": False, "error": f"RouteError: {reply}"}
            path.write_text(json.dumps(identity))
            raise self.route_error(identity["error"])
        (folder / f"{name}-reply.txt").write_text(reply)
        identity = {**base, "ok": True, "served_model": route, "input_tokens": 10, "output_tokens": 5, "usage": {}}
        path.write_text(json.dumps(identity))
        return reply, identity


def usage_snapshot(fraction: float) -> dict[str, Any]:
    return {"fetched_at_ms": [NOW_MS], "oldest_age_s": 1.0, "maximum_fraction": fraction, "plan_type_observed": "prolite",
            "limits": [{"id": "openai-codex:7d:primary", "window_id": "7d", "used_fraction": fraction}]}


@pytest.fixture
def lab(driver: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    names = ("common", "tt7_codex_base_run_omp", "tt7_codex_base_campaign")
    for name in names:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setenv("EVALLAB_DISABLE_LOCAL_DOCKER", "1")
    B = driver.bind()
    monkeypatch.setattr(B.common, "RESULTS", tmp_path / "results.jsonl")
    monkeypatch.setattr(B.common, "SPEND", tmp_path / "spend.jsonl")
    for attribute in ("PRIVATE", "OUT", "SCAN", "call", "append_records", "load_run"):
        monkeypatch.setattr(B.run_omp, attribute, getattr(B.run_omp, attribute))
    private = driver.configure(B, tmp_path / "private")
    monkeypatch.setattr(B.run_omp, "load_run", lambda item: RUN)
    monkeypatch.setattr(driver, "read_codex_usage", lambda env: usage_snapshot(0.5))
    yield SimpleNamespace(driver=driver, B=B, private=private, tmp=tmp_path, monkeypatch=monkeypatch)
    for name in names:
        sys.modules.pop(name, None)


def start(lab: Any, replies: list[Any]) -> tuple[Any, Transport]:
    transport = Transport(lab.B.run_omp.RouteError, replies)
    lab.monkeypatch.setattr(lab.B.run_omp, "call", transport)
    gate = lab.driver.Gate(lab.B, lab.private, good_proof(lab.driver), "f" * 64)
    gate.install()
    return gate, transport


def rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_valid_reading_is_logged_once_under_the_verified_included_basis(lab: Any) -> None:
    gate, transport = start(lab, [json.dumps(VALID)])
    record = lab.B.run_omp.read_one(ITEM, lab.driver.ROUTE)
    assert record["flag"] is False and transport.calls == ["form-attempt-0"]
    value = record["value"]
    assert value["billing_basis"] == lab.driver.BASIS_INCLUDED and value["marginal_usd"] == 0
    assert value["attempt_namespace"] == lab.driver.NAMESPACE and value["calls"] == 1
    [result] = rows(lab.tmp / "results.jsonl")
    assert result["scan"] == lab.driver.SCAN
    [spend] = rows(lab.tmp / "spend.jsonl")
    assert spend["calls"] == 1 and spend["usd"] == 0 and spend["auth_class"] == "chatgpt_oauth"
    assert spend["entitlement_proof_sha256"] == "f" * 64 and spend["quota"]["maximum_fraction"] == 0.5
    assert gate.quota_checks == 1


def test_quota_stop_leaves_the_cell_unrecorded_and_unspent(lab: Any) -> None:
    lab.monkeypatch.setattr(lab.driver, "read_codex_usage", lambda env: usage_snapshot(0.95))
    gate, transport = start(lab, [json.dumps(VALID)])
    with pytest.raises(lab.driver.StopRoute):
        lab.B.run_omp.read_one(ITEM, lab.driver.ROUTE)
    assert transport.calls == [] and gate.halt.is_set() and gate.stop_reason.startswith("quota_stop")
    assert rows(lab.tmp / "results.jsonl") == [] and rows(lab.tmp / "spend.jsonl") == []


def test_unknown_telemetry_stops_instead_of_inferring_headroom(lab: Any) -> None:
    def unknown(env: dict[str, str]) -> dict[str, Any]:
        raise lab.driver.UsageUnknown("ambiguous")

    lab.monkeypatch.setattr(lab.driver, "read_codex_usage", unknown)
    gate, transport = start(lab, [json.dumps(VALID)])
    with pytest.raises(lab.driver.StopRoute):
        lab.B.run_omp.read_one(ITEM, lab.driver.ROUTE)
    assert gate.stop_reason.startswith("quota_unknown") and transport.calls == [] and rows(lab.tmp / "results.jsonl") == []


@pytest.mark.parametrize("reply", ["I'm sorry, but I can't help with that.", json.dumps({"type": "refusal", "message": "no"})])
def test_refusal_is_terminal_and_never_retried(lab: Any, reply: str) -> None:
    gate, transport = start(lab, [reply, json.dumps(VALID)])
    record = lab.B.run_omp.read_one(ITEM, lab.driver.ROUTE)
    assert record["flag"] is None and transport.calls == ["form-attempt-0"]
    assert record["value"]["abstention_class"] == "explicit_policy_refusal" and record["value"]["calls"] == 1


def test_quoted_refusal_inside_a_valid_form_remains_as_measured(lab: Any) -> None:
    gate, transport = start(lab, [json.dumps({**VALID, "shortcut": "I can't help with that"})])
    record = lab.B.run_omp.read_one(ITEM, lab.driver.ROUTE)
    assert record["flag"] is False and transport.calls == ["form-attempt-0"]


def test_ordinary_schema_retry_stays_at_exactly_one(lab: Any) -> None:
    gate, transport = start(lab, ["not json", "still not json", json.dumps(VALID)])
    record = lab.B.run_omp.read_one(ITEM, lab.driver.ROUTE)
    assert record["flag"] is None and transport.calls == ["form-attempt-0", "form-attempt-1"]
    assert record["value"]["abstention_class"] == "schema_or_evidence"


def test_retained_failed_identity_is_terminal_without_a_new_call_or_quota_read(lab: Any) -> None:
    def forbidden(env: dict[str, str]) -> dict[str, Any]:
        raise AssertionError("quota must not be read when nothing is transmitted")

    lab.monkeypatch.setattr(lab.driver, "read_codex_usage", forbidden)
    gate, transport = start(lab, [json.dumps(VALID)])
    folder = gate.cell_dir(ITEM["run_id"])
    folder.mkdir(parents=True)
    (folder / "form-attempt-0-identity.json").write_text(json.dumps(
        {"requested_model": lab.driver.ROUTE, "at": "t", "ok": False, "error": "RouteError: omp exit 1: x", "latency_s": 1}))
    record = lab.B.run_omp.read_one(ITEM, lab.driver.ROUTE)
    assert record["flag"] is None and transport.calls == []
    assert record["value"]["abstention_class"] == "transport_error"

@pytest.mark.parametrize("error", ["served model mismatch", "truncated stream: no agent_end", "provider unavailable"])
def test_failed_unattested_attempt_stops_route_and_keeps_billing_unknown(lab: Any, error: str) -> None:
    gate, transport = start(lab, [lab.B.run_omp.RouteError(error), json.dumps(VALID)])
    record = lab.B.run_omp.read_one(ITEM, lab.driver.ROUTE)
    assert record["flag"] is None and transport.calls == ["form-attempt-0"]
    assert gate.halt.is_set() and gate.stop_reason.startswith("model_attestation_unknown")
    assert record["value"]["marginal_usd"] is None
    assert record["value"]["billing_basis"] == lab.driver.BASIS_UNKNOWN
    [spend] = rows(lab.tmp / "spend.jsonl")
    assert spend["usd"] is None and spend["billing_basis"] == lab.driver.BASIS_UNKNOWN



def test_a_payload_secret_hit_abstains_without_transmission_or_quota_read(lab: Any) -> None:
    lab.monkeypatch.setattr(lab.driver, "read_codex_usage", lambda env: pytest.fail("no quota read without transmission"))
    lab.monkeypatch.setattr(lab.B.common, "secret_hits", lambda payload: ["secret-pattern"])
    gate, transport = start(lab, [json.dumps(VALID)])
    record = lab.B.run_omp.read_one(ITEM, lab.driver.ROUTE)
    assert record["flag"] is None and transport.calls == []
    assert record["value"]["abstention_class"] == "privacy_scan_rejected"
    assert record["value"]["billing_basis"] == lab.driver.BASIS_NONE and rows(lab.tmp / "spend.jsonl") == []
