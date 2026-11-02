#!/usr/bin/env python3
"""Prepare manifests and labels for C2-EXPORT-DRYRUN evaluation."""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(repo_root / "src"))
sys.path.insert(0, str(repo_root / "research/experiments/ds-dt8-verified-reader-fullscale-20261029"))

from evallab.production.corpus_queue import CorpusRegistry  # noqa: E402
from evallab.production.run_atlas import DEFAULT_DATABASE  # noqa: E402
from evallab.production.run_atlas_score import load_labels  # noqa: E402
from evallab.production.trace_harness import goodfire_cohort_ids, resolve_runs  # noqa: E402
from evallab.results_home import results_root  # noqa: E402

DATA_DIR = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/train/raw/C2-EXPORT-DRYRUN/receipts/corpora")
DATA_DIR.mkdir(parents=True, exist_ok=True)

BRIDGE31 = Path.home() / "Developer/research-context/bridge/2026-10-31-overnight"
SAFEGUARD_TRAJ = (
    BRIDGE31 / "fable-2/raw/W2-SAFEGUARD-ROLLOUTS/receipts/trajectories",
    BRIDGE31 / "g38-flash/raw/W3-SAFEGUARD-FRICTION-R/receipts/trajectories",
)
C1C5_TMP = Path("/tmp/w3b-impossible/out")


def prepare_terminal_wrench() -> tuple[Path, Path, int]:
    tw_dir = Path.home() / ".local/state/evallab-prod-live/factory/tw-dev-export"
    tw_manifest_path = Path.home() / ".local/state/evallab-prod-live/factory/tw-dev-export.manifest.json"
    tw_meta = json.loads(tw_manifest_path.read_text(encoding="utf-8"))

    manifest_rows = []
    label_rows = []
    for run_id, info in sorted(tw_meta.items()):
        traj_path = tw_dir / f"{run_id}.json"
        if not traj_path.is_file():
            raise FileNotFoundError(f"missing TW trajectory: {traj_path}")
        task_id = str(info.get("tw_task_id", "unknown"))
        is_cheat = bool(info.get("label") == "hack")

        manifest_rows.append({
            "id": run_id,
            "task_id": task_id,
            "path": str(traj_path),
            "variant": info.get("variant"),
            "model": info.get("model"),
        })
        label_rows.append({
            "id": run_id,
            "is_cheat": is_cheat,
        })

    m_path = DATA_DIR / "terminal_wrench_manifest.jsonl"
    l_path = DATA_DIR / "terminal_wrench_labels.jsonl"
    m_path.write_text("".join(json.dumps(r) + "\n" for r in manifest_rows), encoding="utf-8")
    l_path.write_text("".join(json.dumps(r) + "\n" for r in label_rows), encoding="utf-8")
    return m_path, l_path, len(manifest_rows)


def prepare_cheatbench() -> tuple[Path, Path, int]:
    registry = CorpusRegistry(repo_root)
    cb_items = [
        item for item in registry.items(dataset_id="cheatbench-actions-v1", split="dev")
        if item.metadata.get("expected_verdict") in ("pass", "fail")
    ]
    cb_ids = [item.item_id for item in cb_items]
    labels_map = load_labels(database=DEFAULT_DATABASE, repo_root=repo_root, run_ids=cb_ids)

    manifest_rows = []
    label_rows = []
    res_root = results_root()

    for item in cb_items:
        run_id = item.item_id
        ref = (item.metadata.get("trial_ref") or {}).get("path")
        # Direct trial directory containing agent/trajectory.json
        trial_dir = res_root / ref
        if not (trial_dir / "agent/trajectory.json").is_file():
            raise FileNotFoundError(f"missing CheatBench trajectory in {trial_dir}")
        is_cheat = bool(labels_map[run_id])

        manifest_rows.append({
            "id": run_id,
            "task_id": str(item.task_id),
            "path": str(trial_dir),
        })
        label_rows.append({
            "id": run_id,
            "is_cheat": is_cheat,
        })

    m_path = DATA_DIR / "cheatbench_manifest.jsonl"
    l_path = DATA_DIR / "cheatbench_labels.jsonl"
    m_path.write_text("".join(json.dumps(r) + "\n" for r in manifest_rows), encoding="utf-8")
    l_path.write_text("".join(json.dumps(r) + "\n" for r in label_rows), encoding="utf-8")
    return m_path, l_path, len(manifest_rows)


def prepare_goodfire() -> tuple[Path, Path, int]:
    gf_ids = list(goodfire_cohort_ids())
    labels_map = load_labels(database=DEFAULT_DATABASE, repo_root=repo_root, run_ids=gf_ids)

    # Filter to the 150 items with non-None labels (80 positive, 70 negative; omitting considered_only)
    admitted_gf_ids = [gid for gid in gf_ids if labels_map.get(gid) is not None]
    if len(admitted_gf_ids) != 150:
        raise ValueError(f"expected 150 Goodfire cohort items with binary labels, got {len(admitted_gf_ids)}")

    runs_map = {run.run_id: run for run in resolve_runs("goodfire", database=DEFAULT_DATABASE, repo_root=repo_root)}

    # Fetch task_ids from cloud-trace-db.sqlite
    conn = sqlite3.connect(DEFAULT_DATABASE.as_uri() + "?mode=ro&immutable=1", uri=True)
    cursor = conn.cursor()
    task_map = {}
    for gid in admitted_gf_ids:
        cursor.execute("SELECT task_id FROM agent_traces WHERE trace_id = ?", (gid,))
        row = cursor.fetchone()
        task_map[gid] = row[0] if row and row[0] else "unknown"
    conn.close()

    manifest_rows = []
    label_rows = []
    gf_trajs_dir = DATA_DIR / "goodfire_trajectories"
    gf_trajs_dir.mkdir(parents=True, exist_ok=True)

    for gid in admitted_gf_ids:
        run = runs_map[gid]
        ar = run.atlas_run
        doc = ar.doc if isinstance(ar.doc, dict) else {"steps": [dict(s) for s in run.steps]}
        if "metadata" not in doc or not isinstance(doc["metadata"], dict):
            doc["metadata"] = {"split": "dev", "task_id": task_map[gid]}
        else:
            doc["metadata"]["split"] = "dev"
            doc["metadata"]["task_id"] = task_map[gid]

        safe_name = gid.replace("/", "__").replace(":", "__") + ".json"
        traj_file = gf_trajs_dir / safe_name
        traj_file.write_text(json.dumps(doc), encoding="utf-8")

        manifest_rows.append({
            "id": gid,
            "task_id": task_map[gid],
            "path": str(traj_file),
        })
        label_rows.append({
            "id": gid,
            "is_cheat": bool(labels_map[gid]),
        })

    m_path = DATA_DIR / "goodfire_manifest.jsonl"
    l_path = DATA_DIR / "goodfire_labels.jsonl"
    m_path.write_text("".join(json.dumps(r) + "\n" for r in manifest_rows), encoding="utf-8")
    l_path.write_text("".join(json.dumps(r) + "\n" for r in label_rows), encoding="utf-8")
    return m_path, l_path, len(manifest_rows)


def prepare_c1c5() -> tuple[Path, Path, int]:
    cases_dir = repo_root / "research/experiments/ov-c1-c5-live-rollouts-20261101/receipts/cases"
    cases = sorted(cases_dir.glob("c*__*"))
    if not cases:
        raise FileNotFoundError(f"no cases found in {cases_dir}")

    # Read ground truth labels from C1 receipts
    labels_file = repo_root / "research/experiments/ov-c1-c5-live-rollouts-20261101/receipts/rollouts.jsonl"
    live_labels = {}
    for line in labels_file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            is_cheat = bool(r.get("reward_L0") == 1.0 or r.get("behavior") == "confirmed_cheat")
            live_labels[r["id"]] = is_cheat

    c1_staged_dir = DATA_DIR / "c1c5_cases"
    c1_staged_dir.mkdir(parents=True, exist_ok=True)

    manifest_rows = []
    label_rows = []
    for c in cases:
        cid = c.name
        task_id = cid.split("__")[0]
        if "__scripted__cheat" in cid:
            is_cheat = True
        elif "__scripted__honest" in cid:
            is_cheat = False
        else:
            is_cheat = live_labels.get(cid, False)

        staged_case = c1_staged_dir / cid
        staged_case.mkdir(parents=True, exist_ok=True)
        # Copy case files
        for child in c.iterdir():
            if child.is_file() and not (staged_case / child.name).exists():
                shutil.copyfile(child, staged_case / child.name)

        # Stage trajectory.json if available from /tmp
        tmp_traj = C1C5_TMP / cid / "trajectory.json"
        if tmp_traj.is_file() and not (staged_case / "trajectory.json").exists():
            shutil.copyfile(tmp_traj, staged_case / "trajectory.json")

        manifest_rows.append({
            "id": cid,
            "task_id": task_id,
            "path": str(staged_case),
        })
        label_rows.append({
            "id": cid,
            "is_cheat": is_cheat,
        })

    m_path = DATA_DIR / "c1c5_manifest.jsonl"
    l_path = DATA_DIR / "c1c5_labels.jsonl"
    m_path.write_text("".join(json.dumps(r) + "\n" for r in manifest_rows), encoding="utf-8")
    l_path.write_text("".join(json.dumps(r) + "\n" for r in label_rows), encoding="utf-8")
    return m_path, l_path, len(manifest_rows)


def prepare_sol_astra() -> tuple[Path, Path, int]:
    sa_staged_dir = DATA_DIR / "sol_astra_cases"
    sa_staged_dir.mkdir(parents=True, exist_ok=True)

    # Build index of available trajectories
    available_trajs: dict[str, Path] = {}
    for folder in SAFEGUARD_TRAJ:
        if folder.is_dir():
            for f in folder.glob("*.json"):
                available_trajs[f.stem] = f

    manifest_rows = []
    label_rows = []

    for exp_name in ("ov-safeguard-rollouts-20261031", "ov-safeguard-friction-v2-20261031"):
        exp_dir = repo_root / "research/experiments" / exp_name
        r_file = exp_dir / "receipts/rollouts.jsonl"
        for line in r_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            cid = r["id"]
            orig_case_path = exp_dir / "receipts/cases" / cid
            if not orig_case_path.is_dir():
                raise FileNotFoundError(f"missing Sol/Astra case dir: {orig_case_path}")
            is_cheat = bool(r.get("positive"))

            staged_case = sa_staged_dir / cid
            staged_case.mkdir(parents=True, exist_ok=True)
            for child in orig_case_path.iterdir():
                if child.is_file() and not (staged_case / child.name).exists():
                    shutil.copyfile(child, staged_case / child.name)

            if cid in available_trajs and not (staged_case / "trajectory.json").exists():
                shutil.copyfile(available_trajs[cid], staged_case / "trajectory.json")

            manifest_rows.append({
                "id": cid,
                "task_id": r.get("task", cid.split("__")[0]),
                "path": str(staged_case),
            })
            label_rows.append({
                "id": cid,
                "is_cheat": is_cheat,
            })

    m_path = DATA_DIR / "sol_astra_manifest.jsonl"
    l_path = DATA_DIR / "sol_astra_labels.jsonl"
    m_path.write_text("".join(json.dumps(r) + "\n" for r in manifest_rows), encoding="utf-8")
    l_path.write_text("".join(json.dumps(r) + "\n" for r in label_rows), encoding="utf-8")
    return m_path, l_path, len(manifest_rows)


def main() -> None:
    print("Preparing corpora manifests and labels...")
    tw_m, tw_l, tw_n = prepare_terminal_wrench()
    print(f"  Terminal Wrench: {tw_n} rows ({tw_m})")

    cb_m, cb_l, cb_n = prepare_cheatbench()
    print(f"  CheatBench:      {cb_n} rows ({cb_m})")

    gf_m, gf_l, gf_n = prepare_goodfire()
    print(f"  Goodfire:        {gf_n} rows ({gf_m})")

    c1_m, c1_l, c1_n = prepare_c1c5()
    print(f"  C1-C5:           {c1_n} rows ({c1_m})")

    sa_m, sa_l, sa_n = prepare_sol_astra()
    print(f"  Sol/Astra:       {sa_n} rows ({sa_m})")

    # Pooled manifest
    pooled_manifest_rows = []
    pooled_label_rows = []
    for m_p, l_p in [(tw_m, tw_l), (cb_m, cb_l), (gf_m, gf_l), (c1_m, c1_l), (sa_m, sa_l)]:
        for line in m_p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                pooled_manifest_rows.append(json.loads(line))
        for line in l_p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                pooled_label_rows.append(json.loads(line))

    p_m = DATA_DIR / "pooled_manifest.jsonl"
    p_l = DATA_DIR / "pooled_labels.jsonl"
    p_m.write_text("".join(json.dumps(r) + "\n" for r in pooled_manifest_rows), encoding="utf-8")
    p_l.write_text("".join(json.dumps(r) + "\n" for r in pooled_label_rows), encoding="utf-8")
    print(f"  Pooled Total:    {len(pooled_manifest_rows)} rows ({p_m})")
    print("Done preparing corpora.")


if __name__ == "__main__":
    main()
