"""Backfill blind filter metadata on the 44 HAR-81 Docent runs (HAR-109 item 2).

Adds to each run's ``metadata.trace_lab``:
  - ``stop_reason``: Eval Lab ``report run`` stop reason; when the stop is
    ``trial_budget_exhausted`` the binding ceiling is appended as
    ``trial_budget_exhausted:ceiling:<input_tokens|output_tokens|requests|total_tokens>``.
    The ceiling is pure infra arithmetic (job lab-metadata.json caps vs the
    trial's token/episode counters) -- no probe-03 or hand labels.
  - ``verdict``: Eval Lab ``report run`` outcome verdict.
  - ``domain``: Eval Lab task-store domain (code/cyber/terminal/...).

Blind: this script never reads probe-03 capabilities.jsonl or hand keys.

Three modes (two interpreters: the eval-lab venv lacks docent, and the
docent env lacks evallab):
  compute : venv python, Eval Lab only. Writes <out> JSON keyed by raw trial.
  apply   : keys run + docent env. Privacy/count precheck, 44 metadata
            updates (read-modify-write of the trace_lab block), postcheck.
  verify  : keys run + docent env. DQL visibility + distribution output.

$0 model spend in every mode (local files + Docent metadata API only).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

CID = "69be1862-004b-43c2-bd49-e20688d3f965"
TRACE_LAB = Path(__file__).resolve().parent.parent
DERIVED = Path.home() / "Developer" / "eval-lab" / "derived" / "trace-lab"
R528 = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs")
R531 = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs")
TASK_STORE = Path.home() / "Developer" / "eval-lab" / "derived" / "task-store" / "hf"

CEILING_DIMS = ("input_tokens", "output_tokens", "requests", "total_tokens")


def raw_trials_81() -> list[tuple[Path, str, str]]:
    """[(raw_trial_dir, job, trial)] for the 44 HAR-81 trials."""
    man = json.loads((DERIVED / "normalized" / "har81" / "manifest.json").read_text())
    out = []
    for t in man["trials"]:
        if not t["job"].startswith("har81-"):
            continue
        for base in (R528, R531):
            p = base / t["job"] / t["trial"]
            if p.is_dir():
                out.append((p, t["job"], t["trial"]))
                break
        else:
            raise SystemExit(f"raw trial dir missing: {t['job']}/{t['trial']}")
    return out


def task_domain(task_name: str) -> str:
    """Eval Lab task-store domain for mimo-v2.6-rl/<task_id> (code/cyber/...)."""
    short = task_name.split("/")[-1]
    hits = []
    for store in sorted(TASK_STORE.iterdir()):
        if (store / "tasks" / short).is_dir():
            prefix = "FineEnvs__MiMo-V2.6-RL-harbor-"
            hits.append(store.name[len(prefix):].split("@")[0] if store.name.startswith(prefix) else store.name)
    if len(hits) != 1:
        raise ValueError(f"{task_name}: domain ambiguous {hits}")
    return hits[0]


def budget_caps(job_dir: Path) -> dict[str, int | None]:
    lab = json.loads((job_dir / "lab-metadata.json").read_text())
    src: dict = {}
    for path in ("provider_usage/limits", "harness_tree/execution_settings"):
        node: object = lab
        for part in path.split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        if isinstance(node, dict):
            src.update(node)

    def get(*names: str) -> object:
        return next((src[n] for n in names if isinstance(src.get(n), (int, float))), None)

    return {
        "input_tokens": get("max_input_tokens"),
        "output_tokens": get("max_output_tokens"),
        "requests": get("max_requests"),
        "total_tokens": get("max_total_tokens"),
    }


def binding_ceiling(trial_dir: Path) -> str:
    """Which TrialBudgetExhausted ceiling bound hardest, by utilization."""
    result = json.loads((trial_dir / "result.json").read_text())
    agent = result.get("agent_result") or {}
    meta = agent.get("metadata") or {}
    n_in = agent.get("n_input_tokens") or 0
    n_out = agent.get("n_output_tokens") or 0
    used = {
        "input_tokens": n_in,
        "output_tokens": n_out,
        "requests": meta.get("n_episodes") or 0,
        "total_tokens": n_in + n_out,
    }
    caps = budget_caps(trial_dir.parent)
    best, best_ratio = "ceiling:trial_budget", -1.0
    for dim in CEILING_DIMS:
        cap = caps[dim]
        if cap:
            ratio = used[dim] / cap
            if ratio > best_ratio:
                best, best_ratio = f"ceiling:{dim}", ratio
    return best


def cmd_compute(out: Path) -> int:
    from evallab.interpretation.run_report import build_run_report
    from evallab.results import load_job

    job_cache: dict[str, object] = {}
    rows: dict[str, dict] = {}
    trials = raw_trials_81()
    print(f"resolved {len(trials)} raw trial dirs", flush=True)
    for trial_dir, job, trial in trials:
        report = build_run_report(trial_dir)
        stop = report["outcome"]["stop_reason"]
        if stop == "trial_budget_exhausted":
            stop = f"{stop}:{binding_ceiling(trial_dir)}"
        task = report["identity"]["task"]
        # Cross-check task/reward against Eval Lab load_job (acceptance).
        if job not in job_cache:
            job_cache[job] = load_job(trial_dir.parent)
        recs = [t for t in job_cache[job].trials if Path(str(t.path)).name == trial]
        assert len(recs) == 1, f"{job}/{trial}: load_job match {len(recs)}"
        rec = recs[0]
        lj_task = (json.loads(rec.result.replace("'", '"')) if isinstance(rec.result, str) else rec.result)["task_name"] \
            if isinstance(rec.result, str) else rec.result["task_name"]
        lj_reward = (json.loads(rec.rewards.replace("'", '"')) if isinstance(rec.rewards, str) else rec.rewards)["reward"] \
            if isinstance(rec.rewards, str) else rec.rewards["reward"]
        assert lj_task == task, f"{trial}: load_job task {lj_task} != report {task}"
        assert float(lj_reward) == float(report["outcome"]["reward"]), f"{trial}: reward mismatch"
        rows[trial] = {
            "job": job,
            "task": task,
            "reward": report["outcome"]["reward"],
            "verdict": report["outcome"]["verdict"],
            "stop_reason": stop,
            "stop_detail": report["outcome"]["stop_detail"],
            "domain": task_domain(task),
            "load_job_task": lj_task,
            "load_job_reward": lj_reward,
        }
    out.write_text(json.dumps(rows, indent=1, sort_keys=True), encoding="utf-8")
    from collections import Counter
    print("stop:", dict(Counter(r["stop_reason"] for r in rows.values())))
    print("verdict:", dict(Counter(r["verdict"] for r in rows.values())))
    print("domain:", dict(Counter(r["domain"] for r in rows.values())))
    print(f"wrote {out} ({len(rows)} trials, all load_job cross-checks passed)")
    return 0


def _client():
    from docent import Docent
    return Docent()


def cmd_apply(payload: Path) -> int:
    rows = json.loads(payload.read_text())
    client = _client()
    before = client.get_collection_collaborators(CID)
    ids = client.list_agent_run_ids(CID)
    print(f"collaborators before: {len(before)} ({[c.get('subject', {}).get('email') for c in before]})")
    print(f"runs before: {len(ids)}")
    assert len(before) == 1 and len(ids) == 44, "privacy/count precheck failed"

    trial_to_id: dict[str, str] = {}
    for rid in ids:
        meta = client.get_agent_run_metadata(CID, rid)
        trial_to_id[meta["trace_lab"]["trial"]] = rid
    missing = set(rows) - set(trial_to_id)
    assert not missing, f"trials without Docent run: {sorted(missing)}"

    # Snapshot tasks/rewards before (acceptance: identical to Eval Lab).
    before_pairs = {}
    for trial in rows:
        before_pairs[trial] = client.get_agent_run_metadata(CID, trial_to_id[trial])["trace_lab"]

    n_updated = 0
    for trial, row in sorted(rows.items()):
        rid = trial_to_id[trial]
        meta = client.get_agent_run_metadata(CID, rid)
        block = dict(meta["trace_lab"])
        assert block["task"] == row["task"] and float(block["reward"]) == float(row["reward"]), \
            f"{trial}: Docent task/reward drift vs Eval Lab"
        assert not any(k in block for k in ("first_failure", "outcome_relevant_failure", "hand_", "probe03")), \
            f"{trial}: unexpected label field already present"
        block.update({"stop_reason": row["stop_reason"], "verdict": row["verdict"], "domain": row["domain"]})
        client.update_agent_run_metadata(CID, rid, {"trace_lab": block})
        back = client.get_agent_run_metadata(CID, rid)["trace_lab"]
        assert (back["stop_reason"], back["verdict"], back["domain"]) == \
            (row["stop_reason"], row["verdict"], row["domain"]), f"{trial}: read-back mismatch"
        n_updated += 1
    after = client.get_collection_collaborators(CID)
    ids_after = client.list_agent_run_ids(CID)
    print(f"updated {n_updated} runs")
    print(f"collaborators after: {len(after)}; runs after: {len(ids_after)}")
    assert len(after) == 1 and len(ids_after) == 44, "privacy/count postcheck failed"
    return 0


def cmd_verify() -> int:
    client = _client()
    collabs = client.get_collection_collaborators(CID)
    print(f"collaborators: {len(collabs)} ({[c.get('subject', {}).get('email') for c in collabs]})")
    q = "SELECT COUNT(id) AS n FROM (SELECT id FROM agent_runs) AS t"
    print("run count:", client.dql_result_to_dicts(client.execute_dql(CID, q)))
    for label, field in (("stop_reason", "stop_reason"), ("verdict", "verdict"), ("domain", "domain")):
        qq = (f"SELECT val, COUNT(val) AS n FROM (SELECT metadata_json->'trace_lab'->>'{field}' AS val "
              "FROM agent_runs) AS s GROUP BY val ORDER BY n DESC")
        print(label, client.dql_result_to_dicts(client.execute_dql(CID, qq)))
    blind = ("SELECT COUNT(id) AS n FROM (SELECT id FROM agent_runs WHERE "
             "(metadata_json->'trace_lab' ? 'first_failure') OR "
             "(metadata_json->'trace_lab' ? 'outcome_relevant_failure')) AS s")
    print("runs with probe-03/hand fields (want 0):", client.dql_result_to_dicts(client.execute_dql(CID, blind)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("compute")
    p.add_argument("--out", default=str(TRACE_LAB / "docent" / "har81_metadata_backfill.json"))
    p2 = sub.add_parser("apply")
    p2.add_argument("--payload", default=str(TRACE_LAB / "docent" / "har81_metadata_backfill.json"))
    sub.add_parser("verify")
    args = parser.parse_args(argv)
    if args.mode == "compute":
        return cmd_compute(Path(args.out))
    if args.mode == "apply":
        return cmd_apply(Path(args.payload))
    return cmd_verify()


if __name__ == "__main__":
    raise SystemExit(main())
