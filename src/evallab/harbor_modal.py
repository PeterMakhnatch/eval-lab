"""Modal transport with a recorded agent-phase egress lock.

Harbor's ``ModalEnvironment`` enforces per-phase outbound policies at
sandbox creation and through ``_apply_network_policy``, but leaves no
on-disk record of what was actually applied; the setup fingerprint's
runtime lock check reads ``egress-lock.json`` from the trial directory
(the daytona/docker wrappers write theirs). ``LockedModalEnvironment``
captures the policy from the creation result and from each successful
policy application — the exact network mode and allowlist hosts Harbor
passed to Modal, plus the created sandbox's id — and writes that
record. ``applied`` is true only when an observed policy is actually
restrictive (``no-network``, or an allowlist); a public policy records
``applied: false``. Nothing is fabricated from spec intent: if no
restrictive policy is ever applied, the record says so.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harbor.environments.modal import ModalEnvironment  # ty: ignore[unresolved-import]

#: Written into the trial directory for locked Modal trials: whether the
#: egress lock was requested, whether a restrictive policy was actually
#: applied by Modal, when, and by which observation.
EGRESS_LOCK_RECORD = "egress-lock.json"
MODAL_LOCK_MECHANISM = "modal-agent-phase-outbound-allowlist"

_RESTRICTIVE_MODES = frozenset({"no-network", "allowlist"})


def _mode_value(policy: Any) -> str:
    mode = getattr(policy, "network_mode", None)
    value = getattr(mode, "value", mode)
    return str(value) if value is not None else ""


def policy_is_restrictive(policy: Any) -> bool:
    """True when a policy Harbor applied actually restricts egress."""
    return _mode_value(policy) in _RESTRICTIVE_MODES


def _policy_hosts(policy: Any) -> list[str]:
    hosts = getattr(policy, "allowed_hosts", None) or []
    return [str(host) for host in hosts]


def _parse_lock_flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


class LockedModalEnvironment(ModalEnvironment):  # ty: ignore[unresolved-import]
    """Modal environment that records the applied egress policy."""

    def __init__(
        self, *args: Any, evallab_egress_lock: bool | str = False, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self._evallab_egress_lock = _parse_lock_flag(evallab_egress_lock)

    async def _create_sandbox(  # ty: ignore[unresolved-import]
        self,
        *,
        entrypoint: Any = None,
        block_network: bool | None = None,
        experimental_options: Any = None,
    ) -> Any:
        sandbox = await super()._create_sandbox(
            entrypoint=entrypoint,
            block_network=block_network,
            experimental_options=experimental_options,
        )
        # The created sandbox carries Harbor's startup policy; record what
        # creation actually produced, never the spec's intent alone.
        self._record_policy(source="creation", policy=self.network_policy, sandbox=sandbox)
        return sandbox

    async def _apply_network_policy(self, network_policy: Any) -> None:  # ty: ignore[unresolved-import]
        await super()._apply_network_policy(network_policy)
        # Modal accepted the outbound-policy call: this application is
        # observed fact, so it may raise the record to applied.
        self._record_policy(
            source="apply",
            policy=network_policy,
            sandbox=getattr(self, "_sandbox", None),
        )

    def _record_policy(self, *, source: str, policy: Any, sandbox: Any) -> None:
        try:
            mode = _mode_value(policy)
            application = {
                "source": source,
                "network_mode": mode,
                "allowed_hosts": _policy_hosts(policy),
                "applied": mode in _RESTRICTIVE_MODES,
                "sandbox_id": getattr(sandbox, "object_id", None)
                or getattr(sandbox, "id", None),
                "recorded_at": datetime.now(UTC).isoformat(),
            }
            trial_dir = Path(self.trial_paths.trial_dir)
            record_path = trial_dir / EGRESS_LOCK_RECORD
            record: dict[str, Any] = {}
            if record_path.is_file():
                try:
                    existing = json.loads(record_path.read_text())
                    if isinstance(existing, dict):
                        record = existing
                except (OSError, json.JSONDecodeError):
                    record = {}
            applications = record.get("applications")
            if not isinstance(applications, list):
                applications = []
            fingerprint = (
                application["source"],
                application["network_mode"],
                tuple(application["allowed_hosts"]),
                application["sandbox_id"],
            )
            def _existing_fingerprint(item: Any) -> tuple[Any, ...]:
                hosts = item.get("allowed_hosts") if isinstance(item, dict) else None
                return (
                    item.get("source") if isinstance(item, dict) else None,
                    item.get("network_mode") if isinstance(item, dict) else None,
                    tuple(hosts) if isinstance(hosts, list) else (),
                    item.get("sandbox_id") if isinstance(item, dict) else None,
                )

            if not any(
                _existing_fingerprint(item) == fingerprint for item in applications
            ):
                applications.append(application)
            applied_apps = [item for item in applications if item.get("applied")]
            current = applied_apps[-1] if applied_apps else application
            now = datetime.now(UTC).isoformat()
            record.update(
                {
                    "schema_version": 2,
                    "requested": bool(record.get("requested"))
                    or self._evallab_egress_lock,
                    "applied": bool(applied_apps),
                    "locked_at": record.get("locked_at")
                    or (now if applied_apps else None),
                    "recorded_at": now,
                    "sandbox_id": current.get("sandbox_id"),
                    "session_id": getattr(self, "session_id", None),
                    "network_block_all": current.get("network_mode") == "no-network",
                    "network_mode": current.get("network_mode"),
                    "allowed_hosts": current.get("allowed_hosts") or [],
                    "mechanism": MODAL_LOCK_MECHANISM if applied_apps else None,
                    "applications": applications,
                    "error": None,
                }
            )
            trial_dir.mkdir(parents=True, exist_ok=True)
            record_path.write_text(json.dumps(record, indent=2) + "\n")
        except Exception as error:  # never fail the trial for the record
            self.logger.warning("could not write egress-lock.json: %s", error)
