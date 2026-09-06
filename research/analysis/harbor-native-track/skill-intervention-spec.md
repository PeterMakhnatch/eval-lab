# Task 4 — first --skill intervention: analysis spec (DESIGNED, NOT RUN)

Status: frozen spec. Execution needs Peter's approve (billable, ~$1.50). The data
engineer executes blind per §5; no analysis change after unblinding.

## 1. Question

Does injecting a debugging-discipline skill change success rate, failure depth,
cost, or tool-use behavior on `terminal-bench-html-js-filter`, holding task,
agent, model, and date window fixed?

## 2. Design

- Task: `terminal-bench-html-js-filter` (local path `library/tasks/...`; rationale:
  0/6 scored locally → headroom; deterministic verifier modulo browser timeouts,
  handled in §6).
- Arms (k=3 fresh trials each): CONTROL = `harbor run <task> --agent codex
  --model <PINNED>`; TREATMENT = identical + `--skill ./skills/debugging-discipline`.
  Model pinned at execution and recorded (recommend cheapest model clearing the
  pack; do not mix models across arms).
- The skill under test: `research/analysis/harbor-native-track/skills/debugging-discipline/SKILL.md`
  (frozen in this commit; any edit = new intervention, new spec version).
- Cost envelope: ~$0.25/trial observed → 6 trials ≈ $1.50. Abort if a single
  trial exceeds $1.00 (record, exclude, note).

## 3. Pre-declared metrics (no other number may be headlined)

| # | Metric | Source | Denominator |
|---|---|---|---|
| M1 (primary) | success rate (reward 1.0) | `result.json:verifier_result.rewards.reward` | n=3/arm scored trials |
| M2 | failed-vector count | `verifier/test-stdout.txt`, parse `assert N == 0` | per trial; arm median reported |
| M3 | cost_usd sum | ATIF `final_metrics.total_cost_usd` | per arm |
| M4 | tool-call count | ATIF agent steps `tool_calls` length | per trial; arm median |
| M5 | blind-retry count | consecutive identical `exec` command strings within a trial | per trial; arm median |

## 4. Claim boundaries (frozen before data)

- n=3/arm: vendored MDE ≈ 1.1 for rates — **no significance claim is licensed**.
  Report Wilson 95% per arm + raw deltas + M2 medians. The read-out is a pilot
  effect-size + cost observation, not a skill efficacy claim.
- M2 (failed-vector count) is the sensitive metric: a skill that halves vectors
  12→6 without flipping reward is a real, reportable partial effect.
- No cross-task generalization. No "skills work" headline. One task, one skill,
  one model.

## 5. Blind execution protocol (data engineer)

1. Flip a coin (record result in `envelope.txt`, do not share). Map heads→(A=control,B=treatment).
2. Run both jobs same day, same `--n-concurrent 1`, `--export-traces` on.
3. Deliver two directories named only `ARM_A/`, `ARM_B/` (full Harbor job dirs).
4. Analyst runs the frozen analysis below, writes results, THEN opens the envelope.
5. Any deviation (crash rerun, timeout rerun) logged in `deviations.log` before unblinding.

## 6. Exclusion rules (frozen)

- Agent-launch crash (0 agent steps + exception): rerun once; second crash →
  trial recorded missing (null), denominator n=2 for that arm with note.
- Verifier `Page.goto` timeout present AND clean-test passed: rerun once; repeat →
  keep trial, report M1 as observed and M2 with timeout-flagged note (timeouts
  logged "No execution detected" are not filter misses — established in Task 3).
- No other exclusions. No post-hoc slice changes.

## 7. Frozen analysis (exact commands; run before unblinding)

```bash
# per-arm table from two blinded job dirs ($A, $B = ARM_A, ARM_B paths)
uv run python - <<'EOF'
import json, glob, os, re
for arm, path in (("A", "$A"), ("B", "$B")):
    rows = []
    for rj in sorted(glob.glob(path + "/**/result.json", recursive=True)):
        trial = os.path.dirname(rj)
        if not os.path.isdir(trial + "/agent"): continue
        r = json.load(open(rj)); tj = trial + "/agent/trajectory.json"
        rw = ((r.get("verifier_result") or {}).get("rewards") or {}).get("reward")
        d = json.load(open(tj)) if os.path.exists(tj) else {"steps": [], "final_metrics": {}}
        calls = [c for s in d["steps"] if s.get("source") == "agent" for c in (s.get("tool_calls") or [])]
        cmds = [json.dumps(c.get("arguments"), sort_keys=True) for c in calls if (c.get("function_name") or c.get("name")) == "exec"]
        blind = sum(1 for i in range(1, len(cmds)) if cmds[i] == cmds[i - 1])
        m = re.search(r"assert (\d+) == 0", open(trial + "/verifier/test-stdout.txt").read()) if os.path.exists(trial + "/verifier/test-stdout.txt") else None
        fm = d.get("final_metrics") or {}
        rows.append({"trial": os.path.basename(trial), "reward": rw, "failed_vectors": int(m.group(1)) if m else None,
                     "cost": fm.get("total_cost_usd"), "tool_calls": len(calls), "blind_retries": blind})
    print("ARM", arm, json.dumps(rows, indent=1))
EOF
```

Then: M1 Wilson per arm (vendored `wilson_ci`), M2–M5 medians, cost sums. Write
`skill-intervention-results.md` in this folder, then unblind and append the
arm mapping. If the envelope mapping contradicts the analysis file's arm labels,
the analysis stands and the contradiction is reported, not edited.
