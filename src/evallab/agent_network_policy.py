"""Agent-phase egress lock for MiMo task packages (``agent-network-none@1``).

V6 (fetching the upstream fix / newer release over the network) is closed on
Eval Lab's own runs only by the run-time egress lock. Harbor's Docker egress
sidecar is opt-in per task: when every declared policy is ``public`` (the
MiMo default — clean packages carry ``[environment] network_mode = "public"``
and no phase override) no sidecar is attached at all
(``docker.py:_requires_egress_control``), so a clean package run by anyone
else leaves V6 open. FineEnvs 1.3.0 only adds an ``/etc/hosts`` blocklist
that a root agent can rewrite.

The declaration this transform adds is the strongest task-level policy that
still leaves network-dependent graders working, as Harbor 0.24.0 implements
it:

* ``[agent] network_mode = "no-network"`` — an explicit *agent-phase*
  override (``models/task/config.py:PhaseNetworkPolicyConfig``: ``[agent]``
  and ``[verifier]`` carry a phase override used only when set). The
  effective agent policy during ``agent.run()`` is this override
  (``trial/network_policy.py:resolve_agent_phase_policy``), applied through
  ``trial.py:_phase_network_policy`` (``set_network_policy`` before the
  agent runs, baseline restored after).
* The verifier is untouched: no ``[verifier]`` phase override and no
  ``[verifier.environment]`` table, so under
  ``[verifier] environment_mode = "separate"`` (what separate-verifier@3
  declares) the verifier environment is a fresh copy of the still-public
  ``[environment]`` (``models/task/verifier_mode.py:
  resolve_effective_verifier_env_config``) and the verifier phase resolves
  to that public baseline (``resolve_verifier_phase_policy`` falls back to
  the baseline when no explicit override exists). Graders that fetch at
  grade time keep working. Environment setup/build also keeps network —
  only the agent phase is locked.

Why not ``[environment] network_mode = "no-network"``: the verifier
baseline would inherit it (fresh copy of ``[environment]``), so the
separate-verifier image build and setup would run offline and network
graders would break unless rescued by a phase switch; agent setup would
also lose network. The phase override locks exactly the window the agent
controls and nothing else.

Backend enforcement as Harbor 0.24.0 implements it (all fail closed —
unsupported policies raise instead of running open):

* Docker (Linux containers): any non-public policy attaches the egress
  sidecar (``network_mode: service:sidecar``) and ``no-network`` runs
  ``network-policy deny-all`` — an nftables output chain redirecting TCP
  to a transparent proxy with an empty allowlist, so ``pip``/``curl``/``git
  ls-remote`` over TCP all fail. By sidecar design DNS to the configured
  resolvers and ICMP still pass (``bin/network-policy:nft_dns_rules``);
  names resolve but nothing can be fetched. Windows containers reject
  non-public policies outright, hence the ``os`` guard below.
* Daytona: ``no-network`` maps to ``network_block_all=True`` at sandbox
  create and on phase switch (``daytona/environment.py``), with
  ``dynamic_network_policy`` outside compose mode.
* Modal: ``no-network`` maps to empty outbound domain + CIDR allowlists
  (``modal.py:_dynamic_network_policy_spec_args``), with
  ``disable_internet``/``dynamic_network_policy`` outside compose mode.
  Modal tasks that ship their own ``docker-compose.yaml`` run in compose
  (DinD) mode where Modal cannot enforce network isolation
  (``disable_internet=False``) — MiMo task packages ship only
  ``environment/Dockerfile``, so this does not apply to the clean set, but
  a future compose-based task would fail closed at capability validation
  rather than run open.

Chain position: order-independent with respect to separate-verifier@3
(the verifier tables are untouched, and @3's ``read_parent_info`` does not
reject an ``[agent]`` override), but apply it after the rest of the clean
chain so the network posture is derived from the final package. Chain
adoption is the clean-set owner's call; this derivation records
``default_chain: False`` like other non-adopted variants.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from evallab.task_variants import VariantInvalid, VariantRecord, derive_task

#: Transform id recorded in lineage.
TRANSFORM_ID = "agent-network-none@1"

#: Package-relative path this transform rewrites.
TASK_TOML_REL = "task.toml"

#: The agent-phase policy this transform declares.
AGENT_NETWORK_MODE = "no-network"


def _section_key_present(config: dict[str, Any], section: str, key: str) -> bool:
    table = config.get(section)
    return isinstance(table, dict) and key in table


def build_task_toml(parent_text: str) -> str:
    """Parent ``task.toml`` plus ``[agent] network_mode = "no-network"``.

    Refuses a parent that already declares an agent-phase network policy
    (second application), an ``[agent] allowed_hosts`` without a mode
    (combining it with ``no-network`` would be an invalid Harbor config),
    a Windows target OS (Harbor's Docker backend rejects non-public
    policies for Windows containers), or unparsable TOML. The result is
    proven to parse with the override landed and every other parsed field
    identical.
    """
    try:
        config = tomllib.loads(parent_text)
    except tomllib.TOMLDecodeError as exc:
        raise VariantInvalid(f"parent {TASK_TOML_REL} is not valid TOML: {exc}") from exc

    agent = config.get("agent")
    if isinstance(agent, dict):
        if "network_mode" in agent:
            raise VariantInvalid(
                "parent already declares [agent] network_mode "
                f"({agent['network_mode']!r}); refusing a second agent-network-none@1"
            )
        if "allowed_hosts" in agent:
            raise VariantInvalid(
                "parent [agent] carries allowed_hosts without network_mode; "
                "adding no-network would be an invalid Harbor config"
            )
    elif agent is not None:
        raise VariantInvalid("parent [agent] table is not a table")

    environment = config.get("environment")
    if isinstance(environment, dict) and str(environment.get("os", "linux")).lower() == "windows":
        raise VariantInvalid(
            "parent targets Windows; Harbor's Docker backend rejects "
            "non-public network policies for Windows containers"
        )

    lines = parent_text.splitlines(keepends=True)
    try:
        agent_idx = next(i for i, line in enumerate(lines) if line.strip() == "[agent]")
    except StopIteration:
        new_text = parent_text if parent_text.endswith("\n") or not parent_text else parent_text + "\n"
        new_text += f'\n[agent]\nnetwork_mode = "{AGENT_NETWORK_MODE}"\n'
    else:
        lines.insert(agent_idx + 1, f'network_mode = "{AGENT_NETWORK_MODE}"\n')
        new_text = "".join(lines)

    try:
        new_config = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise VariantInvalid(f"derived {TASK_TOML_REL} does not parse: {exc}") from exc
    new_agent = new_config.get("agent")
    if not isinstance(new_agent, dict) or new_agent.get("network_mode") != AGENT_NETWORK_MODE:
        raise VariantInvalid("derived task.toml did not land [agent] network_mode")

    # Prove nothing else changed: the parsed configs must agree once the
    # single declared value is masked out.
    masked_parent = tomllib.loads(parent_text)
    masked_new = tomllib.loads(new_text)
    if isinstance(masked_parent.get("agent"), dict):
        masked_parent["agent"] = {k: v for k, v in masked_parent["agent"].items()}
        masked_parent["agent"]["network_mode"] = "<agent-network-none@1>"
    else:
        masked_parent["agent"] = {"network_mode": "<agent-network-none@1>"}
    masked_new["agent"]["network_mode"] = "<agent-network-none@1>"
    if masked_parent != masked_new:
        raise VariantInvalid("derived task.toml changed fields beyond [agent] network_mode")
    return new_text


def build_changes(
    parent_dir: Path | str,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """Build the ``derive_task`` changes mapping plus lineage inputs.

    Only ``task.toml`` changes; instruction, environment, tests, solution
    and metadata bytes are untouched by construction.
    """
    parent = Path(parent_dir)
    try:
        parent_text = (parent / TASK_TOML_REL).read_text(encoding="utf-8")
    except OSError as exc:
        raise VariantInvalid(f"parent has no readable {TASK_TOML_REL}: {exc}") from exc
    new_text = build_task_toml(parent_text)
    changes: dict[str, bytes | None] = {TASK_TOML_REL: new_text.encode("utf-8")}
    inputs: dict[str, Any] = {
        "agent_network_mode": AGENT_NETWORK_MODE,
        "verifier_network": "unchanged (public baseline via [environment] copy)",
        "source": "harbor-0.24 task-level [agent] phase network policy",
        "default_chain": False,
    }
    return changes, inputs


def derive_agent_network_none(
    parent_dir: Path | str,
    *,
    rationale: str = (
        "Declare [agent] network_mode = \"no-network\" so the agent phase "
        "cannot fetch the upstream fix / newer release (V6) on any Harbor "
        "backend, while the separate verifier keeps the public baseline "
        "that network-dependent graders need."
    ),
    created_by: str = "agent-network-policy",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive the ``agent-network-none@1`` variant of a package."""
    parent = Path(parent_dir)
    changes, inputs = build_changes(parent)
    kwargs: dict[str, Any] = {}
    if repo_root is not None:
        kwargs["repo_root"] = repo_root
    if parent_source is not None:
        kwargs["parent_source"] = parent_source
    if variants_root is not None:
        kwargs["variants_root"] = variants_root
    return derive_task(
        parent,
        changes=changes,
        transform=TRANSFORM_ID,
        rationale=rationale,
        created_by=created_by,
        inputs=inputs,
        **kwargs,
    )


__all__ = [
    "AGENT_NETWORK_MODE",
    "TASK_TOML_REL",
    "TRANSFORM_ID",
    "build_changes",
    "build_task_toml",
    "derive_agent_network_none",
]
