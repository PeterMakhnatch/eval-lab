"""Served-model attestation for the DT-3 readings.

Reads the per-call receipts embedded in both reading files (``served_model`` and
``requested_model`` come from the ``agent_end`` event of each ``omp -p`` stream) and
re-scans the sessions written under ``--session-dir``. Any served model other than
``deepseek/deepseek-flash`` fails the attestation.

Usage::

    python attest.py --capped readings-capped.jsonl --uncapped readings-uncapped.jsonl \
        --sessions /tmp/dt3/sessions --out attestation.json
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

ROUTE = "deepseek/deepseek-flash"


def stream_models(paths: list[Path]) -> tuple[Counter, Counter, Counter]:
    served: Counter = Counter()
    requested: Counter = Counter()
    statuses: Counter = Counter()
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            served[row["status"]] = served[row["status"]] + 0
            statuses[row.get("status", "?")] += 1
            for call in row.get("calls") or []:
                served[str(call.get("served_model"))] += 1
                requested[str(call.get("requested_model"))] += 1
    served.pop("0", None)
    return served, requested, statuses


def session_models(root: Path) -> tuple[Counter, int]:
    seen: Counter = Counter()
    files = 0
    for path in sorted(root.rglob("*.json")) + sorted(root.rglob("*.jsonl")):
        files += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            stack = [event]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    if node.get("role") == "assistant" and node.get("model"):
                        seen[f"{node.get('provider')}/{node.get('model')}"] += 1
                    stack.extend(node.values())
                elif isinstance(node, list):
                    stack.extend(node)
    return seen, files


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capped", type=Path, action="append", default=[])
    parser.add_argument("--uncapped", type=Path, action="append", default=[])
    parser.add_argument("--sessions", type=Path, action="append", default=[])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    readings = list(args.capped) + list(args.uncapped)
    served, requested, statuses = stream_models(readings)
    sessions: Counter = Counter()
    files = 0
    for root in args.sessions:
        seen, count = session_models(root)
        sessions.update(seen)
        files += count
    document = {
        "requested_model": ROUTE,
        "readings": [str(path) for path in readings],
        "runs_by_status": dict(sorted(statuses.items())),
        "calls_attested_stream": dict(sorted(served.items())),
        "calls_requested_stream": dict(sorted(requested.items())),
        "session_files_scanned": files,
        "calls_attested_sessions": dict(sorted(sessions.items())),
        "all_stream_calls_are_requested_model": set(served) == {ROUTE},
        "all_sessions_are_requested_model": bool(sessions) and set(sessions) == {ROUTE},
    }
    args.out.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in document.items() if k != "readings"}, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
