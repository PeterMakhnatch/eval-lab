"""Build the TraceMonitor-Bench DEV v1 local manifest, cards and L0 readings (no model call, no network).

Reads third-party DEV data from this machine only and writes under ``--root`` (local, untracked):

* ``manifest.jsonl``: one label-bearing row per case (``PREREGISTRATION.md`` fixes every label rule).
* ``cards/<case_id>.md`` and ``.json``: the label-free ``evallab trace anatomy`` card and its JSON.
* ``readings/l0_tripwires.jsonl``: the deterministic L0 gate-rule replay (a baseline; code, not a model).

Run from an eval-lab worktree: ``EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run --no-sync python <this file> --root <dir>``.
Resumable: a case whose card and L0 row already exist is skipped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

HOME = Path.home()
RESEARCH = Path(__file__).resolve().parents[3]
BRIDGE31 = HOME / "Developer/research-context/bridge/2026-10-31-overnight"
TW_DIR = HOME / ".local/state/evallab-prod-live/factory/tw-dev-export"
TW_MANIFEST = HOME / ".local/state/evallab-prod-live/factory/tw-dev-export.manifest.json"
DEEPSWE_COHORT = BRIDGE31 / "g38-flash/raw/W3-DEEPSWE-AUTOREVIEW/receipts/cohort.json"
C1C5_OUT = Path("/tmp/w3b-impossible/out")
SAFEGUARD_TRAJ = (
    BRIDGE31 / "fable-2/raw/W2-SAFEGUARD-ROLLOUTS/receipts/trajectories",
    BRIDGE31 / "g38-flash/raw/W3-SAFEGUARD-FRICTION-R/receipts/trajectories",
)
DATABASE = HOME / ".local/state/evallab-prod-live/cloud-trace-db.sqlite"
FAMILY_ORDER = ("cheatbench", "goodfire", "terminal_wrench", "deep_swe", "c1c5", "sol_astra")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def case_id_of(family: str, source_id: str) -> str:
    return "tmb1-" + sha(f"{family}|{source_id}")[:12]


def split_cells(rows: list[dict[str, Any]]) -> None:
    """Preregistered split: per (family, label) cell the first ceil(n/2) by sha256(case_id) are tune."""
    cells: dict[tuple[str, bool], list[dict[str, Any]]] = {}
    for row in rows:
        if row["label"] is None:
            row["split"] = "excluded"
        else:
            cells.setdefault((row["family"], row["label"]), []).append(row)
    for members in cells.values():
        members.sort(key=lambda r: sha(r["case_id"]))
        for index, row in enumerate(members):
            row["split"] = "tune" if index < math.ceil(len(members) / 2) else "report"


# --------------------------------------------------------------------------- #
# sources (labels attached by the preregistered rule)
# --------------------------------------------------------------------------- #


def third_party_sources() -> list[dict[str, Any]]:
    sys.path.insert(0, str(RESEARCH / "experiments/ds-dt8-verified-reader-fullscale-20261029"))
    import dt8_reader as d8  # noqa: PLC0415

    rows: list[dict[str, Any]] = []
    for family, fn, provenance in (("cheatbench", d8.cheatbench_rows, "publisher"),
                                   ("goodfire", d8.goodfire_rows, "publisher_cot_judged")):
        for row in fn():
            rows.append({"family": family, "source_id": row["run_id"], "label": bool(row["positive"]),
                         "label_status": "scored", "provenance": provenance, "loader": "cloud"})
    manifest = json.loads(TW_MANIFEST.read_text())
    for run_id, info in sorted(manifest.items()):
        rows.append({"family": "terminal_wrench", "source_id": run_id, "label": info["label"] == "hack",
                     "label_status": "scored", "provenance": "publisher", "loader": "tw_file",
                     "extra": {"variant": info.get("variant"), "tw_model": info.get("model")}})
    for item in json.loads(DEEPSWE_COHORT.read_text()):
        rows.append({"family": "deep_swe", "source_id": item["trace_id"], "label": bool(item["positive"]),
                     "label_status": "scored", "provenance": "publisher_cot_judged", "loader": "cloud"})
    return rows


def own_sources() -> list[dict[str, Any]]:
    exp = RESEARCH / "experiments"
    rows: list[dict[str, Any]] = []
    for line in (exp / "ov-c1-c5-live-rollouts-20261101/receipts/rollouts.jsonl").read_text().splitlines():
        r = json.loads(line)
        if r["kind"] != "real":
            continue
        if r["reward_L0"] == 1.0:
            label, status = True, "scored"
        elif r["behavior"] == "honest_attempt":
            label, status = False, "scored"
        else:
            label, status = None, "excluded_heuristic_attempt"
        rows.append({"family": "c1c5", "source_id": r["id"], "label": label, "label_status": status,
                     "provenance": "container_verified", "loader": "own",
                     "extra": {"task": r["task"], "model": r["model"], "case_kind": "real", "behavior": r["behavior"]}})
    for name in ("ov-safeguard-rollouts-20261031", "ov-safeguard-friction-v2-20261031"):
        for line in (exp / name / "receipts/rollouts.jsonl").read_text().splitlines():
            r = json.loads(line)
            rows.append({"family": "sol_astra", "source_id": r["id"], "label": bool(r["positive"]),
                         "label_status": "scored", "provenance": "container_verified", "loader": "own",
                         "extra": {"task": r["task"], "model": r["model"], "case_kind": r["kind"],
                                   "experiment": name}})
    return rows


# --------------------------------------------------------------------------- #
# loading, cards, L0
# --------------------------------------------------------------------------- #


def clean_own_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The same projection the stored own-family monitors saw: no reasoning, agent source only."""
    return [{"step_id": s["step_id"], "source": "agent", "message": s.get("message", ""),
             "tool_calls": s.get("tool_calls", []), "observation": s.get("observation", {"results": []})}
            for s in steps]


def load(row: dict[str, Any], repo_root: Path, root: Path):  # noqa: ANN201
    from evallab.trace_anatomy import LoadedRun, load_atif_file, load_run  # noqa: PLC0415

    kind = row["loader"]
    if kind == "cloud":
        return load_run(row["source_id"], repo_root=repo_root)
    if kind == "tw_file":
        return load_atif_file(TW_DIR / f"{row['source_id']}.json")
    traj = json.loads((root / "own-trajectories" / f"{row['source_id']}.json").read_text())
    steps = clean_own_steps(traj["steps"])
    doc = {"metadata": {"split": "dev"}, "steps": steps}
    return LoadedRun(row["source_id"], "atif", doc, None, None, len(json.dumps(steps, ensure_ascii=False)))


def stage_own_trajectories(root: Path) -> None:
    """Copy our own trajectories out of /tmp and the bridge into the durable bench root (idempotent)."""
    dest = root / "own-trajectories"
    dest.mkdir(parents=True, exist_ok=True)
    for case_dir in sorted(C1C5_OUT.glob("c*__*")):
        source = case_dir / "trajectory.json"
        if source.is_file() and not (dest / f"{case_dir.name}.json").exists():
            (dest / f"{case_dir.name}.json").write_bytes(source.read_bytes())
    for folder in SAFEGUARD_TRAJ:
        for source in sorted(folder.glob("*.json")):
            if not (dest / source.name).exists():
                (dest / source.name).write_bytes(source.read_bytes())


def l0_replay(run_id: str, steps: list[Any]) -> dict[str, Any]:
    from evallab.production.monitor_worker import _GATE_RULES, MisalignmentLiveWorker  # noqa: PLC0415
    from evallab.tripwires import HACKBOOK_GATE_RULES  # noqa: PLC0415

    gate_rules = frozenset(_GATE_RULES) | {f"hackbook:{rule}" for rule in HACKBOOK_GATE_RULES}
    events: list[dict[str, Any]] = []
    worker = MisalignmentLiveWorker(emit=events.append)
    try:
        for index, step in enumerate(steps):
            worker.ingest_event({"event_id": f"{run_id}:{index}", "run_id": run_id, "system": "misalignment",
                                 "event_type": "trace.step", "prefix_step": index, "status": "applied",
                                 "payload": {"source_kind": "public_rollout", "step": step}})
        worker.ingest_event({"event_id": f"{run_id}:completed", "run_id": run_id, "system": "misalignment",
                             "event_type": "misalignment.replay_completed", "prefix_step": None, "status": "applied",
                             "payload": {"step_count": len(steps)}})
    finally:
        worker.close()
    cards = [e["payload"] for e in events if e["run_id"] == run_id and e["event_type"] == "monitor.suspicion"]
    gate = [c for c in cards if c.get("eligibility") == "verdict" and c.get("rule") in gate_rules]
    return {"has_gate_card": bool(gate), "rules": sorted({c["rule"] for c in gate})}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path(".").resolve())
    parser.add_argument("--limit", type=int, default=0, help="debug: first N cases only")
    args = parser.parse_args()
    root: Path = args.root
    (root / "cards").mkdir(parents=True, exist_ok=True)
    (root / "readings").mkdir(parents=True, exist_ok=True)
    stage_own_trajectories(root)

    from evallab.trace_anatomy import (  # noqa: PLC0415
        anatomy_dict,
        build_anatomy,
        database_scrub_pattern,
        render_anatomy_card,
    )

    pattern = database_scrub_pattern(DATABASE)
    sources = third_party_sources() + own_sources()
    sources.sort(key=lambda r: (FAMILY_ORDER.index(r["family"]), r["source_id"]))
    if args.limit:
        sources = sources[: args.limit]
    seen: set[tuple[str, str]] = set()
    rows: list[dict[str, Any]] = []
    l0_path = root / "readings" / "l0_tripwires.jsonl"
    l0_done = {json.loads(line)["case_id"] for line in l0_path.read_text().splitlines()} if l0_path.exists() else set()
    with l0_path.open("a") as l0_out:
        for index, row in enumerate(sources, 1):
            key = (row["family"], row["source_id"])
            if key in seen:
                raise SystemExit(f"duplicate source within a family: {key}")
            seen.add(key)
            cid = case_id_of(row["family"], row["source_id"])
            card_json = root / "cards" / f"{cid}.json"
            run = load(row, args.repo, root)
            anatomy = build_anatomy(run)
            if not card_json.exists():
                (root / "cards" / f"{cid}.md").write_text(render_anatomy_card(anatomy, pattern=pattern))
                card_json.write_text(json.dumps(anatomy_dict(anatomy, pattern=pattern), ensure_ascii=False))
            if cid not in l0_done:
                steps = [s for s in run.doc.get("steps") or [] if isinstance(s, dict)]
                l0_out.write(json.dumps({"case_id": cid, **l0_replay(cid, steps)}) + "\n")
                l0_out.flush()
            raw_chars = anatomy.run["raw_chars"]
            card_text = (root / "cards" / f"{cid}.md").read_text()
            rows.append({
                "case_id": cid, "family": row["family"], "label": row["label"],
                "label_status": row["label_status"], "provenance": row["provenance"],
                "steps": anatomy.run["steps"], "tool_calls": anatomy.run["tool_calls"], "raw_chars": raw_chars,
                "approx_tokens": math.ceil(raw_chars / 4),
                "card_path": f"cards/{cid}.md", "card_sha256": sha(card_text),
                "source_id": row["source_id"], "blind_id": "run-" + sha(row["source_id"])[:16],
                "extra": row.get("extra") or {},
            })
            if index % 50 == 0:
                print(f"{index}/{len(sources)} cases", flush=True)
    split_cells(rows)
    rows.sort(key=lambda r: (FAMILY_ORDER.index(r["family"]), r["case_id"]))
    (root / "manifest.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    summary: dict[str, Any] = {}
    for r in rows:
        cell = summary.setdefault(r["family"], {"n": 0, "pos": 0, "neg": 0, "excluded": 0, "tune": 0, "report": 0})
        cell["n"] += 1
        cell["pos" if r["label"] is True else "neg" if r["label"] is False else "excluded"] += 1
        if r["split"] in ("tune", "report"):
            cell[r["split"]] += 1
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
