"""Count the admitted seed without opening trajectory payloads."""
from collections import Counter
from typing import Any

from evallab.failure_library.bank import IssueItem


def counts(rows: list[IssueItem], registrations: dict[str, dict] | None = None) -> dict[str, Any]:
    registrations = registrations or {}
    groups: dict[str, Any] = {}
    for source in sorted({row.source for row in rows} | registrations.keys()):
        selected = [row for row in rows if row.source == source]
        resolved = [row for row in selected if any(
            ref.normalized_step_id is not None and ref.event_kind in {"call", "result"}
            and ref.resolution != "unmatched-result" for ref in row.step_locators
        )]
        groups[source] = {
            "rows": len(selected),
            "label_state": dict(sorted(Counter(row.label_state for row in selected).items())),
            "mechanism": dict(sorted(Counter(row.mechanism for row in selected).items())),
            "protocol": dict(sorted(Counter(row.protocol for row in selected).items())),
            "by_protocol_label_state_mechanism": dict(sorted(Counter(
                f"{row.protocol}|{row.label_state}|{row.mechanism}" for row in selected
            ).items())),
            "by_label_state_mechanism": dict(sorted(Counter(
                f"{row.label_state}|{row.mechanism}" for row in selected
            ).items())),
            "with_resolved_steps": len(resolved),
            "resolved_steps_by_label_state": dict(sorted(Counter(
                row.label_state for row in resolved
            ).items())),
            **{field: dict(sorted(Counter(
                "unknown" if getattr(row, field) is None else str(getattr(row, field)).lower()
                for row in selected
            ).items())) for field in ("task_available", "grader_available", "trajectory_available",
                                     "exposure", "split")},
        }
        if source in registrations:
            groups[source]["registration"] = registrations[source]
    return {"rows": len(rows), "binary_rows": sum(
        row.label_state in {"executed-positive", "reviewed-control"} for row in rows
    ), "sources": groups, "protocol": dict(sorted(Counter(row.protocol for row in rows).items())),
        "by_protocol_label_state_mechanism": dict(sorted(Counter(
            f"{row.protocol}|{row.label_state}|{row.mechanism}" for row in rows
        ).items()))}
