"""Classify one G5 position wave after its tick (used by run_g5.sh).

    python g5_wave_outcome.py WAVE_FILE STARTED_ISO [--runs-dir runs] [--events queue/events.jsonl]
        [--specs-dir DIR --capture CALLS_JSONL]

WAVE_FILE lines are ``<job name> <spec id>``. Prints one JSON object:

- ``terminal``: jobs with a result.json written at or after STARTED (a result
  older than the wave is stale and does not count);
- ``not_terminal``: jobs without such a result;
- ``infra_failures``: terminal jobs with no verifier reward whose exception is
  not an agent stop (``evallab.database.AGENT_STOP_EXCEPTIONS``), the same
  semantics the lab's quiet-failure counter uses; a scored trial is never infra;
- ``refused``: ``<job>:<reason_code>`` for dispatch refusals since STARTED;
- with ``--specs-dir`` and ``--capture``: ``models_seen`` (request and response
  model names of captured calls started at or after STARTED, with counts) and
  ``unexpected_models`` (any not served by a spec of this wave, e.g. a base-model
  call inside an adapter-only wave).

Exit status 0 when the next wave may start (every job terminal, fewer than 3
infra failures, no refusal, no unexpected model), else 1.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from evallab.database import AGENT_STOP_EXCEPTIONS
from evallab.execution_contracts import MIMO_SELFHOSTED_MODEL_PREFIX


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _models_seen(capture: Path, started: datetime) -> Counter[str]:
    seen: Counter[str] = Counter()
    with capture.open() as handle:
        for line in handle:
            record = json.loads(line)
            if not record.get("started_at") or _ts(record["started_at"]) < started:
                continue
            for body in (record.get("request_body"), record.get("response_body")):
                if isinstance(body, dict) and body.get("model"):
                    seen[str(body["model"])] += 1
    return seen


def classify(
    wave_file: Path,
    started: datetime,
    runs_dir: Path,
    events: Path,
    specs_dir: Path | None = None,
    capture: Path | None = None,
) -> dict:
    names = [line.split()[0] for line in wave_file.read_text().splitlines() if line.strip()]
    terminal, not_terminal, infra = [], [], []
    for name in names:
        fresh = [
            path
            for path in sorted((runs_dir / name).glob("*/result.json"))
            if datetime.fromtimestamp(path.stat().st_mtime, UTC) >= started
        ]
        if not fresh:
            not_terminal.append(name)
            continue
        terminal.append(name)
        for path in fresh:
            result = json.loads(path.read_text())
            rewards = (result.get("verifier_result") or {}).get("rewards")
            exception = (result.get("exception_info") or {}).get("exception_type")
            if not rewards and exception not in AGENT_STOP_EXCEPTIONS:
                infra.append(f"{name}:{exception}")
    refused = set()
    if events.exists():
        for line in events.read_text().splitlines():
            event = json.loads(line)
            if (
                event.get("event") == "dispatch_refused"
                and event.get("job_name") in names
                and event.get("occurred_at")
                and _ts(event["occurred_at"]) >= started
            ):
                refused.add(f"{event['job_name']}:{event.get('reason_code')}")
    outcome = {
        "specs": len(names),
        "terminal": terminal,
        "not_terminal": not_terminal,
        "infra_failures": infra,
        "refused": sorted(refused),
    }
    if specs_dir is not None and capture is not None:
        expected = {
            str(json.loads((specs_dir / f"{name}.json").read_text())["model"]).removeprefix(
                MIMO_SELFHOSTED_MODEL_PREFIX
            )
            for name in names
        }
        seen = _models_seen(capture, started) if capture.exists() else Counter()
        outcome["expected_models"] = sorted(expected)
        outcome["models_seen"] = dict(seen)
        outcome["unexpected_models"] = sorted(set(seen) - expected)
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wave_file", type=Path)
    parser.add_argument("started")
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    parser.add_argument("--events", type=Path, default=Path("queue/events.jsonl"))
    parser.add_argument("--specs-dir", type=Path)
    parser.add_argument("--capture", type=Path)
    args = parser.parse_args()
    outcome = classify(
        args.wave_file, _ts(args.started), args.runs_dir, args.events, args.specs_dir, args.capture
    )
    print(json.dumps(outcome))
    advance = (
        not outcome["not_terminal"]
        and len(outcome["infra_failures"]) < 3
        and not outcome["refused"]
        and not outcome.get("unexpected_models")
    )
    return 0 if advance else 1


if __name__ == "__main__":
    sys.exit(main())
