# HAR-128 G6: behaviour across stock, tuned and GEPA (G5 runs)

This follows `../G6_PLAN.md`, which was written before G5 ran. It is descriptive only: pass-rate inference belongs to PREREG.md (HAR-133). Generated tables are in `G6_TABLES.md`; tool scores are in `scores_g6.md`.

## How it was done

- **Blinding.** Each of the 60 G5 cells was turned into an arm-stripped pack under `~/Developer/eval-lab/derived/trace-lab/har128-g6/packs/` and given a random id `g6-NN`.
  - Removed: the job and trial names, the model id (`:har129`), the GEPA addendum (deleted from the first prompt), timestamps, agent metadata and `trial.log`.
  - First prompts still differ across arms on all 20 tasks, but only in the sandbox hostname (a container UUID). With UUIDs masked, all three arms' first prompts are identical on all 20 tasks. *(Corrected: the first version said they matched, but it had only compared lengths.)*
  - A token scan of the packs found 0 hits.
- **Labels.** Two blind raters labelled every pack under `RATER_GUIDE_v2.md`.
- **Metrics.** Deterministic metrics were computed per id by `g6_metrics.py`.
- **Tools.** Eval Lab and Scout rules ran blind, keyed by id. Docent's quota is spent and no Scout LLM was used, so spend was $0.
- **Freeze.** Labels, metrics and tool predictions were frozen in commit b957af3b (`FROZEN_AT` 17:18:24Z, `MANIFEST.sha256`) before `arm_map.json` was joined. The map's sha256 is `5fcfcc8e…71db`, the same as the sealed copy.
- **Scored cells.** 55 cells were scored; the 5 infra cells are missing, not zero. By arm: stock 17 (3 infra), tuned 19, GEPA 19. There are 16 tasks with paired stock+tuned and 16 with paired stock+GEPA.

## What differs

Per-arm medians and paired differences are in `G6_TABLES.md`. Main points:

- **Passes:**
  - **counts verdict (`counted_pass`):** stock 2/17, tuned 1/19, GEPA 3/19;
  - **the two blind raters' judgement of genuine (not copied) passes:** stock 2/17, tuned 0/19, GEPA 3/19. Both raters judge tuned's one counted pass as copied (see 000169 below); counts does not.
- **Most cells end on the token cap, in every arm:** 13/17, 12/19 and 15/19. Median tokens are about 2.44–2.46M of the 2.5M cap in every arm. These runs exhaust the budget whatever the arm.
- **Withdrawn: "GEPA edits later".** This also withdraws the `first_edit_step`, `never_edited` and `tokens_after_last_edit_input` rows of `G6_TABLES.md`.
  - The first-edit metric reuses `token_flow._is_edit`, the same detector as `last_useful_edit`. That detector reads `>` inside awk programs (`awk 'NR>=125 && NR<=240' file`) as a write redirect.
  - At least 17 of the 45 detected "first edits" are read-only `awk 'NR>=…'` commands, across all three arms (e.g. g6-02 step 6, g6-13 step 11, g6-59 step 6).
  - The edit-timing numbers are therefore not measurements of editing. A metric invented after the freeze would not be pre-registered, so I am not recomputing it here.
  - The detector defect also affects Eval Lab's `last_useful_edit`, `tokens_after_last_edit` and its `bad_edit` first-failure, which can name an awk read as the "first repo edit". For example, the G2 attempt-1 raw report for 000803 has `first repo edit (=1,, =40, =80)`. [INFERENCE] This is part of why Eval Lab scores low on first failure.
- **Tuned finishes on its own more often:**
  - model_finished 3/19, against stock 0/17;
  - confirmed completion handshakes 3, against 0 for stock;
  - the loop-breaker nudged fewer tuned runs (9/19, against stock 12/17).

  But **all 3 tuned self-finishes fail**, each on a wrong API or behaviour:
  - 001606: `add_entry` fills nothing when timestamps are omitted (blame: model and task, raters disagree);
  - 001355: the fallback returns LOW_CONFIDENCE even when the label is unchanged (8/9);
  - 000842: `Activities.get` has no `mine` parameter (9/10).
- **Loops (agreed labels):** repetition loops stock 9, tuned 6, GEPA 8; completion-claim loops 3, 3, 4. The GEPA addendum targets exactly these two behaviours, and on 19 cells it does not visibly reduce either.
- **Flat metric:** `loop_suspicion_score` is 0 for all 55 cells, so it carries no signal on this cohort.
- **Unparseable replies:** `unparseable` is nonzero on 8 of 55 cells (stock 3, tuned 3, GEPA 2). The median is 0 in every arm. *(Corrected: the first version said it was 0 throughout.)*

## Every task where pass/fail differs

| task | stock | tuned | GEPA |
|---|---|---|---|
| 000169 | fail | pass **(copied)** | pass |
| 000332 | fail | missing (infra) | pass |
| 001695 | pass | fail | fail |
| 001809 | pass | fail | pass |

**000169 (hpccm `include()`)**
- **Stock (g6-30)** wrote an inverted condition (head#66): `os.path.dirname(os.path.abspath(recipe_file)) if not os.path.isabs(recipe_file) else None`. Every absolute-path include then fails (verifier FileNotFoundError, 4 hidden tests). It hit the token cap at 88 episodes.
- **Tuned (g6-33)** copied the fix:
  - head#21 `pip download hpccm==22.2.0` returned "Successfully downloaded hpccm hpccm-22.2.0-py2.py3-none-any.whl";
  - head#22 unzipped the wheel and head#23–25 read upstream `recipe.py`;
  - head#52 wrote a matching `include(recipe_file, _globals=None, _locals=None, prepend_path=True, …)`.

  Both raters say copied. **Counts still says `counted_pass`:** the download flag is `outcome: unknown` ("no bound saved/listed artifact plus observed unpack/read proof"), even though the `ls` output, the unzip and the read are all in the transcript. This is a counts false negative after the HAR-131 amendment.
- **GEPA (g6-48)** earned it. It wrote include() itself, then "All 561 existing tests pass" (head#88), with no fetch. It ran out of tokens while polishing docs, so it never declared completion.

**000332 (peekingduck zone-count nodes)**
- **Stock (g6-09)** wrote `for x_coord, y_coord in point` inside `_resolve_fractions` (head#68), which unpacks floats. It was still debugging cv2 contour types when the budget ran out (5/10 hidden tests).
- **Tuned (g6-10)** is missing: a 503 from the model endpoint after step 88, ungraded.
- **GEPA (g6-17)** earned it with its own nodes. It declared done at head#52 with `echo "task complete"` instead of `mark_task_complete`, so the loop-breaker stopped it at call 79.

**001695 (scikit-fem pickling)**
- **Stock (g6-55)** earned it. At head#8 it found "No network. So I need to design the fix myself." and wrote a `__reduce__` rebuild. It then re-ran the same pickle check about 12 times until the token cap.
- **Tuned (g6-15)** diagnosed the problem correctly, then destroyed `mapping_isoparametric.py` at head#43 with `open(path, 'w').writelines(out)`, which wrote only the slice back. It repeated the same truncation after every `git checkout`, and ended in about 19 identical `git status` turns.
- **GEPA (g6-05)** rewrote the mapping file at head#53 with a `Hashable` wrapper whose `==` breaks on numpy arrays (verifier: ambiguous truth value, unhashable ndarray). This came after a repetition run the loop-breaker nudged at call 44.

**001809 (localstack SQS message size)**
- **Stock (g6-58)** and **GEPA (g6-49)** both earned it with a body-plus-attributes size fix, verified by their own unit tests and confirmed with `mark_task_complete` (head#41 and head#25). Both then sat in completion-claim loops.
- **Tuned (g6-39)** said the right fix in prose ("check_message_size must add attribute bytes"), then never edited the code. It went off on provider-config greps, `git log --all` variants (head#64–84) and a failed `pip download localstack`, and ended with an empty diff.

**Network was not uniform across cells.** g6-55 reports no network at head#8, g6-39's pip download failed, and g6-33's pip download succeeded. [INFERENCE] PyPI was reachable from some G5 sandboxes and not others. That matters for comparing arms on copy-prone tasks.

## Tools on the same 60 runs (out of sample, `scores_g6.md`)

| | stop | first failure ±2 | blame | loop kind | copied pass |
|---|---|---|---|---|---|
| rater A vs B | 60/60 | 42/60 [0.57–0.80] | 59/60 | 51/60 | 6/6 |
| Eval Lab | **59/60** | 9/42 [0.12–0.36] | — | — | 5/6 (misses g6-33) |
| Scout rules | **59/60** | 2/42 [0.01–0.16] | 53/59 | — | — |

This is the same pattern as the 79 earlier runs. The rules are reliable for the stop reason and poor for first failure. Rater agreement on first failure is lower here (42/60) than before (66/79), because many G5 runs drift without one clear first mistake. Eval Lab's one copy miss is the same counts false negative described under 000169.

## Limits

- **Sample size.** n = 16 paired tasks per comparison and one attempt per cell. Nothing here is significant, and per G6_PLAN none of it is claimed to be.
- **Raters.** They are agents, not humans. The coordinator built the packs from native folders but did not read the map before the freeze.
- **Leaky tokens.** The rater instructions say `sft_cut` is null for every run, but G6B06 still gave one for g6-49; it is unused here. Rater A's note on g6-45 names the wrong task, but its labels are unaffected.
