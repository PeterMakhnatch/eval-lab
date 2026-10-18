"""Native Modal environments with separate setup and task interpreter scopes.

``TaskModalEnvironment`` leaves the image/task PATH intact for agent commands
and tests. Only the benign environment setup healthcheck uses the helper PATH.
For a bundled separate verifier, startup runs the unchanged clean setup before
Harbor seeds agent artifacts. Its ready marker makes the verifier's later
setup call a no-op, so tests can use their original interpreter/dependencies.
No hidden tests are uploaded to the agent environment.

``RegradeModalEnvironment`` inherits that startup boundary and bounds sandbox
lifetime. ``harbor trial regrade`` has no ``--environment-kwarg`` option, so
the class supplies the bound; the lab admits it only under a
:class:`evallab.regrade.CloudSpendGate`.

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
from pathlib import Path, PurePosixPath
from typing import Any

from harbor.environments.modal import ModalEnvironment  # ty: ignore[unresolved-import]

from evallab.separate_verifier import (
    MIMO_STATE_DIR,
    SETUP_HELPER_ENV,
    V2_GRADE_DIR,
    V2_SETUP_SUBDIR,
    render_verifier_setup_bootstrap,
)

#: Longest a regrade's verifier sandbox may live: the MiMo verifier timeout
#: (2,100 s) plus artifact upload and teardown margin.
REGRADE_SANDBOX_TIMEOUT_SECS = 3600

#: Written into the trial directory for locked Modal trials: whether the
#: egress lock was requested, whether a restrictive policy was actually
#: applied by Modal, when, and by which observation.
EGRESS_LOCK_RECORD = "egress-lock.json"
MODAL_LOCK_MECHANISM = "modal-agent-phase-outbound-allowlist"

#: Opt-in capability: one provider create, with no inherited Tenacity retry.
NATIVE_SINGLE_CREATE_SUPPORTED = True

_RESTRICTIVE_MODES = frozenset({"no-network", "allowlist"})


_TESTS_DIR = "/tests"
_SETUP_BUNDLE_NAME = PurePosixPath(V2_SETUP_SUBDIR).name


class TaskModalEnvironment(ModalEnvironment):  # ty: ignore[unresolved-import]
    """Harbor Modal with a modern setup interpreter, not a global PATH pin."""

    def __init__(
        self,
        *args: Any,
        sandbox_create_max_attempts: int | None = None,
        **kwargs: Any,
    ) -> None:
        if sandbox_create_max_attempts is not None and (
            type(sandbox_create_max_attempts) is not int
            or sandbox_create_max_attempts != 1
        ):
            raise ValueError("sandbox_create_max_attempts must be 1 or None")
        super().__init__(*args, **kwargs)
        self._sandbox_create_max_attempts = sandbox_create_max_attempts

    async def _create_sandbox(  # ty: ignore[unresolved-import]
        self,
        *,
        entrypoint: Any = None,
        block_network: bool | None = None,
        experimental_options: Any = None,
    ) -> Any:
        create = super()._create_sandbox
        arguments = {
            "entrypoint": entrypoint,
            "block_network": block_network,
            "experimental_options": experimental_options,
        }
        if self._sandbox_create_max_attempts == 1:
            # A lost response may already have allocated a billable sandbox.
            # Harbor 0.24's default two-create retry cannot fit that reservation.
            implementation = getattr(create, "__wrapped__", None)
            if implementation is None:
                raise RuntimeError("pinned Harbor Modal single-create entry is unavailable")
            return await implementation(self, **arguments)
        return await create(**arguments)

    async def run_healthcheck(self, healthcheck: Any = None) -> None:
        # The readable environment setup is benign; do not inspect or stage
        # sibling tests/ to prepare an agent sandbox. Per-step overrides and
        # ordinary healthchecks without this setup contract keep task PATH.
        if healthcheck is not None or not (
            self.environment_dir / "setup" / "setup.sh"
        ).is_file():
            await super().run_healthcheck(healthcheck)
            return
        with self.scoped_exec_env(SETUP_HELPER_ENV):
            await super().run_healthcheck(healthcheck)

    async def start(self, force_build: bool) -> None:
        await super().start(force_build)
        # Harbor 0.24 disables environment-dir upload for dedicated verifier
        # images. Only that environment may consume the bundled clean setup;
        # the primary environment never reads/uploads the hidden tests.
        if self._enable_environment_dir_upload or not (
            self.environment_dir / _SETUP_BUNDLE_NAME / "setup.sh"
        ).is_file():
            return
        with self.scoped_exec_env(SETUP_HELPER_ENV):
            result = await self.exec(
                render_verifier_setup_bootstrap(
                    tests_dir=_TESTS_DIR, state_dir=MIMO_STATE_DIR, grade_dir=V2_GRADE_DIR
                ),
                timeout_sec=self._sandbox_timeout,
                user="root",
            )
        # test.sh reopens its own setup.log on each repeat. Keep the actual
        # bootstrap stdout/stderr outside verifier/ so those reruns cannot
        # erase the cleanup and ready-marker evidence.
        setup_log = (
            Path(self.trial_paths.trial_dir) / f"modal-setup-{self.session_id}.log"
        )
        setup_log.parent.mkdir(parents=True, exist_ok=True)
        setup_log.write_text(
            (result.stdout or "")
            + (result.stderr or "")
            + f"\nmodal setup: return_code={result.return_code}\n"
        )
        if result.return_code != 0:
            raise RuntimeError(
                f"Modal verifier setup failed (exit {result.return_code}); see {setup_log}"
            )


class RegradeModalEnvironment(TaskModalEnvironment):
    """Native setup/task interpreter separation with a bounded regrade lifetime."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["sandbox_timeout_secs"] = min(
            int(kwargs.get("sandbox_timeout_secs", REGRADE_SANDBOX_TIMEOUT_SECS)),
            REGRADE_SANDBOX_TIMEOUT_SECS,
        )
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


class LockedModalEnvironment(TaskModalEnvironment):
    """Record the actual effective egress policy accepted by Modal."""

    def __init__(
        self,
        *args: Any,
        evallab_egress_lock: bool | str = False,
        **kwargs: Any,
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
