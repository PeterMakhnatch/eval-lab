"""Validate + convert a real ATIF trajectory with the shipped harbor-atif2otel API.

No backend, no OTLP export. Writes OTel JSON and asserts a root AGENT span.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from harbor_atif2otel import (
    convert_trajectory,
    resource_spans_to_otlp_json,
    validate_trajectory,
)


def _span_kind(attrs: list[dict]) -> str | None:
    for attr in attrs:
        if attr.get("key") == "openinference.span.kind":
            value = attr.get("value") or {}
            return value.get("stringValue")
    return None


def _span_name(span: dict) -> str:
    return span.get("name") or ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trajectory", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    traj_path = args.trajectory.resolve()
    out_path = args.out.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    trajectory = json.loads(traj_path.read_text())
    agent = trajectory.get("agent", {})
    print(f"trajectory={traj_path}")
    print(f"schema_version={trajectory.get('schema_version')}")
    print(f"agent={agent.get('name')}@{agent.get('version')}")
    print(f"session_id={trajectory.get('session_id')}")
    print(f"n_steps={len(trajectory.get('steps') or [])}")

    issues = validate_trajectory(trajectory)
    print(f"validate_issues={len(issues)}")
    for issue in issues:
        print(f"  issue: {issue}")

    resource_spans = convert_trajectory(trajectory)
    payload = resource_spans_to_otlp_json(resource_spans)
    out_path.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"otel_json={out_path}")
    print(f"otel_bytes={out_path.stat().st_size}")

    spans: list[dict] = []
    for rs in payload.get("resourceSpans") or []:
        for ss in rs.get("scopeSpans") or []:
            spans.extend(ss.get("spans") or [])

    kinds = [_span_kind(s.get("attributes") or []) for s in spans]
    names = [_span_name(s) for s in spans]
    roots = [s for s in spans if not s.get("parentSpanId")]
    root_kinds = [_span_kind(s.get("attributes") or []) for s in roots]

    print(f"n_spans={len(spans)}")
    print(f"span_kinds={sorted({k for k in kinds if k})}")
    print(f"n_root_spans={len(roots)}")
    print(f"root_kinds={root_kinds}")
    print(f"root_names={[ _span_name(s) for s in roots ]}")

    if issues:
        print("FAIL: validate_trajectory reported issues")
        return 1
    if not spans:
        print("FAIL: convert emitted no spans")
        return 1
    if "AGENT" not in root_kinds:
        print("FAIL: no root AGENT span")
        return 1
    print("OK: valid ATIF; non-empty OTel payload with root AGENT span")
    # Keep names in output so a skeptic can see this is not an empty file.
    if names:
        print("first_span_name=" + names[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
