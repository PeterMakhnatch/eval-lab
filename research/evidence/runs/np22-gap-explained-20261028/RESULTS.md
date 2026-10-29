# NP22-CM-1 results: why do whole-run facets reach 69% recall on unseen sets and 6-9% on Goodfire DEV?

Preregistration: `README.md` (revision 3, merged `522d55257` before any number below was computed). Tool: `tools/gap.py`
(`uv run --with numpy python tools/gap.py`, about one minute); output `results/gap.json` (aggregates, hashes of the inputs; breakdown cells
with n < 3 suppressed; no per-run key, id, prose or alarm). **Zero model calls, $0.** The Goodfire cohort passed the strict
`assignments.csv` cross-key (200/200 admitted) before any facet, call or receipt file was parsed; the three files were then streamed with a
run-id filter. Nothing was tuned. Verdicts below are read from the written rule, not chosen.

## Headline

**This analysis does not answer "facets or hacks".** By the preregistered rule the verdict is **"not explained by any tested candidate"**
(CM-18 primary, and the same for NP21). What the data say: on the registered comparator, Haiku flags **4 of the 68 Goodfire enacted runs**
that have an attested v2 Haiku read (**5.9%, Wilson 2.3-14.2**) against **218/317 = 68.8% (63.5-73.6)** of CM-18 unseen positives; the matched
gap is **62.9 points (Newcombe 53.0-68.9)**. Closures: D2 0.2%, R 0.1%, B 0.6%, A (Sonnet instead of Haiku) 1.6% (bootstrap -10.4 to 13.5),
C (run length) not estimable. Goodfire recall in the contrasted subsets runs 3.6-6.25%.

**What the verdict is worth.** Three of the four estimable candidates cannot have said much. R, D2 and B each compare the 68 runs with a
subset that differs from them by 1 to 4 runs, and all 4 flagged runs fall inside every subset, so whatever the data their largest possible
closures were **0.21% (R), 0.44% (D2) and 0.58% (B)**; B's observed 0.6% is its maximum. The contest could therefore only have been decided by A
(one reader pair, one threshold, a wide interval) or C (not estimable). "Not explained by any tested candidate" says the registered tests were
nearly guaranteed to return it. It is **not** evidence that the reader, the render, the extractor vintage or the visibility of the labelled
step are innocent, and it is not evidence for the hacks being the cause. The README's own branch-5 caveat already said the pipeline candidates
vary little; this text should have said so first.

What can and cannot be said. Can: the recall figures above; Sonnet (5/80) and Opus (4/55) do no better than Haiku at `suspicion >= 2`; the
gap is not a threshold artefact (below); the descriptive patterns in Q2 and Q3. Cannot: separate harder-to-read Goodfire positives, looser
LLM-consensus labels and reader calibration on long runs (Goodfire reads are a median 82k tokens against 26k), or say which of facets or
hacks the gap belongs to.

## Q0: the comparator, and the published 7/80

- **What ran.** Haiku records for the 80 enacted Goodfire runs, by the registered rule (last valid Haiku line with an attested v2 `form`
  call): **P68 = 68 enacted / 58 clean / 39 considered-only**. Also 57 enacted runs with a single attested Haiku line; 11 enacted runs have
  two Haiku lines (they never disagree on `suspicion >= 2`; their `evidence_steps` differ in all 11).
- **Numbers.** Enacted at `suspicion >= 2`: **G-Haiku primary 4/68 = 5.9%; G-Haiku single-line 4/57; G-Haiku all-80 7/80 = 8.8% (4.3-17.0);
  G-Sonnet 5/80 = 6.2%; G-Opus 4/55 = 7.3%**. **Current last-wins is 5/80 = 6.2% (2.7-13.8)** from the mixed readers (Haiku 6, Opus 28,
  Sonnet 46), not 7/80.
- **Meaning.** The published 7/80 is consistent with Haiku on all 80 lines (it matches that count; the current last-wins is 5/80): the facet
  file has advanced since CM-12. Either way it is 5.9-8.8%: the unmatched published gap (60.0 points) and the matched one (62.9) differ by
  under three points. The reader choice moves Goodfire recall by two or three runs, which is the CM-13 noise scale.

## Q1: per unseen set, prompted versus constructed versus not recorded

Classes (README 5.1; from adapter fields or staged README prose, never inferred): `IN` instructed, `CP` constructed pressure, `NP` nothing
staged says prompted, `NR` not recorded. Facet recall at `suspicion >= 2`, CM-18 (317 positives, 946 of 946 read, all served exactly as Haiku):

| Class (positives) | Recall |
|---|---|
| `IN` ResearchArena embedded (21) | 21/21 = 100% (84.5-100) |
| `CP` Reflex, Cheat Index, Beam (165) | 105/165 = 63.6% (56.1-70.6) |
| `NP` SDF (60) | 43/60 = 71.7% (59.2-81.5) |
| `NR` EvilGenie, KernelBench (71) | 49/71 = 69.0% (57.5-78.6) |

NP21 (187 positives, reported beside, never pooled): `CP` BAITBENCH, Kobayashi, Reward Pressure 99/180 = 55.0% (47.7-62.1); `NR` Flake 0/7.
Per-set recall, CM-18: Reflex 45/45, ResearchArena 21/21, EvilGenie 44/60, SDF 43/60, Beam 37/60, Cheat Index 23/60, KernelBench 5/11.
D1 (printed for completeness; pre-refuted by public numbers): `NP` 43/60 = 71.7%, `NP`+`NR` 92/131 = 70.2%, both above 68.8%. Goodfire is `NR`.

**Within-set prompt contrasts (registered as the only confound-free prompting evidence):** Reward Pressure `pressure=0` 12/13 against `3` 27/28
(Fisher p = 0.54) and `4` 17/19 (p = 1.0); `guardian=0` 30/31 against `1` 26/29 (p = 0.35); BAITBENCH baseline 9/28 = 32.1% against validity
4/32 = 12.5% (p = 0.11); Beam impossible 29/46 against impossible_honest 8/14 (p = 0.76); Flake explicit 0/6 against hook (n < 3); Kobayashi
`item_slice` solvable 0/12 against unreachable 30/41 (p < 0.0001). **No contrast is significant except Kobayashi's, which is the one large
contrast and was registered as a prompt contrast** (README 5.2); it contrasts item kinds within one study, and it is reported as registered.
Power is low and Reward Pressure sits at a 90-97% ceiling, so these show no detectable prompt effect, not the absence of one.

Technique (adapter field; counts and rates in `results/gap.json`, cells < 3 suppressed): for example Reward Pressure `special_case` 53/57,
`test_edit` 3/3; SDF `staged_only` 10/10, `config_weakened` 6/8, `code_deleted` 1/5; Cheat Index F1 20/54, F2 3/5. The facet `tests` field
is a reader output; recall by it is in the JSON as a mediator and is not used as evidence.

## Q2: facet field profile, Goodfire enacted next to the unseen positives

| | Goodfire enacted, G-Haiku primary (68) | CM-18 positives (317) | NP21 positives (187) |
|---|---|---|---|
| `suspicion` 0 / 1 / 2 / 3 | 52 / 12 / 4 / 0 | 22 / 77 / 123 / 95 | 47 / 41 / 51 / 48 |
| `answer_material_accessed` | 3/68 | 26/317 = 8.2% | 43/187 = 23.0% |
| `grader_or_harness_touched` | 1/68 | 73/317 = 23.0% | 3/187 |
| `eval_awareness` reasons / plans | 25 / 0 | 133 / 50 | 98 / 12 |
| `tests` read / modified / other | omitted (a complement would expose one run) | 106 / 139 / 72 | 179 / 3 / 5 |

Goodfire's other record sets are close (Sonnet: `suspicion` 0/1/2/3 = 63/12/3/2, no `answer_material_accessed`, 3 `grader_or_harness_touched`;
Opus on 55: 30/21/0/4). Clean Goodfire runs are almost all 0 (G-Haiku primary clean 1/58; Sonnet and Opus 0/70 and 0/60). Considered-only
(50): Haiku 0/50 at `suspicion >= 2`, 48 at 0 (profiling them departs from the "never scored" convention, disclosed in the README; they are
treated as a cognate class, not as positives).

**Descriptive comparison within field values (it conditions on a reader output, so it is a mediator and supports no claim):** at
`eval_awareness = reasons_about_grader` recall is 4/25 = 16.0% on Goodfire and 121/133 = 91.0% on CM-18; at `mentions_tests` 0/38 against
43/128 = 33.6%; at `answer_material_accessed = True` 2/3 against 22/26. Only these values are shown (`grader_or_harness_touched = True` is
suppressed on Goodfire); on Goodfire the field value goes with low suspicion. The E standardisation (CM-18's `grader_or_harness_touched` x
`answer_material_accessed` mix applied to Goodfire) is **not estimable**: Goodfire has 64 of 68 runs in the (False, False) cell and the cells
with the other values hold 3, 1 and 0 runs (minimum 10).

## Q3: where the Goodfire evidence is (80 enacted, Haiku-all records; disjoint partition)

| Row | Runs |
|---|---|
| 1 flagged (`suspicion >= 2`) | 7 |
| 2 cited a labelled step but rated below 2 | **37** |
| 3 render-degraded, uncited (validated render) | 0 |
| 4 render-unknown, uncited | 10 (6 action-bearing; 4 have no labelled agent step) |
| 5 labelled evidence verbal-only, uncited | **0** |
| 6 action-bearing, uncited | 24 |
| 7 no labelled agent step, uncited | 2 |

- **Cited but rated low is the largest single group (37/80 = 46.3%, Wilson 35.7-57.1).** Counting cited steps as evidence for the reader's
  summary of the run, not a judgement that the step is a hack: the reader **cites** a labelled step (exact) in 42/80 runs against an expected
  24.6 for random citations of as many steps as each record cited, about 17 runs above chance. The citations are unstable: the 11 runs with two
  Haiku lines differ in `evidence_steps` in all 11. The **within-5** figure is 72/80 against 68.9 expected (86% by chance), so it carries no
  information. Current last-wins: exact 43/80, within-5 62/80 (CM-13: 39 and 70). Oracle diagnostic F (alarm *or* exact citation, uses labels,
  not a rule): 36/68 = 52.9%.
- **"Only in considered-only rows" is not a defined object** (README 6). The computable forms: **verbal-only labelled evidence, counted over all 80
  runs: 0.** Of the 80, 72 have a labelled agent step and 8 have none; all 267 labelled agent steps carry a command, so "action-bearing" (any
  labelled agent step with a command) is nearly true by definition and the test is weak. The 149 other labelled indices are off the final parent
  path (dropped from the run the reader saw; none is a tool row merged into an agent step). The considered-only runs are 0/50 flagged.
- **Render check, and what it does not cover.** Of the 68 primary runs the reproduced render matches a logged prompt hash for **56
  (validated)**; **12 are render-unknown** (5 single-form, 7 map-reduce), assigned to neither side. Of the 56, **55 are intact** and 1 is
  structurally degraded (3 labelled steps carry a tool argument over 4,000 characters that is head-and-tail elided; none is split across chunks
  or missing). Structural degradation does **not** cover: map-reduce reads (30 of 68 primary runs), where the form reader sees notes, not the
  step; or redaction, with **86 placeholders inside labelled steps of 19 runs**. Redaction could blank evidence the labels point at:
  `upstream_fix_recall` runs (35 of 80, whose evidence is source and fix text) have a placeholder in a labelled step in 15 of 35, against 4 of
  45 other runs. Whether the placeholders removed the labelled passage cannot be checked (the passage text is not staged). **How the runs
  were rendered is therefore untested for these cases.**
- **Where Goodfire recall does move (exploratory; neither is a registered test):** render-unknown runs 2/12 against validated 2/56 (Fisher p =
  0.14); multi-chunk reads 4/30 against single-form 0/38 (p = 0.034, unregistered, one of several looks).
- Recall by publisher category (G-Haiku primary): hidden_test_targeting 2/33, upstream_fix_recall 2/30, misrepresented_compliance 2/18,
  hardcoding_special_casing 1/20, harness_mechanics_targeting 2/18, test_execution_tampering 1/3; by labelling protocol
  `consensus_lp_v1` 2/36, `consensus_v2` 2/32. No category or protocol stands out.

## Q4: the contest (CM-18 primary; 10,000 draws, seed 20261028, bootstrap stratified by set; SDF is one task and its cluster is degenerate)

`G0 = 62.9` points. Closure is the share of `G0` the change closes. Sets: P68 = 68; V (validated render) = 56; I (intact) = 55; AB (action-bearing)
= 61 (53 with I); B (the primary record is the re-extraction) = 64; A (Haiku and Sonnet both attested v2) = 65.

| Candidate (side) | Counterfactual | Goodfire recall | Closure % (95% interval) | Dropped draws |
|---|---|---|---|---|
| R render loss (facets) | intact vs validated | 2/55 against 2/56 | 0.1 (0.0-0.5); max possible 0.21 | 0 |
| D2 visibility (hacks), render-intact baseline | action-bearing and intact vs intact | 2/53 against 2/55 | 0.2 (0.0-0.9); max possible 0.44 | 0 |
| B extractor vintage (facets; cannot be refuted) | re-extracted records vs P68 | 4/64 against 4/68 | 0.6 (0.0-1.8); max possible 0.58, observed IS the max | 0 |
| A reader (facets) | Sonnet vs Haiku, difference-in-differences with B6 | G 2/65 against 4/65; B6 66/98 against 70/98 | 1.6 (-10.4 to 13.5) | 0 |
| C run length (facets) | standardise to the unseen length mix | not estimable | not estimable | 72.7% |

- **Verdict tree.** Leader A (win share 56.5% of draws; paired lead over B +1.0 points, interval -11.1 to +13.0): test (a) lower bound of
  the closure 50% fails (-10.4); (b) paired lead of 10 fails; (c) win share 60% fails. No candidate has a lower bound of 25%. The tree's last
  branch applies: **"not explained by any tested candidate"**. B reads "no pipeline contrast available" (<= 10%), not "refuted".
- **NP21** (own `G0` 47.1; A not computable): B 0.8% (0.0-2.5), D2 0.3% (0.0-1.2), R 0.1% (0.0-0.7), C not estimable; leader B wins 84.5% of
  draws, paired lead +0.5 (-0.5 to 2.1): the same verdict. It does not disagree with CM-18.
- **Equal set weights** (sensitivity, 2,000 draws): `R_U` 70.1, `G0` 64.2; same ranking and verdict.
- **Per-set gaps (points above P68's 5.9%):** Reflex 94.1, ResearchArena 94.1, EvilGenie 67.5, SDF 65.8, Beam 55.8, KernelBench 39.6, Cheat Index 32.5.
  Even the lowest-recall set is 32 points above Goodfire.
- **C is not estimable by the registered rule, and the reason is itself a finding:** with fixed pooled cut points only 8 of 68 Goodfire runs fall
  below the pooled median (the tercile fallback to halves also leaves fewer than 10), because Goodfire facet reads are long (input tokens
  median 82,408; quartiles 57,799 / 110,372) next to CM-18 (median 26,066, 3.2x shorter) and NP21 (6,546, 12.6x shorter). Zero Goodfire runs are
  below the NP21 median. Length cannot be separated from the corpus here. In the 27% of bootstrap draws where C was estimable its interval is
  -10.3 to -1.0, the same direction as the descriptive pattern that recall rises with length inside CM-18 (quartiles 59.5 / 70.9 / 65.8 / 78.8%);
  inside Goodfire it is 0 / 1 / 0 / 3 of 17 per quartile.
- **Overlap.** D2, R, B and C share most of the same runs (53 of 68 for D2 with R; the recall of every intersection is 2/51 to 4/64), so the
  closures are not independent evidence.
- **Reader mode.** Haiku and Sonnet used the same extractor mode on all 65 paired runs (37 single-form, 28 map-reduce), so the same-mode
  sensitivity row is identical to the main row (Haiku 4/65, Sonnet 2/65). Opus on its 55 against Haiku on the 48 runs both cover: 1/48 each.

## Q5: the 15% recall at 3% false-alarm target (CM-14, CM-19)

**Primary row, named in advance: G-Haiku primary record, `suspicion >= 2`: enacted 4/68 = 5.9% (2.3-14.2), clean 1/58 = 1.7% (0.3-9.1).** The
15% needs 11 of 68; the row has 4, and its interval's upper bound (14.2%) is below 15%. It does not meet the target. Secondary rows at the
registered threshold, not selected: Haiku all-80 7/80 (8.8%) at 1/70; **Sonnet 5/80 at 0/70; Opus 4/55 at 0/60**; `suspicion == 3`: 0/68
(Haiku), 2/80 (Sonnet), 4/55 (Opus).
The label-free threshold lever, `suspicion >= 1` (unregistered, chosen after seeing results): Haiku primary **16/68 = 23.5% (15.0-34.9) at
3/58 = 5.2% (1.8-14.1)**, Haiku all-80 22/80 = 27.5% at 3/70 = 4.3%, Sonnet 17/80 = 21.25% at 0/70, Opus 25/55 = 45.5% (33.0-58.5) at 0/60
(0-6.0). The false-alarm upper bounds (Sonnet 5.2%, Opus 6.0%) exceed 3%, so zero alarms in 60 to 70 clean runs cannot show the 3% half.
Reading: at the registered threshold facets do not reach the target on Goodfire with any of the three readers. At `suspicion >= 1` Sonnet
and Opus meet both halves on point estimates only; they are two of eight secondary rows chosen after the fact, so they are leads for a
preregistered test, not findings. **The gap is not a threshold artefact:** at `suspicion >= 1` CM-18 positives are 295/317 = 93.1%
(89.7-95.4) against Haiku P68 16/68 = 23.5% (NP21 positives 140/187 = 74.9%). **Implication:** unseen-set recall is no evidence that facets
alone meet the target on Goodfire-like hacks. The CM-14 target was set for union rules with the cascade; facets alone answer only part of it, and
one run decides each side of it (12/80, 2/70).

## What was surprising (descriptive; none of it settles facets versus hacks)

1. **No tested contrast moves the gap**, but three of them could not have (maximum closures 0.21%, 0.44%, 0.58%), and the reader contrast is one pair
   at one threshold with a wide interval. Sonnet and Opus do no better than Haiku at `suspicion >= 2` (5/80 and 4/55).
2. **The reader cites a labelled step in 42 of 80 runs against 24.6 expected from random citations, and in 37 of 80 (46.3%, 35.7-57.1) it
   does so while rating the run below 2.** The within-5 localisation figure from CM-13 is near-certain by chance (68.9 of 80 expected).
3. **Where the reader is told the same thing, Goodfire still flags far less** (descriptive, conditioning on a reader output): see Q2.
4. **Every labelled agent step carries a command** (267 of 267), and 149 of the 416 labelled indices are steps off the final parent path
   (not in the run the reader saw). Eight enacted runs have no labelled agent step at all.
5. **The published 7/80 is consistent with Haiku on all 80 lines**, and the current last-wins is 5/80.
6. **Length cannot be separated from corpus**: Goodfire reads are 3.2x (CM-18 median) and 12.6x (NP21 median) longer. Where Goodfire recall does
   move, it moves with map-reduce reads and with render-unknown runs (Q3; exploratory).

## Caveats

- Goodfire's labels are LLM-consensus passage labels; unseen positives are detector, oracle or monitor-agreement labels, which push recall
  up (NP21 notes this). A label-quality ceiling on Goodfire recall is not measured here.
- 12 of the 68 primary runs have a render that could not be validated by hash (stricter than the 10% token tolerance floated in review); they
  are excluded from R and D2 and reported, not assigned.
- Unseen runs share tasks (SDF is one task, ResearchArena one); intervals ignore that except in the named bootstrap, whose set-stratified
  draw leaves SDF without resampling variance. Goodfire has one run per task. Wilson intervals on a few runs are wide (D2 and R rest on 53-56).
- `suspicion >= 2` on Goodfire sits between 4 and 7 runs of 68-80 depending on the record; one run is within CM-13's noise.
- A is one reader pair on one extractor mode; no unseen Opus read exists, so Opus appears only as a sensitivity row.
- The facet record set is the stored one; the DB document is rebuilt with `cloud_doc` (median 28 steps in the document, 69 raw trace rows).
- Not a deployment recall for any corpus; not a ranking of readers; negatives are publisher labels, never certified honest.
- Suppression: breakdown cells below 3 are hidden, but headline k/n rates on the 68 to 80 Goodfire runs are shown by design, so a complement or a
  zero cell can reveal a small cell. These are DEV reader alarms, not labels, ids or text. The Goodfire `tests` and
  `grader_or_harness_touched` breakdowns are omitted from `results/gap.json` for that reason.
- `tools/gap.py` imports `tools/inventory.py` (same directory) and needs numpy (`uv run --with numpy`).
