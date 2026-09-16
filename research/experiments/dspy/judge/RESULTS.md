# DSPy calibration judge — results (2026-09-16)

Model: GLM 5.3 Flash (`zai/glm-5.3-flash`, hidden thinking off, temperature 0)
via the Lab's Z.ai Coding Plan route. Metric: exact per-criterion agreement with
the sealed keys (`evallab.calibrate.dspy_metric`).

## Evidence-bound contract (HAR-60) — what changed and what the judge sees now

Everything under "Pre-evidence results" below was measured with a judge that saw
only `rubric_json` (reference facts + criteria) and the document. The sealed keys
were decided against the incident's evidence directory (`/app/evidence`), which
that judge never had; GEPA compensated by memorising fixture file names as
"not invented". Those numbers are historical: they are not evidence that the
repaired contract passes, and the daily digest now reports their records as
"pre-evidence … not calibration".

The judge input is now `family, rubric_json, evidence, document`, where
`evidence` is the family's evidence directory vendored byte-for-byte from the
pinned task source (`research/calibration-evidence/<family>/`, manifest with
upstream revision `harbor-practice@a3bedf45` and per-file sha256; 7 files,
≈9.6 kB per family). Bundles and records carry `evidence_digest`. A rendered
real example — the exact ChatAdapter messages for `10-correct-timeline-dense`,
with the `service-config.yaml` the document cites present verbatim under
`[[ ## evidence ## ]]` — is `artifacts/input-examples/checkout-pool-exhaustion/10-correct-timeline-dense.md`
(`python -m judge.run show-input`).

Contract failures observed on real inputs, none of them silent:

- loading the overnight `gepa-checkout/program.json` onto the new signature is
  refused: *"saved fields ['Family:', 'Rubric Json:', 'Document:', …] do not
  match the judge signature […, 'Evidence:', …]; compiled against a different
  input contract"* (DSPy would otherwise have paired the saved field texts
  positionally and shifted every description onto the wrong field);
- a judge that omits any criterion no longer becomes a bundle: the runner exits
  with every missing `document: dimension.criterion` cell listed and writes
  only the metrics file (with `incomplete`), so `evallab calibrate` has nothing
  to record;
- an edited, missing or unlisted evidence file fails `load_evidence_pack` by
  name before any model call.

One live call on `19-fabricated-evidence-dashboards` with the unoptimized
program under the new contract (5,488 prompt tokens): `invents_evidence = yes`,
rationale *"It cites a Datadog monitor 88412 and a PagerDuty note attributed to
s.lindqvist claiming 12,400 customers; neither appears in the evidence files or
reference facts."* — the decision is now made against the files, not against a
memorised list. Unoptimized full-family baselines under the new contract are in
`artifacts/baseline-evidence/` (see the table at the end of this file).

## Pre-evidence results (historical; judge saw rubric + document only)

Every number below has a `artifacts/<run>/metrics-<family>-<split>.json` on the
`feat/dspy-overnight-20260916` branch (PR #433); full-family runs also have a
`bundle-<family>.json` and, when clean, a record there under
`research/calibration/records/<family>/`.

**Clean** = the optimizer never saw any document of the scored set.
**Contaminated** = 16 of 22 scored documents were optimizer-visible; reported
for completeness only, never written as a record.

### Headline

| Program | Trained on | checkout heldout (6) | checkout all (22) | retry-storm heldout (6) | retry-storm all (22) |
|---|---|---|---|---|---|
| Prior record: Codex gpt-5.6-sol, plain prompt (2026-08-14) | — | — | 0.763 | — | — |
| DSPy CoT, unoptimized | — | 0.881 (subset of all) | **0.851** clean | — | **0.818** clean |
| GEPA light (120 metric calls) | checkout train 12 / val 4 | **0.988** clean | 0.971 *contaminated* | 0.948 clean | **0.912** clean — meets 0.90 |
| GEPA light | retry-storm train 12 / val 4 | 0.940 clean | **0.948** clean — meets 0.90 | 0.969 clean | *contaminated*, not scored |
| GEPA light | both families, train 24 / val 8 | 0.964 clean | *contaminated*, not scored | 0.969 clean | *contaminated*, not scored |

Records written (all `status: measured`, `--skip-catalog`; catalog rows can be
added with `evallab calibrate ... --predictions` against the shared database):

- `checkout-pool-exhaustion-20260916-glm-5-3-flash-bbfcdacbd3.json` — unoptimized, 0.8506, below floor
- `retry-storm-backlog-20260916-glm-5-3-flash-e05b4fda9f.json` — unoptimized, 0.8182, below floor
- `retry-storm-backlog-20260916-dspy-gepa-checkout-glm-5-3-flash-e05b4fda9f.json` — GEPA (checkout), **0.9119, meets floor** (321/352 cells; four-cell margin)
- `checkout-pool-exhaustion-20260916-dspy-gepa-retry-glm-5-3-flash-bbfcdacbd3.json` — GEPA (retry-storm), **0.9481, meets floor** (292/308 cells; fifteen-cell margin)

Both families now hold a record at or above the 0.90 floor, each produced by a
program that never saw a document of the family it was scored on. The daily
digest line is now: "2 of 5 measured record(s) reach their agreement floor".

### Reverse transfer: per-criterion movement on unseen checkout (all 22)

| Criterion | unoptimized | GEPA (retry-storm) | Δ |
|---|---|---|---|
| `evidence_fidelity.invents_evidence` | 7/22 | 20/22 | +13 |
| `causal_reasoning.grounded_in_evidence` | 16/22 | 20/22 | +4 |
| `causal_reasoning.uncertainty_is_genuine` | 14/22 | 18/22 | +4 |
| `causal_reasoning.separates_contributing_factors` | 18/22 | 21/22 | +3 |
| `action_quality.actions_are_actionable` | 19/22 | 21/22 | +2 |
| `causal_reasoning.rules_out_the_decoy` | 20/22 | 22/22 | +2 |
| `action_quality.proposes_unsupported_work` | 20/22 | 21/22 | +1 |
| `causal_reasoning.identifies_the_mechanism` | 21/22 | 22/22 | +1 |
| six criteria unchanged (`misstates_a_fact` stays 19/22) | | | 0 |

The same picture in the other direction: `invents_evidence` carries most of the
gain (+13 of +30 cells), and the compiled instruction again names the training
family's fixtures (`worker-config.yaml`, `notify-worker.log`, `oncall-chat.txt`,
`ticket.md`, plus `/app/evidence`) as "not invented". On the unseen checkout
documents the rule generalised anyway because the rubric-level reasoning in the
instruction ("citing real evidence-pack artifacts is not invention even if not
enumerated in reference_facts") is what transfers, not the file list. The
weakest unseen documents are `11-subtly-wrong-cause-tls` (0.71) and
`19-fabricated-evidence-dashboards` (0.79); `uncertainty_is_genuine` (18/22) and
`misstates_a_fact` (19/22) are the criteria left below 0.9.

Run facts (`artifacts/gepa-retry/optimize-summary.json`): 11 candidates,
validation aggregate 0.797 (seed) → best 0.953 at index 9; task LM ≈149 calls /
≈$0.15 and reflection LM 12 calls / ≈$0.03 over the resumed segment (the first
segment, two iterations killed by the quota stop, wrote no summary and is
uncounted); 52 min wall at 2 threads. GEPA resumed from `optimizer-log/` after
the quota reset without repeating finished iterations.

### Joint-family GEPA: one instruction for both rubrics

Held-out only (6 + 6 documents; the remaining 32 were optimizer-visible, so no
full-family record is clean): checkout 0.964 (81/84 cells) and retry-storm
0.969 (93/96). Against the single-family programs on the same slices —
checkout heldout: checkout-trained 83/84, retry-trained 79/84; retry heldout:
retry-trained 93/96, checkout-trained 91/96 — the joint program is within two
cells of the best in-family program on each slice with a single instruction.

What changed in the text (`artifacts/gepa-both/inspect.json`): 7,627 chars,
39 lines, 0 demos; family-specific fact tokens fell to 12 (checkout) + 22
(retry-storm) from 39 and 44 in the single-family instructions, and the
"do not flag these files" list was replaced by a rubric-level rule — *"the
document is graded against an actual evidence directory (/app/evidence) …
reference_facts summarize these files but are NOT exhaustive"* — with one
`misstates_a_fact` example from each family. Checkout fixture names still
appear (`metrics.csv`, `deploys.csv`, `alerts.log`, `service-config.yaml`) as
illustrations, so seeing two rubrics generalised the rule but did not remove
the evidence-contract confound; only vendoring the evidence pack into
`rubric_json` does that.

Run facts (`artifacts/gepa-both/optimize-summary.json`): 9 candidates,
validation aggregate (8 documents) 0.827 (seed) → best 0.930 at index 7; task
LM 142 calls / ≈$0.13, reflection LM 9 calls / ≈$0.02; 53 min wall at
2 threads, single segment.

### Per-criterion movement on the unseen family (retry-storm, all 22)

| Criterion | unoptimized | GEPA (checkout) | Δ |
|---|---|---|---|
| `evidence_fidelity.invents_evidence` | 8/22 | 17/22 | +9 |
| `causal_reasoning.uncertainty_is_genuine` | 17/22 | 22/22 | +5 |
| `causal_reasoning.grounded_in_evidence` | 16/22 | 21/22 | +5 |
| `action_quality.actions_are_actionable` | 17/22 | 21/22 | +4 |
| `action_quality.actions_trace_to_findings` | 17/22 | 20/22 | +3 |
| `action_quality.proposes_unsupported_work` | 19/22 | 21/22 | +2 |
| `causal_reasoning.separates_contributing_factors` | 15/22 | 17/22 | +2 |
| `action_quality.bounds_the_amplification` | 21/22 | 22/22 | +1 |
| `causal_reasoning.rules_out_the_decoys` | 14/22 | 15/22 | +1 |
| `evidence_fidelity.asserts_unsupported_cause` | 19/22 | 20/22 | +1 |
| `evidence_fidelity.treats_db_cpu_as_cause` | 21/22 | 22/22 | +1 |
| `action_quality.closes_the_detection_gap` | 20/22 | 20/22 | 0 |
| `causal_reasoning.separates_trigger_from_cause` | 20/22 | 20/22 | 0 |
| `evidence_fidelity.blames_the_deploy` | 22/22 | 22/22 | 0 |
| `evidence_fidelity.misstates_a_fact` | 20/22 | 20/22 | 0 |
| `causal_reasoning.identifies_the_mechanism` | 22/22 | 21/22 | −1 |

Remaining weak spots on the unseen family: `rules_out_the_decoys` (15/22) and
`separates_contributing_factors` (17/22) — both are family-specific judgement
calls the checkout-trained instruction says nothing about.

### GEPA run facts (`artifacts/gepa-checkout/optimize-summary.json`)

- 12 candidates; validation aggregate by candidate: 0.821 (seed), 0.893, **0.982**, 0.982, 0.964, 0.946, 0.964, 0.946, 0.982, 0.964, 0.982, 0.964; best index 2.
- Task LM: 137 calls, 429k prompt / 129k completion tokens, ≈$0.14. Reflection LM (same model, temperature 1.0, 16k max tokens): 12 calls, 110k / 26k tokens, ≈$0.03. Wall time 29 min at 3 threads.
- No demos were added (GEPA edits instructions only); the compiled instruction grew from ~400 to 6,966 characters (117 lines).

### What the compiled instruction contains

General rules that transferred (paraphrased from `compiled_instructions`):
absent or "TBD" sections make every positive criterion in that dimension `no`;
generic filler contributing factors are `no` even when a section exists;
open questions about the postmortem *template* are not genuine uncertainty;
`invents_evidence` is `yes` only for a *named* fabricated artifact, and
fabricated support for a correct mechanism goes under `invents_evidence`, not
`asserts_unsupported_cause`; work premised on an unsupported cause is
`proposes_unsupported_work` even when actions elsewhere are sound.

Family-specific content that did **not** transfer and is a memorised shortcut:
a "Domain context (checkout-pool-exhaustion family)" block restating the seven
reference facts, and — verbatim — *"Per reviewer feedback, artifacts like
`deploys.csv`, `metrics.csv`, `service-config.yaml`, and `alerts.log` DO appear
in the evidence pack and are NOT invented — do not flag them."* This is the
missing-evidence input-contract confound (`research/inspections/judge-floor.md`)
learned as a fact about one family's fixtures. It explains the +15 on
`invents_evidence` on checkout (contaminated) versus +9 on retry-storm (clean).

### Cost and rate limits

Whole night through 03:12 EDT: ≈330 GLM calls, ≈$0.35 at LiteLLM list prices
(subscription transport, so no dollars moved). The Coding Plan's 5-hour usage
window was exhausted at 03:11 EDT ("Usage limit reached for 5 hour", reset
18:39 provider time ≈ 06:39 EDT), which killed the reverse-transfer run at
iteration 2; `after-reset.sh` resumes it from GEPA's checkpoint. Eight concurrent
requests trip the per-request limit; three are tolerated.

### Not run tonight (queued as next steps, each ≈150 calls)

- MIPROv2 light on checkout (instructions + 2 bootstrapped demos) for an
  optimizer comparison at equal budget.
- `--thinking` ablation of the unoptimized baseline (hidden reasoning on, 16k
  budget) as the one-variable check that turning thinking off did not cost accuracy.
