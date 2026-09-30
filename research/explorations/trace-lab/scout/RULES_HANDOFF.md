# Rules handoff: deterministic trace rules for `process-job`

Trace Lab's probe-03 capability rules, packaged as one importable module so
Engineering can run them inside `process-job`. No model calls, no launches,
no uploads, $0. Contact: Traces lane (HAR-109).

## Where

`research/explorations/trace-lab/scout/rules.py` — one function,
`analyze_trial_rules(trial_dir, *, evallab_src=None, nop_runs_dir=None)`.
It calls probe-03's `capabilities.py` rule functions
(`research/explorations/trace-lab/probe-03-capabilities/capabilities.py`)
on the trial's own step records; it never reads precomputed labels.

## Inputs (Eval Lab records, read-only, per trial dir)

`result.json` (task, reward, exception, token totals), `config.json`,
`agent/trajectory.json` + `agent/trajectory.cont-*.json` (head+cont stitched
with prefix-drop), `verifier/` (submit verdict, test stdout), `trial.log`
(summarization livelock), `../lab-metadata.json` (proxy ceilings). Optional:
`evallab_src` dir owning `normalize_mimo_tool_calls` (defaults to the HAR-81
normalizer source; pass the current commit's `src` for new runs),
`nop_runs_dir` control tree for the R-ENV-02 cross-check (defaults to
`mimo-ops/runs` when present, else `nop_control_confirms: "unknown"`).

## Outputs (plain dicts, same vocabulary as `har81/capabilities.jsonl`)

`task`, `verifier{scored,reward}`, `stop{reason,exception_type,
natural_completion,task_complete_refs}`, `first_failure` (or null),
`outcome{tag,attribution,rule_id,evidence_step_refs,note,...}`,
`handshake` (first prompt ref, confirmed, echo turns; or null),
`loops{spans,loop_prompt_share,...}`, `wedge{stretches,...}`,
`confirmation_loop`. Evidence cites are probe-03 step refs (`head#12`).
Treatment keys, learnability, and reading sheets are intentionally omitted.

## Tests

`research/explorations/trace-lab/scout/tests/` — 22 deterministic tests:
golden-trial expectations, spot checks per rule family, and a full 44/44
rule-id + stop + evidence-ref match against the frozen
`har81/capabilities.jsonl` (read-only reference). Run:
`uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 -- python -m
pytest research/explorations/trace-lab/scout/tests/ -p no:cacheprovider
-o addopts=''`.

## How `process-job` would call it

```python
import sys
sys.path.insert(0, "research/explorations/trace-lab/scout")
from rules import analyze_trial_rules
rules = analyze_trial_rules(trial_dir)  # trial_dir: Path to one trial
outcome, first = rules["outcome"], rules["first_failure"]
```

Per-trial cost is seconds (single pass over steps; results cached per
process). Scout scanners in `scout/scanners.py` (`rule_outcome`,
`rule_first_failure`, `rule_handshake`, `rule_loops`, `rule_stop`,
`rule_wedge`) already consume this module and map evidence refs to Scout
message cites. Rule definitions live in the probe-03 README
(`probe-03-capabilities/README.md`: R-*, A-*, CC-CLAIM, HANDSHAKE,
LOOP-COST, WEDGE); do not re-tune thresholds without a new held-out key.
