"""Secret-safe preflight for Z.ai / TB4 overnight campaigns.

Given one or more overnight campaign manifests, this surface explains, per
campaign and with deterministic machine-readable reasons, whether the campaign
*may compile*, *may run as calibration*, or *may promote causally* — and
whether launching it would fail closed.

SECURITY BOUNDARY — read before extending.

This surface never emits a credential value, an auth document, a token shape,
or a keychain/file path for a credential. Provider checks report **only the
provider name** plus a boolean and a reason code. The Z.ai coding-plan lane is
mount-injected at launch inside the container (the host adapter never reads the
secret), so host-side "cached provider presence" is reported as such — the
check returns the provider name only, never what is mounted or where.

Fail-closed launch semantics:

- A ``calibration`` campaign launches only when it ``may_calibrate``.
- A ``causal`` campaign launches only when it ``may_promote_causal``; a causal
  campaign that can only calibrate (for example on Darwin, where Docker cannot
  enforce ``no-network`` isolation) is refused at launch. The report still
  labels it ``darwin-calibration-only`` so an operator can re-issue it as a
  calibration campaign if that is the intent.
- ``--compile-only`` (``compile_only=True``) never fails closed on launch: it
  produces the full report and exits 0 as long as the report was produced, so
  operators can still compile/validate tasks on a host whose launch gates are
  closed.

The high-speed Z.ai model is a **provider-access failure, never a model
outcome**: the provider answers HTTP 429 "current subscription plan does not
yet include access". Such a trial must be recorded as a provider-access
failure, never relabeled as reward 0.0. This surface refuses the model in the
allowlist check with a ``provider-access-failure`` reason to keep that
distinction mechanical.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from evallab.harbor_network import (
    HarborNetworkPolicy,
    host_harbor_network_policy,
)

SCHEMA_VERSION = "overnight-campaign-preflight/v1"

#: The only accepted provider prefix for the Z.ai credential lane; the Z.ai
#: credential is scoped to this provider and must never be pointed elsewhere.
ZAI_PROVIDER = "zai-coding-plan"

#: Models the pinned Z.ai Coding Plan adapter is observed to run successfully.
#: Observed 2026-08-29 pilot through the pinned OpenCode adapter.
ALLOWED_ZAI_MODELS: frozenset[str] = frozenset({"glm-5.3", "glm-5.3-flash"})

#: Models NOT included in the current subscription; the provider answers
#: HTTP 429. These are provider-access failures, never model outcomes: a trial
#: reaching them must be recorded as a provider-access failure, never reward 0.
PROVIDER_ACCESS_DENIED_MODELS: frozenset[str] = frozenset({"glm-5.3-highspeed"})

#: Providers whose credential is delivered by a read-only secret mount created
#: at launch inside the container (never read by host code). Presence is
#: reported by provider name only.
MOUNT_INJECTED_PROVIDERS: frozenset[str] = frozenset({ZAI_PROVIDER})

#: Overnight ceilings (lab policy). A campaign declaring more is refused at
#: launch but can still be reported for compile.
MAX_OVERNIGHT_TRIALS = 100
MAX_OVERNIGHT_CONCURRENCY = 8
MAX_PROMPT_TOKEN_CEILING = 1_000_000

#: Free-disk floor, kept in step with `automation.MIN_FREE_DISK_BYTES` /
#: `MIN_FREE_DISK_FRACTION` (the doctor's disk-headroom gate).
MIN_FREE_DISK_BYTES = 5 * 1024**3
MIN_FREE_DISK_FRACTION = 0.05

_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"

LAUNCH_GATE_COMPILE = "compile"
LAUNCH_GATE_CALIBRATE = "calibrate"
LAUNCH_GATE_PROMOTE_CAUSAL = "promote-causal"
LAUNCH_GATE_REFUSED = "refused"


class _FrozenContract(BaseModel):
    """Strict, immutable preflight contract (repo convention)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class OvernightTaskSpec(_FrozenContract):
    """The task package the campaign runs, with the digests it must match."""

    task_path: str = Field(min_length=1)
    expected_package_digest: str = Field(pattern=_SHA256_PATTERN)
    expected_verifier_digest: str = Field(pattern=_SHA256_PATTERN)


class OvernightWheelhouseSpec(_FrozenContract):
    """The offline wheelhouse and its trusted resolver-provenance record."""

    wheelhouse_path: str = Field(min_length=1)
    provenance_path: str = Field(min_length=1)


class OvernightCampaignPreflight(_FrozenContract):
    """Self-contained, strict input for one overnight Z.ai / TB4 campaign.

    Narrow and future-proofed: a future campaign runner is expected to adapt
    into this schema, not the other way around. Parsing is strict
    (``extra="forbid"``, immutable) so a misspelled field fails loudly instead
    of being silently ignored and weakening a gate.
    """

    schema_version: Literal["overnight-campaign-preflight/v1"] = SCHEMA_VERSION
    campaign_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$")
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    task: OvernightTaskSpec
    wheelhouse: OvernightWheelhouseSpec
    n_trials: int = Field(ge=1)
    n_concurrent: int = Field(ge=1)
    prompt_token_ceiling: int = Field(ge=1)
    evidence_mode: Literal["calibration", "causal"]
    network_isolation: Literal["required", "none"] = "none"
    credential_proxy: bool = False


@dataclass(frozen=True)
class PreflightCheck:
    """One deterministic machine-readable check result.

    ``provider`` carries ONLY a provider name (never a credential value, auth
    shape, keychain path, or file path to credential material).
    """

    code: str
    ok: bool
    reason: str
    provider: str | None = None


@dataclass(frozen=True)
class RunPreflightEnvironment:
    """Host facts shared by every campaign in one preflight reading."""

    os_name: str
    docker: PreflightCheck
    disk: PreflightCheck
    network_isolation_enforced: bool
    network_isolation_reason: str | None

    @property
    def linux_enforced_isolation(self) -> bool:
        return self.os_name == "Linux" and self.network_isolation_enforced


@dataclass(frozen=True)
class OvernightCampaignResult:
    """One campaign's checks plus its derived capability verdicts."""

    campaign: OvernightCampaignPreflight
    checks: tuple[PreflightCheck, ...]
    may_compile: bool
    compile_reasons: tuple[str, ...]
    may_calibrate: bool
    calibrate_reasons: tuple[str, ...]
    may_promote_causal: bool
    promote_reasons: tuple[str, ...]
    launch_gate: str


@dataclass(frozen=True)
class RunPreflightReport:
    generated_at: datetime
    environment: RunPreflightEnvironment
    campaigns: tuple[OvernightCampaignResult, ...]
    compile_only: bool
    launch_ok: bool


ProviderPresenceProbe = Callable[[str], PreflightCheck]
DockerProbe = Callable[[], PreflightCheck]
DiskProbe = Callable[[Path], PreflightCheck]


# --- probes ---------------------------------------------------------------


def _default_docker_probe() -> PreflightCheck:
    """Docker daemon reachable, mirrored from `queue.Executor.local_runtime_checks`."""
    if not shutil.which("docker"):
        return PreflightCheck("docker-reachable", False, "docker-cli-not-found")
    try:
        completed = subprocess.run(
            [
                "docker",
                "version",
                "--format",
                "client={{.Client.Version}} server={{.Server.Version}}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return PreflightCheck(
            "docker-reachable", False, f"docker-daemon-unavailable:{type(exc).__name__}"
        )
    output = (completed.stdout or completed.stderr).strip().splitlines()
    detail = output[0] if output else "no version output"
    return PreflightCheck("docker-reachable", completed.returncode == 0, detail)


def _default_disk_probe(repo_root: Path) -> PreflightCheck:
    """Free disk headroom on the checkout volume (doctor's floor)."""
    usage = shutil.disk_usage(repo_root.resolve())
    required = max(MIN_FREE_DISK_BYTES, int(usage.total * MIN_FREE_DISK_FRACTION))
    if usage.free >= required:
        return PreflightCheck("disk-headroom", True, f"free={usage.free} required={required}")
    return PreflightCheck(
        "disk-headroom", False, f"insufficient-disk-free={usage.free} required={required}"
    )


def _default_provider_presence(provider: str) -> PreflightCheck:
    """Cached provider presence by provider name only.

    For mount-injected lanes (Z.ai) the credential is created at launch inside
    the container and is never readable by host code, so presence is reported
    as such — only the provider name and a reason code are emitted, never a
    credential value or auth shape. Unknown providers fail closed.
    """
    if provider in MOUNT_INJECTED_PROVIDERS:
        return PreflightCheck(
            "provider-presence", True, "provider-credential-injected-at-launch", provider=provider
        )
    return PreflightCheck(
        "provider-presence", False, "unknown-provider-credential-source", provider=provider
    )


# --- checks ---------------------------------------------------------------


def _normalize_model(provider: str, model: str) -> tuple[str, str | None]:
    """Return ``(basename, provider_prefix)`` for a model string.

    ``model`` may be bare (``glm-5.3``) or provider-prefixed
    (``zai-coding-plan/glm-5.3``). When prefixed, the prefix must equal the
    campaign's declared provider or the check refuses.
    """
    if "/" in model:
        prefix, _, basename = model.partition("/")
        if not prefix or not basename:
            return model, "malformed-model"
        return basename, prefix
    return model, None


def check_model_allowlist(provider: str, model: str) -> PreflightCheck:
    """Allowlist the model under its provider.

    A provider-prefixed model must use the campaign's declared provider.
    High-speed models are provider-access failures (HTTP 429), never reward 0:
    the reason code is ``provider-access-failure`` so no downstream layer can
    mislabel the trial as a model outcome.
    """
    basename, prefix = _normalize_model(provider, model)
    if prefix is not None:
        if prefix != provider:
            return PreflightCheck(
                "model-allowlist",
                False,
                f"model-provider-mismatch:model={model!r} provider={provider!r}",
            )
        if provider not in MOUNT_INJECTED_PROVIDERS and prefix not in (provider,):
            return PreflightCheck("model-allowlist", False, f"unknown-provider:{prefix!r}")
    if basename in PROVIDER_ACCESS_DENIED_MODELS:
        return PreflightCheck(
            "model-allowlist",
            False,
            f"provider-access-failure:model={basename} provider-access-denied-never-reward-0",
        )
    if basename in ALLOWED_ZAI_MODELS:
        return PreflightCheck("model-allowlist", True, f"model-allowlisted:{basename}")
    return PreflightCheck("model-allowlist", False, f"model-not-allowlisted:{basename}")


def _resolve_repo_path(repo_root: Path, relative: str) -> Path:
    resolved_root = repo_root.resolve()
    candidate = (resolved_root / relative).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise ValueError(f"path escapes repository root: {relative!r}")
    return candidate


def _task_checks(
    repo_root: Path, task: OvernightTaskSpec
) -> tuple[PreflightCheck, PreflightCheck, PreflightCheck]:
    """Path existence, package digest, and verifier digest checks."""
    try:
        task_dir = _resolve_repo_path(repo_root, task.task_path)
    except ValueError as exc:
        return (
            PreflightCheck("task-path", False, f"task-path-unsafe:{exc}"),
            PreflightCheck("task-package-digest", False, "task-path-unsafe"),
            PreflightCheck("task-verifier-digest", False, "task-path-unsafe"),
        )
    if not task_dir.is_dir() or task_dir.is_symlink():
        return (
            PreflightCheck("task-path", False, "task-path-missing"),
            PreflightCheck("task-package-digest", False, "task-path-missing"),
            PreflightCheck("task-verifier-digest", False, "task-path-missing"),
        )
    try:
        from evallab.registry import compute_task_digests

        digests = compute_task_digests(task_dir)
    except Exception as exc:  # noqa: BLE001 - deterministic reason, no secret
        return (
            PreflightCheck("task-path", True, "task-present"),
            PreflightCheck("task-package-digest", False, f"digest-unavailable:{type(exc).__name__}"),
            PreflightCheck("task-verifier-digest", False, f"digest-unavailable:{type(exc).__name__}"),
        )
    package_ok = digests.package == task.expected_package_digest
    verifier_ok = digests.verifier == task.expected_verifier_digest
    return (
        PreflightCheck("task-path", True, "task-present"),
        PreflightCheck(
            "task-package-digest",
            package_ok,
            "package-digest-match" if package_ok else "package-digest-mismatch",
        ),
        PreflightCheck(
            "task-verifier-digest",
            verifier_ok,
            "verifier-digest-match" if verifier_ok else "verifier-digest-mismatch",
        ),
    )


def _wheelhouse_checks(
    repo_root: Path, wheelhouse: OvernightWheelhouseSpec
) -> tuple[PreflightCheck, PreflightCheck]:
    """Wheelhouse presence and trusted resolver-provenance checks."""
    try:
        wheelhouse_dir = _resolve_repo_path(repo_root, wheelhouse.wheelhouse_path)
        provenance_path = _resolve_repo_path(repo_root, wheelhouse.provenance_path)
    except ValueError as exc:
        return (
            PreflightCheck("wheelhouse-present", False, f"wheelhouse-path-unsafe:{exc}"),
            PreflightCheck("wheelhouse-provenance", False, "wheelhouse-path-unsafe"),
        )
    present = wheelhouse_dir.is_dir() and not wheelhouse_dir.is_symlink()
    nonempty = present and any(wheelhouse_dir.glob("*.whl"))
    present_check = PreflightCheck(
        "wheelhouse-present",
        present and nonempty,
        "wheelhouse-present" if present and nonempty else "wheelhouse-missing-or-empty",
    )
    if not present_check.ok:
        return present_check, PreflightCheck(
            "wheelhouse-provenance", False, "wheelhouse-missing-or-empty"
        )
    if not provenance_path.is_file() or provenance_path.is_symlink():
        return present_check, PreflightCheck(
            "wheelhouse-provenance", False, "wheelhouse-provenance-missing"
        )
    try:
        from evallab.mcp_substrate import (
            ResolverProvenance,
            SubstrateError,
            verify_provenance_wheelhouse,
        )

        provenance = ResolverProvenance.from_json(
            json.loads(provenance_path.read_text(encoding="utf-8"))
        )
        verify_provenance_wheelhouse(wheelhouse_dir, provenance)
    except (OSError, ValueError, SubstrateError) as exc:
        return present_check, PreflightCheck(
            "wheelhouse-provenance", False, f"wheelhouse-provenance-invalid:{exc}"
        )
    return present_check, PreflightCheck("wheelhouse-provenance", True, "wheelhouse-provenance-ok")


def _ceiling_checks(campaign: OvernightCampaignPreflight) -> tuple[PreflightCheck, ...]:
    if campaign.n_trials > MAX_OVERNIGHT_TRIALS:
        trials = PreflightCheck(
            "trial-count-ceiling",
            False,
            f"trial-count-exceeds-ceiling:{campaign.n_trials}>{MAX_OVERNIGHT_TRIALS}",
        )
    else:
        trials = PreflightCheck("trial-count-ceiling", True, "trial-count-within-ceiling")
    if campaign.n_concurrent > MAX_OVERNIGHT_CONCURRENCY:
        concurrency = PreflightCheck(
            "concurrency-ceiling",
            False,
            f"concurrency-exceeds-ceiling:{campaign.n_concurrent}>{MAX_OVERNIGHT_CONCURRENCY}",
        )
    else:
        concurrency = PreflightCheck("concurrency-ceiling", True, "concurrency-within-ceiling")
    if campaign.prompt_token_ceiling > MAX_PROMPT_TOKEN_CEILING:
        token = PreflightCheck(
            "prompt-token-ceiling",
            False,
            f"prompt-token-ceiling-exceeds-cap:{campaign.prompt_token_ceiling}>{MAX_PROMPT_TOKEN_CEILING}",
        )
    else:
        token = PreflightCheck("prompt-token-ceiling", True, "prompt-token-ceiling-within-cap")
    return trials, concurrency, token


def _causal_checks(
    campaign: OvernightCampaignPreflight, env: RunPreflightEnvironment
) -> tuple[PreflightCheck, ...]:
    isolation = PreflightCheck(
        "isolation-eligibility",
        env.linux_enforced_isolation and campaign.network_isolation == "required",
        (
            "linux-enforced-isolation-ok"
            if env.linux_enforced_isolation and campaign.network_isolation == "required"
            else (
                "isolation-not-enforced"
                if not env.linux_enforced_isolation
                else "network-isolation-not-required"
            )
        ),
    )
    proxy = PreflightCheck(
        "credential-proxy-eligibility",
        campaign.credential_proxy,
        "credential-proxy-present" if campaign.credential_proxy else "credential-proxy-required",
    )
    return isolation, proxy


def _compile_codes() -> frozenset[str]:
    return frozenset(
        {
            "model-allowlist",
            "task-path",
            "task-package-digest",
            "task-verifier-digest",
            "wheelhouse-present",
            "wheelhouse-provenance",
        }
    )


def _calibrate_extra_codes() -> frozenset[str]:
    return frozenset(
        {
            "provider-presence",
            "docker-reachable",
            "disk-headroom",
            "trial-count-ceiling",
            "concurrency-ceiling",
            "prompt-token-ceiling",
        }
    )


def assess_campaign(
    campaign: OvernightCampaignPreflight,
    env: RunPreflightEnvironment,
    *,
    repo_root: Path = Path("."),
    provider_presence: ProviderPresenceProbe,
) -> OvernightCampaignResult:
    """Compute one campaign's checks and capability verdicts."""
    checks: list[PreflightCheck] = [
        provider_presence(campaign.provider),
        check_model_allowlist(campaign.provider, campaign.model),
    ]
    checks.extend(_task_checks(repo_root, campaign.task))
    checks.extend(_wheelhouse_checks(repo_root, campaign.wheelhouse))
    checks.extend(_ceiling_checks(campaign))
    checks.append(env.docker)
    checks.append(env.disk)
    checks.extend(_causal_checks(campaign, env))
    if env.os_name == "Darwin" or not env.network_isolation_enforced:
        checks.append(
            PreflightCheck(
                "darwin-calibration-only",
                True,
                "host-egress-isolation-not-enforced-run-is-calibration-only",
            )
        )

    by_code = {check.code: check for check in checks}

    compile_codes = _compile_codes()
    compile_reasons = tuple(
        sorted(check.code for code in compile_codes if (check := by_code.get(code)) and not check.ok)
    )
    may_compile = not compile_reasons

    calibrate_codes = compile_codes | _calibrate_extra_codes()
    calibrate_reasons = tuple(
        sorted(
            check.code
            for code in calibrate_codes
            if (check := by_code.get(code)) is not None and not check.ok
        )
    )
    may_calibrate = not calibrate_reasons

    promote_reasons: list[str] = []
    if campaign.evidence_mode != "causal":
        promote_reasons.append("evidence-mode-not-causal")
    if not may_calibrate:
        promote_reasons.append("calibration-gate-blocked")
    isolation = by_code.get("isolation-eligibility")
    proxy = by_code.get("credential-proxy-eligibility")
    if isolation is not None and not isolation.ok:
        promote_reasons.append(isolation.code)
    if proxy is not None and not proxy.ok:
        promote_reasons.append(proxy.code)
    if campaign.evidence_mode == "causal" and not env.linux_enforced_isolation:
        promote_reasons.append("host-isolation-not-enforced")
    may_promote_causal = (
        campaign.evidence_mode == "causal"
        and may_calibrate
        and isolation is not None
        and isolation.ok
        and proxy is not None
        and proxy.ok
    )
    promote_reasons = sorted(set(promote_reasons))

    if campaign.evidence_mode == "calibration":
        launch_gate = LAUNCH_GATE_CALIBRATE if may_calibrate else LAUNCH_GATE_REFUSED
    else:
        launch_gate = LAUNCH_GATE_PROMOTE_CAUSAL if may_promote_causal else LAUNCH_GATE_REFUSED

    return OvernightCampaignResult(
        campaign=campaign,
        checks=tuple(checks),
        may_compile=may_compile,
        compile_reasons=compile_reasons,
        may_calibrate=may_calibrate,
        calibrate_reasons=calibrate_reasons,
        may_promote_causal=may_promote_causal,
        promote_reasons=tuple(promote_reasons),
        launch_gate=launch_gate,
    )


def build_environment(
    *,
    docker_probe: DockerProbe | None = None,
    disk_probe: DiskProbe | None = None,
    network_policy: HarborNetworkPolicy | None = None,
    repo_root: Path | None = None,
) -> RunPreflightEnvironment:
    """Assemble the shared host facts for a preflight reading."""
    policy = network_policy if network_policy is not None else host_harbor_network_policy()
    return RunPreflightEnvironment(
        os_name=platform.system(),
        docker=(docker_probe or _default_docker_probe)(),
        disk=(disk_probe or _default_disk_probe)(repo_root or Path(".")),
        network_isolation_enforced=policy.network_isolation_enforced,
        network_isolation_reason=policy.network_isolation_reason,
    )


def build_run_preflight(
    repo_root: Path,
    campaigns: Sequence[OvernightCampaignPreflight],
    *,
    compile_only: bool = False,
    now: datetime | None = None,
    docker_probe: DockerProbe | None = None,
    disk_probe: DiskProbe | None = None,
    provider_presence: ProviderPresenceProbe | None = None,
    network_policy: HarborNetworkPolicy | None = None,
) -> RunPreflightReport:
    """Build a full preflight report for the given overnight campaigns.

    Fail-closed: ``launch_ok`` is true only when every campaign's requested
    evidence mode can be honored. ``compile_only`` keeps the report but does
    not gate on launch.
    """
    resolved_root = repo_root.resolve()
    presence = provider_presence or _default_provider_presence
    env = build_environment(
        docker_probe=docker_probe,
        disk_probe=disk_probe,
        network_policy=network_policy,
        repo_root=resolved_root,
    )

    results: list[OvernightCampaignResult] = []
    for campaign in campaigns:
        results.append(
            assess_campaign(
                campaign,
                env,
                repo_root=resolved_root,
                provider_presence=presence,
            )
        )

    launch_gates = {
        result.launch_gate for result in results if result.campaign.evidence_mode == "causal"
    }
    calibration_gates = {
        result.launch_gate for result in results if result.campaign.evidence_mode == "calibration"
    }
    launch_ok = (
        calibration_gates <= {LAUNCH_GATE_CALIBRATE}
        and launch_gates <= {LAUNCH_GATE_PROMOTE_CAUSAL}
    )
    if compile_only:
        launch_ok = True
    return RunPreflightReport(
        generated_at=now or datetime.now(UTC),
        environment=env,
        campaigns=tuple(results),
        compile_only=compile_only,
        launch_ok=launch_ok,
    )


def _check_line(check: PreflightCheck) -> str:
    marker = "ok" if check.ok else "FAIL"
    provider = f" provider={check.provider}" if check.provider is not None else ""
    return f"  [{marker}] {check.code}{provider} ({check.reason})"


def render_run_preflight(report: RunPreflightReport) -> str:
    """The whole surface as deterministic text (same bytes each run)."""
    env = report.environment
    lines = [
        f"OVERNIGHT RUN PREFLIGHT ({report.generated_at.isoformat()})",
        f"os={env.os_name} isolation_enforced={env.network_isolation_enforced} "
        f"isolation_reason={env.network_isolation_reason or 'none'}",
        f"mode={'compile-only' if report.compile_only else 'launch'} "
        f"launch_ok={report.launch_ok}",
        "ENVIRONMENT",
        _check_line(env.docker),
        _check_line(env.disk),
    ]
    for result in report.campaigns:
        c = result.campaign
        lines.append(
            f"CAMPAIGN {c.campaign_id} provider={c.provider} model={c.model} "
            f"evidence={c.evidence_mode} launch_gate={result.launch_gate}"
        )
        for check in result.checks:
            lines.append(_check_line(check))
        lines.append(
            f"  VERDICT compile={result.may_compile} "
            f"calibrate={result.may_calibrate} promote_causal={result.may_promote_causal}"
        )
        if result.compile_reasons:
            lines.append(f"  compile blocked: {', '.join(result.compile_reasons)}")
        if result.calibrate_reasons:
            lines.append(f"  calibrate blocked: {', '.join(result.calibrate_reasons)}")
        if result.promote_reasons:
            lines.append(f"  promote blocked: {', '.join(result.promote_reasons)}")
    return "\n".join(lines) + "\n"


def _check_to_dict(check: PreflightCheck) -> dict[str, Any]:
    return {
        "code": check.code,
        "ok": check.ok,
        "reason": check.reason,
        "provider": check.provider,
    }


def run_preflight_to_dict(report: RunPreflightReport) -> dict[str, Any]:
    """Canonical JSON-friendly report (sorted keys, deterministic)."""
    return {
        "generated_at": report.generated_at.isoformat(),
        "compile_only": report.compile_only,
        "launch_ok": report.launch_ok,
        "environment": {
            "os_name": report.environment.os_name,
            "docker": _check_to_dict(report.environment.docker),
            "disk": _check_to_dict(report.environment.disk),
            "network_isolation_enforced": report.environment.network_isolation_enforced,
            "network_isolation_reason": report.environment.network_isolation_reason,
        },
        "campaigns": [
            {
                "campaign_id": result.campaign.campaign_id,
                "provider": result.campaign.provider,
                "model": result.campaign.model,
                "evidence_mode": result.campaign.evidence_mode,
                "checks": [_check_to_dict(check) for check in result.checks],
                "verdict": {
                    "may_compile": result.may_compile,
                    "compile_reasons": list(result.compile_reasons),
                    "may_calibrate": result.may_calibrate,
                    "calibrate_reasons": list(result.calibrate_reasons),
                    "may_promote_causal": result.may_promote_causal,
                    "promote_reasons": list(result.promote_reasons),
                    "launch_gate": result.launch_gate,
                },
            }
            for result in report.campaigns
        ],
    }
