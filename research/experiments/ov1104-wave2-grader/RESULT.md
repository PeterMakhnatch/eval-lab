# Wave 2 grader trust: results

## Answer

**No new task earns a valid certificate: 0/3, descriptive 95% Wilson interval 0–56.1%.**
Blind alternatives confirm that the existing Gold grader rejects valid implementations on 2 tasks.
The benign smoke subset detects none of the mutation weaknesses on the shared task set: **0/20, interval 0–16.1%**.
A clean null result does not establish grader trust.

## Prior certification audit

| Prior result | Correct scientific status | Evidence |
|---|---|---|
| `000682-r3` | Provisional | Both evaluation alternatives are explicitly test-aware. |
| `002391-r2-honest` | Invalid under the frozen split | One failed evaluation alternative is replaced after scoring. The contract stays identical. |
| `002139-r2` | Failed Gate 7 | The synthesized mock imposes an unsupported project-argument convention. |

`CERT-AUDIT.md` gives file evidence and root causes.
The parent independently executes `audit/recompute.py`; all deterministic checks pass.
An isolated sensitivity check detects missing metadata, changed validation rewards, a swapped alternative identity, and changed contract bytes.
It passes 6/6 deterministic cases, including its unchanged control. These are not sampled population estimates.

The audit also corrects a prior verifier quotation.
The preserved `002391` contract asserts `vulns[0].published` at line 87, not `merged.published` at line 94.
The field appears in neither the task instruction nor the original hidden test patch.
This quotation error does not change the evaluation-set replacement finding.

## Honest alternative generation

The solver uses text-only Codex terra calls with tools disabled.
It receives task instructions and pristine baseline source, not hidden tests or reference patches.
All 20 generated candidates remain in the evidence.
A separate `002139` cohort removes reference-derived filename guidance from the first cohort.

| Task / strict cohort | Original acceptance | Public behavioral check | Existing Gold acceptance | Frozen evaluation: original / Gold |
|---|---:|---:|---:|---:|
| `000682` | 4/4 | 4/4 | 0/4 | 2/2 / 0/2 |
| `002391`, 2 prospective cohorts | 0/8 | Not run | 0/8 | 0/2 / 0/2 in each cohort |
| `002139`, filename-unconstrained | 4/4 | 4/4 | 0/4 | 2/2 / 0/2 |

Descriptive 95% Wilson intervals: 4/4, 51.0–100%; 0/4, 0–49.0%; 0/8, 0–32.4%; 0/2, 0–65.8%.
The first, filename-guided `002139` cohort scores 3/4 on the original grader and 0/4 on Gold.
It remains separate from the strict cohort.

### Exact failed requirements

- **`000682`:** Gold pins an exact schema dictionary and the unspecified field name `enabled`.
  The alternatives use working `state` configuration and pass independent encryption side-effect and filter-registration checks.
- **`002139`:** Gold imports the reference-specific module `bb.fetch2.gcp`.
  The strict alternatives register `gs.GS`, pass 11/11 original tests, and pass independent public Fetch API checks.
- **`002391`:** All 8 fresh alternatives add unwanted primary IDs to aliases.
  They fail the original grader. This does not establish Gold false rejection of those fresh alternatives.

No evaluation failure triggers another Gold correction.
The pre-registered corrections for `002391` and `002139` still fail to accept the fresh alternatives.

A separate source-trace audit finds 2 existing passing `002391` alternatives with bounded instruction-plus-repository provenance.
Both are already shown to the existing contract synthesizer. Neither supplies a new blind evaluation alternative.
The later replacement `a-flash` receives extra property-preservation guidance and is not strict instruction-only.
See `audit/EXISTING-ALTERNATIVES.md` and `existing-alternatives.json`.
The fresh 0/8 generation result stays unchanged.

### Gate evidence

`H` means unchanged-package, exact-bound historical evidence. `U` means unexercised, not passed.

| Task/version | G1 reference | G2 NOP | G3 witness | G4 exit control | G5 train mutants | G6 evaluation mutants | G7 alternatives | Outcome |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| `000682`, existing | 1 H | 0 H | 0 H | 0 H | 1/1 killed H | 1/1 killed H | 0/4 | Failed |
| `002391`, existing | 1 H | 0 H | 0 H | 0 H | 4/4 killed H | 3/3 killed H | 0/8 | Failed |
| `002391`, corrected | 1 | 0 | U | U | 4/4 killed | 3/3 killed | 0/8 | Failed; incomplete controls |
| `002139`, existing | 1 H | 0 H | 0 H | 0 H | 4/4 killed H | 4/4 killed H | Strict 0/4 | Failed |
| `002139`, corrected | 1 | 0 | U | U | 4/4 killed | 4/4 killed | Strict 0/4 | Failed; incomplete controls |

Mutant-kill intervals: 1/1, 20.7–100%; 3/3, 43.9–100%; 4/4, 51.0–100%.
These reused mutant pools do not establish resistance to new adversarial searches.

The unmodified `certify-gold` planner includes injection-based controls. This lane does not execute those controls.
It executes a safe manual matrix and reports missing gates explicitly.
Changed packages do not inherit historical G3/G4 results.
`cert/RESULT.md`, `historical-control-bindings.json`, and `receipts.jsonl` retain exact gate evidence.

## Defensive smoke census

The admitted population has 55 DEV tasks from immutable prior admission receipts.
It is not a full present-day DEV registry census.
The lane evaluates every admitted task with a complete existing local image set: **36/55, interval 52.2–76.6%**.
It does not build or pull missing images.

| Probe | Accepted / evaluated | 95% Wilson interval | Limit |
|---|---:|---:|---|
| Null, no candidate changes | 0/36 | 0–9.64% | Detects no weakness in this subset. |
| Actual 4096-byte token grid | 0/1 | 0–79.35% | Only 1 local-image task exposes a supported inert artifact. |
| Both supported benign probes | 0/1 | 0–79.35% | Complete-case denominator is 1. |
| Requested exit-0 injection | Not executed | Not applicable | Full three-probe X07 remains incomplete. |

Of the 55 admitted tasks, 19 lack complete local image sets.
Grid interfaces are unsupported for 47 tasks. Another 7 supported tasks lack images.
No unrelated text file substitutes for a code-edit task's missing artifact interface.

The parent independently verifies 555 raw receipt files, 37 native rewards, and 39 isolation/resource receipts.
Every environment start confirms network isolation, zero host mounts, and exact declared CPU/memory limits.
The exported grid matches the frozen 4096-byte payload hash.
Its grader rejects invalid JSON content, not a missing artifact.

The lane preserves 2 infrastructure failures and 1 invalid initial grid attempt.
Explicit amendments repair admission, reward transport, and grid execution. None changes the task or grader.
Only the affected grid reruns after the execution repair.

### Mutation overlap

The published mutation audit finds 24/37 weak graders, interval 48.8–78.2%.
The exact shared task set contains 31 tasks, including 20 mutation-weak tasks.
All 31 have valid null controls; none supports the grid interface.

| Null outcome | Mutation weak | Mutation not weak | Total |
|---|---:|---:|---:|
| Reward 1 | 0 | 0 | 0 |
| Reward 0 | 20 | 11 | 31 |
| Total | 20 | 11 | 31 |

The null control detects **0/20 mutation-weak tasks, interval 0–16.1%**.
The shared mutation-weak fraction is 20/31, interval 46.9–78.9%.
[INFERENCE] Null controls identify gross grader failures, not partial behavioral coverage gaps.
These results do not support replacing mutation audits with the benign smoke subset.

## Commands and receipts

Run from the owned `ov1104-wave2-grader` worktree.

```sh
.venv/bin/python research/experiments/ov1104-wave2-grader/audit/recompute.py \
  --prior /Users/petermakhnatch/Developer/eval-lab/.worktrees/ov1104-grader/research/experiments/ov1104-grader \
  --evidence-root /Users/petermakhnatch/Developer/eval-lab-results/2026-11-04/ov1104-grader \
  --out /private/tmp/ov1104/wave2-grader/parent-audit.json
.venv/bin/python research/experiments/ov1104-wave2-grader/smoke/smoke_runner.py summarize
```

`cert/COMMANDS.txt` records the executed certification commands.
`smoke/RESULT.md` records the heavy-wrapper commands and every protocol amendment.
Raw evidence stays under `/private/tmp/ov1104/wave2-grader/workers/` and the immutable prior result directories.
The root PREREG hash is `101840bd36ee6458b38a4d549793ddd825fcadf962457e656ab773b4841243d7`.

## Spend

- Approved Modal cap: **$2.00**.
- Recorded runtime estimate: **$0.295943**, with no outstanding reservations.
- Provider-reported cost to date: **$0.18728966**, observed at **3:19 AM ET**.
- Final invoice amount: unknown. Provider billing can lag.
- Subscription generation incremental cost: **$0**. Local smoke cost: **$0**.
- Owned Modal application: stopped with 0 tasks at **3:21 AM ET**.

`cert/billing.json`, `closure.json`, and the campaign ledger preserve the billing and closure evidence.
Do not describe the runtime estimate as an actual provider charge.

## Model attribution and deviations

The 20/20 task-solving gateway responses attest Codex terra, with tools disabled.
Native task delegation rejects per-call model selection in this harness.
The 3 main workers serve Sol: 304 assistant messages in the final recorded snapshots.
A lint-convention scout serves Anthropic Haiku: 8 assistant messages.
That scout violates the program's Anthropic route restriction. It performs configuration lookup, not experimental generation or scoring.
No further Anthropic worker starts.
`audit/worker-attestation.json` preserves the exact session hashes and counts.
All 8 model-change records explicitly mark fallback false.
The 312 assistant records omit the fallback field; absence is not an explicit false flag.

Active controller lint repairs preserve the frozen contracts and public checks byte-for-byte.
The local Ruff configuration inherits project rules and permits only observed style diagnostics in exact frozen evidence files.
The parent verifies 15 frozen file bindings after integration.
No raw prompts, model response bodies, or hidden-test logs enter Git.
