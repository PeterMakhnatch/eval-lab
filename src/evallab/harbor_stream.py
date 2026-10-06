"""Pre-stage Harbor's pinned code-server before the agent-phase egress lock.

Called from :meth:`NativeMimoAgent.setup` (agent setup runs under the baseline
network policy, before Trial applies the agent-phase lock in ``run``). When the
trial requested ``--stream``, the later SSH viewer launch
(``harbor/viewer/stream.sh``) downloads a pinned code-server archive from
GitHub unless ``code-server`` is already on ``PATH``. Downloading after the
lock would fail, so this helper stages the exact same pinned asset while
egress is still open.

Trusted pins, not a competing table: the code-server version and per-arch
digests are loaded lazily from the installed Harbor package's own
``harbor/viewer/stream.sh`` asset (bounded anchored patterns). If that asset
is absent or unparsable (e.g. pre-upgrade installs without the stream viewer),
the helper records an explicit ``pin_unavailable`` failure and stages nothing.

Bound lock gate: ``BoundedDaytonaEnvironment.exec`` applies the egress lock on
the first exec once ``_egress_lock_due`` is set, so this helper reads the
actual ``_egress_locked`` / ``_egress_lock_due`` flags and the public
``network_policy`` BEFORE ANY sandbox exec. When due or locked, even the
already-on-PATH binary cannot be discovered without triggering the lock, so
the helper performs zero execs and records the ``locked`` conflict instead.
An existing on-PATH binary is otherwise preserved as-is (never replaced);
only a numeric ``x.y.z`` version parsed from its output is attested, with no
claim about compatibility against the archive pin.

Fail-open observer on ``Exception``: installer/observer failures never raise
into setup and the existing lock/policy is never written. ``attestation``
lands at ``logs_dir/stream-preparation.json`` (atomic tmp+replace, no
credentials, host paths, tracebacks, or raw command output). By design
``asyncio.CancelledError`` (``BaseException``, not ``Exception``) propagates
unchanged. Each exec receives a timeout; provider and filesystem completion
have no hard end-to-end bound.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

_RELEASE_HOST = "github.com"
_ATTESTATION_NAME = "stream-preparation.json"

_VERSION_RE = re.compile(r"(?m)^version=(\d+\.\d+\.\d+)\s*$")
_DIGEST_RE = re.compile(r"(?m)^\s*arch=(amd64|arm64)\s*\n\s*digest=([0-9a-f]{64})\s*$")
_SEMVER_RE = re.compile(r"^(\d+\.\d+\.\d+)(?:[ \t]|$)")
_ARCHES = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


class _PinError(Exception):
    """Installed Harbor viewer asset missing or unparsable."""


def _trusted_stream_sh() -> str:
    try:
        spec = importlib.util.find_spec("harbor")
    except Exception as exc:
        raise _PinError("cannot locate installed Harbor package") from exc
    if spec is None or not spec.origin:
        raise _PinError("installed Harbor package has no origin")
    try:
        text = (Path(spec.origin).parent / "viewer" / "stream.sh").read_text(encoding="utf-8")
    except OSError as exc:
        raise _PinError("cannot read trusted stream.sh") from exc
    if not text.strip():
        raise _PinError("trusted stream.sh is empty")
    return text


def _load_pins() -> tuple[str, dict[str, str]]:
    text = _trusted_stream_sh()
    version = _VERSION_RE.search(text)
    digests = dict(_DIGEST_RE.findall(text))
    if version is None or set(digests) != {"amd64", "arm64"}:
        raise _PinError("trusted stream.sh lacks pinned version/arch digests")
    return version.group(1), digests


def _network_mode(environment: Any) -> str | None:
    mode = getattr(getattr(environment, "network_policy", None), "network_mode", None)
    value = getattr(mode, "value", mode)
    return str(value) if value is not None else None


def _semver(text: Any) -> str | None:
    if not isinstance(text, str):
        return None
    match = _SEMVER_RE.match(text)
    return match.group(1) if match else None


def _attest(logs_dir: Path, record: dict[str, Any]) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    temporary = logs_dir / (_ATTESTATION_NAME + ".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(logs_dir / _ATTESTATION_NAME)


def _record(
    *,
    outcome: str,
    requested: bool,
    network_mode: str | None,
    pin: str | None = None,
    arch: str | None = None,
    version: str | None = None,
    checksum: bool = False,
    failure: str | None = None,
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "stream_requested": requested,
        "code_server_pin": pin,
        "arch": arch,
        "network_mode": network_mode,
        "code_server_version": version,
        "checksum_verified": checksum,
        "failure": failure,
    }


async def prepare_locked_stream(environment: Any, logs_dir: Path) -> None:
    """Pre-stage pinned code-server for a requested ``--stream`` trial.

    Fail-open on ``Exception``; ``asyncio.CancelledError`` propagates.
    """
    try:
        await _prepare(environment, logs_dir)
    except Exception:
        try:
            _attest(
                logs_dir,
                _record(outcome="failed", requested=True, network_mode=None,
                        failure="unexpected"),
            )
        except Exception:
            pass


async def _prepare(environment: Any, logs_dir: Path) -> None:
    requested = bool(getattr(environment, "stream_enabled", False))
    network_mode = _network_mode(environment)
    if not requested:
        _attest(logs_dir, _record(
            outcome="not_requested", requested=False, network_mode=network_mode))
        return

    # Zero execs past this point when the Bound lock is held or due: the
    # first exec while due would take the lock unexpectedly.
    if bool(getattr(environment, "_egress_locked", False)):
        _attest(logs_dir, _record(
            outcome="locked", requested=True, network_mode=network_mode,
            failure="egress_locked"))
        return
    if bool(getattr(environment, "_egress_lock_due", False)):
        _attest(logs_dir, _record(
            outcome="locked", requested=True, network_mode=network_mode,
            failure="egress_lock_due"))
        return

    capabilities = getattr(environment, "capabilities", None)
    if getattr(capabilities, "stream", None) is False:
        _attest(logs_dir, _record(
            outcome="unsupported", requested=True, network_mode=network_mode,
            failure="environment_without_stream_capability"))
        return

    if network_mode == "no-network":
        _attest(logs_dir, _record(
            outcome="locked", requested=True, network_mode=network_mode,
            failure="egress_already_locked"))
        return
    if network_mode == "allowlist" and not _release_host_allowed(environment):
        _attest(logs_dir, _record(
            outcome="locked", requested=True, network_mode=network_mode,
            failure="allowlist_without_release_host"))
        return

    try:
        pin, digests = _load_pins()
    except _PinError:
        _attest(logs_dir, _record(
            outcome="failed", requested=True, network_mode=network_mode,
            failure="pin_unavailable"))
        return

    present = await environment.exec("command -v code-server", timeout_sec=30)
    if getattr(present, "return_code", 1) == 0:
        observed = await environment.exec(
            "code-server --version 2>/dev/null", timeout_sec=30)
        version = _semver(getattr(observed, "stdout", None))
        verified = getattr(observed, "return_code", 1) == 0 and version is not None
        _attest(logs_dir, _record(
            outcome="already_present" if verified else "failed",
            requested=True, network_mode=network_mode, pin=pin,
            version=version if verified else None,
            failure=None if verified else "version_unverified"))
        return

    machine = await environment.exec("uname -m", timeout_sec=30)
    arch = _ARCHES.get(_first_token(getattr(machine, "stdout", None)))
    digest = digests.get(arch or "")
    if arch is None or not digest:
        _attest(logs_dir, _record(
            outcome="failed", requested=True, network_mode=network_mode,
            pin=pin, failure="unsupported_arch"))
        return

    install = await environment.exec(
        _install_script(pin, arch, digest), timeout_sec=300)
    stdout = getattr(install, "stdout", None)
    stdout_text = stdout if isinstance(stdout, str) else ""
    if getattr(install, "return_code", 1) != 0:
        _attest(logs_dir, _record(
            outcome="failed", requested=True, network_mode=network_mode,
            pin=pin, arch=arch,
            checksum="HARBOR_STREAM_STAGE=verified" in stdout_text,
            failure=_install_failure(stdout_text)))
        return

    observed = await environment.exec("code-server --version 2>/dev/null", timeout_sec=30)
    version = _semver(getattr(observed, "stdout", None))
    if getattr(observed, "return_code", 1) != 0 or version != pin:
        _attest(logs_dir, _record(
            outcome="failed", requested=True, network_mode=network_mode,
            pin=pin, arch=arch, checksum=True,
            failure="version_unverified" if version is None else "version_mismatch"))
        return
    _attest(logs_dir, _record(
        outcome="prestaged", requested=True, network_mode=network_mode,
        pin=pin, arch=arch, version=version, checksum=True))


def _release_host_allowed(environment: Any) -> bool:
    hosts = getattr(
        getattr(environment, "network_policy", None), "allowed_hosts", None) or []
    return any(
        isinstance(entry, str) and entry.strip().lower()
        in (_RELEASE_HOST, "*." + _RELEASE_HOST)
        for entry in hosts
    )


def _first_token(stdout: Any) -> str:
    """First whitespace token of command output, else empty string."""
    if not isinstance(stdout, str) or not stdout.split():
        return ""
    return stdout.strip().split()[0]


def _install_script(pin: str, arch: str, digest: str) -> str:
    # Mirrors the trusted harbor/viewer/stream.sh (pinned version, per-arch
    # digest, checksum before extract) but stages into a fixed directory and
    # links the binary onto PATH so the later viewer launch needs no fetch.
    # Stage markers on stdout attribute failures without leaking stderr.
    return (
        "set -eu\n"
        'stage=""\n'
        "for candidate in /opt/harbor-code-server \"$HOME/.harbor-code-server\"; do\n"
        '  if mkdir -p "$candidate" 2>/dev/null; then stage="$candidate"; break; fi\n'
        "done\n"
        '[ -n "$stage" ] || exit 11\n'
        'trap \'rm -f "$stage/archive.tar.gz"\' EXIT\n'
        "curl --fail --location --silent --show-error --max-time 120 "
        f'"https://github.com/coder/code-server/releases/download/v{pin}/'
        f'code-server-{pin}-linux-{arch}.tar.gz" '
        '-o "$stage/archive.tar.gz"\n'
        "echo HARBOR_STREAM_STAGE=downloaded\n"
        f'printf \'%s  %s\\n\' "{digest}" "$stage/archive.tar.gz"'
        " | sha256sum --check --status\n"
        "echo HARBOR_STREAM_STAGE=verified\n"
        'tar -xzf "$stage/archive.tar.gz" -C "$stage"\n'
        "echo HARBOR_STREAM_STAGE=extracted\n"
        f'ln -sf "$stage/code-server-{pin}-linux-{arch}/bin/code-server"'
        " /usr/local/bin/code-server\n"
        "command -v code-server >/dev/null\n"
        "echo HARBOR_STREAM_STAGE=linked\n"
    )


def _install_failure(stdout_text: str) -> str:
    if "HARBOR_STREAM_STAGE=extracted" in stdout_text:
        return "link_failed"
    if "HARBOR_STREAM_STAGE=verified" in stdout_text:
        return "extract_failed"
    if "HARBOR_STREAM_STAGE=downloaded" in stdout_text:
        return "checksum_mismatch"
    return "download_failed"


__all__ = ["prepare_locked_stream"]
