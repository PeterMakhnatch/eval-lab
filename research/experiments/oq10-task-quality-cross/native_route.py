"""OQ-10 native Sonnet/Haiku/Kimi boundary over the pinned OQ-6 isolation stack.

This module does not reimplement the native transport, quota parser, consumed-attempt
custody, or raw/session attestation. It deterministically *produces* private copies of
the frozen OQ-6 ``quota.py`` and ``transport.py`` that differ from the originals only in
two reviewed top-level assignments, pins every input and output by SHA-256, and runs
the unchanged ``admit_route`` / ``call`` functions of those copies.

Reviewed assignment changes (see ``REVIEWED_CHANGES``):

* ``quota._ROUTES`` becomes exactly the Sonnet 5.5, Haiku 5.5, and K3.1 Pro routes
  listed below. Pool parsing and thresholds are untouched: Anthropic keeps its shared
  5h + 7d policy; Kimi independently keeps 5h + monthly-total + monthly-code (+ 7d
  when present). Every route requires fresh, strictly-below-90%, fully accounted
  subscription coverage; no new pool or model-alias equivalence is invented.
* ``transport.CONTROLLED_CONFIG_TEXT`` gains ``compaction.enabled: false``. The key is
  the registered ``compaction.enabled`` setting ("Automatic conversation compaction",
  default true) in the oh-my-pi v18.12.6 ``docs/settings.md``. A role that does not fit
  its window is therefore reported missing; the input is never adaptively shortened.

The original ROOT-relative ``CONTRACT_PATH`` expression remains unchanged. A
byte-identical contract is published beside the generated transport, so its real
``__file__`` attributes custody to the code that actually ran without relocating
the original contract's pinned session-attestation dependency.

Importing or producing this runtime does not read task bodies or call a model.
Callers and tests may inject ``admission_function`` / ``call_function`` / token counts.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple

SCHEMA_RUNTIME = "oq10.native-runtime/v1"
SCHEMA_RESULT = "oq10.native-route/v1"
RECIPE_VERSION = "oq10-native-runtime-recipe-v2"

SONNET_ROUTE = "anthropic/claude-sonnet-5-5"
HAIKU_ROUTE = "anthropic/claude-haiku-5-5"
KIMI_ROUTE = "kimi-code/k3.1-pro"
ROUTES: tuple[str, ...] = (HAIKU_ROUTE, SONNET_ROUTE, KIMI_ROUTE)
# Native catalog windows, not output maxTokens (K3.1 Pro's output cap is 32,000).
ROUTE_CONTEXT_TOKENS: dict[str, int] = {
    HAIKU_ROUTE: 100_000, SONNET_ROUTE: 1_000_000, KIMI_ROUTE: 1_048_576,
}

OQ6_QUOTA_ROUTES = frozenset({
    "anthropic/claude-opus-5-5",
    "anthropic/claude-sonnet-5-5",
    "google-antigravity/claude-opus-5-5",
    "google-antigravity/claude-sonnet-5-5",
    "kimi-code/kimi-for-coding",
    "meta/muse-spark-1.3",
})
COMPACTION_BLOCK = "compaction:\n  enabled: false\n"
COMPACTION_SETTING_SOURCE = (
    "https://raw.githubusercontent.com/can1357/oh-my-pi/v18.12.6/docs/settings.md"
)
CONTRACT_NAME = "transport-contract.json"
REVIEWED_CHANGES: tuple[str, ...] = (
    "quota._ROUTES := {anthropic/claude-haiku-5-5, anthropic/claude-sonnet-5-5, kimi-code/k3.1-pro}",
    "transport.CONTROLLED_CONFIG_TEXT += compaction.enabled: false",
)
GENERATED_QUOTA_NAME = "oq10_quota.py"
GENERATED_TRANSPORT_NAME = "oq10_transport.py"
GENERATED_CONFIG_NAME = "controlled-subscription-oq10.yml"

EXPECTED_ARRAY = "findings"
PARSE_OK = "ok"
OLD_GATE = "old_0_8"
NEW_GATE = "new_verified_0_5"
OLD_GATE_FLOOR = 0.8

_MANIFEST_KEYS = frozenset({
    "schema", "recipe_version", "producer", "originals", "dependencies", "generated",
    "controlled_config", "routes", "route_context_tokens", "reviewed_changes",
    "compaction",
})
_ORIGINAL_CONTRACT_EXPRESSION = 'ROOT / "transport-contract.json"'
_RECEIPT_STATUSES = frozenset({"answered", "unanswered", "quarantined_identity"})
_EVIDENCE_FIELDS = (
    "status", "error", "errors", "exit_code", "wall_seconds", "started_at", "ended_at",
    "usage", "tokens_in", "tokens_out", "reasoning_tokens", "uncached_tokens_in",
    "reported_assistant_usage", "served_identity", "served_provider", "served_model",
    "served_identities", "model_fallback", "retries", "stdout_sha256",
    "raw_response_sha256", "stderr_sha256", "response_sha256", "session_sha256",
    "prompt_sha256", "system_sha256", "config_sha256", "contract_sha256",
    "attestation_source_sha256", "controller_sha256", "native_tool_inventory_basis",
    "native_tool_inventory_proven_empty", "isolation_valid", "receipt_path",
    "call_consumed_sha256",
)


class RuntimeIntegrityError(ValueError):
    """A pinned source, generated file, dependency, or manifest field is not as frozen."""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hex(value: Any, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise RuntimeIntegrityError(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def _read_regular(path: Path, label: str) -> bytes:
    if not path.is_file():
        raise RuntimeIntegrityError(f"{label} is not a regular file: {path}")
    return path.read_bytes()


def _checked_read(path: Path, sha256: str, label: str) -> bytes:
    data = _read_regular(path, label)
    if _sha(data) != sha256:
        raise RuntimeIntegrityError(f"{label} changed: SHA-256 differs from its pin")
    return data


def _pin(path: Path, data: bytes) -> dict[str, str]:
    return {"path": str(path), "sha256": _sha(data)}


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError("non-finite JSON number: " + value)


def _attestation_path(contract_bytes: bytes) -> Path:
    """Resolve the contract's session-attestation path exactly as ``transport.call`` does."""
    try:
        contract = json.loads(
            contract_bytes, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant
        )
    except (ValueError, UnicodeError) as error:
        raise RuntimeIntegrityError("transport contract is not unambiguous JSON") from error
    if not isinstance(contract, dict):
        raise RuntimeIntegrityError("transport contract is not an object")
    value = contract.get("session_attestation")
    if not isinstance(value, str) or not value.strip():
        raise RuntimeIntegrityError("transport contract has no session_attestation path")
    return Path(value).resolve()


def _publish(path: Path, data: bytes) -> None:
    """Exclusive-create ``path``; an existing byte-identical regular file is accepted."""
    if path.is_symlink():
        raise FileExistsError(f"{path} is a symlink; refusing to use it")
    try:
        with path.open("xb") as stream:
            stream.write(data)
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
            raise FileExistsError(
                f"{path} already exists with different content; refusing to overwrite"
            ) from None


# --------------------------------------------------------------------------- #
# Syntax-aware, byte-exact reviewed-assignment rewrite
# --------------------------------------------------------------------------- #


def _parse_module(source: bytes, label: str) -> ast.Module:
    if b"\r" in source or source.startswith(b"\xef\xbb\xbf"):
        raise RuntimeIntegrityError(f"{label}: CR line endings or a BOM are unsupported")
    try:
        return ast.parse(source, filename=label)
    except (SyntaxError, ValueError) as error:
        raise RuntimeIntegrityError(f"{label}: source does not parse") from error


def _span(source: bytes, node: ast.expr) -> tuple[int, int]:
    """Byte offsets of ``node`` (ast column offsets are UTF-8 byte offsets)."""
    if node.end_lineno is None or node.end_col_offset is None:
        raise RuntimeIntegrityError("expression has no end position")
    starts = [0]
    for line in source.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    return (
        starts[node.lineno - 1] + node.col_offset,
        starts[node.end_lineno - 1] + node.end_col_offset,
    )


def _splice(source: bytes, edits: Sequence[tuple[int, int, bytes]]) -> bytes:
    result = source
    limit = len(source)
    for start, end, replacement in sorted(edits, reverse=True):
        if not 0 <= start <= end <= limit:
            raise RuntimeIntegrityError("overlapping or out-of-range source edit")
        result = result[:start] + replacement + result[end:]
        limit = start
    return result


def _single_assignment(tree: ast.Module, name: str) -> ast.Assign:
    """The one top-level ``name = ...``; any other binding of ``name`` is unexpected."""
    matches = [
        node for node in tree.body
        if isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name
    ]
    if len(matches) != 1:
        raise RuntimeIntegrityError(f"expected exactly one top-level assignment of {name}")
    target = matches[0].targets[0]
    for node in ast.walk(tree):
        if node is target:
            continue
        rebinds = (
            (isinstance(node, ast.Name) and node.id == name
             and not isinstance(node.ctx, ast.Load))
            or (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == name)
            or (isinstance(node, ast.alias)
                and (node.asname or node.name.split(".")[0]) == name)
            or (isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names)
            or (isinstance(node, ast.ExceptHandler) and node.name == name)
        )
        if rebinds:
            raise RuntimeIntegrityError(f"{name} is bound somewhere other than its assignment")
    return matches[0]




class _Generated(NamedTuple):
    quota: bytes
    transport: bytes
    config: bytes


def _verify_reviewed_only(
    original: ast.Module, generated_source: bytes, names: Sequence[str], label: str
) -> dict[str, ast.Assign]:
    """Prove the generated module equals the original with only ``names`` re-valued."""
    generated = _parse_module(generated_source, label)
    replaced: dict[str, ast.Assign] = {}
    for name in names:
        replaced[name] = _single_assignment(generated, name)
        _single_assignment(original, name).value = replaced[name].value
    if ast.dump(original) != ast.dump(generated):
        raise RuntimeIntegrityError(f"{label}: generated source differs beyond reviewed changes")
    return replaced


def _generate(quota_source: bytes, transport_source: bytes) -> _Generated:
    """Pure, deterministic producer of the private quota/transport copies and config."""
    quota_tree = _parse_module(quota_source, "quota.py")
    routes = _single_assignment(quota_tree, "_ROUTES")
    if not isinstance(routes.value, ast.Set) or ast.literal_eval(routes.value) != OQ6_QUOTA_ROUTES:
        raise RuntimeIntegrityError("quota._ROUTES is not the reviewed OQ-6 literal set")
    start, end = _span(quota_source, routes.value)
    route_literal = "{" + ", ".join(json.dumps(route) for route in ROUTES) + "}"
    quota_out = _splice(quota_source, [(start, end, route_literal.encode("ascii"))])

    transport_tree = _parse_module(transport_source, "transport.py")
    config = _single_assignment(transport_tree, "CONTROLLED_CONFIG_TEXT")
    contract = _single_assignment(transport_tree, "CONTRACT_PATH")
    old_text = config.value.value if isinstance(config.value, ast.Constant) else None
    if (
        not isinstance(old_text, str) or not old_text.endswith("\n")
        or "compaction" in old_text or "'''" in old_text or "\\" in old_text
    ):
        raise RuntimeIntegrityError("transport.CONTROLLED_CONFIG_TEXT is not the reviewed literal")
    config_start, config_end = _span(transport_source, config.value)
    segment = transport_source[config_start:config_end]
    if not (segment.startswith(b"'''") and segment.endswith(b"'''") and len(segment) >= 6):
        raise RuntimeIntegrityError("transport.CONTROLLED_CONFIG_TEXT is not a ''' literal")
    expected_contract = ast.parse(_ORIGINAL_CONTRACT_EXPRESSION, mode="eval").body
    if ast.dump(contract.value) != ast.dump(expected_contract):
        raise RuntimeIntegrityError("transport.CONTRACT_PATH is not ROOT / transport-contract.json")
    new_text = old_text + COMPACTION_BLOCK
    transport_out = _splice(transport_source, [
        (config_start, config_end, ("'''" + new_text + "'''").encode("utf-8")),
    ])

    generated_quota = _verify_reviewed_only(quota_tree, quota_out, ["_ROUTES"], "oq10_quota.py")
    generated_transport = _verify_reviewed_only(
        transport_tree, transport_out, ["CONTROLLED_CONFIG_TEXT"], "oq10_transport.py",
    )
    if ast.literal_eval(generated_quota["_ROUTES"].value) != set(ROUTES):
        raise RuntimeIntegrityError("generated quota route set is not the OQ-10 route set")
    if ast.literal_eval(generated_transport["CONTROLLED_CONFIG_TEXT"].value) != new_text:
        raise RuntimeIntegrityError("generated controlled config is not the reviewed profile")
    return _Generated(quota_out, transport_out, new_text.encode("utf-8"))


# --------------------------------------------------------------------------- #
# Runtime producer / loader
# --------------------------------------------------------------------------- #


def _producer_pin() -> dict[str, str]:
    path = Path(__file__).resolve()
    return _pin(path, path.read_bytes())


def prepare_runtime(
    source_quota: Path | str,
    quota_sha256: str,
    source_transport: Path | str,
    transport_sha256: str,
    output_directory: Path | str,
) -> dict[str, Any]:
    """Generate (or byte-verify) the private OQ-10 copies and return the pin manifest.

    Originals are read only after their SHA-256 matches. Existing generated files are
    never overwritten: a byte-identical reread is accepted, anything else raises.
    Pin the returned manifest (and this module) before any hydration or model call.
    """
    quota_path = Path(source_quota).resolve()
    transport_path = Path(source_transport).resolve()
    output = Path(output_directory).resolve()
    if output in (quota_path.parent, transport_path.parent):
        raise RuntimeIntegrityError("generated copies must not be written beside the originals")
    quota_bytes = _checked_read(quota_path, _hex(quota_sha256, "quota_sha256"), "source quota")
    transport_bytes = _checked_read(
        transport_path, _hex(transport_sha256, "transport_sha256"), "source transport"
    )
    contract_path = transport_path.parent / CONTRACT_NAME
    contract_bytes = _read_regular(contract_path, "transport contract")
    attestation_path = _attestation_path(contract_bytes)
    attestation_bytes = _read_regular(attestation_path, "session attestation source")

    generated = _generate(quota_bytes, transport_bytes)

    output.mkdir(parents=True, exist_ok=True)
    quota_out = output / GENERATED_QUOTA_NAME
    transport_out = output / GENERATED_TRANSPORT_NAME
    config_out = output / GENERATED_CONFIG_NAME
    contract_out = output / CONTRACT_NAME
    _publish(quota_out, generated.quota)
    _publish(transport_out, generated.transport)
    _publish(config_out, generated.config)
    _publish(contract_out, contract_bytes)
    return {
        "schema": SCHEMA_RUNTIME,
        "recipe_version": RECIPE_VERSION,
        "producer": _producer_pin(),
        "originals": {
            "quota": _pin(quota_path, quota_bytes),
            "transport": _pin(transport_path, transport_bytes),
        },
        "dependencies": {
            "transport_contract": _pin(contract_path, contract_bytes),
            "session_attestation": _pin(attestation_path, attestation_bytes),
        },
        "generated": {
            "quota": _pin(quota_out, generated.quota),
            "transport": _pin(transport_out, generated.transport),
            "transport_contract": _pin(contract_out, contract_bytes),
        },
        "controlled_config": _pin(config_out, generated.config),
        "routes": list(ROUTES),
        "route_context_tokens": dict(ROUTE_CONTEXT_TOKENS),
        "reviewed_changes": list(REVIEWED_CHANGES),
        "compaction": {
            "setting": "compaction.enabled",
            "value": False,
            "source": COMPACTION_SETTING_SOURCE,
        },
    }


class _Loaded(NamedTuple):
    quota: ModuleType
    transport: ModuleType
    config_path: Path
    config_sha256: str
    contract_path: Path
    contract_sha256: str
    attestation_path: Path
    attestation_sha256: str
    transport_path: Path
    transport_sha256: str
    quota_sha256: str


def _section(manifest: Mapping[str, Any], key: str, keys: frozenset[str]) -> Mapping[str, Any]:
    section = manifest[key]
    if not isinstance(section, Mapping) or set(section) != keys:
        raise RuntimeIntegrityError(f"manifest.{key} has unexpected keys")
    return section


def _manifest_pin(section: Any, label: str) -> tuple[Path, str]:
    if not isinstance(section, Mapping) or set(section) != {"path", "sha256"}:
        raise RuntimeIntegrityError(f"manifest {label} pin must be exactly path and sha256")
    text = section["path"]
    if not isinstance(text, str) or not Path(text).is_absolute():
        raise RuntimeIntegrityError(f"manifest {label} path must be absolute")
    return Path(text), _hex(section["sha256"], f"manifest {label} sha256")


def _exec_module(name: str, path: Path, source: bytes) -> ModuleType:
    # Not registered in sys.modules; compiling from the hash-checked bytes avoids both
    # source re-reads and bytecode caches next to the pinned files.
    module = ModuleType(name)
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec", dont_inherit=True), module.__dict__)
    return module


def _load(manifest: Mapping[str, Any]) -> _Loaded:
    if not isinstance(manifest, Mapping) or set(manifest) != _MANIFEST_KEYS:
        raise RuntimeIntegrityError("runtime manifest has unexpected keys")
    if manifest["schema"] != SCHEMA_RUNTIME or manifest["recipe_version"] != RECIPE_VERSION:
        raise RuntimeIntegrityError("runtime manifest schema or recipe version differs")
    if list(manifest["routes"]) != list(ROUTES):
        raise RuntimeIntegrityError("manifest routes are not the three permitted OQ-10 routes")
    if dict(manifest["route_context_tokens"]) != ROUTE_CONTEXT_TOKENS:
        raise RuntimeIntegrityError("manifest route context limits differ")
    if list(manifest["reviewed_changes"]) != list(REVIEWED_CHANGES):
        raise RuntimeIntegrityError("manifest reviewed-change list differs")
    compaction = manifest["compaction"]
    if compaction != {
        "setting": "compaction.enabled", "value": False, "source": COMPACTION_SETTING_SOURCE,
    }:
        raise RuntimeIntegrityError("manifest compaction control differs")

    # The producer may be relocated for integration; its bytes may not change.
    _, producer_sha = _manifest_pin(manifest["producer"], "producer")
    if _sha(Path(__file__).resolve().read_bytes()) != producer_sha:
        raise RuntimeIntegrityError("this producer module differs from its pinned source")

    originals = _section(manifest, "originals", frozenset({"quota", "transport"}))
    dependencies = _section(
        manifest, "dependencies", frozenset({"transport_contract", "session_attestation"})
    )
    generated = _section(
        manifest, "generated", frozenset({"quota", "transport", "transport_contract"})
    )

    quota_path, quota_sha = _manifest_pin(originals["quota"], "original quota")
    transport_path, transport_sha = _manifest_pin(originals["transport"], "original transport")
    contract_path, contract_sha = _manifest_pin(
        dependencies["transport_contract"], "transport contract"
    )
    attestation_path, attestation_sha = _manifest_pin(
        dependencies["session_attestation"], "session attestation"
    )
    generated_quota_path, generated_quota_sha = _manifest_pin(generated["quota"], "generated quota")
    generated_transport_path, generated_transport_sha = _manifest_pin(
        generated["transport"], "generated transport"
    )
    copied_contract_path, copied_contract_sha = _manifest_pin(
        generated["transport_contract"], "copied transport contract"
    )
    config_path, config_sha = _manifest_pin(manifest["controlled_config"], "controlled config")

    quota_bytes = _checked_read(quota_path, quota_sha, "original quota")
    transport_bytes = _checked_read(transport_path, transport_sha, "original transport")
    contract_bytes = _checked_read(contract_path, contract_sha, "transport contract")
    _checked_read(attestation_path, attestation_sha, "session attestation source")
    if contract_path != transport_path.parent / CONTRACT_NAME:
        raise RuntimeIntegrityError("contract pin is not the original transport's CONTRACT_PATH")
    if _attestation_path(contract_bytes) != attestation_path:
        raise RuntimeIntegrityError("contract session_attestation differs from its pin")

    for path in (generated_quota_path, generated_transport_path, config_path, copied_contract_path):
        if path.is_symlink():
            raise RuntimeIntegrityError(f"generated file is a symlink: {path}")
    generated_quota = _checked_read(generated_quota_path, generated_quota_sha, "generated quota")
    generated_transport = _checked_read(
        generated_transport_path, generated_transport_sha, "generated transport"
    )
    config_bytes = _checked_read(config_path, config_sha, "controlled config")
    copied_contract_bytes = _checked_read(
        copied_contract_path, copied_contract_sha, "copied transport contract"
    )
    if (
        copied_contract_bytes != contract_bytes
        or copied_contract_path != generated_transport_path.parent / CONTRACT_NAME
    ):
        raise RuntimeIntegrityError("private transport contract is not the byte-identical sibling")

    expected = _generate(quota_bytes, transport_bytes)
    if (
        expected.quota != generated_quota or expected.transport != generated_transport
        or expected.config != config_bytes
    ):
        raise RuntimeIntegrityError("generated files are not the recipe output of the originals")

    quota = _exec_module(f"oq10_quota_{generated_quota_sha[:12]}", generated_quota_path,
                         generated_quota)
    transport = _exec_module(f"oq10_transport_{generated_transport_sha[:12]}",
                             generated_transport_path, generated_transport)
    if getattr(quota, "_ROUTES", None) != set(ROUTES):
        raise RuntimeIntegrityError("loaded quota route set is not the OQ-10 route set")
    if getattr(transport, "CONTROLLED_CONFIG_TEXT", None) != config_bytes.decode("utf-8"):
        raise RuntimeIntegrityError("loaded transport config profile differs from the config pin")
    if getattr(transport, "CONTRACT_PATH", None) != copied_contract_path:
        raise RuntimeIntegrityError("loaded transport CONTRACT_PATH differs from its pin")
    owned = getattr(transport, "OWNED_ARTIFACTS", None)
    if not isinstance(owned, tuple) or not all(isinstance(name, str) for name in owned):
        raise RuntimeIntegrityError("loaded transport has no OWNED_ARTIFACTS tuple")
    if not callable(getattr(quota, "admit_route", None)) \
            or not callable(getattr(transport, "call", None)):
        raise RuntimeIntegrityError("loaded modules lack admit_route/call")
    return _Loaded(
        quota=quota, transport=transport, config_path=config_path, config_sha256=config_sha,
        contract_path=copied_contract_path, contract_sha256=contract_sha,
        attestation_path=attestation_path, attestation_sha256=attestation_sha,
        transport_path=generated_transport_path, transport_sha256=generated_transport_sha,
        quota_sha256=generated_quota_sha,
    )


def load_runtime(manifest: Mapping[str, Any]) -> tuple[ModuleType, ModuleType]:
    """Validate every manifest pin, regenerate and compare, then load the two modules.

    There is no fallback to the original modules: any drift raises RuntimeIntegrityError.
    """
    loaded = _load(manifest)
    return loaded.quota, loaded.transport


# --------------------------------------------------------------------------- #
# One native role call
# --------------------------------------------------------------------------- #


def _input_window(
    route: str, prompt: str, system: str, estimate_tokens: Callable[[str, str], int] | None
) -> dict[str, Any]:
    limit = ROUTE_CONTEXT_TOKENS[route]
    if estimate_tokens is None:
        return {
            "performed": False, "estimated_tokens": None, "limit_tokens": limit,
            "within_limit": None,
        }
    estimate = estimate_tokens(prompt, system)
    if isinstance(estimate, bool) or not isinstance(estimate, int) or estimate < 0:
        raise ValueError("token estimate must be a nonnegative integer")
    return {
        "performed": True, "estimated_tokens": estimate, "limit_tokens": limit,
        "within_limit": estimate <= limit,
    }


def _admission_summary(record: Any, route: str) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise TypeError("admission function must return a receipt mapping")
    reasons = record.get("reasons")
    codes = [
        str(reason.get("code")) for reason in reasons
        if isinstance(reason, Mapping)
    ] if isinstance(reasons, list) else []
    eligible = (
        record.get("eligible") is True and record.get("blocked") is False
        and record.get("status") == "eligible" and record.get("route") == route
    )
    return {
        "eligible": eligible,
        "status": record.get("status"),
        "route": record.get("route"),
        "reason_codes": codes,
        "receipt_path": record.get("receipt_path"),
        "fetchedAt": record.get("fetchedAt"),
        "checked_at_ms": record.get("checked_at_ms"),
        "pool_values": record.get("pool_values"),
    }


def _pin_mismatches(receipt: Mapping[str, Any], loaded: _Loaded) -> list[str]:
    required = {
        "config_sha256": loaded.config_sha256,
        "contract_sha256": loaded.contract_sha256,
        "attestation_source_sha256": loaded.attestation_sha256,
    }
    optional = {
        "controller_sha256": loaded.transport_sha256,
        "controller_path": str(loaded.transport_path),
        "contract_path": str(loaded.contract_path),
        "attestation_source": str(loaded.attestation_path),
    }
    bad = [key for key, value in required.items() if receipt.get(key) != value]
    bad += [key for key, value in optional.items() if key in receipt and receipt[key] != value]
    return sorted(bad)


def _answer_problem(receipt: Mapping[str, Any], route: str) -> str | None:
    if receipt.get("served_identities") != [route]:
        return "served_identity_mismatch"
    if receipt.get("isolation_valid") is not True:
        return "isolation_invalid"
    if receipt.get("native_tool_inventory_proven_empty") is not True:
        return "tool_inventory_not_proven_empty"
    if receipt.get("model_fallback") is not False or receipt.get("retries") != 0:
        return "fallback_or_retry_observed"
    if receipt.get("errors"):
        return "receipt_errors_on_answer"
    answer = receipt.get("answer")
    if not isinstance(answer, Mapping) or not isinstance(answer.get(EXPECTED_ARRAY), list):
        return "answer_without_findings_array"
    return None


def _result(
    loaded: _Loaded, directory: Path, route: str, *, disposition: str, reason: str | None,
    sent: bool, terminal: bool, reread: bool, window: dict[str, Any] | None,
    admission: dict[str, Any] | None, admission_directory: Path | None,
    receipt: Mapping[str, Any] | None = None, answer: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    receipt_file = directory / "receipt.json"
    evidence = {name: (receipt.get(name) if receipt is not None else None)
                for name in _EVIDENCE_FIELDS}
    findings = answer.get(EXPECTED_ARRAY) if answer is not None else None
    return {
        "schema": SCHEMA_RESULT,
        "route": route,
        "directory": str(directory),
        "disposition": disposition,
        "reason": reason,
        "sent": sent,
        "attempt_consumed": sent,
        "terminal": terminal,
        "reread": reread,
        "answer": dict(answer) if answer is not None else None,
        "findings_count": len(findings) if isinstance(findings, list) else None,
        "evidence": evidence,
        "receipt": dict(receipt) if receipt is not None else None,
        "receipt_file_sha256": _sha(receipt_file.read_bytes()) if receipt_file.is_file() else None,
        "input_window": window,
        "admission": admission,
        "admission_directory": str(admission_directory) if admission_directory else None,
        "runtime": {
            "quota_sha256": loaded.quota_sha256,
            "transport_sha256": loaded.transport_sha256,
            "config_sha256": loaded.config_sha256,
            "contract_sha256": loaded.contract_sha256,
            "attestation_sha256": loaded.attestation_sha256,
        },
    }


def run_native(
    directory: Path | str,
    *,
    route: str,
    prompt: str,
    system: str,
    runtime_manifest: Mapping[str, Any],
    timeout_seconds: int = 180,
    admission_function: Callable[..., Mapping[str, Any]] | None = None,
    call_function: Callable[..., Mapping[str, Any]] | None = None,
    estimate_tokens: Callable[[str, str], int] | None = None,
) -> dict[str, Any]:
    """Run one exactly-routed native role call, at most once per ``directory``.

    Dispositions: ``answered`` (attested, isolation-proven findings container),
    ``missing`` (oversize before send, or a consumed attempt that did not answer; always
    terminal) and ``quota_pending`` (fresh admission closed; nothing sent or consumed,
    ``terminal`` false). A directory that already holds a receipt or any transport-owned
    artifact is never re-admitted or re-issued: ``transport.call`` rereads it.

    ``estimate_tokens(prompt, system)`` is the caller's bound on everything that will
    occupy the window (system, prompt, wrapper, reserved output). Above the route's known
    limit the role is missing before send; the input is never rewritten or shortened.
    Without it the result records ``input_window.performed = false``.
    """
    if not isinstance(route, str) or route not in ROUTES:
        raise ValueError(f"route must be exactly one of {list(ROUTES)}")
    if not isinstance(prompt, str) or not isinstance(system, str):
        raise ValueError("prompt and system must be strings")
    if (
        isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int)
        or not 1 <= timeout_seconds <= 600
    ):
        raise ValueError("timeout_seconds must be an integer between 1 and 600")
    loaded = _load(runtime_manifest)
    directory = Path(directory).resolve()
    admit = admission_function if admission_function is not None else loaded.quota.admit_route
    call = call_function if call_function is not None else loaded.transport.call
    marker = directory / "call-consumed.json"
    prior = (directory / "receipt.json").exists() or any(
        (directory / name).exists() for name in loaded.transport.OWNED_ARTIFACTS
    )

    window: dict[str, Any] | None = None
    admission: dict[str, Any] | None = None
    admission_directory: Path | None = None
    if not prior:
        window = _input_window(route, prompt, system, estimate_tokens)
        if window["within_limit"] is False:
            return _result(
                loaded, directory, route, disposition="missing",
                reason=(
                    f"input_window_exceeded:{window['estimated_tokens']}>{window['limit_tokens']}"
                ),
                sent=False, terminal=True, reread=False, window=window, admission=None,
                admission_directory=None,
            )
        admission_directory = directory.parent / (directory.name + ".quota-admissions")
        admission = _admission_summary(admit(route, output_directory=admission_directory), route)
        if not admission["eligible"]:
            codes = ",".join(admission["reason_codes"]) or "blocked"
            return _result(
                loaded, directory, route, disposition="quota_pending",
                reason="quota_pending:" + codes, sent=False, terminal=False, reread=False,
                window=window, admission=admission, admission_directory=admission_directory,
            )

    try:
        receipt = call(
            directory, route=route, prompt=prompt, system=system,
            expected_array=EXPECTED_ARRAY, config=loaded.config_path,
            config_sha256=loaded.config_sha256, timeout_seconds=timeout_seconds,
        )
    except Exception as error:
        if prior or not marker.is_file():
            raise  # nothing was consumed by this call: a caller/programming error
        return _result(
            loaded, directory, route, disposition="missing",
            reason="transport_exception:" + type(error).__name__, sent=True, terminal=True,
            reread=False, window=window, admission=admission,
            admission_directory=admission_directory,
        )
    if not isinstance(receipt, Mapping):
        raise TypeError("call function must return a receipt mapping")

    status = receipt.get("status")
    consumed = receipt.get("attempt_consumed") is True or marker.is_file()
    common: dict[str, Any] = {
        "sent": consumed, "terminal": True, "reread": prior, "window": window,
        "admission": admission, "admission_directory": admission_directory, "receipt": receipt,
    }
    mismatches = _pin_mismatches(receipt, loaded)
    if mismatches:
        return _result(
            loaded, directory, route, disposition="missing",
            reason="receipt_pin_mismatch:" + ",".join(mismatches), **common,
        )
    if status not in _RECEIPT_STATUSES:
        return _result(
            loaded, directory, route, disposition="missing", reason="malformed_receipt_status",
            **common,
        )
    if status != "answered":
        reason = receipt.get("error")
        return _result(
            loaded, directory, route, disposition="missing",
            reason=reason if isinstance(reason, str) and reason else str(status), **common,
        )
    problem = _answer_problem(receipt, route)
    if problem is not None:
        return _result(loaded, directory, route, disposition="missing", reason=problem, **common)
    return _result(
        loaded, directory, route, disposition="answered", reason=None,
        answer=receipt["answer"], **common,
    )


# --------------------------------------------------------------------------- #
# Paired gate flags
# --------------------------------------------------------------------------- #


def _missing_flags(reason: str) -> dict[str, Any]:
    return {
        "status": "missing", "reason": reason, OLD_GATE: None, NEW_GATE: None,
        "memberships": None,
    }


def _valid_confidence(value: Any) -> bool:
    return (
        not isinstance(value, bool) and isinstance(value, (int, float))
        and math.isfinite(value) and 0 <= value <= 1
    )


def _memberships_from_eligibility(
    payloads: Sequence[Any], eligibility_function: Callable[[float, bool], bool]
) -> dict[str, list[str]]:
    old: list[str] = []
    new: list[str] = []
    seen: set[str] = set()
    for index, row in enumerate(payloads, 1):
        if not isinstance(row, Mapping):
            raise ValueError("each parsed candidate must be an object")
        original_id = row.get("candidate_id")
        if not isinstance(original_id, str) or not original_id.strip() or original_id in seen:
            raise ValueError("candidate_id must be a unique nonblank string")
        seen.add(original_id)
        confidence = row.get("confidence")
        if not _valid_confidence(confidence):
            raise ValueError("candidate confidence must be a finite number in [0, 1]")
        verified = row.get("citation_verified")
        if type(verified) is not bool:
            raise ValueError("citation_verified must be an actual parser verification boolean")
        citations = row.get("citations")
        if not isinstance(citations, (list, tuple)) or not citations:
            raise ValueError("parsed candidates must retain their citations")
        ordinal = f"C{index}"
        old_eligible = verified is True and confidence >= OLD_GATE_FLOOR
        new_eligible = eligibility_function(confidence, verified)
        if type(new_eligible) is not bool:
            raise ValueError("eligibility function must return a bool")
        if old_eligible:
            old.append(ordinal)
        if new_eligible:
            new.append(ordinal)
    return {OLD_GATE: old, NEW_GATE: new}


def export_flags(
    parsed_candidate_payloads: Any,
    *,
    parse_status: str,
    paired_module: Any = None,
    eligibility_function: Callable[[float, bool], bool] | None = None,
    raw_findings_count: int | None = None,
) -> dict[str, Any]:
    """Task-level paired gate flags: ``True``/``False`` from a parsed array, else ``None``.

    Exactly one policy source must be injected: a pinned ``paired`` module
    (``GATES`` + ``gate_memberships``) or the production ``is_proposal_eligible``.
    There is no hidden import. Only the parser's ``citation_verified`` boolean and
    ``confidence`` are read; model ``verified``/``supported`` keys are ignored. Old gate:
    ``citation_verified is True and confidence >= 0.8``. New gate: the injected production
    function. ``parse_status`` must be exactly ``"ok"`` (the production parser accepted the
    native answer); native failure, schema failure, partial parse (``raw_findings_count``
    differs from the parsed rows), or invalid rows are unknown, never ``False``. A valid
    empty findings array is ``False`` for both gates.
    """
    if (paired_module is None) == (eligibility_function is None):
        raise ValueError("inject exactly one of paired_module or eligibility_function")
    if paired_module is not None and tuple(getattr(paired_module, "GATES", ())) != (
        OLD_GATE, NEW_GATE,
    ):
        raise ValueError("paired module GATES differ from the OQ-10 gate names")
    if not isinstance(parse_status, str):
        return _missing_flags("parse_status_not_a_string")
    if parse_status != PARSE_OK:
        return _missing_flags("parse_status:" + parse_status)
    if not isinstance(parsed_candidate_payloads, (list, tuple)):
        return _missing_flags("parsed_payloads_not_an_array")
    if raw_findings_count is not None and (
        isinstance(raw_findings_count, bool) or not isinstance(raw_findings_count, int)
        or raw_findings_count < 0
    ):
        raise ValueError("raw_findings_count must be a nonnegative integer or None")
    if raw_findings_count is not None and raw_findings_count != len(parsed_candidate_payloads):
        return _missing_flags("partial_parse")
    try:
        if paired_module is not None:
            memberships = paired_module.gate_memberships(parsed_candidate_payloads)
        elif eligibility_function is not None:
            memberships = _memberships_from_eligibility(
                parsed_candidate_payloads, eligibility_function
            )
        else:
            raise RuntimeError("no policy source injected")
    except (ValueError, TypeError, KeyError) as error:
        return _missing_flags("invalid_parsed_payloads:" + type(error).__name__ + ":" + str(error))
    if not isinstance(memberships, Mapping) or any(
        not isinstance(memberships.get(gate), list) for gate in (OLD_GATE, NEW_GATE)
    ):
        return _missing_flags("gate_memberships_malformed")
    return {
        "status": "ok",
        "reason": None,
        OLD_GATE: len(memberships[OLD_GATE]) > 0,
        NEW_GATE: len(memberships[NEW_GATE]) > 0,
        "memberships": {gate: list(memberships[gate]) for gate in (OLD_GATE, NEW_GATE)},
    }
