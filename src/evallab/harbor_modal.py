"""Modal environments for the lab.

``RegradeModalEnvironment`` serves paid snapshot regrades (``harbor
trial regrade``): that path takes ``--env`` but no
``--environment-kwarg``, so the two settings every lab Modal run passes
as kwargs (:func:`evallab.execution_contracts.build_command`) are fixed
by the class — the ``persistent_env`` PATH pin
(:data:`evallab.execution_contracts.MODAL_PATH_PIN`), so the verifier's
setup runs under the image's /usr/bin interpreter, as on Docker, and a
bounded sandbox lifetime (Harbor's default is a billable 24 hours,
which a dead controller would otherwise leave running). Import through
``evallab.harbor_modal:RegradeModalEnvironment``; the regrade path
admits it only under a :class:`evallab.regrade.CloudSpendGate`.

``LockedModalEnvironment`` records the applied agent-phase egress lock:
Harbor's ``ModalEnvironment`` enforces per-phase outbound policies at
sandbox creation and through ``_apply_network_policy``, but leaves no
on-disk record of what was actually applied; the setup fingerprint's
runtime lock check reads ``egress-lock.json`` from the trial directory
(the daytona/docker wrappers write theirs). The record is written from
the *effective* arguments actually sent to Modal — an explicit
``block_network`` override wins over the static policy (Harbor's DinD
path passes ``False`` for image pulls, which sends public networking
even under a no-network baseline) — and from each policy application
Modal accepts. ``applied`` is true only when the effective sent policy
is restrictive (``no-network``, or an allowlist); otherwise the record
says ``applied: false``. Nothing is fabricated from spec intent.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harbor.environments.modal import ModalEnvironment  # ty: ignore[unresolved-import]

from evallab.execution_contracts import MODAL_PATH_PIN

#: Longest a regrade's verifier sandbox may live: the MiMo verifier timeout
#: (2,100 s) plus artifact upload and teardown margin.
REGRADE_SANDBOX_TIMEOUT_SECS = 3600

#: Written into the trial directory for locked Modal trials: whether the
#: egress lock was requested, whether a restrictive policy was actually
#: applied by Modal, when, and by which observation.
EGRESS_LOCK_RECORD = "egress-lock.json"
MODAL_LOCK_MECHANISM = "modal-agent-phase-outbound-allowlist"

_RESTRICTIVE_MODES = frozenset({"no-network", "allowlist"})


class RegradeModalEnvironment(ModalEnvironment):  # ty: ignore[unresolved-import]
    """Harbor's native Modal environment with the lab's PATH pin and lifetime bound."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["sandbox_timeout_secs"] = min(
            int(kwargs.get("sandbox_timeout_secs", REGRADE_SANDBOX_TIMEOUT_SECS)),
            REGRADE_SANDBOX_TIMEOUT_SECS,
        )
        kwargs["persistent_env"] = {**(kwargs.get("persistent_env") or {}), "PATH": MODAL_PATH_PIN}
        super().__init__(*args, **kwargs)


def _mode_value(policy: Any) -> str:
    mode = getattr(policy, "network_mode", None)
    value = getattr(mode, "value", mode)
    return str(value) if value is not None else ""


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
        mode, hosts = self._effective_creation_policy(block_network)
        self._record_application(
            source="creation", mode=mode, hosts=hosts, sandbox=sandbox
        )
        return sandbox

    def _effective_creation_policy(
        self, block_network: bool | None
    ) -> tuple[str, list[str]]:
        """The policy actually sent to Modal at sandbox creation.

        Mirrors ``ModalEnvironment._create_sandbox``: dynamic networking
        sends the startup policy's outbound kwargs (and forces
        ``block_network=False``); otherwise an explicit ``block_network``
        override wins over the static policy — Harbor's DinD path passes
        ``False`` for image pulls, which is public networking even when
        the baseline is no-network, and must not record as locked.
        """
        policy = self.network_policy
        if getattr(self, "_dynamic_network", False):
            return _mode_value(policy), _policy_hosts(policy)
        effective_block = block_network
        if effective_block is None:
            effective_block = self._network_disabled
        if effective_block:
            return "no-network", []
        if self._network_is_allowlist:
            return "allowlist", _policy_hosts(policy)
        return "public", []

    async def _apply_network_policy(self, network_policy: Any) -> None:  # ty: ignore[unresolved-import]
        await super()._apply_network_policy(network_policy)
        # Modal accepted the outbound-policy call: this application is
        # observed fact, so it may raise the record to applied.
        self._record_application(
            source="apply",
            mode=_mode_value(network_policy),
            hosts=_policy_hosts(network_policy),
            sandbox=getattr(self, "_sandbox", None),
        )

    def _record_application(
        self, *, source: str, mode: str, hosts: list[str], sandbox: Any
    ) -> None:
        try:
            application = {
                "source": source,
                "network_mode": mode,
                "allowed_hosts": list(hosts),
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

            def _existing_fingerprint(item: Any) -> tuple[Any, ...]:
                item_hosts = item.get("allowed_hosts") if isinstance(item, dict) else None
                return (
                    item.get("source") if isinstance(item, dict) else None,
                    item.get("network_mode") if isinstance(item, dict) else None,
                    tuple(item_hosts) if isinstance(item_hosts, list) else (),
                    item.get("sandbox_id") if isinstance(item, dict) else None,
                )

            fingerprint = (
                application["source"],
                application["network_mode"],
                tuple(application["allowed_hosts"]),
                application["sandbox_id"],
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
