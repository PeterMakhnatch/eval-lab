#!/usr/bin/env python3
"""Single Modal cells for the ov1104-grader lane: source probe and patch grading on a task package.

Usage (from the eval-lab worktree root, Modal only, never local Docker):

  EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run --no-sync python research/experiments/ov1104-grader/cert/cells.py \
      probe --package PKG --workdir /testbed --out DIR PATH [PATH ...]
  EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run --no-sync python research/experiments/ov1104-grader/cert/cells.py \
      grade --package PKG --workdir /testbed --patch FILE --name NAME

`probe` tars the pre-patch PATHs from the task's workdir and extracts them under DIR (no reference applied).
`grade` applies FILE as the agent's change and runs the package's own grader; prints one JSON line with the reward.
Every cell is reserved and settled on the lane ledger and appended to the ov1104 spend log.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "src"))

from evallab.modal_cells import ReserveSettleSpendGate, SandboxMeter, modal_cell_runner  # noqa: E402

APPROVAL = "Peter 2026-11-03 23:35 ET: $30 overnight infra budget (ov1104), lane cap per PROGRAM.md"
LANE_CAP_USD = 6.0
LEDGER = Path.home() / ".local/state/evallab-ov1104/grader-spend.json"
SPEND_JSONL = Path.home() / ".local/state/evallab-ov1104/spend.jsonl"
CELLS_DIR = Path.home() / "Developer/eval-lab-results/2026-11-04/ov1104-grader/cells"
PROBE_MARKER = "OV1104_PROBE_TAR"
PROBE_DONE = "OV1104_PROBE_DONE"
GRADE_MARKER = "OV1104_PATCH_APPLIED"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _runner(app: str):
    gate = ReserveSettleSpendGate(
        approval=APPROVAL,
        cap_usd=LANE_CAP_USD,
        usd_per_execution=0.008,
        spend_ledger=LEDGER,
        modal_app_name=app,
        billing_tag="ov1104",
    )
    meter = SandboxMeter(LANE_CAP_USD)
    return modal_cell_runner(
        repo_root=REPO_ROOT, spend_gate=gate, meter=meter, timeout_seconds=300, results_dir=CELLS_DIR
    )


def _log_spend(what: str, est_usd: float, receipt: str) -> None:
    SPEND_JSONL.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "lane": "grader",
        "what": what,
        "provider": "modal",
        "est_usd": round(float(est_usd or 0.0), 6),
        "actual_usd": None,
        "receipt": receipt,
    }
    with SPEND_JSONL.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")


def probe(args: argparse.Namespace) -> int:
    paths = " ".join(args.paths)
    script = (
        f"set -u\ncd {args.workdir} || exit 1\n"
        f"tar czf /tmp/ov1104-probe.tgz {paths} || exit 1\n"
        f"echo {PROBE_MARKER} $(base64 < /tmp/ov1104-probe.tgz | tr -d '\\n')\n"
        f"echo {PROBE_DONE}\n"
    )
    cell_dir = CELLS_DIR / args.name
    res = _runner("ov1104-grader-probe")(cell_dir, Path(args.package), script)
    _log_spend(f"probe {args.name}", res.get("est_usd", 0.0), str(cell_dir / "result.json"))
    log = res.get("agent_log", "")
    if PROBE_DONE not in log:
        print(json.dumps({"status": res.get("status"), "error": "probe did not finish", "tail": log[-500:]}))
        return 1
    blob = next(line.split(" ", 1)[1] for line in log.splitlines() if line.startswith(PROBE_MARKER + " "))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(blob)), mode="r:gz") as tar:
        tar.extractall(out, filter="data")
    print(json.dumps({"status": res.get("status"), "est_usd": res.get("est_usd"), "wall_s": res.get("wall_s"), "out": str(out)}))
    return 0


def grade(args: argparse.Namespace) -> int:
    patch_text = Path(args.patch).read_text(encoding="utf-8", errors="replace")
    script = (
        f"set -u\ncd {args.workdir} || exit 1\n"
        f"echo {_b64(patch_text)} | base64 -d > /tmp/ov1104-agent.patch\n"
        "git apply --whitespace=nowarn /tmp/ov1104-agent.patch || patch -p1 < /tmp/ov1104-agent.patch || exit 1\n"
        f"echo {GRADE_MARKER}\n"
    )
    cell_dir = CELLS_DIR / args.name
    res = _runner("ov1104-grader-grade")(cell_dir, Path(args.package), script)
    _log_spend(f"grade {args.name}", res.get("est_usd", 0.0), str(cell_dir / "result.json"))
    applied = GRADE_MARKER in res.get("agent_log", "")
    print(
        json.dumps(
            {
                "name": args.name,
                "status": res.get("status"),
                "reward": res.get("reward"),
                "patch_applied": applied,
                "est_usd": res.get("est_usd"),
                "wall_s": res.get("wall_s"),
                "cache_hit": res.get("cache_hit"),
                "test_output_tail": (res.get("test_output_tail") or "")[-400:],
                "cell_dir": str(cell_dir),
            }
        )
    )
    return 0 if res.get("reward") in (0.0, 1.0) else 1


def main() -> int:
    os.environ.setdefault("EVALLAB_DISABLE_LOCAL_DOCKER", "1")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--package", required=True)
    p.add_argument("--workdir", default="/testbed")
    p.add_argument("--out", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("paths", nargs="+")
    p.set_defaults(fn=probe)
    g = sub.add_parser("grade")
    g.add_argument("--package", required=True)
    g.add_argument("--workdir", default="/testbed")
    g.add_argument("--patch", required=True)
    g.add_argument("--name", required=True)
    g.set_defaults(fn=grade)
    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
