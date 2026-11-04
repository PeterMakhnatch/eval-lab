import collections
import hashlib
import json
import random
from pathlib import Path

root = Path(__file__).resolve().parent
runs_path = Path("/private/tmp/ov1104/shared/detbench/runs.jsonl")
baseline = Path.home() / "Developer/eval-lab-results/line/replay-v1/baseline.json"
protocol = root / "PROTOCOL.md"
inputs = [runs_path, baseline, protocol, Path(__file__)]
hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
(root / "review-input-hashes.json").write_text(json.dumps(hashes, indent=2) + "\n")
runs = {r["run_key"]: r for r in map(json.loads, runs_path.open()) if r["split"] == "dev"}
decisions = {
    (r["corpus"], r["run_id"]): r["decision"] for r in json.loads(baseline.read_text())["decisions"]
}
groups = collections.defaultdict(lambda: collections.defaultdict(list))
for key, r in runs.items():
    decision = decisions[(r["set"], key[len(r["set"]) + 1 :])]
    groups[r["set"]][r["cluster"]].append((r["label"], decision))
blocks = {s: list(dict(sorted(cs.items())).values()) for s, cs in sorted(groups.items())}
scenarios = {"review_only": {"review"}, "keep_or_review": {"keep", "review"}}
counts = {name: [0, 0] for name in scenarios}
for cs in blocks.values():
    for c in cs:
        for label, d in c:
            for name, allowed in scenarios.items():
                if d in allowed:
                    counts[name][0] += label
                    counts[name][1] += 1
rng = random.Random(110407)
draws = {name: [] for name in scenarios}
for _ in range(5000):
    chosen = [cluster for cs in blocks.values() for cluster in (rng.choice(cs) for _ in cs)]
    for name, allowed in scenarios.items():
        obs = [label for c in chosen for label, decision in c if decision in allowed]
        if obs:
            draws[name].append(sum(obs) / len(obs))
result = {}
for name, (k, n) in counts.items():
    values = sorted(draws[name])

    def q(p, values=values):
        i = (len(values) - 1) * p
        lo = int(i)
        return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (i - lo)

    result[name] = {
        "cheats": k,
        "selected": n,
        "rate": k / n,
        "ci95": [q(0.025), q(0.975)],
        "valid_draws": len(values),
    }
assert hashes == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
(root / "review-sensitivity.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
