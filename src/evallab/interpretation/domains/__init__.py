"""Per-benchmark domain sections for run reports (``evallab report run``).

Each plugin reads a benchmark's verifier outputs from the trial directory and
adds a ``domain`` section to the ``evallab.run_report/v1`` report. Plugins are
read-only and deterministic: no wall clock, network, or model calls. A trial
with no matching plugin gets ``domain: None``; malformed inputs yield
``{"plugin", "version", "status": "unreadable", "reason"}``, never an
exception.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class DomainPlugin(Protocol):
    """A benchmark-specific section of the run report."""

    name: str  # stable id, e.g. "synthetic_hospital"
    version: str  # plugin section schema version, e.g. "1"

    def detect(self, trial_dir: Path, result: dict[str, Any]) -> bool:
        """True when this plugin owns the trial (task identity or verifier shape)."""
        ...

    def build(self, trial_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
        """Build the section dict. Must include ``plugin``, ``version`` and ``sources``.

        ``sources`` lists trial-relative paths actually read. On malformed
        input return ``{"plugin", "version", "status": "unreadable", "reason"}``.
        """
        ...

    def render_markdown(self, section: dict[str, Any]) -> list[str]:
        """Render the section body (the ``## Domain: <name>`` heading is added by the caller)."""
        ...


PLUGINS: tuple[DomainPlugin, ...] = ()


def domain_section(trial_dir: Path, result: dict[str, Any]) -> dict[str, Any] | None:
    """First matching plugin's section, or None when no plugin detects the trial."""
    trial = Path(trial_dir)
    for plugin in PLUGINS:
        try:
            detected = plugin.detect(trial, result)
        except Exception:
            continue
        if not detected:
            continue
        try:
            section = plugin.build(trial, result)
        except Exception as exc:
            return {
                "plugin": plugin.name,
                "version": plugin.version,
                "status": "unreadable",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        if not isinstance(section, dict):
            return {
                "plugin": plugin.name,
                "version": plugin.version,
                "status": "unreadable",
                "reason": "plugin returned a non-object section",
            }
        section.setdefault("plugin", plugin.name)
        section.setdefault("version", plugin.version)
        return section
    return None


def render_domain_markdown(section: dict[str, Any] | None) -> list[str]:
    """Markdown lines for the domain section (empty when there is none)."""
    if not section:
        return []
    name = section.get("plugin", "unknown")
    lines = [f"## Domain: {name}", ""]
    for plugin in PLUGINS:
        if plugin.name == name:
            try:
                lines.extend(plugin.render_markdown(section))
            except Exception as exc:
                lines.append(f"- Domain section unreadable: {type(exc).__name__}: {exc}")
            return lines
    status = section.get("status")
    reason = section.get("reason")
    if status == "unreadable":
        lines.append(f"- Domain section unreadable ({reason}).")
    else:
        lines.append(f"- No renderer for domain plugin {name!r}.")
    return lines
