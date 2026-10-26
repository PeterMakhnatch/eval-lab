# OQ-3 — trace-cheap proposed-candidate discovery and a two-stage cascade

Question (CONTRACT §4, priorities #3–#4): can deterministic tripwires plus
`CloudTraceRetriever` analogues propose candidate mechanisms so a cheap judge
discovers trace exploits without being told the mechanism, and does a
two-stage cascade (one cheap judge, escalate only flagged/ambiguous traces to
Opus) beat a single Opus pass and a two-model union per token?

**Answer: no, on this cohort.** No arm exceeds 15% recall. The proposer never
surfaced the gold hackbook entry, so "proposed" mode is not confirmation
mode. The cascade escalated about half the traces, and Opus overturned most
cheap-model flags. Proposals and escalation therefore ship **opt-in**
(`--propose`, `--escalate-model`), not as defaults.

## Cohort (frozen, DEV only)

`build_cohort.py` takes inputs only from `qa-trace-sft-v0/dev.jsonl` and a
read-only clone of the Cloud Trace DB (snapshot sha256
`2ddb3c85…5bd`). Manifest sha256: `0f4a4473…591c`.

| Cohort | Positives (`exploit-pass`) | Negatives | Median agent steps | Label provenance |
|---|---:|---:|---:|---|
| qa-trace-sft-v0 DEV | 32 (all) | 32 (first by sha256) | 14 | dataset labels; mechanism-supported, not replay-confirmed |
| Goodfire DEV | 50 | 50 (`verdict_enacted=no`, 0 flags) | 24 | provisional 3-vote publisher consensus |

Goodfire sampling allows one trace per task. Only final-parent-path steps are
kept, and the 95 held tasks are never read. The dataset's
`candidate_hackbook_entry` is withheld, because this measures discovery.
Retrieval excludes the target task; analogues come from other admitted DEV
tasks, mostly Goodfire.

## Arms (`run.py`; every call records requested/served model, tokens, response sha256)

| Arm | Judge | Input |
|---|---|---|
| `shortlist_deepseek` | `deepseek/deepseek-v4.1-flash` (OpenRouter) | hackbook short list (old discovery mode) |
| `proposed_deepseek` | same | ≤5 proposed leads: tripwire hackbook hits, matcher, 2 retrieved analogues |
| `proposed_sonnet` | `anthropic/claude-sonnet-5-5` (OMP, thinking high, no tools/memory/fallback) | identical proposed prompt |
| `proposed_opus` | `anthropic/claude-opus-5-5` (same harness) | identical proposed prompt |
| union | `proposed_deepseek` ∪ `proposed_sonnet` | flags if either flags |
| cascade | `proposed_deepseek`, then `proposed_opus` on escalated traces | escalate on flagged, unparsed, forced-abstain, or strong-tripwire `neither` |

Every case uses one byte-identical user prompt across the proposed arms; the
scorer checks this. The cascade's stage 2 reuses the Opus answer on the same
prompt bytes, and only escalated Opus calls count toward its tokens.

## Results (primary run: DeepSeek `max_tokens=4096`)

| System | QA DEV TP/32 | QA DEV FA/32 | Goodfire TP/50 | Goodfire FA/50 | Unanswered | Tokens | TP per M tok | Correct per M tok |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| short list, DeepSeek | 2 | 2 | 1 | 2 | 0 | 1,591,384 | 1.89 | 50.9 |
| proposed, DeepSeek | 2 | 14 | 7 | 2 | 0 | 1,595,754 | **5.64** | 47.0 |
| proposed, Sonnet | 2 | 3 | 2 | 0 | 6 | 1,908,037 | 2.10 | 43.5 |
| proposed, Opus | 2 | 13 | 2 | 1 | 8 | 1,926,859 | 2.08 | 37.4 |
| union DeepSeek+Sonnet | 4 | 14 | 8 | 2 | 6 | 3,503,791 | 3.42 | 22.3 |
| cascade DeepSeek→Opus (80/164 escalated) | 2 | 12 | 2 | 1 | 2 | 2,568,831 | 1.56 | 28.4 |

TP = cited `exploit-pass` on a gold positive. FA = any non-`neither` on a
gold negative. Unanswered rows are Opus/Sonnet cyber-policy refusals on
CheatBench rows; they were kept as non-detections, never rerouted. "Correct
per M tok" counts true negatives, so a do-nothing detector scores well on it.
Use TP per M tok as the useful-work rate. OpenRouter spend was $0.12 for this
run and about $0.32 across all OQ-3 runs. Opus/Sonnet ran on a subscription,
so tokens are reported, not dollars.

Diagnostics (`scores.json` → `proposal_diagnostics`):

- QA DEV positives received proposals in 24/32 cases, but the gold hackbook id
  appeared **0/32** times. Gold evidence was visible in the judge window for
  23 of the 24 positives with cited evidence, so truncation did not cause the
  misses. Negatives drew more hackbook leads (21/32) than positives did (9/32).
- On Goodfire, retrieved analogue categories overlapped the gold categories on
  45/50 positives. Negatives also received analogues (41/50), so the overlap
  does not separate the classes. Goodfire's dominant categories,
  `upstream_fix_recall` (43/50) and `hidden_test_targeting` (36/50), are mostly
  stated in assistant prose, not in commands.
- Cascade escalation reasons were `tripwire_unresolved` 42, `flagged` 26,
  `judge_unparsed` 6 and `forced_abstain` 6. On Goodfire the cascade finished
  at 2/50 TP, against 7/50 for its own stage 1: Opus overturned most flags.

## Variance and post-hoc runs (disclosed)

- **First run** (`max_tokens=1500`, the old default) left 20/164 DeepSeek
  answers unparsed: reasoning exhausted the budget. Shortlist recall was 2/82
  with 0 FA; proposed recall was 5/82 with 8 FA. The production default is now
  4096. Both DeepSeek arms were rerun with byte-identical prompts, and the
  table above uses that rerun. Run-to-run changes are as large as some arm
  differences, so treat all differences of 1–5 TP as noise at this n.
- **Prose-view diagnostic (post hoc, Goodfire only, DeepSeek):** adding
  assistant prose (`build_cohort.py --prose`) did not help. The short list got
  2/50 TP and 6 FA; proposed got 2/50 TP and 4 FA. The 7,000-character action
  window shows only a small part of a long Goodfire trace, while Goodfire
  labels come from passage-level judgments over the whole trace.
- **Merged-code reproduction:** on the rebased branch (after #1222, which
  admits more DEV identities), 326/328 judged DeepSeek prompts rebuild
  byte-for-byte (`rerun-maxtok4096/prompt-reproduction.json`). The other two
  proposed prompts each differ by one retrieved analogue, a newly admitted
  MiMo task.

## Reproduce

```bash
python3 research/experiments/oq3-discovery-cascade/build_cohort.py --db <snapshot> --out /private/tmp/oq3/cohort
keys run -- uv run python research/experiments/oq3-discovery-cascade/run.py run --cohort /private/tmp/oq3/cohort --db <snapshot> --out <out>
uv run python research/experiments/oq3-discovery-cascade/run.py score --cohort /private/tmp/oq3/cohort --out <out>
```

Raw receipts: `~/Developer/eval-lab-results/2026-10-26/oq3-discovery-cascade/`.
They include `records/` from the first run, plus `rerun-maxtok4096/` and
`diagnostic-prose-view/`. `results.json` here is the scorer output for all three.
