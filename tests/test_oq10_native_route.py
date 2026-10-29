"""Synthetic-only tests for the OQ-10 native route boundary.

No model, network, subprocess, real OQ-6 file, or benchmark record is touched: the
"originals" are tiny fixture modules written to a temporary directory, and admission /
transport calls are injected fakes.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "research/experiments/oq10-task-quality-cross/native_route.py"
)
_SPEC = importlib.util.spec_from_file_location("oq10_native_route_under_test", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
native_route = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = native_route
_SPEC.loader.exec_module(native_route)

HAIKU = native_route.HAIKU_ROUTE
SONNET = native_route.SONNET_ROUTE
KIMI = native_route.KIMI_ROUTE

OLD_ROUTES_LITERAL = """{
    "anthropic/claude-opus-5-5",
    "anthropic/claude-sonnet-5-5",
    "google-antigravity/claude-opus-5-5",
    "google-antigravity/claude-sonnet-5-5",
    "kimi-code/kimi-for-coding",
    "meta/muse-spark-1.3",
}"""
OLD_CONFIG = (
    'memory:\n  backend: "off"\nmnemopi:\n  autoRecall: false\nhindsight:\n  autoRecall: false\n'
    "autolearn:\n  enabled: false\nretry:\n  enabled: false\n  maxRetries: 0\n"
    "  modelFallback: false\n  usageAwareFallback: false\n  fallbackChains: {}\n"
    'advisor:\n  enabled: false\nproviders:\n  cacheWarming: "off"\n'
)
CONTRACT_LINE = 'CONTRACT_PATH = ROOT / "transport-contract.json"'

QUOTA_SOURCE = f'''# synthetic stand-in for the frozen quota module
from __future__ import annotations

from pathlib import Path

_STOP_FRACTION = 0.9
_ROUTES = {OLD_ROUTES_LITERAL}


def admit_route(route, *, output_directory, command_runner=None, now=None):
    directory = Path(output_directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "fixture-admission.txt").write_text(route)
    return {{
        "status": "eligible", "eligible": True, "blocked": False, "route": route,
        "reasons": [], "pool_values": [], "fetchedAt": 1, "checked_at_ms": 2,
        "receipt_path": str(directory / "fixture-admission.txt"),
    }}
'''

TRANSPORT_SOURCE = f"""# synthetic stand-in for the frozen transport module
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
{CONTRACT_LINE}
CONTROLLED_CONFIG_TEXT = '''{OLD_CONFIG}'''
OWNED_ARTIFACTS = (
    "call-consumed.json", "prompt.txt", "system.txt", "controlled-subscription.yml",
    "stdout.jsonl", "stderr.txt", "response.txt", "sessions",
)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def call(directory, *, route, prompt, system, expected_array, config, config_sha256,
         timeout_seconds=600):
    contract = CONTRACT_PATH.read_bytes()
    attestation = Path(json.loads(contract)["session_attestation"]).read_bytes()
    return {{
        "status": "answered", "attempt_consumed": True, "requested_route": route,
        "expected_array": expected_array, "timeout_seconds": timeout_seconds,
        "config_sha256": config_sha256, "contract_sha256": _sha(contract),
        "attestation_source_sha256": _sha(attestation),
        "controller_sha256": _sha(Path(__file__).read_bytes()),
        "controller_path": str(Path(__file__).resolve()), "contract_path": str(CONTRACT_PATH),
        "served_identities": [route], "isolation_valid": True,
        "native_tool_inventory_proven_empty": True, "model_fallback": False, "retries": 0,
        "errors": [], "answer": {{"findings": []}},
    }}
"""


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_sources(root: Path, *, quota: str = QUOTA_SOURCE, transport: str = TRANSPORT_SOURCE):
    original = root / "original"
    original.mkdir()
    attestation = original / "attestation.py"
    attestation.write_text("# synthetic attestation source\n")
    contract = original / "transport-contract.json"
    contract.write_text(json.dumps({
        "session_attestation": str(attestation), "required_flags": ["-p"],
    }))
    quota_path = original / "quota.py"
    quota_path.write_text(quota)
    transport_path = original / "transport.py"
    transport_path.write_text(transport)
    return SimpleNamespace(
        original=original, quota=quota_path, transport=transport_path, contract=contract,
        attestation=attestation,
        quota_sha=sha(quota_path.read_bytes()), transport_sha=sha(transport_path.read_bytes()),
    )


@pytest.fixture
def env(tmp_path: Path):
    root = tmp_path.resolve()
    sources = write_sources(root)
    output = root / "generated"
    manifest = native_route.prepare_runtime(
        sources.quota, sources.quota_sha, sources.transport, sources.transport_sha, output
    )
    return SimpleNamespace(root=root, sources=sources, output=output, manifest=manifest)


def snapshot(directory: Path) -> dict[str, bytes]:
    return {str(p.relative_to(directory)): p.read_bytes() for p in sorted(directory.rglob("*"))
            if p.is_file()}


def receipt_for(manifest: dict[str, Any], route: str, **overrides: Any) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "status": "answered", "attempt_consumed": True, "requested_route": route,
        "config_sha256": manifest["controlled_config"]["sha256"],
        "contract_sha256": manifest["dependencies"]["transport_contract"]["sha256"],
        "attestation_source_sha256": manifest["dependencies"]["session_attestation"]["sha256"],
        "controller_sha256": manifest["generated"]["transport"]["sha256"],
        "served_identities": [route], "served_identity": route, "isolation_valid": True,
        "native_tool_inventory_proven_empty": True,
        "native_tool_inventory_basis": "native_requestControls_empty_tool_lists",
        "model_fallback": False, "retries": 0, "errors": [],
        "answer": {"findings": [{"direction": "synthetic"}]},
        "usage": {"input": 10, "output": 5, "cacheRead": None, "cacheWrite": None,
                  "totalTokens": None, "reasoningTokens": None},
        "tokens_in": None, "tokens_out": 5, "reasoning_tokens": None, "wall_seconds": 2.5,
        "raw_response_sha256": "a" * 64, "stdout_sha256": "a" * 64, "response_sha256": "b" * 64,
    }
    receipt.update(overrides)
    return receipt


class Recorder:
    """Injected admission/call seams that record their invocations."""

    def __init__(self, manifest: dict[str, Any], *, eligible: bool = True,
                 receipt: dict[str, Any] | None = None, consume: bool = False,
                 raises: Exception | None = None) -> None:
        self.manifest = manifest
        self.eligible = eligible
        self.receipt = receipt
        self.consume = consume
        self.raises = raises
        self.admissions: list[tuple[str, Path]] = []
        self.calls: list[dict[str, Any]] = []

    def admit(self, route: str, *, output_directory: Path) -> dict[str, Any]:
        self.admissions.append((route, Path(output_directory)))
        if self.eligible:
            return {"status": "eligible", "eligible": True, "blocked": False, "route": route,
                    "reasons": [], "pool_values": [{"pool": "5h"}, {"pool": "7d"}],
                    "fetchedAt": 10, "checked_at_ms": 11, "receipt_path": "/synthetic/receipt"}
        return {"status": "blocked", "eligible": False, "blocked": True, "route": route,
                "reasons": [{"code": "quota_threshold", "detail": "7d at/above 90%"}],
                "pool_values": [], "fetchedAt": 10, "checked_at_ms": 11}

    def call(self, directory: Path, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs, directory=Path(directory)))
        if self.consume:
            Path(directory).mkdir(parents=True, exist_ok=True)
            (Path(directory) / "call-consumed.json").write_text("{}")
        if self.raises is not None:
            raise self.raises
        return self.receipt if self.receipt is not None else receipt_for(
            self.manifest, kwargs["route"])


def run(env: Any, recorder: Recorder, directory: Path, route: str = SONNET, **kwargs: Any):
    return native_route.run_native(
        directory, route=route, prompt="synthetic prompt", system="synthetic system",
        runtime_manifest=env.manifest, admission_function=recorder.admit,
        call_function=recorder.call, **kwargs,
    )


# --------------------------------------------------------------------------- #
# Runtime producer
# --------------------------------------------------------------------------- #


def test_generated_copies_differ_only_by_reviewed_assignments(env):
    manifest, sources = env.manifest, env.sources
    quota = Path(manifest["generated"]["quota"]["path"]).read_text()
    transport = Path(manifest["generated"]["transport"]["path"]).read_text()
    new_routes = (
        '{"anthropic/claude-haiku-5-5", "anthropic/claude-sonnet-5-5", '
        '"kimi-code/k3.1-pro"}'
    )
    assert quota == QUOTA_SOURCE.replace(OLD_ROUTES_LITERAL, new_routes)
    new_config = OLD_CONFIG + "compaction:\n  enabled: false\n"
    expected_transport = TRANSPORT_SOURCE.replace(
        f"'''{OLD_CONFIG}'''", f"'''{new_config}'''"
    )
    assert transport == expected_transport
    config = Path(manifest["controlled_config"]["path"])
    assert config.read_text() == new_config
    assert manifest["controlled_config"]["sha256"] == sha(config.read_bytes())
    assert manifest["compaction"]["setting"] == "compaction.enabled"
    assert manifest["compaction"]["value"] is False
    assert Path(manifest["generated"]["transport_contract"]["path"]).read_bytes() == (
        sources.contract.read_bytes()
    )


def test_manifest_pins_every_input_and_output(env):
    manifest, sources = env.manifest, env.sources
    pairs = {
        manifest["originals"]["quota"]["path"]: manifest["originals"]["quota"]["sha256"],
        manifest["originals"]["transport"]["path"]: manifest["originals"]["transport"]["sha256"],
        manifest["dependencies"]["transport_contract"]["path"]:
            manifest["dependencies"]["transport_contract"]["sha256"],
        manifest["dependencies"]["session_attestation"]["path"]:
            manifest["dependencies"]["session_attestation"]["sha256"],
        manifest["generated"]["quota"]["path"]: manifest["generated"]["quota"]["sha256"],
        manifest["generated"]["transport"]["path"]: manifest["generated"]["transport"]["sha256"],
        manifest["generated"]["transport_contract"]["path"]:
            manifest["generated"]["transport_contract"]["sha256"],
    }
    for path, digest in pairs.items():
        assert sha(Path(path).read_bytes()) == digest
    assert manifest["dependencies"]["transport_contract"]["path"] == str(sources.contract)
    assert manifest["dependencies"]["session_attestation"]["path"] == str(sources.attestation)
    assert manifest["producer"]["sha256"] == sha(_MODULE_PATH.read_bytes())
    assert json.loads(json.dumps(manifest)) == manifest


def test_prepare_and_load_never_mutate_originals_or_add_files(env):
    before = snapshot(env.sources.original)
    native_route.load_runtime(env.manifest)
    native_route.load_runtime(env.manifest)
    assert snapshot(env.sources.original) == before
    assert sorted(p.name for p in env.output.iterdir()) == [
        native_route.GENERATED_CONFIG_NAME, native_route.GENERATED_QUOTA_NAME,
        native_route.GENERATED_TRANSPORT_NAME,
        native_route.CONTRACT_NAME,
    ]


def test_loaded_modules_use_generated_routes_config_and_original_contract(env):
    _, transport = native_route.load_runtime(env.manifest)
    assert transport.CONTROLLED_CONFIG_TEXT.endswith("compaction:\n  enabled: false\n")
    assert transport.CONTROLLED_CONFIG_TEXT.encode() == Path(
        env.manifest["controlled_config"]["path"]).read_bytes()
    assert env.output / native_route.CONTRACT_NAME == transport.CONTRACT_PATH
    assert transport.CONTRACT_PATH.read_bytes() == env.sources.contract.read_bytes()
    assert Path(transport.__file__) == Path(env.manifest["generated"]["transport"]["path"])
    assert not any(name.startswith("oq10_quota_") for name in sys.modules)


def test_prepare_is_deterministic_and_never_overwrites(env):
    again = native_route.prepare_runtime(
        env.sources.quota, env.sources.quota_sha, env.sources.transport,
        env.sources.transport_sha, env.output,
    )
    assert again == env.manifest
    generated = Path(env.manifest["generated"]["quota"]["path"])
    generated.write_text(generated.read_text() + "# tampered\n")
    with pytest.raises(FileExistsError):
        native_route.prepare_runtime(
            env.sources.quota, env.sources.quota_sha, env.sources.transport,
            env.sources.transport_sha, env.output,
        )
    assert generated.read_text().endswith("# tampered\n")


@pytest.mark.parametrize("which", ["quota", "transport"])
def test_prepare_stops_on_source_digest_drift(tmp_path, which):
    sources = write_sources(tmp_path.resolve())
    output = tmp_path.resolve() / "generated"
    digests = {"quota": sources.quota_sha, "transport": sources.transport_sha}
    digests[which] = "0" * 64
    with pytest.raises(native_route.RuntimeIntegrityError):
        native_route.prepare_runtime(
            sources.quota, digests["quota"], sources.transport, digests["transport"], output
        )
    assert not output.exists()


def test_prepare_refuses_to_write_beside_originals(tmp_path):
    sources = write_sources(tmp_path.resolve())
    with pytest.raises(native_route.RuntimeIntegrityError):
        native_route.prepare_runtime(
            sources.quota, sources.quota_sha, sources.transport, sources.transport_sha,
            sources.original,
        )
    assert not (sources.original / native_route.GENERATED_QUOTA_NAME).exists()


@pytest.mark.parametrize("label", [
    "second_routes_assignment", "changed_route_set", "config_already_compacts",
    "contract_expression_changed", "routes_rebound_in_function", "missing_config",
])
def test_prepare_rejects_unexpected_source_structure(tmp_path, label):
    quota, transport = QUOTA_SOURCE, TRANSPORT_SOURCE
    if label == "second_routes_assignment":
        quota += "\n_ROUTES = set()\n"
    elif label == "changed_route_set":
        quota = quota.replace('    "meta/muse-spark-1.3",\n', "")
    elif label == "config_already_compacts":
        transport = transport.replace("providers:", "compaction:\n  enabled: true\nproviders:")
    elif label == "contract_expression_changed":
        transport = transport.replace(CONTRACT_LINE, 'CONTRACT_PATH = ROOT / "other.json"')
    elif label == "routes_rebound_in_function":
        quota += "\n\ndef tamper():\n    global _ROUTES\n    _ROUTES = {'x/y'}\n"
    else:
        transport = transport.replace("CONTROLLED_CONFIG_TEXT = ", "OTHER_NAME = ")
    sources = write_sources(tmp_path.resolve(), quota=quota, transport=transport)
    output = tmp_path.resolve() / "generated"
    with pytest.raises(native_route.RuntimeIntegrityError):
        native_route.prepare_runtime(
            sources.quota, sources.quota_sha, sources.transport, sources.transport_sha, output
        )
    assert not output.exists()


# --------------------------------------------------------------------------- #
# Loader integrity
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("pin", [
    ("originals", "quota"), ("originals", "transport"), ("dependencies", "transport_contract"),
    ("dependencies", "session_attestation"), ("generated", "quota"), ("generated", "transport"),
    ("generated", "transport_contract"),
])
def test_load_runtime_rejects_any_pinned_file_drift(env, pin):
    section, name = pin
    path = Path(env.manifest[section][name]["path"])
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(native_route.RuntimeIntegrityError):
        native_route.load_runtime(env.manifest)


def test_load_runtime_rejects_controlled_config_drift(env):
    path = Path(env.manifest["controlled_config"]["path"])
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(native_route.RuntimeIntegrityError):
        native_route.load_runtime(env.manifest)


def test_load_runtime_rejects_generated_file_that_is_not_recipe_output(env):
    path = Path(env.manifest["generated"]["quota"]["path"])
    forged = path.read_text().replace("_STOP_FRACTION = 0.9", "_STOP_FRACTION = 0.99")
    path.write_text(forged)
    env.manifest["generated"]["quota"]["sha256"] = sha(forged.encode())
    with pytest.raises(native_route.RuntimeIntegrityError, match="recipe output"):
        native_route.load_runtime(env.manifest)


@pytest.mark.parametrize("mutate", [
    lambda m: m.update(extra=1),
    lambda m: m.pop("compaction"),
    lambda m: m.update(routes=[SONNET]),
    lambda m: m.update(routes=[HAIKU, SONNET, "anthropic/claude-opus-5-5"]),
    lambda m: m["route_context_tokens"].update({HAIKU: 1_000_000}),
    lambda m: m["compaction"].update(value=True),
    lambda m: m["producer"].update(sha256="0" * 64),
    lambda m: m["controlled_config"].update(sha256="0" * 64),
    lambda m: m["generated"].update(extra={}),
    lambda m: m.update(recipe_version="other"),
])
def test_load_runtime_rejects_manifest_tampering(env, mutate):
    manifest = copy.deepcopy(env.manifest)
    mutate(manifest)
    with pytest.raises(native_route.RuntimeIntegrityError):
        native_route.load_runtime(manifest)


# --------------------------------------------------------------------------- #
# run_native
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("route", [
    "anthropic/claude-opus-5-5", "openai-codex/gpt-6.1-sol:max", "anthropic/claude-sonnet-5-5:high",
    "claude-sonnet-5-5", "", "google-antigravity/claude-sonnet-5-5", "meta/muse-spark-1.3",
])
def test_unregistered_routes_are_rejected_before_dispatch(env, route):
    recorder = Recorder(env.manifest)
    with pytest.raises(ValueError, match="route"):
        run(env, recorder, env.root / "cell", route=route)
    assert recorder.admissions == [] and recorder.calls == []


def test_fresh_call_admits_then_sends_once_with_findings_container(env):
    recorder = Recorder(env.manifest)
    cell = env.root / "cell"
    result = run(env, recorder, cell, timeout_seconds=120)
    assert [route for route, _ in recorder.admissions] == [SONNET]
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["expected_array"] == "findings"
    assert call["route"] == SONNET and call["timeout_seconds"] == 120
    assert call["prompt"] == "synthetic prompt" and call["system"] == "synthetic system"
    assert call["config"] == Path(env.manifest["controlled_config"]["path"])
    assert call["config_sha256"] == env.manifest["controlled_config"]["sha256"]
    assert result["disposition"] == "answered" and result["sent"] is True
    assert result["terminal"] is True and result["reread"] is False
    assert result["findings_count"] == 1
    admission_directory = recorder.admissions[0][1]
    assert admission_directory == env.root / "cell.quota-admissions"
    assert result["admission"]["eligible"] is True
    assert result["admission"]["pool_values"] == [{"pool": "5h"}, {"pool": "7d"}]


def test_evidence_is_preserved_without_invented_usage(env):
    recorder = Recorder(env.manifest)
    result = run(env, recorder, env.root / "cell")
    evidence = result["evidence"]
    assert evidence["usage"]["input"] == 10 and evidence["usage"]["cacheRead"] is None
    assert evidence["tokens_in"] is None and evidence["tokens_out"] == 5
    assert evidence["wall_seconds"] == 2.5
    assert evidence["raw_response_sha256"] == "a" * 64
    assert evidence["served_identities"] == [SONNET] and evidence["served_identity"] == SONNET
    assert evidence["native_tool_inventory_proven_empty"] is True
    assert evidence["reasoning_tokens"] is None and evidence["session_sha256"] is None
    assert result["receipt"]["answer"] == result["answer"]


def test_each_native_route_uses_one_profile_and_its_own_fresh_admission(env):
    recorder = Recorder(env.manifest)
    run(env, recorder, env.root / "sonnet-cell", route=SONNET)
    run(env, recorder, env.root / "haiku-cell", route=HAIKU)
    run(env, recorder, env.root / "kimi-cell", route=KIMI)
    assert [route for route, _ in recorder.admissions] == [SONNET, HAIKU, KIMI]
    assert {(c["config"], c["config_sha256"]) for c in recorder.calls} == {(
        Path(env.manifest["controlled_config"]["path"]),
        env.manifest["controlled_config"]["sha256"],
    )}

def test_kimi_route_has_no_anthropic_admission_dependency(env):
    recorder = Recorder(env.manifest)

    def kimi_only_admission(route, **kwargs):
        assert route == KIMI
        return recorder.admit(route, **kwargs)

    result = native_route.run_native(
        env.root / "independent-kimi", route=KIMI,
        prompt="synthetic prompt", system="synthetic system",
        runtime_manifest=env.manifest, admission_function=kimi_only_admission,
        call_function=recorder.call,
    )
    assert result["sent"] is True
    assert [route for route, _ in recorder.admissions] == [KIMI]


def test_default_wiring_uses_the_generated_quota_and_transport(env):
    # No injected admission/call: the generated fixture modules answer, proving the
    # defaults come from the loaded pinned copies and carry their pins into the receipt.
    result = native_route.run_native(
        env.root / "cell", route=HAIKU, prompt="p", system="s", runtime_manifest=env.manifest,
    )
    assert result["disposition"] == "answered"
    assert (env.root / "cell.quota-admissions" / "fixture-admission.txt").read_text() == HAIKU
    assert result["receipt"]["controller_path"] == env.manifest["generated"]["transport"]["path"]
    assert result["receipt"]["contract_path"] == env.manifest["generated"]["transport_contract"]["path"]
    assert result["receipt"]["timeout_seconds"] == 180


def test_closed_quota_is_unsent_pending_and_not_consumed(env):
    cell = env.root / "cell"
    recorder = Recorder(env.manifest, eligible=False)
    result = run(env, recorder, cell)
    assert result["disposition"] == "quota_pending"
    assert result["reason"] == "quota_pending:quota_threshold"
    assert result["sent"] is False and result["attempt_consumed"] is False
    assert result["terminal"] is False and result["evidence"]["usage"] is None
    assert recorder.calls == [] and not cell.exists()
    # Later fresh admission may open; the cell was never consumed.
    recorder.eligible = True
    assert run(env, recorder, cell)["disposition"] == "answered"
    assert len(recorder.admissions) == 2 and len(recorder.calls) == 1


def test_sent_failure_is_terminal_missing_and_never_redispatched(env):
    cell = env.root / "cell"
    failed = receipt_for(
        env.manifest, SONNET, status="unanswered", answer=None, error="timeout_no_retry",
        errors=["timeout_no_retry"], served_identities=[], isolation_valid=False,
        usage={"input": None}, tokens_in=None, tokens_out=None, wall_seconds=180.0,
    )
    failed.pop("answer")
    recorder = Recorder(env.manifest, receipt=failed, consume=True)
    first = run(env, recorder, cell)
    assert first["disposition"] == "missing" and first["reason"] == "timeout_no_retry"
    assert first["sent"] is True and first["terminal"] is True and first["answer"] is None
    assert first["evidence"]["tokens_out"] is None and first["evidence"]["wall_seconds"] == 180.0
    second = run(env, recorder, cell)
    # Existing custody: no fresh admission, and the generic transport (which rereads and
    # never reissues) is the only thing consulted.
    assert len(recorder.admissions) == 1
    assert second["reread"] is True and second["sent"] is True
    assert second["disposition"] == "missing"


def test_consumed_directory_skips_admission_and_oversize_preflight(env):
    cell = env.root / "cell"
    cell.mkdir()
    (cell / "call-consumed.json").write_text("{}")
    consumed = receipt_for(env.manifest, HAIKU, status="unanswered",
                           error="prior_consumed_attempt_cannot_be_reissued")
    recorder = Recorder(env.manifest, receipt=consumed)
    estimates: list[int] = []
    result = run(env, recorder, cell, route=HAIKU,
                 estimate_tokens=lambda prompt, system: estimates.append(1) or 10**9)
    assert recorder.admissions == [] and estimates == []
    assert result["reread"] is True and result["disposition"] == "missing"
    assert result["reason"] == "prior_consumed_attempt_cannot_be_reissued"
    assert result["input_window"] is None


def test_reread_of_an_answered_receipt_returns_the_answer_without_admission(env):
    cell = env.root / "cell"
    cell.mkdir()
    (cell / "receipt.json").write_text("{}")
    recorder = Recorder(env.manifest)
    result = run(env, recorder, cell)
    assert recorder.admissions == []
    assert result["reread"] is True and result["disposition"] == "answered"
    assert result["receipt_file_sha256"] == sha(b"{}")


def test_exception_after_consumption_is_terminal_missing(env):
    cell = env.root / "cell"
    recorder = Recorder(env.manifest, consume=True, raises=RuntimeError("boom"))
    result = run(env, recorder, cell)
    assert result["disposition"] == "missing" and result["sent"] is True
    assert result["reason"] == "transport_exception:RuntimeError"
    assert result["terminal"] is True


def test_exception_before_consumption_propagates(env):
    recorder = Recorder(env.manifest, raises=ValueError("invalid input"))
    with pytest.raises(ValueError, match="invalid input"):
        run(env, recorder, env.root / "cell")


@pytest.mark.parametrize(("route", "estimate", "outcome"), [
    (HAIKU, 100_000, "answered"),
    (HAIKU, 100_001, "missing"),
    (SONNET, 100_001, "answered"),
    (SONNET, 1_000_000, "answered"),
    (SONNET, 1_000_001, "missing"),
])
def test_oversize_role_is_missing_before_send_with_route_limit(env, route, estimate, outcome):
    recorder = Recorder(env.manifest)
    seen: list[tuple[str, str]] = []

    def estimator(prompt: str, system: str) -> int:
        seen.append((prompt, system))
        return estimate

    cell = env.root / "cell"
    result = run(env, recorder, cell, route=route, estimate_tokens=estimator)
    assert seen == [("synthetic prompt", "synthetic system")]
    assert result["disposition"] == outcome
    assert result["input_window"]["estimated_tokens"] == estimate
    if outcome == "missing":
        assert result["reason"].startswith("input_window_exceeded:")
        assert result["sent"] is False and result["terminal"] is True
        assert recorder.admissions == [] and recorder.calls == [] and not cell.exists()
    else:
        assert len(recorder.calls) == 1


def test_missing_estimator_is_recorded_not_assumed_to_fit(env):
    result = run(env, Recorder(env.manifest), env.root / "cell", route=HAIKU)
    assert result["input_window"] == {
        "performed": False, "estimated_tokens": None, "limit_tokens": 100_000,
        "within_limit": None,
    }


@pytest.mark.parametrize("bad", [-1, True, 1.5, "10"])
def test_invalid_token_estimate_raises_instead_of_sending(env, bad):
    recorder = Recorder(env.manifest)
    with pytest.raises(ValueError, match="estimate"):
        run(env, recorder, env.root / "cell", estimate_tokens=lambda prompt, system: bad)
    assert recorder.admissions == [] and recorder.calls == []


@pytest.mark.parametrize("overrides", [
    {"controller_sha256": "0" * 64},
    {"config_sha256": "0" * 64},
    {"contract_sha256": "0" * 64},
    {"attestation_source_sha256": "0" * 64},
    {"controller_path": "/elsewhere/transport.py"},
])
def test_receipt_that_is_not_bound_to_the_pins_is_missing(env, overrides):
    recorder = Recorder(env.manifest, receipt=receipt_for(env.manifest, SONNET, **overrides))
    result = run(env, recorder, env.root / "cell")
    assert result["disposition"] == "missing"
    assert result["reason"].startswith("receipt_pin_mismatch:")
    assert result["answer"] is None and result["sent"] is True


@pytest.mark.parametrize(("overrides", "reason"), [
    ({"served_identities": [HAIKU]}, "served_identity_mismatch"),
    ({"served_identities": [SONNET, HAIKU]}, "served_identity_mismatch"),
    ({"isolation_valid": False}, "isolation_invalid"),
    ({"native_tool_inventory_proven_empty": False}, "tool_inventory_not_proven_empty"),
    ({"model_fallback": True}, "fallback_or_retry_observed"),
    ({"retries": 1}, "fallback_or_retry_observed"),
    ({"errors": ["x"]}, "receipt_errors_on_answer"),
    ({"answer": {"judgments": []}}, "answer_without_findings_array"),
    ({"status": "quarantined_identity", "error": "unexpected_identity_or_fallback"},
     "unexpected_identity_or_fallback"),
    ({"status": "weird"}, "malformed_receipt_status"),
])
def test_answer_is_downgraded_when_custody_recheck_fails(env, overrides, reason):
    recorder = Recorder(env.manifest, receipt=receipt_for(env.manifest, SONNET, **overrides))
    result = run(env, recorder, env.root / "cell")
    assert result["disposition"] == "missing" and result["reason"] == reason
    assert result["answer"] is None


def test_run_native_validates_timeout_and_text_types(env):
    recorder = Recorder(env.manifest)
    for timeout in (0, 601, True, 1.5):
        with pytest.raises(ValueError, match="timeout"):
            run(env, recorder, env.root / "cell", timeout_seconds=timeout)
    with pytest.raises(ValueError, match="strings"):
        native_route.run_native(
            env.root / "cell", route=SONNET, prompt=b"x", system="s",
            runtime_manifest=env.manifest, admission_function=recorder.admit,
            call_function=recorder.call,
        )
    assert recorder.admissions == [] and recorder.calls == []


def test_run_native_refuses_drifted_runtime_before_admission(env):
    path = Path(env.manifest["generated"]["transport"]["path"])
    path.write_bytes(path.read_bytes() + b"\n")
    recorder = Recorder(env.manifest)
    with pytest.raises(native_route.RuntimeIntegrityError):
        run(env, recorder, env.root / "cell")
    assert recorder.admissions == [] and recorder.calls == []


# --------------------------------------------------------------------------- #
# export_flags
# --------------------------------------------------------------------------- #


def production_eligible(confidence, citations_verified, *, min_verified_confidence=0.5,
                        min_unverified_confidence=0.8):
    """Verbatim semantics of evallab.production.tq_context.is_proposal_eligible."""
    import math

    if not isinstance(citations_verified, bool):
        return False
    for value in (confidence, min_verified_confidence, min_unverified_confidence):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not 0 <= value <= 1 or not math.isfinite(value)):
            return False
    floor = min_verified_confidence if citations_verified else min_unverified_confidence
    return confidence >= floor


def candidate(confidence, verified, ordinal=1, **extra):
    row = {
        "candidate_id": f"synthetic-{ordinal}", "direction": "d", "mechanism": "m",
        "confidence": confidence, "citation_verified": verified,
        "citations": [{"path": "tests/x", "start": 1, "end": 2}],
    }
    row.update(extra)
    return row


def flags(payloads, **kwargs):
    kwargs.setdefault("eligibility_function", production_eligible)
    return native_route.export_flags(payloads, parse_status="ok", **kwargs)


@pytest.mark.parametrize(("confidence", "verified", "old", "new"), [
    (0.8, True, True, True),
    (0.79, True, False, True),
    (0.5, True, False, True),
    (0.49, True, False, False),
    (0.8, False, False, True),
    (0.79, False, False, False),
    (1.0, False, False, True),
    (0.0, True, False, False),
])
def test_gate_boundaries_follow_old_rule_and_production_eligibility(confidence, verified, old,
                                                                     new):
    result = flags([candidate(confidence, verified)])
    assert result["status"] == "ok"
    assert (result["old_0_8"], result["new_verified_0_5"]) == (old, new)


def test_any_eligible_candidate_sets_the_task_flag_and_ordinals_are_original():
    result = flags([candidate(0.3, True, 1), candidate(0.6, True, 2), candidate(0.9, True, 3)])
    assert result["memberships"] == {"old_0_8": ["C3"], "new_verified_0_5": ["C2", "C3"]}
    assert (result["old_0_8"], result["new_verified_0_5"]) == (True, True)


def test_valid_empty_findings_array_is_false_for_both_gates_not_null():
    result = flags([])
    assert (result["old_0_8"], result["new_verified_0_5"]) == (False, False)
    assert result["status"] == "ok"


@pytest.mark.parametrize("status", [
    "native_missing", "schema_invalid", "quota_pending", "oversize_missing", "", "OK", "parsed",
])
def test_non_ok_parse_status_is_unknown_never_false(status):
    result = native_route.export_flags(
        [candidate(0.99, True)], parse_status=status, eligibility_function=production_eligible
    )
    assert result["old_0_8"] is None and result["new_verified_0_5"] is None
    assert result["status"] == "missing"


def test_non_string_status_and_non_array_payloads_are_unknown():
    for payloads, status in ((None, "ok"), ({"a": 1}, "ok"), ("x", "ok"), ([], None)):
        result = native_route.export_flags(
            payloads, parse_status=status, eligibility_function=production_eligible
        )
        assert result["old_0_8"] is None and result["new_verified_0_5"] is None


def test_model_claimed_flags_never_select_the_gate():
    result = flags([candidate(0.9, False) | {
        "verified": True, "supported": True, "citations_verified": True,
    }])
    assert result["old_0_8"] is False and result["new_verified_0_5"] is True
    result = flags([candidate(0.6, False) | {"verified": True, "supported": True}])
    assert (result["old_0_8"], result["new_verified_0_5"]) == (False, False)


@pytest.mark.parametrize("row", [
    candidate(0.9, 1),
    candidate(0.9, "true"),
    candidate(0.9, None),
    candidate(True, True),
    candidate(float("nan"), True),
    candidate(1.5, True),
    candidate(-0.1, True),
    candidate("0.9", True),
    candidate(0.9, True, citations=[]),
    candidate(0.9, True, citation_id=None) | {"candidate_id": ""},
    "not an object",
])
def test_invalid_parser_rows_are_unknown_not_false(row):
    result = flags([row])
    assert result["status"] == "missing"
    assert result["old_0_8"] is None and result["new_verified_0_5"] is None


def test_duplicate_candidate_ids_are_unknown():
    rows = [candidate(0.9, True, 1), candidate(0.9, True, 2, candidate_id="synthetic-1")]
    assert flags(rows)["old_0_8"] is None


def test_partial_parse_is_unknown_when_raw_count_differs():
    rows = [candidate(0.9, True)]
    assert flags(rows, raw_findings_count=2)["new_verified_0_5"] is None
    assert flags(rows, raw_findings_count=1)["new_verified_0_5"] is True
    assert flags([], raw_findings_count=0)["new_verified_0_5"] is False
    with pytest.raises(ValueError, match="raw_findings_count"):
        flags(rows, raw_findings_count=-1)


def test_exactly_one_policy_source_must_be_injected():
    with pytest.raises(ValueError, match="exactly one"):
        native_route.export_flags([], parse_status="ok")
    fake = SimpleNamespace(GATES=("old_0_8", "new_verified_0_5"),
                           gate_memberships=lambda rows: {"old_0_8": [], "new_verified_0_5": []})
    with pytest.raises(ValueError, match="exactly one"):
        native_route.export_flags([], parse_status="ok", paired_module=fake,
                                  eligibility_function=production_eligible)


def test_injected_paired_module_is_the_policy_and_its_errors_are_unknown():
    calls: list[Any] = []

    def memberships(rows):
        calls.append(rows)
        return {"old_0_8": [], "new_verified_0_5": ["C1"], "union": ["C1"]}

    paired = SimpleNamespace(GATES=("old_0_8", "new_verified_0_5"), gate_memberships=memberships)
    rows = [candidate(0.99, True)]
    result = native_route.export_flags(rows, parse_status="ok", paired_module=paired)
    assert calls == [rows]
    assert (result["old_0_8"], result["new_verified_0_5"]) == (False, True)
    assert result["memberships"] == {"old_0_8": [], "new_verified_0_5": ["C1"]}

    def reject(rows):
        raise ValueError("citation_verified must be an actual parser verification boolean")

    broken = SimpleNamespace(GATES=paired.GATES, gate_memberships=reject)
    result = native_route.export_flags(rows, parse_status="ok", paired_module=broken)
    assert result["old_0_8"] is None and "citation_verified" in result["reason"]

    malformed = SimpleNamespace(GATES=paired.GATES, gate_memberships=lambda rows: {"union": []})
    result = native_route.export_flags(rows, parse_status="ok", paired_module=malformed)
    assert result["status"] == "missing" and result["reason"] == "gate_memberships_malformed"

    with pytest.raises(ValueError, match="GATES"):
        native_route.export_flags(
            rows, parse_status="ok",
            paired_module=SimpleNamespace(GATES=("a", "b"), gate_memberships=memberships),
        )
