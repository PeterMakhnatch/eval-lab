"""One read-only pass of the fleet source over the live herdr panes (LS-FREEZE).

Runs ``FleetSource.observe`` with the real runner (``herdr pane list``, ``herdr tab list``,
``herdr pane process-info``, ``ps``) and a throwaway state file, then prints per pane whether the
frozen-lane probe fired. It signals nothing, reads no pane text and writes only the throwaway state.

    uv run python live_probe.py [--state /tmp/ls-freeze-probe-state.json] > live-probe.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from evallab.production import live_supervisor as ls
from evallab.production import live_supervisor_fleet as fl


def probe(state: Path, now: datetime) -> dict[str, object]:
    source = fl.FleetSource(state_path=state, fetch_quota=False)
    rows = []
    for observation in source.observe(now):
        if observation.subject.kind != fl.KIND_LANE:
            continue
        hits = source.triggers(observation)
        m = observation.metrics
        rows.append({
            "pane": observation.subject.id, "owner": observation.subject.owner, "herdr_status": m["agent_status"],
            "herdr_agent": m["agent"], "omp_probe": m["omp_probe"], "omp_pids": m["omp_pids"],
            "omp_stopped": m["omp_stopped"], "omp_background": m["omp_background"],
            "pane_foreground": m["pane_foreground"], "session_read": m["session_read"],
            "hits": [{"trigger": h.trigger, "kind": h.kind, "action": ls.propose(observation.subject, [h]).action}
                     for h in hits],
        })
    with_omp = [r for r in rows if r["omp_pids"]]
    return {
        "observed_at": now.isoformat(), "panes": len(rows), "panes_with_interactive_omp": len(with_omp),
        "frozen_flagged": [r["pane"] for r in rows if any(h["kind"] == "infra" for h in r["hits"])],  # type: ignore[attr-defined]
        "stopped_state_T": [r["pane"] for r in rows if r["omp_stopped"]],
        "background_state_S": [r["pane"] for r in rows if r["omp_background"] and not r["omp_stopped"]],
        "healthy_omp_panes": [r["pane"] for r in with_omp if not r["omp_background"] and not r["omp_stopped"]],
        "probe_unavailable": [r["pane"] for r in rows if r["omp_probe"] != "ok"],
        "rows": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=Path("/tmp/ls-freeze-probe-state.json"))
    json.dump(probe(parser.parse_args().state, datetime.now(UTC)), sys.stdout, indent=1)
