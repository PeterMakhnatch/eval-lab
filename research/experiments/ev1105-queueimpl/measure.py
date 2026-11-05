"""Exercise the production ordering-only export on frozen DEV inputs. No model calls."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
from uuid import uuid4

from evallab.line.core import check_batch, run_line

ROOT = Path(__file__).resolve().parents[3]


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def measure(data: Path, cache: Path, labels: Path, private: Path, output: Path) -> dict:
    # Reuse the predecessor's task-cluster bootstrap rather than invent a new estimator.
    source = ROOT / "research/experiments/ev1105-queue/evaluate.py"
    module_spec = importlib.util.spec_from_file_location("queue_ranking", source)
    assert module_spec and module_spec.loader
    ranking = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(ranking)
    private.mkdir(parents=True, exist_ok=True)
    original = json.loads((data / "spec.json").read_text())
    batches = {}
    checks = {}
    for name, ordered, audit in (("arrival", False, 0), ("ordered", True, 0), ("audit", True, 10)):
        spec = json.loads(json.dumps(original))
        spec["batch_id"] = f"queueimpl-{name}-{uuid4().hex}"
        spec["out_root"] = str(private / "batches")
        spec["judge"]["write"] = False
        spec["autoreview"]["j3"] = {"mode": "off"}
        spec["audit"]["keep_top_n"] = audit
        if ordered:
            spec["review_order"] = {"j3_cache": str(cache)}
        spec_path = private / f"{name}-spec.json"
        spec_path.write_text(json.dumps(spec))
        batch = run_line(spec_path, ROOT)
        checks[name] = check_batch(batch, ROOT)
        if not checks[name]["passed"]:
            raise ValueError(f"line check failed: {name}: {checks[name]}")
        batches[name] = batch
    decisions = {name: rows(batch / "decisions.jsonl") for name, batch in batches.items()}
    def policy(row: dict) -> dict:
        return {key: value for key, value in row.items() if key not in {"review_priority", "review_priority_source"}}
    baseline = [policy(row) for row in decisions["arrival"]]
    changed = {name: sum(a != policy(b) for a, b in zip(baseline, values, strict=True))
               for name, values in decisions.items()}
    if any(changed.values()):
        raise ValueError(f"ordering changes policy: {changed}")
    # Labels enter only after all 3 production runs and checks finish.
    dev = {r["run_key"]: r for r in rows(labels) if r["split"] == "dev"}
    queues = {}
    readings = {row["run_id"]: row["reading"] for row in rows(cache)}
    for name, file in (("review", "review.jsonl"), ("kept", "kept.jsonl")):
        before = rows(batches["arrival"] / "export" / file)
        after = rows(batches["ordered"] / "export" / file)
        keys = [f"{r['corpus']}:{r['id']}" for r in before]
        priorities = {f"{r['corpus']}:{r['id']}": r["review_priority"] for r in after}
        metrics = ranking.evaluate_subset(keys, dev, {"j3": {k: priorities[k] if priorities[k] is not None else -1 for k in keys}})
        queues[name] = {
            "n": len(keys), "cheats": sum(dev[k]["label"] for k in keys),
            "complete": sum(v is not None for v in priorities.values()),
            "fallback": sum(v is None for v in priorities.values()),
            "metrics": metrics,
            "historical_load": {
                "runs": len(before),
                "windows": sum(len(readings.get(r["run_id"], {}).get("windows", [])) for r in before),
                **{
                    field: sum(w[field] for r in before for w in readings.get(r["run_id"], {}).get("windows", []))
                    for field in ("tokens_in", "tokens_out", "cost_usd")
                },
            },
        }
        if name == "review":
            expected = sorted(keys, key=lambda k: (priorities[k] is None, -(priorities[k] or 0)))
            if expected != [f"{r['corpus']}:{r['id']}" for r in after]:
                raise ValueError("production export order differs from measured order")
        else:
            kept_order = sorted(keys, key=lambda k: (priorities[k] is None, -(priorities[k] or 0)))
    audit = rows(batches["audit"] / "export/keep-audit.jsonl")
    audit_keys = [f"{r['corpus']}:{r['id']}" for r in audit]
    default_audit_path = batches["ordered"] / "export/keep-audit.jsonl"
    default_audit = rows(default_audit_path) if default_audit_path.exists() else []
    if len(audit) != 10 or default_audit or audit_keys != kept_order[:10]:
        raise ValueError("keep audit does not respect top N/default off")
    result = {
        "schema": "evallab.queueimpl_measurement/v1", "split": "dev",
        "fallback": "scored first descending; stable arrival for ties and unavailable readings",
        "decision_changes": changed, "model_calls": 0, "cost_usd": 0,
        "bootstrap": {"unit": "task_cluster", "replicates": 2000, "seed": 1106, "interval": "percentile 95%"},
        "inputs": {str(p): digest(p) for p in (data / "spec.json", cache, labels)},
        "production_checks": {k: v["passed"] for k, v in checks.items()},
        "private_batches": {k: str(v) for k, v in batches.items()},
        "queues": queues,
        "keep_audit": {"requested": 10, "n": len(audit), "cheats": sum(dev[k]["label"] for k in audit_keys), "default_n": len(default_audit)},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"decision_changes": changed, "checks": result["production_checks"], "output": str(output)}))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path.home() / "Developer/eval-lab-results/line/replay-v1")
    parser.add_argument("--cache", type=Path, default=Path("/private/tmp/ov1106/j3line/j3-cache.jsonl"))
    parser.add_argument("--labels", type=Path, default=Path("/private/tmp/ov1104/shared/detbench/runs.jsonl"))
    parser.add_argument("--private", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "results/aggregates.json")
    args = parser.parse_args()
    measure(args.data, args.cache, args.labels, args.private, args.output)
