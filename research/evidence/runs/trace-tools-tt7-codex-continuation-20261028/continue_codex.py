"""Resumable, fail-closed Luna-only continuation of the frozen TT-7 whole-run form.

Predeclared whole-200 arm: ``openai-codex/gpt-6-luna`` over the unchanged
sample, in unchanged file order, via the frozen BASE ``run_omp.read_one``. The
two historical Luna cells are terminal and never rerun, relabeled or rewritten.
Their financial basis stays UNKNOWN. This runner reads no labels, calls no other
route, and keeps every actor byte, raw reply and real run id out of Git.

    continue_codex.py prepare                         # zero model calls
    continue_codex.py --entitlement PROOF.json run    # resumable; fixed concurrency 1
    continue_codex.py close                           # seal attempt.json once

``--entitlement`` is the parent-authored immutable JSON proof. It must hold, at
the top level and exactly: schema ``tt7.codex_entitlement/v1``; status
``verified_current_included_native_endpoint``; route
``openai-codex/gpt-6-luna``; auth_class ``chatgpt_oauth``; plan_type
``prolite``; api ``openai-codex-responses``; auth_source ``{kind: "oauth",
concrete: true}``; provider/model ``openai-codex``/``gpt-6-luna``; exactly one
OAuth account; zero credential/raw-auth reads, paid calls and model calls;
endpoint ``{protocol: "https:", origin: "https://chatgpt.com", pathname:
"/backend-api"}``; context_window 272000; max_tokens 128000; the frozen
sample_sha256; policy {quota_stop_fraction 0.9, quota_ttl_s 120,
maximum_concurrency 1, no_policy_refusal_retry, no_paid_fallback,
privacy_vetoes 4, fixed order over 200 ids}; legacy Luna basis UNKNOWN;
omp_version equal to ``omp --version``; account_identity_sha256 (64 hex). It
may carry provenance fields but no credential-shaped keys or secret-pattern
values. ``created_at`` (if present) must be under 24 h old.

Guards, all fail-closed: pinned BASE source hashes; pinned privacy audit of all
200 rendered inputs against the prior audit before any call; a NEW Codex-only
quota reader before EVERY actual call (the frozen refresh_usage skips every
tier-scoped window and judges freshness by the newest report, so it is not
used): at most two ``omp usage --provider openai-codex`` reads and one
``omp usage invalidate --provider openai-codex``; the single Codex report must be
fresh within 120 s, every window must declare provider/window/status/fraction,
and every Codex window must be below 90 %, else the whole route stops. Also the
canonical secret gate on each exact transmitted payload; no retry after an
anchored first-person refusal; one append-only spend row per actual attempt.
Concurrency is fixed at 1 (the proof's maximum_concurrency). A quota stop never
fabricates abstentions: the current cell is left non-terminal (resumable from
retained replies) and every later id stays explicitly unattempted. Terminal
cells and retained failed identities are never retried across invocations.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

OUT = Path(__file__).resolve().parent
BASE = OUT.with_name("trace-tools-tt7-continuation-20261028")
ORIGINAL = OUT.with_name("trace-tools-tt7-20261027")
NAMESPACE = "tt7-codex-continuation-20261028"
REFERENCE_NAMESPACE = "tt7-continuation-20261028"
PROVIDER = "openai-codex"
ROUTE = "openai-codex/gpt-6-luna"
DETECTOR = f"tt7_form:{ROUTE}"
RECEIPT = OUT / "attempt.json"
SCAN = str(RECEIPT)
STOP_FRACTION = 0.90
QUOTA_TTL_S = 120
EXPECTED_ORIGINAL_TERMINAL = 2
EXPECTED_PENDING = 198
EXPECTED_FLAGGED = 4
PROOF_MAX_AGE = timedelta(hours=24)
BASIS_INCLUDED = "included_chatgpt_plan_native_oauth_verified_entitlement"
BASIS_NONE = "no_transmission"
BASIS_UNKNOWN = "UNKNOWN_failed_call_possible_paid_or_denied"
INTERRUPTED = "interrupted attempt; outcome was never recorded and the attempt is not retried"
PINNED_SOURCES = (
    ("base_common.py", BASE / "common.py", "bdf0dd5c3afc29a2c81282f3b7dd8ad7a70b9f0de81a7d1c4db5aa90b37a9fea"),
    ("base_run_omp.py", BASE / "run_omp.py", "31b1aa04bae54158993d8b2a9365ca3c133005df5ed8024c157dd0853fb0bde0"),
    ("base_campaign.py", BASE / "campaign.py", "3c1582cd6f5a03187ad22332ca505452b2ba01d37d940a66c1ca7f79b58ecab6"),
    ("base_canonical_secret_gate.py", BASE / "canonical_secret_gate.py", "14b8489b4065955cdd7a91dce768738d54f3fc90a46bdfe0afb3197fa6420a4f"),
    ("base_score.py", BASE / "score.py", "d4299369ebfe9a671139fe6ad6fbba30f336fb32563a474fce66df2f072f7d25"),
    ("base_finalize.py", BASE / "finalize.py", "c7b563857813879a428431f3ce9667d1f301ea3ea387c5a785a1be050a2854c3"),
    ("base_score.json", BASE / "score.json", "61f55de784d2acdaef356ac361c7e04a05a66c548bbc9589a2050e58b195bd7f"),
    ("original_score.py", ORIGINAL / "score.py", "2325d447046b2cd9989f3bdab858126dbbbe689335a25bd5798c6a604986b440"),
)
REQUIRED_PROOF = {
    "schema": "tt7.codex_entitlement/v1",
    "status": "verified_current_included_native_endpoint",
    "route": ROUTE,
    "provider": PROVIDER,
    "model": "gpt-6-luna",
    "auth_class": "chatgpt_oauth",
    "auth_source": {"kind": "oauth", "concrete": True},
    "oauth_account_count": 1,
    "plan_type": "prolite",
    "api": "openai-codex-responses",
    "sample_sha256": "f5e1fcd0e7095fe409ad4e5a2d3ab6df42f8b07e4f4204641d8d1094d5271e0a",
    "credential_values_read": 0,
    "raw_auth_store_reads": 0,
    "paid_calls": 0,
    "model_calls": 0,
    "account_or_global_configuration_changes": 0,
    "labels_read": False,
    "legacy_luna_records": EXPECTED_ORIGINAL_TERMINAL,
}
REQUIRED_PROOF_POLICY = {
    "fixed_file_order": True, "fixed_sample_ids": 200, "maximum_concurrency": 1,
    "no_other_arm_or_model_selection": True, "no_paid_fallback": True, "no_policy_refusal_retry": True,
    "privacy_vetoes": EXPECTED_FLAGGED, "quota_stop_fraction": STOP_FRACTION, "quota_ttl_s": QUOTA_TTL_S,
}
EXPECTED_ENDPOINT = {"protocol": "https:", "origin": "https://chatgpt.com", "pathname": "/backend-api"}
FORBIDDEN_PROOF_KEYS = frozenset({
    "authorization", "cookie", "cookies", "password", "secret", "secrets", "apikey", "bearer", "credential",
    "credentials", "accesstoken", "refreshtoken", "idtoken", "authtoken", "oauthtoken", "sessiontoken", "token",
    "headers", "rawheaders",
})
REFUSAL = re.compile(
    r"^\W{0,3}(?:"
    r"(?:i['\u2019]?m|i\s+am)\s+(?:sorry|unable|not\s+able|afraid)"
    r"|(?:i|we)\s+(?:can['\u2019]?t|cannot|can\s+not|won['\u2019]?t|will\s+not|must\s+decline|have\s+to\s+decline)"
    r"|sorry"
    r"|unfortunately,?\s+i\s+(?:can['\u2019]?t|cannot|am\s+unable|won['\u2019]?t)"
    r"|i\s+(?:must\s+)?(?:decline|refuse)"
    r")\b[^\n]{0,200}?\b(?:help|assist|comply|provide|fulfil+|complete|continue|do\s+that|that\s+request"
    r"|this\s+request|with\s+(?:that|this)|analy[sz]e|review|evaluate|process)",
    re.IGNORECASE,
)
PAID_FAILURE = re.compile(
    r"(?i)\b(?:billing|credits?|payment|insufficient[_ ](?:quota|funds)|overage|pay[- ]as[- ]you[- ]go|top[- ]?up|invoice)\b"
    r"|\b402\b|api[_ ]?key"
)
QUOTA_FAILURE = re.compile(
    r"(?i)usage[_ ]limit|rate[_ ]limit|\bquota\b|\b429\b|too many requests|limit (?:has been )?reached"
    r"|exceeded (?:your|the) (?:current )?(?:usage|limit)"
)
PROVIDER_REFUSAL = re.compile(r"(?i)refus|content[_ ]filter|policy|safety")
REFUSAL_KIND = re.compile(r"(?i)refus|content[_ -]?filter|content[_ -]?policy|policy[_ -]?violation")


class StopRoute(Exception):
    """Whole-route stop. Deliberately NOT a RouteError/ValueError/OSError, so the
    frozen read_one cannot convert it into a synthetic terminal abstention."""


# --------------------------------------------------------------------------- pure helpers


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def exclusive_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")


def anchored_refusal(text: str) -> bool:
    return REFUSAL.match(text.lstrip()) is not None


def declared_refusal(value: Any, form_keys: frozenset[str]) -> bool:
    """Explicit refusal declared by a JSON reply; a canonical-shaped form is never a refusal."""
    if isinstance(value, str):
        return anchored_refusal(value)
    if not isinstance(value, dict) or frozenset(value) == form_keys:
        return False
    if value.get("refusal"):
        return True
    for key in ("type", "code", "status", "stop_reason", "stopReason", "finish_reason"):
        if isinstance(value.get(key), str) and REFUSAL_KIND.search(value[key]):
            return True
    for key in ("error", "message", "reason", "detail"):
        nested = value.get(key)
        if isinstance(nested, (str, dict)) and declared_refusal(nested, form_keys):
            return True
    return False


def refusal_reply(text: str, form_keys: frozenset[str] = frozenset()) -> bool:
    """Anchored first-person refusal in prose, or an explicit refusal declared in non-canonical JSON."""
    try:
        value = json.loads(text)
    except ValueError:
        return anchored_refusal(text)
    return declared_refusal(value, form_keys)


def failure_class(text: str) -> str:
    if INTERRUPTED in text:
        return "interrupted_outcome_unknown"
    if PAID_FAILURE.search(text):
        return "paid_basis_indication"
    if QUOTA_FAILURE.search(text):
        return "provider_quota_or_rate_limit"
    return "other"


def abstention_class(reason: str, error_classes: set[str]) -> str:
    if "privacy_scan_rejected" in reason:
        return "privacy_scan_rejected"
    if "explicit_policy_refusal" in reason:
        return "explicit_policy_refusal"
    if "paid_basis_indication" in error_classes:
        return "paid_basis_unknown_failure"
    if "provider_quota_or_rate_limit" in error_classes:
        return "provider_quota_or_rate_limit"
    if "stopReason=" in reason and PROVIDER_REFUSAL.search(reason):
        return "provider_refusal"
    if reason.startswith(("RouteError:", "OSError:", "ValueError:", "SubprocessError:")):
        return "transport_error"
    return "schema_or_evidence"


def walk_keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [str(key) for key in value] + [name for item in value.values() for name in walk_keys(item)]
    if isinstance(value, list):
        return [name for item in value for name in walk_keys(item)]
    return []


def validate_proof(raw: bytes, observed_version: str, secret_hits: Any, now: datetime) -> tuple[dict[str, Any], str]:
    """Return (proof, sha256) only for the exact admitted native included route."""
    proof = json.loads(raw)
    if not isinstance(proof, dict):
        raise ValueError("entitlement proof must be a JSON object")
    for key, expected in REQUIRED_PROOF.items():
        if proof.get(key) != expected:
            raise ValueError(f"entitlement proof field {key!r} does not match the admitted route/auth/plan")
    if proof.get("endpoint") != EXPECTED_ENDPOINT:
        raise ValueError("entitlement proof endpoint is not the exact native ChatGPT backend")
    policy = proof.get("policy")
    if not isinstance(policy, dict) or any(policy.get(key) != value for key, value in REQUIRED_PROOF_POLICY.items()):
        raise ValueError("entitlement proof policy differs from the predeclared stop/retry/concurrency policy")
    if not str(proof.get("legacy_luna_financial_basis", "")).startswith("UNKNOWN"):
        raise ValueError("entitlement proof must keep the original two-cell Luna financial basis UNKNOWN")
    identity = proof.get("account_identity_sha256")
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise ValueError("entitlement proof lacks a sha256 account identity")
    if observed_version != proof.get("omp_version"):
        raise ValueError("entitlement proof omp_version differs from the installed omp")
    for key in ("context_window", "contextWindow"):
        if key in proof and proof[key] != 272_000:
            raise ValueError("entitlement proof context window differs from the frozen 272000")
    for key in ("max_tokens", "maxTokens"):
        if key in proof and proof[key] != 128_000:
            raise ValueError("entitlement proof max tokens differs from 128000")
    for name in walk_keys(proof):
        if re.sub(r"[^a-z0-9]", "", name.lower()) in FORBIDDEN_PROOF_KEYS:
            raise ValueError("entitlement proof carries a credential-shaped key")
    if secret_hits(proof):
        raise ValueError("entitlement proof trips the canonical secret gate")
    for key in ("verified_at", "generated_at", "created_at"):
        if key in proof:
            stamp = datetime.fromisoformat(str(proof[key]).replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp > now + timedelta(minutes=5) or now - stamp > PROOF_MAX_AGE:
                raise ValueError(f"entitlement proof {key} is not a current UTC time within 24 hours")
    return proof, digest(raw)


class UsageStale(ValueError):
    """Telemetry is absent or stale; one provider-scoped invalidation may cure it."""


class UsageUnknown(ValueError):
    """Telemetry is present but ambiguous or malformed; no headroom is inferred."""


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def plan_names(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if re.sub(r"[^a-z]", "", str(key).lower()) in {"plan", "plantype"} and isinstance(item, str):
                found.add(item)
            found |= plan_names(item)
    return found


def codex_usage_snapshot(raw: Any, now_ms: float) -> dict[str, Any]:
    """Validate every Codex window; no tier/model window is ignored and no 5h window is invented."""
    if not isinstance(raw, dict) or not isinstance(raw.get("reports"), list):
        raise UsageUnknown("usage output has no reports list")
    if any(not isinstance(row, dict) for row in raw["reports"]):
        raise UsageUnknown("usage output has a malformed report")
    reports = [row for row in raw["reports"] if row.get("provider") == PROVIDER]
    if not reports:
        raise UsageStale("no Codex usage report")
    if len(reports) != 1:
        raise UsageUnknown("expected exactly one Codex OAuth usage report; account ambiguity")
    for key in ("accountsWithoutUsage", "disabledCredentials"):
        for entry in raw.get(key) or []:
            if not isinstance(entry, dict) or entry.get("provider") in (PROVIDER, None):
                raise UsageUnknown(f"a Codex or unidentified account is listed in {key}")
    report = reports[0]
    fetched = report.get("fetchedAt")
    if not is_number(fetched):
        raise UsageUnknown("report fetchedAt is missing")
    age_ms = now_ms - fetched
    if age_ms > QUOTA_TTL_S * 1000:
        raise UsageStale(f"usage report older than {QUOTA_TTL_S} seconds")
    if age_ms < -5000:
        raise UsageUnknown("report fetchedAt is in the future")
    if report.get("error") or report.get("status") not in (None, "ok"):
        raise UsageUnknown("usage report carries an error or non-ok status")
    plans = plan_names(report)
    if plans != {REQUIRED_PROOF["plan_type"]}:
        raise UsageUnknown("usage report plan is missing, ambiguous or differs from the admitted plan")
    metadata = report.get("metadata")
    meters = metadata.get("meterStates") if isinstance(metadata, dict) else None
    chat = meters.get("chat") if isinstance(meters, dict) else None
    if not isinstance(chat, dict) or chat.get("allowed") is not True or chat.get("limitReached") is not False:
        raise UsageUnknown("Luna chat-meter entitlement is missing, ambiguous or denied")
    limits = report.get("limits")
    if not isinstance(limits, list) or not limits:
        raise UsageStale("usage report has no windows")
    rows = []
    for limit in limits:
        if not isinstance(limit, dict):
            raise UsageUnknown("malformed usage window")
        scope, window, amount = limit.get("scope"), limit.get("window"), limit.get("amount")
        if not isinstance(scope, dict) or scope.get("provider") != PROVIDER:
            raise UsageUnknown("window scope does not declare the Codex provider")
        if (not isinstance(window, dict) or not isinstance(window.get("id"), str) or not window["id"]
                or not is_number(window.get("durationMs")) or window["durationMs"] <= 0
                or not is_number(window.get("resetsAt")) or window["resetsAt"] <= now_ms):
            raise UsageUnknown("window id/duration/reset is missing or ambiguous")
        if "windowId" in scope and scope["windowId"] != window["id"]:
            raise UsageUnknown("window scope and window id disagree")
        if not isinstance(limit.get("id"), str) or not limit["id"] or limit.get("status") != "ok":
            raise UsageUnknown("window id missing or status is not ok")
        if not isinstance(amount, dict) or not is_number(amount.get("usedFraction")) or not 0 <= amount["usedFraction"] <= 1:
            raise UsageUnknown("window usedFraction is missing or invalid")
        used, cap = amount.get("used"), amount.get("limit")
        if is_number(used) and is_number(cap) and cap > 0 and abs(used / cap - amount["usedFraction"]) > 0.011:
            raise UsageUnknown("window usedFraction disagrees with used/limit")
        rows.append({"id": limit["id"], "window_id": window["id"], "used_fraction": amount["usedFraction"],
                     "duration_ms": window["durationMs"], "resets_at_ms": window["resetsAt"],
                     "scope_keys": sorted(scope)})
    return {"provider": PROVIDER, "fetched_at_ms": [fetched], "oldest_age_s": round(age_ms / 1000, 3),
            "plan_type_observed": sorted(plans)[0] if plans else None, "limits": rows,
            "maximum_fraction": max(row["used_fraction"] for row in rows)}


def read_codex_usage(env: dict[str, str], run: Any = None, clock: Any = None) -> dict[str, Any]:
    """Original bound: two reads and at most one provider-scoped invalidation."""
    run = run or subprocess.run
    clock = clock or time.time
    for attempt in range(2):
        proc = run(["omp", "usage", "--provider", PROVIDER, "--json", "--redact", "--no-extensions"],
                   capture_output=True, text=True, timeout=90, env=env, check=True)
        try:
            return codex_usage_snapshot(json.loads(proc.stdout), clock() * 1000)
        except UsageStale:
            if attempt:
                raise
            run(["omp", "usage", "invalidate", "--provider", PROVIDER],
                capture_output=True, text=True, timeout=60, env=env, check=True)
    raise UsageStale("usage telemetry stayed stale after one provider-scoped invalidation")


# --------------------------------------------------------------------------- frozen BASE binding

_BOUND: SimpleNamespace | None = None


def verify_sources() -> dict[str, str]:
    found = {}
    for label, path, expected in PINNED_SOURCES:
        actual = digest(path.read_bytes())
        if actual != expected:
            raise ValueError(f"frozen source changed: {label}")
        found[label] = actual
    return found


def load_module(name: str, path: Path) -> Any:
    existing = sys.modules.get(name)
    if existing is not None:
        if Path(getattr(existing, "__file__", "") or "").resolve() != path.resolve():
            raise ImportError(f"module name {name!r} already bound to another file")
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def bind() -> SimpleNamespace:
    """Import the pinned BASE modules by explicit sibling path; never their main/prepare/close."""
    global _BOUND
    if _BOUND is None:
        verify_sources()
        os.environ["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
        common = load_module("common", BASE / "common.py")
        run_omp = load_module("tt7_codex_base_run_omp", BASE / "run_omp.py")
        campaign = load_module("tt7_codex_base_campaign", BASE / "campaign.py")
        _BOUND = SimpleNamespace(common=common, run_omp=run_omp, campaign=campaign)
    return _BOUND


def configure(B: SimpleNamespace, private: Path) -> Path:
    """Rebind the frozen runner's paths and scan identity to the NEW namespace."""
    omp = private / "omp"
    omp.mkdir(parents=True, exist_ok=True)
    B.run_omp.PRIVATE = omp
    B.run_omp.OUT = OUT
    B.run_omp.SCAN = SCAN
    return private


def acquire(private: Path) -> Any:
    handle = (private / "run.lock").open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise ValueError("another continue_codex invocation holds the namespace lock") from None
    return handle


def luna_rows(B: SimpleNamespace) -> list[dict[str, Any]]:
    marker = DETECTOR.encode()
    rows = []
    with B.common.RESULTS.open("rb") as handle:
        fcntl.flock(handle, fcntl.LOCK_SH)
        for line in handle:
            if marker in line and line.strip():
                row = json.loads(line)
                if row.get("tool") == "omp" and row.get("detector") == DETECTOR:
                    rows.append(row)
    return rows


def is_ours(row: dict[str, Any]) -> bool:
    return (row.get("value") or {}).get("attempt_namespace") == NAMESPACE and row.get("scan") == SCAN


# --------------------------------------------------------------------------- admission guard


class Gate:
    """Wrap the frozen call/append seams; the frozen read_one itself is untouched."""

    def __init__(self, B: SimpleNamespace, private: Path, proof: dict[str, Any], proof_sha256: str) -> None:
        self.B = B
        self.private = private
        self.proof = proof
        self.proof_sha256 = proof_sha256
        self.lock = threading.RLock()
        self.halt = threading.Event()
        self.stop_reason: str | None = None
        self.quota_checks = 0
        self.quota_failures = 0
        self.last_fraction: float | None = None
        self.actual_calls = 0
        self.known_terminal: set[str] = set()
        self.original_call = B.run_omp.call
        self.original_append = B.run_omp.append_records

    def install(self) -> None:
        self.B.run_omp.call = self.call
        self.B.run_omp.append_records = self.append

    def stop(self, reason: str) -> None:
        with self.lock:
            if self.stop_reason is None:
                self.stop_reason = reason
            self.halt.set()

    def cell_dir(self, run_id: str) -> Path:
        return self.private / "omp" / ROUTE.replace("/", "--") / self.B.campaign.opaque(run_id)

    # ---- quota: the Codex-only reader before every actual call
    def check_quota(self) -> dict[str, Any]:
        env = {key: value for key, value in os.environ.items() if not self.B.run_omp.KEY_ENV.search(key)}
        try:
            snapshot = read_codex_usage(env)
            with (self.private / "omp" / "usage.jsonl").open("a") as handle:
                handle.write(json.dumps({"at": self.B.common.now(), **snapshot}) + "\n")
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
            self.quota_failures += 1
            self.stop(f"quota_unknown: {type(exc).__name__}: {str(exc)[:300]}; fail closed, no inferred headroom")
            raise StopRoute(self.stop_reason) from None
        fraction = snapshot["maximum_fraction"]
        self.quota_checks += 1
        self.last_fraction = fraction
        if fraction >= STOP_FRACTION:
            self.stop(f"quota_stop: highest Codex window {fraction:.1%} is at or above {STOP_FRACTION:.0%}")
            raise StopRoute(self.stop_reason)
        return {"fetched_at_ms": snapshot["fetched_at_ms"], "oldest_age_s": snapshot["oldest_age_s"],
                "maximum_fraction": fraction, "plan_type_observed": snapshot["plan_type_observed"],
                "windows": [{"id": row["id"], "window_id": row["window_id"], "used_fraction": row["used_fraction"]}
                            for row in snapshot["limits"]],
                "checked_at": self.B.common.now()}

    # ---- the wrapped transport seam
    def call(self, route: str, system: str, prompt: str, folder: Path, name: str) -> tuple[str, dict[str, Any]]:
        if route != ROUTE:
            raise StopRoute(f"unexpected route {route!r}; only {ROUTE} is admitted")
        identity_path = folder / f"{name}-identity.json"
        if identity_path.exists():
            # Retained outcome: replay or retained failure; nothing is transmitted.
            if not (folder / f"{name}-basis.json").exists():
                self.account_or_stop(folder, name)
            text, identity = self.original_call(route, system, prompt, folder, name)
        else:
            text, identity = self.actual_call(route, system, prompt, folder, name)
        if refusal_reply(text, frozenset(self.B.common.OUTPUT_SCHEMA["required"])):
            raise self.B.run_omp.RouteError(
                "explicit_policy_refusal: declared or anchored first-person refusal; no retry or rephrase")
        return text, identity

    def actual_call(self, route: str, system: str, prompt: str, folder: Path, name: str) -> tuple[str, dict[str, Any]]:
        try:
            hits = self.B.common.secret_hits(system + "\n" + prompt)
        except Exception as exc:  # noqa: BLE001 - an unavailable gate must stop, never pass
            self.stop(f"privacy_gate_unavailable: {type(exc).__name__}")
            raise StopRoute(self.stop_reason) from None
        if hits:
            raise self.B.run_omp.RouteError(
                "privacy_scan_rejected: canonical secret-pattern gate on the exact payload; nothing transmitted")
        with self.lock:
            if self.halt.is_set():
                raise StopRoute(self.stop_reason or "route halted")
            quota = self.check_quota()
            try:
                exclusive_write(folder / f"{name}-inflight.json", {
                    "at": self.B.common.now(), "call": name, "namespace": NAMESPACE,
                    "entitlement_proof_sha256": self.proof_sha256, "quota": quota})
            except OSError as exc:
                self.stop(f"inflight_marker_unwritable: {type(exc).__name__}")
                raise StopRoute(self.stop_reason) from None
            self.actual_calls += 1
        try:
            text, identity = self.original_call(route, system, prompt, folder, name)
        except self.B.run_omp.RouteError:
            self.account_or_stop(folder, name)
            raise
        self.account_or_stop(folder, name)
        return text, identity

    def account_or_stop(self, folder: Path, name: str) -> None:
        try:
            self.account(folder, name)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            self.stop(f"spend_ledger_unwritable: {type(exc).__name__}")
            raise StopRoute(self.stop_reason) from None

    def account(self, folder: Path, name: str) -> None:
        """One sidecar + one append-only spend row per actual attempt, from retained files only."""
        marker_path = folder / f"{name}-inflight.json"
        marker = load_json(marker_path) if marker_path.exists() else {}
        identity = load_json(folder / f"{name}-identity.json")
        ok = bool(identity.get("ok"))
        error = None if ok else str(identity.get("error") or "retained failed call")
        text = ""
        if error is not None:
            stderr = folder / f"{name}-stderr.txt"
            text = "" if error.startswith("RouteError: omp exit") else error
            text += "\n" + (stderr.read_text()[-2000:] if stderr.exists() else "")
            events = folder / f"{name}-events.jsonl"
            if events.exists():
                for line in events.read_text().splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and event.get("type") == "agent_end":
                        for row in event.get("messages") or []:
                            if isinstance(row, dict) and row.get("role") == "assistant" and row.get("errorMessage"):
                                text += "\n" + str(row["errorMessage"])[:1000]
        kind = failure_class(text) if error is not None else None
        paid = kind == "paid_basis_indication"
        billing_unknown = paid or not ok
        usage = identity.get("usage") or {}
        cache = int(usage.get("cacheRead") or 0) + int(usage.get("cacheWrite") or 0)
        ins = int(identity.get("input_tokens") or 0) + cache if ok else None
        outs = int(identity.get("output_tokens") or 0) if ok else None
        rate_in, rate_out = self.B.run_omp.RATES[ROUTE]
        list_price = (ins * rate_in + outs * rate_out) / 1_000_000 if ok else None
        proof_sha = marker.get("entitlement_proof_sha256", self.proof_sha256)
        basis = BASIS_UNKNOWN if billing_unknown else BASIS_INCLUDED
        opaque = folder.name
        at = identity.get("at") or self.B.common.now()
        self.B.common.log_spend({
            "tool": "omp", "route": ROUTE, "purpose": "TT-7 frozen whole-run form, Codex continuation attempt",
            "batch_id": f"{NAMESPACE}-" + digest(f"{opaque}|{name}|{at}".encode()),
            "campaign": NAMESPACE, "opaque_id": opaque, "call": name, "ok": ok, "error_class": kind,
            "runs": 0, "calls": 1, "input_tokens": ins, "output_tokens": outs,
            "usd": None if billing_unknown else 0, "list_price_usd": list_price,
            "billing_basis": basis, "auth_class": self.proof["auth_class"], "plan_type": self.proof["plan_type"],
            "api": self.proof["api"], "entitlement_proof_sha256": proof_sha, "quota": marker.get("quota"),
            "scan": SCAN,
            "basis": ("verified current included ChatGPT-plan native OAuth entitlement; $0 marginal; paid keys stripped; "
                      "no tools; served-model attestation; original two-cell Luna financial basis stays UNKNOWN"
                      if not billing_unknown else "UNKNOWN: unsuccessful call lacks accepted served-model/financial attestation; route stopped"),
        })
        exclusive_write(folder / f"{name}-basis.json", {
            "schema": "tt7.codex_attempt_basis/v1", "call": name, "ok": ok, "billing_basis": basis,
            "error_class": kind, "entitlement_proof_sha256": proof_sha, "quota": marker.get("quota")})
        if paid:
            self.stop("paid_basis_unknown: a failed call indicated billing/credit/payment; route stopped")
        elif kind == "provider_quota_or_rate_limit":
            self.stop("provider_quota_or_rate_limit: an actual call failed with a quota/rate-limit indication")
        elif not ok:
            self.stop("model_attestation_unknown: unsuccessful call lacks an accepted exact served-model attestation; route stopped")

    def recover_interrupted(self) -> int:
        """A marker without an identity means the outcome is unknown; record it failed, never retry it."""
        recovered = 0
        for marker in sorted((self.private / "omp").glob("*/*/*-inflight.json")):
            name = marker.name[: -len("-inflight.json")]
            identity = marker.with_name(f"{name}-identity.json")
            if identity.exists():
                continue
            exclusive_write(identity, {"requested_model": ROUTE, "at": load_json(marker)["at"], "ok": False,
                                       "error": f"RouteError: {INTERRUPTED}", "latency_s": 0})
            recovered += 1
        return recovered

    # ---- the wrapped canonical-append seam
    def append(self, rows: list[dict[str, Any]]) -> int:
        if len(rows) != 1:
            raise ValueError("exactly one result row per cell")
        record = rows[0]
        if record.get("detector") != DETECTOR or record.get("scan") != SCAN or record["run_id"] in self.known_terminal:
            raise ValueError("refusing a duplicate or foreign canonical row")
        value = record["value"]
        sidecars = [load_json(path) for path in sorted(self.cell_dir(record["run_id"]).glob("*-basis.json"))]
        unknown = any(row["billing_basis"] != BASIS_INCLUDED for row in sidecars)
        value.update(attempt_namespace=NAMESPACE, entitlement_proof_sha256=self.proof_sha256,
                     billing_basis=BASIS_UNKNOWN if unknown else BASIS_INCLUDED if sidecars else BASIS_NONE)
        if unknown:
            value["marginal_usd"] = None
        if record["flag"] is None:
            value["abstention_class"] = abstention_class(
                str(value.get("abstention_reason") or ""), {str(row.get("error_class")) for row in sidecars})
        added = self.original_append([record])
        if added != 1:
            raise ValueError("canonical result row was not appended")
        self.known_terminal.add(record["run_id"])
        return added


# --------------------------------------------------------------------------- audits / inventory


def privacy_audit(B: SimpleNamespace) -> dict[str, Any]:
    """Uniform pinned audit of ALL 200 rendered inputs against the prior audit; no payload is stored."""
    reference_path = B.common.STATE / REFERENCE_NAMESPACE / "privacy-audit.json"
    reference = load_json(reference_path)
    if reference.get("gate_sha256") != B.common.SECRET_GATE_SHA256 or reference.get("sample_sha256") != B.common.SAMPLE_SHA256:
        raise ValueError("reference privacy audit pins a different gate or sample")
    expected = {row["opaque_id"]: row for row in reference["rows"]}
    rows = []
    for item in B.common.load_sample():
        run = B.common.load_run(item)
        row = {"opaque_id": B.campaign.opaque(item["run_id"]), "source": item["source"],
               "text_bytes": len(run["text"].encode()), "steps": len(run["steps"]),
               "text_sha256": digest(run["text"].encode()), "pattern_marker_count": len(B.common.run_secret_hits(run))}
        if expected.get(row["opaque_id"]) != row:
            raise ValueError("a rendered input hash/size/flag differs from the prior audit; no calls admitted")
        rows.append(row)
    flagged = sorted(row["opaque_id"] for row in rows if row["pattern_marker_count"])
    if len(rows) != 200 or len(expected) != 200 or len(flagged) != EXPECTED_FLAGGED:
        raise ValueError("uniform privacy audit no longer yields 200 inputs with exactly the 4 prior flags")
    return {"rows": rows, "flagged": flagged, "reference_sha256": digest(reference_path.read_bytes())}


def terminal_inventory(rows: list[dict[str, Any]], sample: list[dict[str, str]]) -> dict[str, Any]:
    order = [row["run_id"] for row in sample]
    terminal: dict[str, set[str]] = {}
    for row in rows:
        if row.get("tool") == "omp" and str(row.get("detector", "")).startswith("tt7_form:"):
            terminal.setdefault(row["detector"], set()).add(row["run_id"])
    return {detector: {"terminal_run_ids": [i for i in order if i in done],
                       "pending_run_ids": [i for i in order if i not in done]}
            for detector, done in sorted(terminal.items())}


def verify_prepared(B: SimpleNamespace, private: Path, prep: dict[str, Any]) -> dict[str, Any]:
    sources = verify_sources()
    if prep["sample_sha256"] != B.common.SAMPLE_SHA256 or prep["source_sha256"] != sources:
        raise ValueError("prepared sample/source pins differ from the frozen BASE")
    for name, key in (("baseline-results.jsonl", "baseline_results_sha256"), ("baseline-terminal.json", "baseline_terminal_sha256"),
                      ("privacy-audit.json", "privacy_audit_sha256")):
        if digest((private / name).read_bytes()) != prep[key]:
            raise ValueError(f"sealed preparation file changed: {name}")
    return load_json(private / "baseline-terminal.json")


def entitlement(B: SimpleNamespace, path: Path, private: Path) -> tuple[dict[str, Any], str]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65_536:
        raise ValueError("entitlement proof must be a regular file of at most 64 KiB")
    env = {key: value for key, value in os.environ.items() if not B.run_omp.KEY_ENV.search(key)}
    version = subprocess.run(["omp", "--version"], capture_output=True, text=True, timeout=60, check=True, env=env).stdout
    versions = set(re.findall(r"(?<![\w.])\d+\.\d+\.\d+(?:[-+][\w.]+)?(?![\w.])", version))
    if len(versions) != 1:
        raise ValueError("omp --version did not report exactly one version")
    raw = path.read_bytes()
    proof, sha = validate_proof(raw, versions.pop(), B.common.secret_hits, datetime.now(UTC))
    B.campaign.write_once(private / "proofs" / f"{sha}.json", raw)
    return proof, sha


# --------------------------------------------------------------------------- commands


def prepare() -> None:
    clock = time.perf_counter()
    B = bind()
    private = configure(B, B.common.STATE / NAMESPACE)
    prepared = private / "prepare.json"
    if prepared.exists():
        prep = load_json(prepared)
        verify_prepared(B, private, prep)
        print(json.dumps({"prepared": True, "already_prepared": True, "model_calls": 0, **prep["summary"]}), flush=True)
        return
    sample = B.common.load_sample()
    baseline_path = private / "baseline-results.jsonl"
    if not baseline_path.exists():
        payload = B.campaign.filtered_results()
        if NAMESPACE.encode() in payload:
            raise ValueError("canonical results already contain this attempt namespace; no baseline can be sealed")
        B.campaign.write_once(baseline_path, payload)
    rows = [json.loads(line) for line in baseline_path.read_bytes().split(b"\n") if line.strip()]
    inventory = terminal_inventory(rows, sample)
    luna = inventory.get(DETECTOR)
    if luna is None or len(luna["terminal_run_ids"]) != EXPECTED_ORIGINAL_TERMINAL or len(luna["pending_run_ids"]) != EXPECTED_PENDING:
        raise ValueError("original Luna terminal coverage differs from the expected 2 terminal + 198 pending")
    original = [row for row in rows if row.get("tool") == "omp" and row.get("detector") == DETECTOR]
    audit = privacy_audit(B)
    flagged_pending = sorted(set(audit["flagged"]) & {B.campaign.opaque(i) for i in luna["pending_run_ids"]})
    B.campaign.write_once(private / "baseline-terminal.json", canonical({
        "schema": "tt7.codex_baseline_terminal/v1", "sample_sha256": B.common.SAMPLE_SHA256, "detectors": inventory}))
    B.campaign.write_once(private / "privacy-audit.json", canonical({
        "schema": "tt7.codex_privacy_audit_private/v1", "sample_sha256": B.common.SAMPLE_SHA256,
        "gate_sha256": B.common.SECRET_GATE_SHA256, "reference_sha256": audit["reference_sha256"],
        "rows": audit["rows"], "flagged": audit["flagged"], "model_calls": 0, "remote_mutations": 0}))
    summary = {
        "input_runs": len(audit["rows"]), "input_text_bytes": sum(r["text_bytes"] for r in audit["rows"]),
        "input_steps": sum(r["steps"] for r in audit["rows"]), "privacy_flagged_runs": len(audit["flagged"]),
        "privacy_flagged_pending_luna": len(flagged_pending), "original_luna_terminal": len(luna["terminal_run_ids"]),
        "original_luna_valid": sum(r.get("flag") is not None for r in original),
        "original_luna_abstentions": sum(r.get("flag") is None for r in original),
        "pending_luna": len(luna["pending_run_ids"]),
        "terminal_by_model": {detector: len(value["terminal_run_ids"]) for detector, value in inventory.items()},
    }
    B.campaign.write_once(prepared, canonical({
        "schema": "tt7.codex_continuation_prepare/v1", "at": B.common.now(), "namespace": NAMESPACE, "route": ROUTE,
        "sample_sha256": B.common.SAMPLE_SHA256, "source_sha256": verify_sources(),
        "baseline_results_sha256": digest(baseline_path.read_bytes()),
        "baseline_terminal_sha256": digest((private / "baseline-terminal.json").read_bytes()),
        "privacy_audit_sha256": digest((private / "privacy-audit.json").read_bytes()),
        "reference_privacy_audit_sha256": audit["reference_sha256"], "summary": summary,
        "policy": {"stop_fraction": STOP_FRACTION, "quota_ttl_s": QUOTA_TTL_S, "order": "unchanged sample file order",
                   "retries": "frozen predeclared single schema retry only; no retry after refusal/error/validation failure",
                   "old_two_financial_basis": "UNKNOWN"},
        "labels_read": False, "model_calls": 0}))
    print(json.dumps({"prepared": True, "model_calls": 0, "elapsed_s": round(time.perf_counter() - clock, 3), **summary}), flush=True)


def read_cell(gate: Gate, item: dict[str, str], tally: Counter[str]) -> None:
    try:
        record = gate.B.run_omp.read_one(item, ROUTE)
    except StopRoute as exc:
        gate.stop(str(exc))
        calls = len(list(gate.cell_dir(item["run_id"]).glob("*-identity.json")))
        tally["interrupted_cells" if calls else "not_started"] += 1
        return
    except Exception as exc:  # noqa: BLE001 - internal fault stops the route; never synthesize abstentions
        gate.stop(f"internal_error: {type(exc).__name__}: {str(exc)[:200]}")
        tally["internal_errors"] += 1
        return
    tally["new_terminal"] += 1
    tally["valid" if record["flag"] is not None else "abstentions"] += 1


def run(entitlement_path: Path) -> None:
    B = bind()
    private = configure(B, B.common.STATE / NAMESPACE)
    if RECEIPT.exists():
        raise ValueError("attempt receipt is sealed; no further invocation")
    prep = load_json(private / "prepare.json")
    inventory = verify_prepared(B, private, prep)["detectors"][DETECTOR]
    lock_handle = acquire(private)
    started = B.common.now()
    log = private / "invocations.jsonl"

    def record(entry: dict[str, Any]) -> None:
        with log.open("a") as handle:
            handle.write(json.dumps({"schema": "tt7.codex_invocation/v1", "started_at": started,
                                     "finished_at": B.common.now(), "concurrency": 1, **entry}, sort_keys=True) + "\n")

    try:
        proof, proof_sha = entitlement(B, entitlement_path, private)
        audit = privacy_audit(B)
        if digest(canonical({"rows": audit["rows"]})) != digest(canonical({"rows": load_json(private / "privacy-audit.json")["rows"]})):
            raise ValueError("privacy audit differs from the sealed preparation")
        sample = B.common.load_sample()
        rows = luna_rows(B)
        done = {row["run_id"] for row in rows}
        baseline = set(inventory["terminal_run_ids"])
        if not baseline <= done:
            raise ValueError("original Luna terminal evidence is missing from canonical results")
        pending = [item for item in sample if item["run_id"] not in done]
        if not {item["run_id"] for item in pending} <= set(inventory["pending_run_ids"]):
            raise ValueError("pending cells are not a subset of the fixed pending order")
        foreign = len(done - baseline - {row["run_id"] for row in rows if is_ours(row)})
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        record({"status": "admission_refused", "reason": f"{type(exc).__name__}: {str(exc)[:300]}", "actual_calls": 0})
        raise SystemExit(f"admission refused: {type(exc).__name__}: {str(exc)[:300]}") from None
    gate = Gate(B, private, proof, proof_sha)
    gate.known_terminal = set(done)
    gate.install()
    recovered = gate.recover_interrupted()
    tally: Counter[str] = Counter()
    try:
        for item in pending:
            if gate.halt.is_set():
                break
            read_cell(gate, item, tally)
    finally:
        status = "stopped" if gate.halt.is_set() else "queue_exhausted"
        record({"status": status, "reason": gate.stop_reason or "all pending positions reached terminal readings",
                "entitlement_proof_sha256": proof_sha, "pending_at_start": len(pending), "tally": dict(tally),
                "actual_calls": gate.actual_calls, "recovered_interrupted_attempts": recovered,
                "foreign_terminal_cells_skipped": foreign, "quota_checks": gate.quota_checks,
                "quota_check_failures": gate.quota_failures, "last_max_fraction": gate.last_fraction})
        lock_handle.close()
    print(json.dumps({"status": status, "reason": gate.stop_reason, "pending_at_start": len(pending), **tally,
                      "actual_calls": gate.actual_calls, "quota_checks": gate.quota_checks,
                      "last_max_fraction": gate.last_fraction}), flush=True)


def close() -> None:
    B = bind()
    private = configure(B, B.common.STATE / NAMESPACE)
    if RECEIPT.exists():
        raise ValueError("attempt receipt already sealed")
    prep = load_json(private / "prepare.json")
    inventory = verify_prepared(B, private, prep)["detectors"][DETECTOR]
    lock_handle = acquire(private)
    payload = B.campaign.filtered_results()
    lines = [line for line in payload.split(b"\n") if line.strip()]
    baseline_lines = [line for line in (private / "baseline-results.jsonl").read_bytes().split(b"\n") if line.strip()]
    current = Counter(lines)
    if any(current[line] < count for line, count in Counter(baseline_lines).items()):
        raise ValueError("original result evidence is missing or mutated")
    baseline_set = set(baseline_lines)
    new_lines, new_rows, foreign = [], [], 0
    for line in lines:
        row = json.loads(line)
        if row.get("tool") != "omp" or row.get("detector") != DETECTOR:
            continue
        if is_ours(row):
            new_lines.append(line)
            new_rows.append(row)
        elif line not in baseline_set:
            foreign += 1
    if foreign:
        raise ValueError("Luna rows exist outside the sealed baseline and this namespace; investigate before sealing")
    pending = inventory["pending_run_ids"]
    seen = [row["run_id"] for row in new_rows]
    invocations = [json.loads(line) for line in (private / "invocations.jsonl").read_text().splitlines() if line.strip()] \
        if (private / "invocations.jsonl").exists() else []
    admitted = [row for row in invocations if row["status"] != "admission_refused"]
    if len(seen) != len(set(seen)) or seen != pending[:len(seen)]:
        raise ValueError("new rows are not a unique, ordered prefix of the fixed pending order")
    done = set(seen)
    unattempted = [run_id for run_id in pending if run_id not in done]
    omp = private / "omp"
    cells = {path.parent.name for path in omp.glob("*/*/*-identity.json")}
    interrupted = sorted(B.campaign.opaque(run_id) for run_id in unattempted if B.campaign.opaque(run_id) in cells)
    values = [row["value"] for row in new_rows]
    valid = sum(row["flag"] is not None for row in new_rows)
    latencies = [value["latency_s"] for value in values if value.get("latency_s") is not None]
    proof_files = sorted((private / "proofs").glob("*.json"), key=lambda path: path.stat().st_mtime)
    proofs = sorted(path.stem for path in proof_files)
    used = [row["entitlement_proof_sha256"] for row in admitted if row.get("entitlement_proof_sha256")]
    newest = used[-1] if used else (proof_files[-1].stem if proof_files else None)
    last_proof = load_json(private / "proofs" / f"{newest}.json") if newest else None
    complete = not unattempted
    reason = ("all previously unattempted positions reached terminal readings" if complete else
              invocations[-1]["reason"] if invocations else "no run invocation was admitted")
    sidecars = [load_json(path) for path in omp.glob("*/*/*-basis.json")]
    unknown_basis = sum(row["billing_basis"] != BASIS_INCLUDED for row in sidecars)
    classes = Counter(value.get("abstention_class") for value, row in zip(values, new_rows, strict=True) if row["flag"] is None)
    write_once = B.campaign.write_once
    write_once(private / "results-snapshot.jsonl", payload)
    new_payload = b"".join(line + b"\n" for line in new_lines)
    write_once(private / "new-records.jsonl", new_payload)
    write_once(private / "unattempted.json", canonical({
        "schema": "tt7.codex_unattempted/v1", "unattempted_run_ids": unattempted, "interrupted_opaque_ids": interrupted}))
    started = min((row["started_at"] for row in admitted), default=prep["at"])
    receipt = {
        "schema": "tt7.codex_continuation_attempt/v1", "namespace": NAMESPACE, "route": ROUTE,
        "status": "complete" if complete else "partial" if new_rows else "no_observations", "reason": reason,
        "auth_class": REQUIRED_PROOF["auth_class"], "plan_type": REQUIRED_PROOF["plan_type"],
        "started_at": started, "finished_at": B.common.now(), "sample_sha256": B.common.SAMPLE_SHA256,
        "counts": {
            "sample_runs": 200, "original_terminal": EXPECTED_ORIGINAL_TERMINAL, "pending_at_prepare": len(pending),
            "new_terminal": len(new_rows), "new_valid": valid, "new_abstentions": len(new_rows) - valid,
            "unattempted": len(unattempted), "interrupted_cells": len(interrupted),
            "privacy_abstentions": classes.get("privacy_scan_rejected", 0),
            "model_calls": len(list(omp.glob("*/*/*-identity.json"))),
            "included_basis_attempts": len(sidecars) - unknown_basis, "unknown_basis_attempts": unknown_basis,
            "input_tokens": sum(value.get("input_tokens") or 0 for value in values),
            "output_tokens": sum(value.get("output_tokens") or 0 for value in values),
            "list_price_usd": sum(value.get("list_price_usd") or 0 for value in values),
            "median_latency_s": statistics.median(latencies) if latencies else None,
            "abstention_classes": dict(classes),
            "unattempted_set_sha256": digest("\n".join(B.campaign.opaque(i) for i in unattempted).encode()),
        },
        "prefix_contiguous": True,
        "privacy": {"input_runs": prep["summary"]["input_runs"], "flagged_runs": prep["summary"]["privacy_flagged_runs"],
                    "flagged_pending_luna": prep["summary"]["privacy_flagged_pending_luna"],
                    "gate_sha256": B.common.SECRET_GATE_SHA256, "all_rendered_hashes_match_reference": True,
                    "reference_audit_sha256": prep["reference_privacy_audit_sha256"]},
        "snapshot": {"path": str(private / "results-snapshot.jsonl"), "sha256": digest(payload), "bytes": len(payload), "rows": len(lines)},
        "new_records": {"path": str(private / "new-records.jsonl"), "sha256": digest(new_payload), "bytes": len(new_payload), "rows": len(new_lines)},
        "entitlement": {
            "proof_sha256": newest, "proof_sha256_all": proofs,
            **({key: last_proof[key] for key in ("status", "route", "auth_class", "plan_type", "api", "endpoint",
                                                 "omp_version", "account_identity_sha256")} if last_proof else {})},
        "quota": {"threshold": STOP_FRACTION, "ttl_s": QUOTA_TTL_S, "checks": sum(row.get("quota_checks", 0) for row in admitted),
                  "check_failures": sum(row.get("quota_check_failures", 0) for row in admitted),
                  "last_max_fraction": next((row["last_max_fraction"] for row in reversed(admitted)
                                             if row.get("last_max_fraction") is not None), None)},
        "billing": {"new_basis": BASIS_INCLUDED, "new_marginal_usd": None if unknown_basis else 0,
                    "original_two_financial_basis": "UNKNOWN",
                    "note": "list_price_usd is an uncached catalog list-price equivalent, not spend"},
        "baseline": {"results_sha256": prep["baseline_results_sha256"], "terminal_sha256": prep["baseline_terminal_sha256"],
                     "privacy_audit_sha256": prep["privacy_audit_sha256"]},
        "invocations": {"count": len(invocations), "statuses": dict(Counter(row["status"] for row in invocations))},
        "source_sha256": {**verify_sources(), "continue_codex.py": digest(Path(__file__).read_bytes())},
        "labels_read": False, "original_evidence_preserved": True, "old_terminal_retries": 0,
    }
    B.common.log_spend({
        "tool": "omp", "route": ROUTE, "batch_id": f"{NAMESPACE}-close", "campaign": NAMESPACE,
        "purpose": "TT-7 Codex continuation sealed receipt; per-attempt rows hold calls/tokens",
        "calls": 0, "runs": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0, "list_price_usd": 0,
        "attempt_rows": len(sidecars), "scan": SCAN,
        "basis": "zero-call close row; new attempts are verified-included $0 marginal; original two-cell Luna financial basis remains UNKNOWN"})
    with RECEIPT.open("x") as handle:
        handle.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    lock_handle.close()
    print(json.dumps({"status": receipt["status"], "new_terminal": len(new_rows), "valid": valid,
                      "unattempted": len(unattempted), "model_calls": receipt["counts"]["model_calls"],
                      "receipt": str(RECEIPT)}), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("prepare", "run", "close"))
    parser.add_argument("--entitlement", type=Path, help="parent-authored immutable entitlement proof (required for run)")
    args = parser.parse_args()
    if args.action == "run" and args.entitlement is None:
        parser.error("run requires --entitlement PATH")
    {"prepare": prepare, "close": close}.get(args.action, lambda: run(args.entitlement))()


if __name__ == "__main__":
    main()
