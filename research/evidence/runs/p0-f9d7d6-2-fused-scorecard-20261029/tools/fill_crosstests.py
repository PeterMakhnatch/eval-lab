"""Fill the model-free native detectors on the CM-18 / NP21 stored runs (label-free; no model call).

Reads only ``steps`` and ``key`` of ``~/.local/state/evallab-cm18/runs.pkl`` and
``~/.local/state/evallab-np21-cm1/runs.pkl`` (the label columns stored beside them are never read),
builds one ``HarnessRun`` per run with the harness' own constructor, and runs the deterministic native
detectors (tripwire, qa_trace_t0, flags, run_health.*) through ``run_detectors`` into a PRIVATE findings
store. Run id is ``<cm18|np21>:<set>:<key>``; the stored cascade/facet rows of those states are keyed by ``key``.
"""

from __future__ import annotations

import os
import pickle
import sys
from pathlib import Path

os.environ.setdefault("EVALLAB_DISABLE_LOCAL_DOCKER", "1")

from evallab.production.trace_harness import (  # noqa: E402
    FindingStore,
    HarnessOptions,
    HarnessRun,
    actor_inputs_available,
    load_detectors,
    run_detectors,
)

STATE = Path.home() / ".local/state"
SOURCES = {"cm18": STATE / "evallab-cm18/runs.pkl", "np21": STATE / "evallab-np21-cm1/runs.pkl"}
PRIVATE = Path.home() / ".local/state/evallab-prod-live/trace-harness/p0-f9d7d6-2"
DETECTORS = ["tripwire", "qa_trace_t0", "flags", "run_health.loop", "run_health.runaway",
             "run_health.latency", "run_health.infra"]


def harness_runs(tag: str) -> list[HarnessRun]:
    with SOURCES[tag].open("rb") as handle:
        rows = pickle.load(handle)
    out = []
    for row in rows:
        steps = tuple(row["steps"])
        out.append(HarnessRun(f"{tag}:{row['set']}:{row['key']}", f"{tag}:{row['set']}", "CROSS-TEST", "unknown",
                              steps, actor_inputs_available(steps)))
    return out


def main() -> int:
    options = HarnessOptions(repo_root=Path.cwd())
    detectors = load_detectors(DETECTORS, options)
    PRIVATE.mkdir(parents=True, exist_ok=True)
    for tag in SOURCES:
        runs = harness_runs(tag)
        summary = run_detectors(runs, detectors, FindingStore(PRIVATE / f"fill-{tag}.jsonl"), log=print)
        print(tag, summary["runs"], "runs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
