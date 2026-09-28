# HAR-85 DSPy arm: approval contract (STAGED, nothing approved)

Gate stated plainly: this arm's dspy.GEPA runs OUTSIDE the Lab queue
(direct `harbor run` per metric call). There is no queue spec to approve,
so `evallab approve` cannot gate it and is NOT invoked. The spend controls
are (1) this recorded approval, (2) `run-after-approval.sh` refusing without
it, (3) `gepa_mimo.py` refusing its paid path without `--approval-file`,
(4) per-trial `cost_limit_usd=1.0`.

## Exact approval commands (Peter runs these; workers never do)

Phase 1 — GEPA train/val (paid student + reflection calls):

```bash
printf 'HAR-85 dspy-arm phase1 approved %s cap $%.2f ref %s\n' \
  "$(date -u +%Y-%m-%dT%H:%MZ)" <CAP> <LINEAR-COMMENT-URL> \
  >/private/tmp/har85-dspy-approval-phase1
cd /Users/petermakhnatch/Developer/eval-lab/.worktrees/har85-dspy-rlm
research/experiments/har85-mimo-gepa/dspy/run-after-approval.sh --phase gepa \
  --approval-file /private/tmp/har85-dspy-approval-phase1 \
  --train-tasks <CSV of train_task_ids> --val-tasks <CSV of train_task_ids> \
  --max-metric-calls <N>
```

Phase 2 — final paired held-out eval, ONCE, after the winner exists:

```bash
printf 'HAR-85 dspy-arm phase2 approved %s cap $%.2f winner %s ref %s\n' \
  "$(date -u +%Y-%m-%dT%H:%MZ)" <CAP> <WINNER-DIGEST> <LINEAR-COMMENT-URL> \
  >/private/tmp/har85-dspy-approval-phase2
.../run-after-approval.sh --phase heldout \
  --approval-file /private/tmp/har85-dspy-approval-phase2 \
  --winner runs/har85-gepa-mimo/phase1/<winner>.json --attempts 3
```

Caps vs standing policy (`policy/standing-approvals.yaml`:
per_job_cost_ceiling_usd 3, daily_cost_ceiling_usd 20): fill `<CAP>` from
`BUDGET.md`. If a phase exceeds $3, Peter must raise
`per_job_cost_ceiling_usd` first — the script does not (and cannot) do that.

Prerequisites before EITHER approval: `../split.provisional.json` merged by
Har85Gepa (the script refuses without it); `ZAI_OPENAPI_API_KEY` present in
the approver's environment (presence check only, never printed); the sealed
HAR-81 split, when available, replaces the provisional manifest and the
train/val lists are regenerated from it.

## Recorded refusal outputs (exercised 2026-09-28, $0)

`run-after-approval.sh` with no args:

```
usage:
  run-after-approval.sh --phase gepa --approval-file <path> [--train-tasks a,b] [--val-tasks c] [--max-metric-calls N]
  run-after-approval.sh --phase heldout --approval-file <path> --winner <policy.json> [--attempts K]
Without a phase + existing approval file this script runs nothing (exit 2).
exit=2
```

`run-after-approval.sh --phase gepa --approval-file /nonexistent`:

```
refusing: approval file not found: /nonexistent
exit=2
```

`gepa_mimo.py` paid path without `--approval-file`:

```
REFUSING: the paid path requires --approval-file pointing at the recorded approval token (see dspy/APPROVAL.md). Use --dry-run for the $0 path.
exit=2
```

## What approval authorizes (and what it does not)

- Authorizes: local-Docker Harbor trials with the paid student route +
  reflection model, bounded by the cap in the approval line and the formula
  in `BUDGET.md`; public Docker image pulls; no data leaves the host
  except paid model API calls.
- Does NOT authorize: cloud sandboxes (Daytona/Modal), GPUs, model-weight
  downloads, publication/upstream PRs, touching the primary checkout or any
  other agent's worktree, or spending on any other arm.
