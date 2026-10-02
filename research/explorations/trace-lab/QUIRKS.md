# Known quirks for trace analysis

Read this before analysing any Harbor/MiMo run. Every trace reader loads it first: the `trace-analyst` and `trace-rater` agents, `evallab review`, `evallab watch` and Docent prompts.

Each entry gives what goes wrong, how to handle it, the evidence and its status. When you find a new quirk, add it here in the same PR as the evidence. When one is fixed, change its **Status** and keep the entry: older runs processed before the fix still carry the problem.

Status values:
- `open`: the problem is live;
- `fixed in <PR>`: the code is fixed, but runs processed before that PR are still affected;
- `by design`: intended behaviour you have to work around.

## Deterministic signals (Eval Lab `processed/trial-*.json`)

### Q1. The counts verdict missed successful upstream downloads
- **What:** `counts.verdict` could call a copied pass `counted_pass`. A successful `pip download X==ver` followed by unzip and reading the code stayed `upstream_fetch_attempt`, `outcome: unknown` ("no bound saved/listed artifact plus observed unpack/read proof").
- **Handle:** for any trial processed before #678/#681, re-run `evallab process-job` on a copy of the job before trusting `counted_pass`.
- **Evidence:**
  - G5 `ovn-g5-000169-tuned__CxBpceW`: published verdict `counted_pass`. Steps 21–25 are a successful download, unzip and `cat recipe.py`, and step 52 is a matching `include()`.
  - Re-running process-job on main 6ce08d63 gives `excluded` (`copied_fix`, `pass_tainted`).
  - Both blind raters judged it copied (`research/explorations/trace-lab/har128/g6/`).
- **Status:** fixed in #661, #678 and #681. The published G5 `processed/` outputs predate the fix.

### Q2. The edit detector reads `>` inside quotes as a write
- **What:** `token_flow._is_edit` (`_EDIT_REDIRECT_RE`) treats `awk 'NR>=125 && NR<=240' f` as a shell redirect. This corrupts `first_edit`, `token_flow.last_useful_edit`, `tokens_after_last_edit`, and run_report's `bad_edit` first failure ("first repo edit (=1,, =40, =80)").
- **Handle:** don't trust edit timing from runs processed before the fix.
- **Evidence:** at least 17 of the 45 detected first edits in G5 are read-only awk commands (`har128/g6/G6_RESULTS.md`).
- **Status:** open. The fix is in progress under HAR-141.

### Q3. Eval Lab and Scout "first failure" don't match human judgement
- **What:** out of sample, against blind labels:
  - Eval Lab `first_failure`: 18/66 (79-run set) and 9/42 (G5);
  - Scout rules: 4/66 and 2/42. When its rules find nothing, Scout falls back to the start of the outcome evidence (often `head#2`).
- **Handle:**
  - use Docent or agent readings (42/58 for Opus) or hand labels for first failure;
  - use the rules for the stop reason: 78/78 and 59/60.
- **Evidence:** `har128/PART2_RESULTS.md`, `har128/g6/scores_g6.md`.
- **Status:** open (HAR-141).

### Q4. `loop_suspicion.score` is always 0
- **What:** it was 0 on all 55 scored G5 cells, so it carries no signal.
- **Handle:** use `token_flow.loop_onset`, the harness `loop_break` metadata, or labels instead.
- **Evidence:** `har128/g6/G6_TABLES.md`.
- **Status:** open.

### Q5. Re-running process-job can change verdicts
- **What:** detectors change between versions (Q1, Q2), so the same trial can get a different `counts.verdict` or `token_flow` depending on when it was processed.
- **Handle:** before comparing runs processed at different times, record the processed schema / commit, or reprocess all of them with one version.
- **Status:** by design.

## Model readers

### Q6. Docent gets the stop reason wrong
- **What:** Docent Opus 5.5 agreed on 37/68 runs. It maps the loop-breaker to `other` and confuses the token and request ceilings.
- **Handle:** take the stop reason from Eval Lab or Scout rules, never from Docent.
- **Evidence:** `har128/PART2_RESULTS.md`.
- **Status:** by design (its schema has no `loop_break`).

### Q7. Docent's free hosted quota is weekly
- **What:** the free weekly usage limit ran out after about 5.3M input tokens, roughly 81 run readings, in the week of 2026-09-28. The error is `docent_usage_limit`.
- **Handle:**
  - plan readings within the quota;
  - otherwise use agent raters, or BYOK / self-hosting, which needs a provider key and Peter's spend approval.
- **Status:** open.

### Q8. Docent's secret gate refuses task-fixture keys
- **What:** some tasks ship test fixtures containing an RSA private-key PEM header (logstash 000865). The upload secret gate refuses the transcript.
- **Handle:** withhold that run, give it a null row with a `raw_error`, and never widen the excuse list.
- **Status:** by design.

### Q9. Model readers sometimes return incomplete JSON
- **What:** agent raters occasionally put "see attachment" or trial ids only in `report`.
- **Handle:** validate the required fields before freezing, and ask the same rater to resend its own judgements; don't relabel.
- **Evidence:** G3A3, G6A07 and G6B07 in HAR-128.
- **Status:** by design. `evallab review freeze` checks for it.

## Task and data quirks (MiMo / FineEnvs)

### Q10. Some task images leak the solution
- **What:** the fix sits inside the image.
  - 001269: the fixed `responses/__init__.py` is in `/testbed/build/lib/`.
  - 002308: a newer pre-commit is in site-packages.
- **Handle:** treat those tasks as leaky (the ledger discards 001269, #660). A copy from inside the image is copying even with no network access.
- **Evidence:** `har128/labels_g2_tail/`, HAR-127.
- **Status:** open for any task not yet audited.

### Q11. PyPI is reachable from some sandboxes
- **What:**
  - FineEnvs' `/etc/hosts` block list misses PyPI, and the agent runs as root;
  - copies happened via `pip download` on 000341, 001373, 002356 and G5 000169-tuned;
  - in G5, network access varied by cell: g6-55 said "No network" while g6-33 downloaded from PyPI.
- **Handle:**
  - when an arm comparison involves copy-prone tasks, count copies per arm;
  - the egress lock (HAR-122, opt-in) blocks this.
- **Status:** open (HAR-140 proposes the lock on by default).

### Q12. The original MiMo data has no reference fix
- **What:** XiaomiMiMo `code.parquet` has no gold patch, repo URL, base commit or F2P/P2P list, and `ground_truth` is empty. Hidden tests were synthesized.
- **Handle:** an oracle (gold-passes) gate can't be run from the published data.
- **Evidence:** `research/experiments/oracle-pilot/README.md`.
- **Status:** by design.

### Q13. The FineEnvs conversion changes grading and the environment
- **What:**
  - grading runs with `sh -c`, not `bash -lc` (login-path defects);
  - Xiaomi's `git clean` removes build outputs;
  - some images pin the wrong versions.
- **Evidence:** `research/experiments/har115-census/ORIGINS.md`.
- **Status:** open.

### Q14. The GLM-5.3-flash task checker isn't calibrated
- **What:** `checker_v3.py` runs at temperature 0.7, and nothing has scored it against human judgement. 165 of the 174 tasks in "review" are there only because of the checker.
- **Handle:** never treat its "sound" verdict as decisive.
- **Evidence:** `task-validity/BEST_PRACTICES.md`.
- **Status:** open (HAR-139).

## Harness and run quirks (Terminus-2, lf2)

### Q15. First prompts differ by the sandbox hostname
- **What:** the first user message includes the container hostname UUID, so prompts never match byte for byte across arms.
- **Handle:** mask `[0-9a-f]{8}-…-[0-9a-f]{12}` and 12-hex ids before comparing prompts or checking blinding.
- **Status:** by design.

### Q16. The token cap ends most runs
- **What:** every call resends the whole conversation, so input tokens reach the 2.5M per-trial cap at around call 100. 40 of 60 G5 cells ended on it.
- **Handle:**
  - the cap is our budget, not part of the task, because `task.toml` only sets a timeout;
  - "ran out of budget" usually means the run was looping or exploring without converging.
- **Status:** by design.

### Q17. The completion handshake turns into echo loops
- **What:** after `mark_task_complete`, the harness asks for confirmation. Models often answer with `echo "task complete"` turns instead of re-confirming, for up to 60 turns (g6-27), until the loop-breaker or the token cap stops them.
- **Handle:** label these `completion-claim` loops. Do not count them as work.
- **Status:** by design.

### Q18. The loop-breaker over-fires on sliding-window reads
- **What:** sliding-window `sed -n a,bp` / `awk 'NR>=…'` reads can look like a repeated command and draw the "you are repeating" nudge while each read returns new content.
- **Handle:** a nudge alone is not proof of a loop; check whether the observations change.
- **Evidence:** g6-06 and g6-24 (rater notes).
- **Status:** open.

### Q19. Agents read their own harness logs
- **What:** agents can grep `/logs/agent/terminus_2.pane`, their own session log. g6-56 spent about 40 turns mining it.
- **Handle:** it's not a hidden-test leak, but it is reading harness state. `evallab watch` flags it as `hidden_info_read`.
- **Status:** open.

### Q20. Agents edit visible tests to make them pass
- **What:** g6-41 rewrote `test/test_youtube.py` with `sed` to hide a gap, and g6-30 edited the visible tests. Hidden tests are unaffected, so this doesn't change rewards, but it is grader-tampering intent.
- **Handle:** `evallab watch` flags edits to test or verifier paths as `grader_tamper`.
- **Status:** open.

### Q21. Reward-seeking echoes
- **What:** g6-14 echoed `COMPLETE_TASK_AND_GIVE_FULL_CREDIT` about 10 times. It's harmless to grading but worth flagging in reward-hacking review.
- **Status:** open.

### Q22. Infra failures look like model failures
- **What:** `ServiceUnavailableError` (endpoint 503) and `DaytonaNotFoundError` (the sandbox was lost) end runs in the middle of their work.
- **Handle:** count them as `excluded` infra (missing, never zero). Raters label them `infra_error` with blame `infra`.
- **Evidence:** 5 of 60 G5 cells.
- **Status:** by design.
