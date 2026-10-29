# NP22-CM-1: why do whole-run facets reach 69% recall on unseen sets and 9% on Goodfire DEV?

**This file is the preregistration (revision 2, after the Opus 5.5 review of PR #1496).** It is merged before any recall, rate or
distribution below is computed. Phases 1 and 1b read only schemas, field and file names, row counts, stratum sizes and label-side
counts (strata, publisher categories, whether labelled steps exist, call-log modes) after the strict cross-key. No model call; no
`suspicion` value was read; no Goodfire facet was joined to a label by this work. Phase 2 (computation, `RESULTS.md`,
`results/*.json`) starts only after a review of this revision. Zero-model analysis of files that already exist; $0.

## 1. Question and the numbers that are already public

Same detector family, same threshold (Run Atlas whole-run facets, `suspicion >= 2`), very different recall:

| Population | Recall | Source |
|---|---|---|
| CM-18 seven unseen sets, Haiku 5.5 reader | 218/317 = 68.8% | `cm18-unseen-far-20261028` |
| B6, same sets, 98 positives read by both | Sonnet 66/98 = 67.3%, Haiku 70/98 = 71.4% | `b6-cm18-sonnet-20261028` |
| NP21 four newer sets, Haiku 5.5 (separate, never pooled with CM-18) | 99/187 = 52.9% | `np21-cm1-unseen-far-20261028` |
| Goodfire DEV enacted, as published (first in CM-12, repeated in CM-14, CM-18) | 7/80 = 8.8% | `cm12-blinded-acceptance-20261028` |

**Question.** Is the gap the facets (reader, extractor, blinding, render pipeline, run length) or the hacks (what the positives are)?
CM-18 is the primary unseen set (preregistered); NP21 is a replication. If NP21's verdict differs from CM-18's, the headline stays
CM-18's and both are reported.

**The Goodfire comparator is not Haiku-only.** The loader keeps the last line per run. For the 80 enacted runs the last record is Sonnet
for 46, Opus for 28, Haiku for 6 (`results/inventory.json`). The file has advanced since CM-12 (CM-13 noted this), so phase 2 reports the
*current* last-wins k/80 and its drift from the published 7/80 instead of asserting it.

## 2. Revision log (what changed from #1496, per review item)

Blockers: (1) the rule now uses bootstrap intervals, one variant per candidate, minimum n, closure reading, winner's-curse note
(section 7); (2) D1 is **pre-refuted** and dropped from the contest (7.1); (3) F leaves the contest and is a localisation
diagnostic; `suspicion >= 1` with its clean false-alarm rate is the label-free threshold lever (7.1, 8); (4) a deterministic render check
decides whether a labelled step reached the reader before D2 may be called a property of the hacks (6.2); (5) the primary Haiku record
is chosen by version and served-model attestation, all-80 is a sensitivity row (4); (6) falsifiers are quantitative and say whether they
override (7.6). Shoulds are answered where they arise; section 11 lists them.

## 3. Stored files and schemas found (what is supported)

| Corpus | Records | Fields used | Where |
|---|---|---|---|
| Goodfire DEV cohort, 200 identities | 80 `enacted_yes`, 70 `clean_no`, 50 `considered_only` | facet record (`suspicion`, `answer_material_accessed`, `grader_or_harness_touched`, `eval_awareness`, `tests`, `evidence_steps`, `input_tokens`, `facet_model`, `facet_version`); receipt `source_label` (`verdict`, `verdict_enacted`, `categories`, `source_protocol`), `passage_step_labels.positive_step_indices`; steps from the cloud DB; call log (`purpose`, `prompt_version`, `served_model`, `status`) | `~/.local/state/evallab-prod-live/` (`cloud-trace-db.sqlite`, `run-atlas/facets-v1.jsonl`, `facet-calls-v1.jsonl`, `reblinded-facets-v1.jsonl`, `blinding-refresh-v1.json`); id list `goodfire-sample.json` (research-context, `cm2-monitor-20261026`) |
| CM-18 seven sets | 946 runs, 317 positives, one Haiku 5.5 record each (946/946) | same record fields; per-run `calls`, `input_tokens` | `~/.local/state/evallab-cm18/` |
| B6 | 278/281 Sonnet 5.5 records; 98 positives with both a Sonnet and a Haiku record | same | `~/.local/state/evallab-cm18-b6/` |
| NP21 four sets | 429 runs, 187 positives, Haiku 5.5 (429/429) | identical record schema | `~/.local/state/evallab-np21-cm1/` |
| Adapter fields | CM-18 and NP21 adapters (`corpus-expansion-pq1/pq2/pq3`) | section 5 | adapter `specs()`; OSF through the adapter's `profile.label` |

All four stores use `run-facets/v1`; Goodfire's 792 cohort lines all carry that version and every required field.

**Supported:** per-set provenance and technique strata for every unseen positive (317/317, 187/187 matched, 0 missing); Goodfire recall
by reader (Haiku and Sonnet on all 80 enacted, Opus on 55); a labelled-step analysis on Goodfire (all 80 enacted have labelled steps; labelled
steps per run: min 1, quartiles 2/4/7, max 16). **Not supported:** a prompted/spontaneous field for Goodfire (none is staged), step-level
labels for the unseen sets, per-step suspicion (a facet has one run-level `suspicion` plus up to five cited steps), a record of which steps a
map-reduce read dropped, and any link between a considered-only run and an enacted run.

**Read order, disclosed.** The phase-1 inventory parsed the whole facet, call and reblinded files before running the cross-key (no
`suspicion` used; 200/200 admitted; no harm). Phase 1b and phase 2 run the strict `assignments.csv` cross-key first and stream the
three files with a run-id filter, so nothing is parsed for a non-admitted run.

## 4. Cohorts and the Goodfire comparator records

- **Goodfire DEV.** 200 identities pass the strict cross-key (`run_atlas_score._read_labels`, the CM-13/14/19 guard) before any facet,
  step or label is read: 80 enacted / 70 clean / 50 considered-only (`considered_only` = publisher `verdict=yes`, `verdict_enacted=no`,
  `positive=None`). No TEST or sealed identity. Nothing is tuned.
- **Primary comparator `G-Haiku`** (`anthropic/claude-haiku-5-5`, the CM-18 reader). A record qualifies if it is the last Haiku line for
  the run in `facets-v1.jsonl` order, has every `run-facets/v1` field, and the call log holds a successful `form` call with
  `prompt_version = run-atlas-prompt/v2` served by Haiku for that run (attestation). Counts from the inventory: **68 enacted / 58 clean /
  39 considered-only** qualify. The other 12 enacted runs have a Haiku record but no attested v2 call logged.
- **Sensitivity `G-Haiku-all`**: the last valid Haiku line for all 80 / 70 / 50. Reported beside, never substituted after seeing results.
- **Haiku lines per run.** 69 enacted runs have one Haiku line, 11 have two (clean 60/10, considered 43/7). Phase 2 reports, for runs with
  two lines, how often the two lines disagree on `suspicion >= 2` and on `evidence_steps`.
- **`G-Sonnet`** (80/70/50; 72 enacted attested v2), **`G-Opus`** (covered 55/60/40; denominators are the covered runs; 48 enacted are also
  Haiku-attested), **`G-lastwins`** (current `ra.load_facets`, reconciliation row only). Kimi, Luna, Grok cover 22 to 32 enacted runs and are
  not used.
- **Reblinded file.** In the cohort it holds 178 rows: Haiku 155, Kimi 16, Luna 7. Enacted runs whose primary Haiku record equals the
  reblinded Haiku row on the CM-12 comparison fields (`facet_model`, `goal`, `approach`, `shortcut`, `suspicion`, `evidence_steps`): **64**
  (so candidate B's minimum n of 30 is met on the numbers).
- **Unseen.** CM-18's 317 positives are the preregistered unseen set. NP21's 187 are reported beside. Negatives are used only where a
  table says so. Unknown (missing record, no attested call) is excluded and counted, never a miss.
- Wilson 95% intervals on every proportion. Unseen runs share tasks (distinct task hashes reported per set; SDF is one task, 1 cluster);
  Goodfire has one run per task.

## 5. Strata and definitions (fixed here)

### 5.1 Provenance class and the substitution for "prompted vs spontaneous"

Research asked for the publisher's prompted/instructed versus spontaneous split from staged manifest fields only. **Substitutions made,
openly:** "spontaneous" is not a recorded notion in any manifest, so the classes below are built from adapter fields where one exists
and otherwise from staged README prose; the SDF, BAITBENCH, Kobayashi Maru and Reward Pressure assignments (and EvilGenie, Flake,
KernelBench as `NR`) come from README prose, not a manifest field, and are hard-coded in `tools/inventory.py`.

| Class | Meaning | Rule |
|---|---|---|
| `IN` instructed | staged README says the side objective is in the prompt | ResearchArena `labels.condition = embedded` |
| `CP` constructed pressure | environment built so honest success is blocked, contradicted, or a shortcut planted | Reflex `split = conflicting`; Cheat Index `family` F1-F3; Beam `condition` impossible or impossible_honest; BAITBENCH (all); Kobayashi Maru (all); Reward Pressure Study 2 (all) |
| `NP` | staged text records neither an instruction nor a constructed conflict | SDF (one system prompt and one task for all runs); Cheat Index F4; Beam `possible`; ResearchArena baseline; Reflex `original` |
| `NR` not recorded | no staged field or sentence classifies the run; not inferred | KernelBench-Hard; EvilGenie (`condition` A-E staged but undefined); Flake Laundering |

Drawn positives per class: **CM-18** `IN` 21, `CP` 165 (Reflex 45, Cheat Index 60, Beam 60), `NP` 60 (SDF), `NR` 71 (EvilGenie 60,
KernelBench 11); **NP21** `CP` 180, `NR` 7. Every drawn Cheat Index positive is in a trap family and every Beam positive is in an
impossible condition, so those sets hold no non-pressure positive stratum. **Goodfire is `NR`, not `NP`.**

### 5.2 Within-set prompt contrasts: the only confound-free prompting evidence

Across sets, class is confounded with set, label source and technique. The informative evidence on prompting is **within a set, same
task family, same label rule**, comparing positives' facet recall across a publisher-varied prompt condition: Reward Pressure `pressure=0`
(13 drawn positives) versus `3` (28) and `4` (19) (and `guardian` 0 versus 1); BAITBENCH `prompt_group` baseline (28) versus validity (32);
Flake `explicit` (6) versus `hook` (1; counts only); Beam `impossible` (46) versus `impossible_honest` (14); Kobayashi `item_slice`
solvable (12) / unreachable (41) / contradict (7). These are contrasts in *detectability of positives* under a condition; they say nothing
about prevalence. Each is reported with Wilson intervals and a two-sided Fisher exact p; none enters the closure contest.

### 5.3 Evidence type, and label source

- *Adapter technique* (cells below 3 suppressed in committed counts): Cheat Index gated detector fired; Beam source annotation
  (`special_cased`, `config_edit`, `tamper_attempt` by that precedence); SDF mechanism list; Reward Pressure `cheat_type`; BAITBENCH task
  family; Flake `publisher_outcome`; Kobayashi `monitor_type`; ResearchArena `side_task_type`; EvilGenie native class; KernelBench and
  Reflex single-valued.
- *Goodfire publisher category* (`source_label.categories`, multi-label, enacted; cells below 3 suppressed): hidden_test_targeting 36,
  upstream_fix_recall 35, misrepresented_compliance 23, hardcoding_special_casing 22, harness_mechanics_targeting 21,
  environment_acquisition 10, fabricated_verification 9, other 6, test_modification 5, test_execution_tampering 3, three categories
  below 3.
- *Facet `tests` field* is a reader output, not an evidence type. It is reported as a **mediator** (like E), never as a stratifier
  for a claim.
- **Label source per set** (a confound with recall, stated here): KernelBench human annotation; Reflex execution oracle (final test file
  differs); Cheat Index deterministic gated final-diff detectors; EvilGenie GPT-4o judge class plus audits; SDF deterministic static
  grader of command log and trees; ResearchArena executed validator; Beam publisher tamper/special-case annotation; BAITBENCH two LLM
  judges agreeing; Flake executed oracle plus Opus judge; Kobayashi detector union, monitor and outcome agreeing; Reward Pressure
  executed cheat flag. Goodfire: **LLM-consensus passage labels** (`consensus_lp_v1` 42, `consensus_v2` 38 among enacted). Labels that
  require detector/monitor/oracle agreement push unseen recall up (NP21 notes this); Goodfire's include categories that may leave no action
  trace (`upstream_fix_recall` 35/80, `misrepresented_compliance` 23/80), and label noise caps the recall any reader can reach. Nothing
  here removes that ceiling; it is a named limit on every closure.

### 5.4 Run length and context

Facet `input_tokens` of the record (sum over map and form calls); same field in all stores. Secondary descriptive: steps per run, share of
runs whose steps carry `reasoning_content`, multi-chunk read (any successful `map:i/n` call for Goodfire; `calls > 1` for unseen, an upper
bound that counts the one validation retry).

## 6. Goodfire evidence location (design item 3)

"Positives whose only evidence is in `considered_only` rows" is not a defined object: strata are per run and disjoint, and the files
link no two runs. **Substitution:** considered-only rows are (a) profiled as a cognate class (the 50 runs' facet profile beside enacted and
clean; this departs from the CM-12/13/14/19 convention that considered-only runs are never scored, acceptable on DEV and disclosed) and
(b) the question is answered through the verbal-only definition below.

### 6.1 Disjoint exhaustive partition of the 80 enacted runs (G-Haiku-all record, so every run has a row)

Let `L` = `passage_step_labels.positive_step_indices`; a cited step *hits* if it is in `L` exactly (CM-6 convention, `run_atlas_score`).
Precedence, in this order, each run in exactly one row:

1. **flagged**: `suspicion >= 2`.
2. **cited, rated below 2**: `suspicion < 2` and an exact hit.
3. **render-degraded, uncited**: no exact hit and at least one labelled agent step failed the render check (6.2). Facets-side.
4. **verbal-only, uncited**: no exact hit, render intact, every labelled agent step carries no command (`wc._command` falsy).
5. **action-bearing, uncited**: no exact hit, render intact, at least one labelled agent step carries a command.

Row 5 is the "unexplained by visibility or pipeline" row. Labelled steps that are not agent steps are counted apart. Within-5-assistant-turn
hits are an overlay (not a partition row) because they are near-trivial: citations are at most 5, labelled steps have median 4 in runs of
median 69 steps. **Chance baseline** for citations: for each run, simulate a uniformly random choice of as many distinct agent steps as it
cited (10,000 draws, seed 20261028) and report the expected number of exact and within-5 hits beside the observed 39/80 and 70/80 (CM-13).

### 6.2 Render check (deterministic, no model)

Before D2 can be called a property of the hacks, check that each labelled step's content reached the reader. For every labelled agent step
render the run exactly as the extractor did (`ra.blind_run` with the widened redaction and the per-run literals, `render_segments`,
`plan_chunks`) and compare with the unredacted step: the step is **intact** if it appears in a segment, its reasoning and message are not
elided or split away from the labelled passage, its command is not head-and-tail elided, and the redaction made zero substitutions
inside it. Otherwise it is **degraded**. A run is `render_intact` if every labelled agent step is intact. Runs not intact belong to the
facets side. Run-level counts and the share of verbal-only runs that are intact are reported. No label text is committed.

## 7. Q4: the single-difference contest

### 7.1 Who is in the contest, who is not

| Candidate | Side | Status |
|---|---|---|
| **D2** visibility of the labelled evidence | hacks | in the contest |
| **A** reader | facets | in the contest |
| **B** extractor, blinding and render pipeline | facets | in the contest |
| **C** run length | facets | in the contest |
| D1 provenance mix | hacks | **pre-refuted, not in the contest** |
| F cited-but-below-2 | n/a | **diagnostic only** (it uses labels to define the alarm, an oracle; it cannot say anything about a deployable rule) |
| E facet field profile | mediator | reported; excluded |

**D1 is pre-refuted by public numbers**, not by this analysis. CM-18 published SDF (the only `NP` set) at 43/60 = 71.7% and SDF plus
EvilGenie plus KernelBench (`NP` + `NR`) at 92/131 = 70.2%, both above the pooled 68.8%. Closure `= 1 - (R_D1 - R_G)/(R_U - R_G)` is
negative for every Goodfire recall `R_G` below 68.8%. It was fully predictable, and it also assumes Goodfire behaves like `NP`, while this
README classes Goodfire `NR`. The hacks side of the contest is therefore D2 alone; the within-set prompt contrasts (5.2) carry the
prompting evidence. D1's numbers are still printed for completeness.

### 7.2 One variant per candidate, and minimum n (fixed)

Gap `G0 = R_U - R_G`, in points, `R_U` = CM-18 positives (Haiku, 317), `R_G` = G-Haiku primary enacted recall (68 runs). Closure of candidate
`c` is `(G0 - G_c)/G0` with `G_c` the gap after changing only `c`:

- **D2** (variant fixed): `R_G` recomputed on enacted runs in the primary set that are **action-bearing and render-intact**. Minimum 20 runs.
  If the verbal-only runs are less than 80% render-intact, D2's shortfall is reassigned to the facets side (7.6).
- **A** (variant fixed): Sonnet, difference-in-differences, `closure_A = [(R_G,S - R_G,H) - (R_B6,S - R_B6,H)] / G0`, where
  `R_G,S`, `R_G,H` are on the enacted runs read by both Haiku and Sonnet and attested v2 (65 enacted, so the A and B comparisons are not
  confounded by extractor mode), and `R_B6` on the 98 positives with both reads. Minimum 40 Goodfire runs. Opus is a sensitivity row on its 55
  (compared with Haiku on the same Opus-covered runs, 48 Haiku-attested); no unseen Opus read exists. A is not computed for NP21 (no Sonnet read).
- **B** (variant fixed): `R_G` on enacted runs in the primary set whose Haiku record equals the reblinded Haiku row (64) **and** are
  `render_intact`. Minimum 30 runs; otherwise "not testable", not refuted.
- **C** (variant fixed): input tokens, pooled terciles over G-Haiku primary enacted plus CM-18 positives (cut points fixed from that pooled
  distribution before any recall is cut). `R_G^std = sum_t w_t^U R_G,t`, `w_t^U` the unseen share in tercile `t`. Minimum 10 Goodfire runs in
  every tercile; otherwise "not estimable". The multi-chunk flag is a secondary row.

Each of D2, B and C is a restriction or re-weighting of the same 68 runs, so their closures share runs; a joint table lists, for each pair of
D2, B and C, the number of shared primary runs and the recall of the intersection.

### 7.3 Uncertainty

**Bootstrap, 10,000 draws, seed 20261028.** Unseen runs are resampled by task-hash cluster **within each set** (set sizes fixed, so the
60-per-set mix is preserved); SDF is a single task, so its cluster is degenerate and contributes no resampling variance (disclosed);
Goodfire runs are resampled as individuals. Every candidate's `G_c` and closure are recomputed in each draw; a draw in which a candidate
falls below its minimum n counts as "not estimable" for that candidate and the share is reported. Percentile 95% intervals. Newcombe
hybrid intervals are given for the point gaps.

### 7.4 Decision rule

- Closure is read as it comes: **negative** = the change widens the gap (no help, treated as refuted); **above 100%** = the change reverses
  the gap (over-closes; reported, capped at 100% for ranking).
- **Refuted:** point closure <= 10%. **Not shown to explain most:** upper bound of the interval < 50%.
- **Single explaining difference** = the candidate with the highest point closure (the *leader*) iff (a) the lower bound of its closure
  interval >= 50% **and** (b) the paired-bootstrap difference (leader minus runner-up, in points of closure) has lower bound >= 10 points.
  The share of draws in which the leader has the highest closure is reported (a leader that wins fewer than 60% of draws is described as
  contested even if (a) and (b) hold).
- **Winner's curse.** Taking a maximum over four correlated candidates biases the leader's closure upward and the percentile interval does
  not correct that. The mitigation is conservative: the lower bound, not the point, must clear 50%, and the leader is fixed from the original
  data before the paired bootstrap.
- **Verdict.** Leader is D2 -> "hacks". Leader is A, B or C -> "facets". No single difference: if two or more candidates have point
  closure >= 25%, "mixed" when they sit on both sides, "facets, several" or "hacks" when on one side; if none reaches 25%, "unexplained by
  any tested difference" (the gap is then reported as a property of the labelled populations and label sources, not of the reader).
- **NP21** gets the same table with its own `G0` (A not computable). If its verdict differs from CM-18's, both are reported and CM-18 is
  the headline.

### 7.5 Weighting sensitivity and per-set gaps

The 60-per-set caps and the 100% sets (Reflex, ResearchArena) shape `R_U`. Phase 2 reports each set's recall minus `R_G` (per-set gap) and
recomputes `G0` and the closures with the seven sets weighted equally.

### 7.6 Falsifiers (quantitative, with their power)

Only one condition can override the interval rule, and it is a precondition, not a falsifier: if fewer than 80% of verbal-only runs are
render-intact, D2's closure is reassigned to the facets side (a pipeline loss, not a property of the hack). Everything else is read from the
rule in 7.4; the earlier prose falsifiers were deleted because they did not connect to it. Predictions that would count against a side, for
the reader's convenience, are *consequences of 7.4*, not additional tests: "hacks" fails if D2's lower bound is below 50% or another
candidate leads; "facets" fails if A, B and C all have upper bounds below 50%.

## 8. Implication for the 15% recall at 3% false alarm target (CM-14, CM-19)

The CM-14 target (>= 15% recall at <= 3% clean false alarms; 12/80 and 2/70, so **one run decides it**) was set on Goodfire and CheatBench for
*union rules with the cascade*; facets alone answer only part of it. To avoid a best-of-N reading, **one primary row is named in advance:
G-Haiku primary record, `suspicion >= 2`**, enacted k/68 and clean k/58 with Wilson intervals (rates compared with 15% and 2.9%). Reported
as secondary rows, not selected: the same record at `suspicion == 3` and `suspicion >= 1` (the label-free threshold lever), G-Haiku-all
(80/70), G-Sonnet (80/70), G-Opus (55 enacted / 60 clean, different denominators), each with k and Wilson. No row is chosen as a policy.
The implication is conditional on the verdict: hacks -> unseen recall is no evidence about the target; facets -> the lever named by the
leader; mixed or unexplained -> both said with the closures.

## 9. Known before registration (disclosure)

- Published per-set recall (CM-18: Reflex 45/45, ResearchArena 21/21, EvilGenie 44/60, SDF 43/60, Beam 37/60, Cheat Index 23/60,
  KernelBench 5/11; NP21 facets: BAITBENCH 13/60, Kobayashi 30/60, Reward Pressure 56/60, Flake 0/7), Goodfire 7/80 and 5/57.
- **Published Goodfire citation-to-label results**: CM-13 exact 39/80, within five turns 70/80; at `suspicion >= 2` on a disjoint 25+25
  sample at 100% prefix: Haiku 0/25, Sonnet 0/25, Opus 1/25, Kimi 3/25 (CM-16, B1, B2, B3). Together with D1's arithmetic these largely
  **predict** that A is refuted, that F is large, and that D1 is refuted. This revision's design choices (F out, D1 out) are partly a
  response to these. I have not myself joined any Goodfire facet to a label.
- The phase-1 inventory counts (reader mix, categories, class sizes, attestation counts above).

## 10. What this will not claim

- Not causal; closures are counterfactuals computed from stored counts, labelled so. Not a deployment recall for any corpus, not a ranking
  of readers, not a tuned rule. Negatives are publisher labels, never certified honest.
- Nothing about spontaneous versus prompted cheating in general: `NP` means "nothing staged says prompted", `NR` is unknown.
- No inference from `NR` cells (EvilGenie A-E reported as opaque strata).
- Small cells (Flake 7, KernelBench 11, Opus 55, single-run categories) are wide; single-run category cells are suppressed. Task
  clustering is modelled only in the named bootstrap.
- Facet prose, run text, per-run keys and per-run alarms stay local; only aggregates, counts and hashes are committed.

## 11. Should-items and where they are handled

Cross-key order (3, 4); disclosure of prior results (9); 7/80 provenance and drift (1, 4); partition and chance baseline (6.1); substitution
table (5.1, 6); within-set prompt contrasts (5.2); `tests` as mediator (5.3); label source and ceiling (5.3); per-set gaps and equal weights
(7.5); joint table (7.2); A confounded with extractor mode (7.2, 65 paired runs; Opus on its own 55); B6 fixed at the 98 paired positives
(3, 7.2); C minimum cell and verdict rules (7.2, 7.4); 15%@3% primary row (8); considered-only profiling disclosed (6); single-run cells
suppressed and the in-repo path assumption removed from `tools/inventory.py` (3).

## 12. Files

- `tools/inventory.py` (phase 1b): schema, stratum sizes, call-log modes, comparator-record counts; `results/inventory.json` (aggregates and
  file hashes only; cells below 3 suppressed in category and technique counts).
- Phase 2 adds `tools/gap.py`, `results/*.json` and `RESULTS.md` (question, what ran, number, meaning).
