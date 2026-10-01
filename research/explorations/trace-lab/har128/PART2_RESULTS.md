# HAR-128 part 2: out-of-sample tool accuracy on 40 HAR-116 runs

These results refresh HAR-119 part 2 with n = 40 instead of 12.

## How the labels and predictions were made

**Labels**
- Two blind raters (agents A and B) labelled every run against `RATER_GUIDE_v2.md`.
- The labels were frozen at 2026-10-01T07:57:30Z in `labels_har116/` (`MANIFEST.sha256`, `FROZEN_AT`).

**Predictions** (`predictions_har116/`) came after the freeze, from an agent that never read the labels:
- **Eval Lab** as merged (`ba8d8358`).
- **Scout's deterministic rules.**
- **Docent with `anthropic/claude-opus-5-5`.** The prompt was frozen in #610, and it ran on the hosted free quota for $0.

**Mapping.** The mapping addendum was written at 08:00:15Z, before any tool ran. The scorer is `score.py`; its Eval Lab step adapter is the one HAR-119 used, and I checked it on all 40 runs.

## Results

Tool rows score only the cells where both raters agree. Brackets are 95% Wilson intervals, and `—` means the tool does not express the field.

| | stop | first failure (±2) | blame | loop present | loop kind | loop onset (±5) | pass copied |
|---|---|---|---|---|---|---|---|
| rater A vs B | 40/40 [0.91–1.00] | 33/40 [0.68–0.91] | 40/40 [0.91–1.00] | 35/40 [0.74–0.95] | 35/40 [0.74–0.95] | 13/16 | 9/9 |
| Docent Opus 5.5 | 28/40 [0.55–0.82] | **27/33 [0.66–0.91]** | 40/40 [0.91–1.00] | **32/35 [0.78–0.97]** | **32/35 [0.78–0.97]** | 11/13 | 9/9 |
| Eval Lab | **40/40 [0.91–1.00]** | 8/33 [0.13–0.41] | — | 27/35 [0.61–0.88] | — | 6/10 | 8/9 |
| Scout rules | **40/40 [0.91–1.00]** | 2/33 [0.02–0.20] | 26/29 [0.74–0.96] | 26/35 [0.58–0.86] | — | 2/11 | — |

## Reading

- **Stop reason: keep it on the rules.** Eval Lab and Scout are exact on all 40 runs.
  - Docent confused the ceilings: it said `request_ceiling` on 9 runs where the token ceiling was hit.
  - It also said `other` for 3 loop-breaks, because its schema has no `loop_break` value.
- **First failure, loop kind and copied pass: use Docent Opus.**
  - On first failure it agrees with the raters about as often as they agree with each other.
  - On loop kind (completion-claim vs repetition) it is close to rater level, which no shipped rule reaches.
  - On the 12 HAR-119 runs it also scored best on first failure (6/9) and loop kind (9/11). Both samples point the same way.
- **Eval Lab's `first_failure` (#560) is the weak spot.** Of its 25 misses:
  - 6 are false alarms on earned passes: `bad_edit` or `harness_rejection` on runs both raters call clean.
  - 10 are `null` on infra crashes, where the raters point at the startup failure.
  - The other 9: 5 point at an earlier step than the raters, 1 at a later step, and 3 are `null` on model failures.

  Its in-sample 8/10 (HAR-109) does not carry over.
- **Blame.**
  - 40/40 for Docent is more informative here than on HAR-119, because the sample contains 10 infra runs and 6 earned passes.
  - It still has no harness or task blame to test.
- **Copied pass.**
  - Eval Lab's `pass_may_be_copied` missed 002308-original, where the model pip-downloaded and extracted the upstream wheel and copied files in. Both raters and Docent flagged it.
  - Eval Lab did flag 002308-leakclosed, where the model copied from a newer copy already in the image's site-packages.

## Limits

- The raters are agents, not humans.
- One rater A label opened `config.json` by accident (noted in `FROZEN_AT`).
- The runs come from one model and one harness family.
- 10 of the 40 runs are infra crashes, which are easy for every tool except Eval Lab's first failure.
