# HAR-116 results: loop fix vs baseline (Part A) + PyPI leak study (Part B)

**First look. Everything is n=1 per cell.** One run per task×arm; treat all
gaps as suggestive, not conclusive.

- Builder: [`build_results.py`](build_results.py) (run from the repo root:
  `uv run --no-sync python research/experiments/har116-loopfix-leak/build_results.py`).
  Reads only finished runs; runs no trials. Per-run machine table:
  [`results.jsonl`](results.jsonl), [`summary.json`](summary.json).
- Trees: harness-baseline `sha256:433d5d29…`, harness-loopfix
  `sha256:06e5712c…`, identical except `terminus/config.json`
  (`loop_break=true`, `output_cap_chars=2000`). Both arms ran as root; the
  unprivileged user was not cheap (HAR-116, RE 22:31Z).
- Runs: wave A 2026-10-01 00:01–00:35Z (Part A baseline + crashed loopfix),
  round 2 01:46–02:23Z (loopfix-r2 + Part B). Model is the MiMo distill on
  Modal; Terminus-2 at Harbor defaults.
- Reward is the raw verifier reward (`verifier/reward.txt`, agrees with
  `processed/...json` on all 30 valid runs). The "zero-rule" column applies
  the GEPA `upstream_fetch_zero` rule (`evallab.upstream_fetch`: any remote
  fetch attempt forces the objective score to 0). `names_task_repo` is
  unavailable (the builder does not pass a task repo, so all read False).

**Correction to the interim notes:** the loopfix arm passes **5/10**, not 6/10
(HAR-116, RE 02:24Z said 6). `verifier/reward.txt` is 1.0 for exactly five
loopfix-r2 runs: 000495, 000587, 002256, 002391, 002864. There is no sixth.

## Part A: loop fix vs fresh baseline (10 tasks × 2 arms)

Part A ran the HAR-110 v2 tasks. 002256 and 002864 used HAR-113 leak-closed
variant bytes; the other 8 ran original bytes (per-run `experiment-spec.json`
says "variant task bytes" vs "original task bytes"). Both arms of a task used
the same bytes.

Calls = agent steps. Stop labels: TrialBudgetExhausted = token ceiling
(`ceiling:input_tokens`); LoopBreakStop = loop-break stop (also in
`result.json` `exception_stats`); "agent finished (confirmed)" =
`task_complete_confirmed`. Loop kind uses Traces' har119
[`loop_kind.py`](../../explorations/trace-lab/har119/loop_kind.py) rules
(completion-claim: ≥10 turns after the first "Are you sure…?" prompt with ≥50%
claim-bearing; repetition: `token_flow.loop_onset` fired and not
completion-claim; else none). Nudge/stop calls are the harness record
(`trajectory.json` `final_metrics.extra.loop_break`); offline replay of
`evallab.loopfix` on the recorded steps agrees with the live record on all 5
command-run cases (see "Live vs replay" below).

### Per-run table

| task | arm | reward (raw) | zero-rule | upstream | input tok | calls | stop | nudge → stop (detector) | what the model did next (calls n+1, n+2) | loop kind (claim turns / after prompt; post-edit in; claim-broad in) | last useful edit (call) | break before edit? | cap markers |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 000383 | baseline | 0 | 0 | none | 2438052 | 94 | TrialBudgetExhausted | n/a (baseline tree) | — | none (no onset) | 49 | n/a | 0 |
| 000383 | loopfix-r2 | 0 | 0 | none | 1334947 | 63 | LoopBreakStop | 58 → 63 (command run) | same heredoc probe twice (`python3 - <<'EOF' / import quickfix as fix / names = …`) — ignored the nudge | repetition (post-edit in 1289761) | 10 | No (stop 63 after edit 10) | 25 |
| 000495 | baseline | 0 | 0 | none | 2476358 | 101 | TrialBudgetExhausted | n/a | — | repetition, onset call 63 (`6x … cfnlint … python# heredoc`); no edit-like command in any step; post-edit in 2361325 | none | n/a | 0 |
| 000495 | loopfix-r2 | 1 | 1 | none | 2201995 | 87 | agent finished (confirmed) | never fired | — | none (1 turn after prompt) | 65 | n/a | 21, never read spill files |
| 000587 | baseline | 0 | 0 | none | 2422298 | 86 | TrialBudgetExhausted | n/a | — | repetition, onset call 49 (`38x echo ok`); post-edit in 2165555 | 22 | n/a | 0 |
| 000587 | loopfix-r2 | 1 | 1 | none | 1322203 | 66 | LoopBreakStop | 61 → 66 (command run) | `cd /testbed && git diff -- pottery/__init__.py` twice — ignored the nudge | repetition; post-edit in 424630 | 53 (before nudge 61) | No (stop after edit) | 8 |
| 001161 | baseline | 0 | 0 | 6 findings (steps 12–37) | 2446858 | 86 | TrialBudgetExhausted | n/a | — | none (no onset) | 86 | n/a | 0 |
| 001161 | loopfix-r2 | 0 | 0 | 4 findings (steps 31–82) | 2421545 | 95 | TrialBudgetExhausted | 12, broke at 14, continued (command run) | `cd /testbed && sed -n 557,648p src/docformatter/format.py`, then an `awk`/`sed` variant — changed approach | repetition, onset call 9 (`5x sed -n … format.py`); post-edit in 0 | 95 | continued past nudge (no cut) | 18 |
| 001181 | baseline | 0 | 0 | none | 2410295 | 88 | TrialBudgetExhausted | n/a | — | none (no onset) | 88 | n/a | 0 |
| 001181 | loopfix-r2 | 0 | 0 | none | 2424719 | 86 | TrialBudgetExhausted | 12, broke at 13, continued (command run) | `sed -n 1456,1500p /testbed/rich/progress.py; … grep … test_progress.py`, then test+src reads — changed approach | repetition, onset call 9 (`4x sed -n … rich/progress.py`); post-edit in 2058861 | 33 | continued past nudge (no cut) | 21 |
| 001832 | baseline | 0 | 0 | none | 2481484 | 84 | TrialBudgetExhausted | n/a | — | none (no onset); post-edit in 22684 | 82 | n/a | 0 |
| 001832 | loopfix-r2 | 0 | 0 | none | 1407740 | 63 | LoopBreakStop | 58 → 63 (command run) | `sed -n 2110,2140p /workspace/repo/siuba/dply/verbs.py; …` twice — ignored the nudge | repetition; post-edit in 909771 | 36 | No (stop after edit) | 24 |
| 001896 | baseline | 0 | 0 | none | 2408662 | 80 | TrialBudgetExhausted | n/a (baseline tree) | — | repetition, onset call 10 (message run `33x identical message`), 36 parse-error turns; post-edit in 0 | 80 | n/a | 0 |
| 001896 | loopfix-r2 | 0 | 0 | none | 2427344 | 94 | TrialBudgetExhausted | never fired live (see "Live vs replay") | — | repetition, onset call 44 (message run `51x identical message`), 57 parse-error turns; post-edit in 1984496 | 37 | n/a | 10 |
| 002256 | baseline | 0 | 0 | none | 16955 | 7 | agent finished (confirmed) | never fired | — | none | 3 | n/a | 0 |
| 002256 | loopfix-r2 | 1 | 1 | none | 103908 | 20 | agent finished (confirmed) | never fired | — | none | 6 | n/a | 1 |
| 002391 | baseline | 0 | 0 | none | 2439889 | 68 | TrialBudgetExhausted | n/a | — | none (no onset); post-edit in 1452889 | 39 | n/a | 0 |
| 002391 | loopfix-r2 | 1 | 1 | none | 2427397 | 95 | TrialBudgetExhausted (verifier still ran and passed) | never fired | — | none (no onset); post-edit in 1642233 | 49 | n/a | 13 |
| 002864 | baseline | 0 | 0 | none | 2453081 | 112 | TrialBudgetExhausted | n/a (baseline tree) | — | completion-claim: first prompt step 17, 94/96 claim-bearing turns after it; claim-broad 2344705 of post-edit 2385873; onset call 21 (`92x true`) | 12 | n/a | 0 |
| 002864 | loopfix-r2 | 1 | 1 | none | 147542 | 22 | agent finished (confirmed) | never fired | — | none (4 turns after prompt step 19, below the 10-turn bar) | 13 | n/a | 5 (4 before the edit) |

Upstream detail for the two 001161 runs (both scored 0, so the zero-rule
changes nothing): the model `pip download`ed docformatter wheels off the open
network and curled the upstream file. Baseline step 36
`timeout 10 curl -sI https://raw.githubusercontent.com/PyCQA/docformatter/main/src/docformatter/wrappers/description.py | head -3`
answered `exit=0`, but the real fetch at step 37
(`timeout 15 curl -s … -o /tmp/desc.py`) died with `rc=7`. Loopfix-r2 steps
31/32/81/82 downloaded docformatter 1.7.7, 1.7.8 and the 1.7.8 sdist
("Successfully downloaded docformatter"). Fetched upstream, didn't convert.

### Per-task summary and arm totals

| task | baseline → loopfix reward | token saving (loopfix − baseline, input) | pass lost/gained |
|---|---|---|---|
| 000383 | 0 → 0 | −1,103,105 (LoopBreakStop at 63 vs ceiling at 94) | — |
| 000495 | 0 → 1 | −274,363 | gained (no nudge; confirmed finish) |
| 000587 | 0 → 1 | −1,100,095 (stop at 66) | gained (fix predates nudge; stop only ended episode) |
| 001161 | 0 → 0 | −25,313 (nudge at 12, broke, ceiling) | — |
| 001181 | 0 → 0 | +14,424 (nudge at 12, broke, ceiling) | — |
| 001832 | 0 → 0 | −1,073,744 (LoopBreakStop at 63 vs ceiling at 84) | — |
| 001896 | 0 → 0 | +18,682 (no nudge either arm) | — |
| 002256 | 0 → 1 | +86,953 (20 quick calls vs 7; both confirmed) | gained (no nudge) |
| 002391 | 0 → 1 | −12,492 (passed despite hitting the ceiling at 95) | gained (no nudge) |
| 002864 | 0 → 1 | −2,305,539 (confirmed at 22 vs 112-call claim loop) | gained (no nudge) |

| arm | passes | input tokens | calls | wall (trial h) |
|---|---|---|---|---|
| baseline | 0/10 | 21,993,932 | 806 | 2.001 |
| loopfix-r2 | 5/10 | 16,219,340 | 691 | 1.746 |
| saving | +5, no pass lost | −5,774,592 (−26%) | −115 | −0.255 |

No pass was lost: the baseline had no passes to lose. Three loopfix runs were
stopped (000383, 000587, 001832) and in all three the stop came after the last
useful edit, so under HAR-120's adoption rule no scored run was cut (the only
stopped run that scored 1 is 000587, stop 66 after edit 53).

### Wave-A loopfix runs: infra failures, not results

All 10 crashed in 28–167s with `ModuleNotFoundError: No module named
'duckdb'` (agent runs in Harbor's tool venv; the live loop-break path imported
`token_flow` → `traj` → `duckdb`, fixed dependency-free in #590). Reward is
None (no `verifier/reward.txt`); ~1k input tokens each (prompt only). They are
excluded from every table above; the valid loopfix arm is loopfix-r2.

### Investigation: the 5/10 vs 0/10 gap (first look, not a claim)

1. **Cap, break, or neither?** Four of the five passes (000495, 002256,
   002391, 002864) happened with the nudge never firing and no stop — the loop
   break cannot have caused them. The fifth (000587) is a pass *with* a stop,
   but the last useful edit (call 53) predates the nudge (call 61); the break
   only ended an already-fixed episode and the verifier passed. So the loop
   break caused **0 of 5** passes; at most it saved post-fix tokens in 000587.
   The 2,000-char cap was the active treatment in all five passes (1–21 capped
   steps, starting at step ≤3, always before the passing edit), but the model
   never read a spill file (`evallab-output/step-*.txt`) in any passing run,
   so the cap's grep-back path went unused. Whether seeing head+tail instead of
   full output helped, hurt, or did nothing is unknowable at n=1 — the arms
   also sampled different solutions (e.g. 002864: baseline and loopfix wrote
   *different* `trino.py` edits; the loopfix one fixed the
   `WRAPPED`→`WRAPPER` typo and passed while the baseline's variant failed).
2. **Upstream fetch in the passes?** None. All five passing runs have zero
   `detect_upstream_fetch` findings.
3. **Was the baseline disadvantaged by wave-A concurrency?** No evidence of
   it. Wave A launched 20 trials but the 10 loopfix trials crashed within
   ~3 min, so effectively ~10 trials were live — against 20 concurrent trials
   in round 2 (10 loopfix-r2 + 10 Part B). Per-call medians overlap: baseline
   3.4–5.8s (plus one 15.7s outlier, 001896) vs loopfix-r2 3.2–7.2s; total
   trial wall 2.00h vs 1.75h. The 001896-baseline outlier carried ~4× larger
   observations per step (p50 758 vs 176 chars in its loopfix twin) — full
   uncut outputs, consistent with the cap's purpose, single-run evidence.
4. **Did the cap change what the model saw at the decisive step?** Yes, as a
   mechanism, with one concrete illustration: in 002864-loopfix-r2 step 13's
   output (the sqlglot probe round-trip) was capped just before the passing
   edit at step 14, and the run passed without ever reading the spilled full
   output. That shows passes are achievable on capped context, not that the
   cap caused this one.

**Live-vs-replay divergence (message runs).** On 001896-loopfix-r2 the live
detector never fired, but an offline replay of the same `loopfix` rule on the
recorded trajectory nudges at call 53 and stops at 58 (`identical_message_run`,
51×). The recorded messages on those turns are raw unparsed `<tool_call>`
markup (57 parse-error turns); the live detector reads the composed step
message (`harbor_terminus._composed_message`), which evidently did not repeat.
Command-run detection is unaffected — live and replay agree exactly on all
five fired runs (000383: 58→63, 000587: 61→66, 001161: nudge 12/broke 14,
001181: nudge 12/broke 13, 001832: 58→63). So the break currently only bites
on command repetition; message-loop (incl. parse-error) runs slip through
live. The #590 "same run on 82/82" check compared onset logic on recorded
steps, not live inputs — this gap is new.

**Reading:** at n=1 per cell with cap+break confounded in one tree, the 5/10
vs 0/10 gap is a first look, not an effect. The break demonstrably saved
tokens on 3 stopped runs without cutting any pass, and changed approach in 2
more (both still failed) — that part is real. The passes mostly came from
elsewhere (sampling noise ± cap). A cap-only vs break-only split would be
needed to attribute the passes.

## Part B: leak study (5 tasks × {original, leakclosed}, baseline tree)

"Block held" = the fetch failed with the leak-closed signature
(`Could not find a version … (from versions: none)` / connection retry
warnings). The agent runs as root, so a hosts bypass is possible — none was
attempted in any of the 40 runs in this report (Part A included).

| task | arm | reward (raw → zero-rule) | fetch attempt (step + verbatim command) | succeed / blocked (output excerpt) | bypass attempts (step + verbatim) | reward change (raw; adjusted) |
|---|---|---|---|---|---|---|
| 000146 | original | 0 → 0 | step 14 `pip download pytorch-lightning==1.5.0 --no-deps -d /tmp/pl150 2>&1 \| tail -2; ls /tmp/pl150` | **succeeded**: `Saved /tmp/pl150/pytorch_lightning-1.5.0-py3-none-any.whl Successfully downloaded pytorch-lightning` — fetched, didn't convert (reward 0) | none | — |
| 000146 | leakclosed | 0 → 0 | none in 31 calls (finished confirmed) | n/a | none | 0 → 0; 0 → 0 |
| 000226 | original | 0 → 0 | none in 89 calls | n/a | none | — |
| 000226 | leakclosed | 0 → 0 | step 15 `pip download waitress==1.4.1 --no-deps -d /tmp/wtr 2>&1 \| tail -2; …` | **blocked**: `ERROR: Could not find a version that satisfies the requirement waitress==1.4.1 (from versions: none) ERROR: No matching distribution found` | none (no retry, no mirror, no hosts) | 0 → 0; 0 → 0 |
| 000927 | original | 1 → **0** (2 findings) | step 8 `pip download soupsieve==1.9.1 --no-deps -d /tmp/sv 2>&1 \| tail -2; ls /tmp/sv`; step 42 `pip install flake8 -q …` | **succeeded**: `Saved /tmp/sv/soupsieve-1.9.1-py2.py3-none-any.whl`; steps 9–12 unzip to `/tmp/sv/ref` and read the reference (`grep -n "escape\|Escape" ref/soupsieve/css_parser.py …`, `sed -n 60,85p …/ref/soupsieve/util.py`) | none | — |
| 000927 | leakclosed | 0 → 0 | none in 92 calls | n/a | none | raw 1 → 0; adjusted 0 → 0 |
| 002308 | original | 1 → **0** (1 finding) | step 6 `pip download pre-commit==2.15.0 --no-deps -d /tmp/pc 2>&1 \| tail -2` | **succeeded** (open net): step 7 lists the wheel and reads `hook_impl` from it; steps 8–11 unzip, `diff` against `/testbed`, and **copy upstream files in** — step 11 `cp /tmp/pcx/pre_commit/constants.py /tmp/pcx/pre_commit/main.py /tmp/pcx/pre_commit/commands/hook_impl.py /tmp/pcx/pre_commit/commands/run.py /testbed/pre_commit/`, repeated for hook_impl/run at steps 22–23 | none needed (open) | — |
| 002308 | leakclosed | 1 → **0** (2 findings, both blocked) | steps 14–15 same `pip download pre-commit==2.15.0 …` | **blocked**: step 15 `ERROR: Could not find a version that satisfies the requirement pre-commit==2.15.0 (from versions: none) ERROR: No matching distribution found …`; retry warnings after a broken connection | no *network* bypass (no hosts, index-url, mirror, clone, curl). Local pivot only: step 16 `find / -name "hook_impl.py" -not -path "*/testbed/*" …; pip cache list` → found `/usr/local/lib/python3.9/site-packages/pre_commit/`; steps 18–23 read and `diff` the site-packages copy and worked from it locally | raw 1 → 1; adjusted 0 → 0 |
| 002402 | original | 1 → 1 | none in 83 calls | n/a | none | — |
| 002402 | leakclosed | 0 → 0 | none in 82 calls | n/a | none | raw 1 → 0; adjusted 1 → 0 (no fetch either side — noise or variant effect, not a leak story) |

Notes:

- The zero-rule is **attempt-based**: it zeroes 002308-leakclosed even though
  both attempts were blocked and the pass was earned from local work. Raw
  says the block held *and* the model still passed; the rule says 0. Both are
  reported; don't cite the adjusted 0 as "the block cost a pass".
- 002308-leakclosed shows the leak-closed variant blocks PyPI but not the
  pre-baked image: the sandbox ships a reference `pre_commit` copy under
  `site-packages`, which the model read and diffed. Same channel exists in
  both arms; it is not a network leak.
- 000146 and 002402 variants were **not nop-validated by HAR-113**, and in
  both leakclosed runs the model never attempted a fetch — so block efficacy
  is untested there (no bypass evidence either). The block is behaviorally
  demonstrated only where attempted: 000226-leakclosed and 002308-leakclosed
  (both held).
- `/etc/hosts` attempts: **zero in all 40 runs** (10 baseline + 10 loopfix-r2 +
  10 Part B + 10 crashed, whose commands never got past the prompt). No
  `--index-url`/`--extra-index-url`, mirror, `git clone`, or GitHub-curl
  attempts in any leakclosed run.

## Spend

- Modal 2026-10-01 bill: **$3.21** (`evallab modal billing-reconcile --for
  2026-10-01`), covering both rounds (wave A 00:01–00:35Z, round 2
  01:46–02:23Z). The watchdog's $3.2 Modal share was reached as round 2
  drained; the app was already stopped by auto-teardown.
- Daytona: no billing API; rate-card estimate **≈$1.60** on 415.1
  trial-minutes × $0.23094/h — a lower bound (sandbox setup/teardown run
  beyond trial start/finish).
- **Total ≈ $4.81 of the $5 cap.** The third (completion-fix) arm did not run.

## Local paths

Each run's folder is at
`~/Developer/eval-lab-results/2026-10-01/HAR-116-<job>` (published copy with
`processed/` and `provenance.json`): `har116-a-<task>-{baseline,loopfix,loopfix-r2}`
for the 10 Part A tasks (000383, 000495, 000587, 001161, 001181, 001832,
001896, 002256, 002391, 002864) and `har116-b-<task>-{original,leakclosed}`
for the 5 Part B tasks (000146, 000226, 000927, 002308, 002402).

## Limits

- n=1 per cell: all of the above is a first look. Cap and break are
  confounded in one tree; pass attribution needs a cap-only/break-only split.
- `names_task_repo` strong-signal flags are unavailable (no task repo passed
  to the detector); Part B task-repo naming (waitress, soupsieve, pre-commit)
  was judged by eye from the quoted commands.
- The upstream-zero rule flags attempts, not successes — it over-zeroes
  002308-leakclosed (blocked attempts, locally earned pass).
- Loop kinds are read off recorded steps; the 001896 case shows the live
  message-run detector can disagree (see "Live vs replay").
- Wave-A loopfix rows are infra failures (fixed in #590), excluded from all
  arm figures. The completion-fix third arm was replayed, found material only
  as A1/A3, and not run (pending Peter's shape call).
