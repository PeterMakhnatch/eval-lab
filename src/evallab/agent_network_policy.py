from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from evallab.task_variants import VariantInvalid, VariantRecord, derive_task

TRANSFORM_ID = "agent-network-none@1"

TASK_TOML_REL = "task.toml"

AGENT_NETWORK_MODE = "no-network"


_AGENT_HEADER_RE = re.compile(r"^\s*\[\s*agent\s*\]\s*(#.*)?$")
_AGENT_INLINE_RE = re.compile(r"""^\s*["']?agent["']?\s*=""")
_AGENT_DOTTED_RE = re.compile(r"""^\s*["']?agent["']?\s*\.""")


def build_task_toml(parent_text: str) -> str:
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
    insert_line = f'network_mode = "{AGENT_NETWORK_MODE}"\n'
    candidates: list[str] = []
    for idx, line in enumerate(lines):
        if _AGENT_HEADER_RE.match(line):
            head = lines[: idx + 1]
            if not head[-1].endswith("\n"):
                head[-1] += "\n"
            candidates.append("".join(head) + insert_line + "".join(lines[idx + 1 :]))
    appended = parent_text if parent_text.endswith("\n") or not parent_text else parent_text + "\n"
    candidates.append(appended + f"\n[agent]\n{insert_line}")

    first_error: str | None = None
    for candidate in candidates:
        error = _candidate_error(parent_text, candidate)
        if error is None:
            return candidate
        first_error = first_error or error

    if isinstance(agent, dict):
        for line in lines:
            if _AGENT_INLINE_RE.match(line):
                raise VariantInvalid(
                    "parent declares agent as an inline table (agent = { ... }); "
                    "rewrite it as an [agent] table before applying agent-network-none@1"
                )
            if _AGENT_DOTTED_RE.match(line):
                raise VariantInvalid(
                    "parent declares agent through dotted keys (agent.<key> = ...); "
                    "rewrite it as an [agent] table before applying agent-network-none@1"
                )
    raise VariantInvalid(first_error or "derived task.toml did not land [agent] network_mode")


def _candidate_error(parent_text: str, new_text: str) -> str | None:
    try:
        new_config = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        return f"derived {TASK_TOML_REL} does not parse: {exc}"
    new_agent = new_config.get("agent")
    if not isinstance(new_agent, dict) or new_agent.get("network_mode") != AGENT_NETWORK_MODE:
        return "derived task.toml did not land [agent] network_mode"

    masked_parent = tomllib.loads(parent_text)
    if isinstance(masked_parent.get("agent"), dict):
        masked_parent["agent"] = {k: v for k, v in masked_parent["agent"].items()}
        masked_parent["agent"]["network_mode"] = "<agent-network-none@1>"
    else:
        masked_parent["agent"] = {"network_mode": "<agent-network-none@1>"}
    new_agent["network_mode"] = "<agent-network-none@1>"
    if masked_parent != new_config:
        return "derived task.toml changed fields beyond [agent] network_mode"
    return None


def build_changes(
    parent_dir: Path | str,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
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
