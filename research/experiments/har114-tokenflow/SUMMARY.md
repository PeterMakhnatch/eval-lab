# HAR-114 token flow: where the distill's budget goes (items 1-2)

$0, read-only. Method: new `evallab.token_flow` module (wired into
`process-job` as `record["token_flow"]`, schema `token_flow/v1`) run over
every trial below via `analyze_runs.py` in this directory. Outputs:
`runs.jsonl` (82 full records), `runs.csv` (compact per-run table),
`aggregates.json`. Definitions and limits live in the module docstring;
key points: last useful edit = last **detected** file-writing signal (edit
tool, write signal, shell redirect, shared traj patterns + bare-redirect
supplement). Read/scratch filtering and `verifier/agent.diff` checks are
heuristics, not proof that every anchor is a real repository edit.
Loop onset = earliest ≥4 normalized-command run or ≥10 identical-message
run (mirrors `traj._analyze_loop_suspicion` / `probe03.identical_runs`,
same `LOOP_MIN_RUN=10`); missing inputs are `None` with a reason, never 0.

HAR-132 replay reproduced all 82 frozen `token_flow` and savings records.
Four zero-share anchors are false positives: quoted awk comparisons in
trials ending `__Y8fE5ft`, `__RZ8qyUu` and `__LRiiKmy`, and socket/HTML
`.write` text in `__XuSksWC`. The frozen records are retained, not hand-edited.
Also, 23 trials have no detected edit and contribute a post-edit share of
1.0 by definition. The aggregate describes this detector, not a verified
fraction of wasted compute.

## 1. Coverage: 82 distinct trials, not 40

The card says "30 HAR-110 v2 runs plus 10 HAR-104". The v2 table
(`research/experiments/har110-python-gepa/results-v2-trials.jsonl`) actually
has 36 cells (18 dev + 12 held-out + 6 v1 extras), and this sweep covers all
of them plus extras:

| source | arm | n | what |
|---|---|---|---|
| HAR-110 | gepa-candidate | 10 | 5 dev + 001896 + 4 held-out, approved prompt |
| HAR-110 | seed-addendum | 10 | 6 dev + 4 held-out, HAR-85 addendum seed |
| HAR-110 | plain-heldout | 4 | plain Terminus on the 4 held-out tasks |
| HAR-110 | seed-addendum-v1 | 3 | v1 split leftovers (000226/002259/002407) |
| HAR-110 | aborted-v1-seed | 1 | `_aborted-…/…-002-df7dfd07…` finished trial (v1 seed 002407), labelled |
| HAR-104 | plain-dev | 10 | the 10-run batch incl. 000927 (no v2 row) |
| HAR-81 | l-d-a2/a3/a4 | 24 | same model, SFT harness settings |
| HAR-81 | p-d-arvo/candidate/format | 20 | same model, SFT harness settings |

Excluded: 13 `gepa-har110-python-train-search/attempt-*` dirs — GEPA
optimizer records (`status: pending_evaluation`, no agent trajectory), not
trials. Golden run = `har81-l-d-a2-arvo-18737` (HAR-106), flagged in the CSV.

Per-arm medians (`ledger_in` = settled proxy input tokens; `after_share` =
input share spent after the last useful edit; onset in calls):

| source | arm | n | med in | med calls | med after_share | no_edit | onset med call | no onset | summ fired/ok | med maxprompt |
|---|---|---|---|---|---|---|---|---|---|---|
| HAR-104 | plain-dev | 10 | 2,362,702 | 93 | 0.90 | 1 | 25.5 | 6 | 3/3 | 44,078 |
| HAR-110 | gepa-candidate | 10 | 2,352,896 | 87.5 | 0.90 | 4 | 22 | 2 | 2/2 | 41,980 |
| HAR-110 | plain-heldout | 4 | 2,359,156 | 85 | 0.50 | 0 | 57 | 1 | 1/1 | 49,633 |
| HAR-110 | seed-addendum | 10 | 2,364,902 | 91 | 0.80 | 3 | 39 | 4 | 2/2 | 43,361 |
| HAR-110 | seed-addendum-v1 | 3 | 2,363,586 | 77 | 1.00 | 1 | — | 3 | 0/0 (+1 failed, see §3) | 48,488 |
| HAR-81 | l-d-a2 | 8 | 2,359,638 | 89.5 | 0.80 | 0 | 23 | 3 | 1/1 | 40,708 |
| HAR-81 | l-d-a3 | 8 | 2,407,542 | 86 | 0.90 | 2 | 33 | 1 | 1/1 (+1 failed) | 41,514 |
| HAR-81 | l-d-a4 | 8 | 2,387,731 | 88 | 0.90 | 4 | 40 | 1 | 3/3 | 46,290 |
| HAR-81 | p-d-arvo | 7 | 2,407,562 | 91 | 1.00 | 4 | 32 | 2 | 3/3 | 54,935 |
| HAR-81 | p-d-candidate | 7 | 2,379,211 | 84 | 0.80 | 2 | 36.5 | 3 | 1/1 | 35,512 |
| HAR-81 | p-d-format | 6 | 2,376,982 | 83.5 | 0.80 | 2 | 34 | 2 | 1/1 | 43,073 |

Corpus: 66× `trial_budget_exhausted`, 4× `agent_timeout`, 12× clean
finishes with no harness stop label (recorded as `None` with reason
"clean finish, unlabeled" — e.g. the 002864 candidate pass, 12 calls).
Rewards: 15 passes (incl. known tainted 000226 ×2 and leaked 001832),
66 fails, 1 infra-`None` (002256 proxy 502). Loop onset fires in 54/82
(30 normalized-command, 9 identical-message, 15 both), median call 33.
23/82 runs have no detected repo edit at all.

## 2. Harness or model? Both, in this order: model loops, harness re-sends

**Prompt growth did NOT change with the harness tree.** HAR-81 (SFT tree:
`trajectory_config` `linear_history+raw_content`) vs HAR-104/110 (Harbor
defaults): median slope 368 vs 348 tokens/call, median max prompt
40,122 vs 44,576, median calls 88.5 vs 88.5, median prompt 24,850 vs
24,692. Same model, same growth; the tree only changes trace layout
(HAR-81 splits `trajectory.cont-N.json`, defaults keep one file).

**Summarisation fires, succeeds, and shrinks — but never saves the run.**
19 events across all arms (threshold 16384, fires at 52–58k used):
every one succeeded, 52–58k → 1.9–4.9k (~13×; median reduction 50,700;
kept = handoff summary + 3 subagent Q&A trajectories on disk). All 19
runs still ended `trial_budget_exhausted` (3 passed anyway). Post-handoff
prompts regrow a median +21k (max +35.8k) because the loop continues.
2 more summarizations failed outright (counter incremented, no handoff
system step): `gepa-…-002-12c6…` (seed-v1 002259) and
`har81-l-d-a3-arvo-42485576` — the harness counts attempts before the
subagents run, so metadata > events means failure.

**The ceiling is hit through looping, amplified by full-history re-send —
never through context.** Over all 82 runs the largest real prompt is
58,551 tokens (`har81-l-d-a4-arvo-42496599`, call 59 / step 60), inside the 65,536
context; the reactive `ContextLengthExceeded` path never fired. What binds
is the *attempted* footprint (used + byte-based reservation, proxy-side):
the 66 exhausted runs have median **2,385,430 settled ledger input tokens**
and **91 metered calls**, below both the 2.5M and the 120-call line, when
one more reservation would cross it (cf. §4). Across **all 82** runs, median
post-edit share is **89.29%** (coarsely 90%), using native `agent_result`
input-token denominators, not proxy reservations. Median onset is call
**33** among the 54 trials with an onset; median metered length over all
82 is **88.5** calls. These are different cohorts. Step refs: golden run last
edit step 11 (`sed -i … stun.c`, call 10), onset step 24 (`echo
"task_complete"` ×95, calls 23–117 — the same 95× run HAR-106 hand-found),
96.7% of its 2.41M input tokens after the edit, max prompt only 29,836.
`har110-dev-002391`: last real edit step 46 (`open(path,"w").write` on
`pip_audit/_audit.py`, call 45, persists in final diff), onset step 59
(10× identical message), 59.6% after, max prompt 47,486.

## 3. The 171,301-token call (002391 call 79): confirmed byte reservation

From the trajectory (not the ledger): 002391's last metered call (call 78,
step 79) used **47,486** real prompt tokens — inside the 65,536 context.
171,301 / 47,486 = 3.61×. Same check on 002256 call 54: trajectory prompt
**6,003** vs 24,048 reserved = 4.01× (ledger figure from the proxy slice,
PR #566). So the reservation is a ~3.6–4× byte-length upper bound
(`_estimate_tokens` = JSON byte length), **not a prompt size**. The real
prompt never exceeded ~58.5k in any of the 82 runs. The failed call's held
reservation is what starved the budget: 002256 carries 27 unresolved
reservations, so its "attempted" footprint (not its 2.3M settled tokens)
tripped the gate — mind this in stop attribution: `*_attempted_*` ledger
fields include held bytes, trajectory/agent_result totals do not.

## 4. Expected savings of 4 candidate changes (for the item-5 proposal)

Methods in `analyze_runs.py::_savings`; per-run values in `runs.csv`.
Baseline: 163.7M settled input tokens over 82 runs. "Would-avoid" =
exhausted runs whose used − saving + 3.6× last-prompt reservation falls
back under 2.5M (and under 120 calls for C/D, which also save calls).

| # | change | method / key assumption | total saved | median/run | would-avoid |
|---|---|---|---|---|---|
| A | cap terminal output fed back at 2000 chars/step | excess chars × later calls ÷ 4, reset at summarisation handoffs; top-5 outputs only (lower bound) | 25.3M (15%) | 317k | 66/66 exhausted* |
| B | summarise at 32k used instead of ~49k | handoff to ~4k, regrow at run's own slope (cap 53k = second fire), stop at actual fire | 50.7M (31%) | 755k | 60/66 |
| C | stop 5 calls after last useful edit | exact sum of later metered prompts; n/a without an edit (59/82) | 61.8M (38%) | 948k, −41 calls med | 38/44 eligible |
| D | stop 5 calls after loop onset | exact sum of later metered prompts; n/a without onset (54/82) | 78.6M (48%) | 1.64M, −52 calls med | 51/51 eligible |

\* A avoids the *token* overshoot (runs die a median ~17k over the
attempted line, killed by one byte-counted reservation); the run would
then continue toward the 120-request ceiling — A buys room, C/D end the
run. Med top-1 fed-back output is 10,068 chars (~2.5k est. tokens,
re-sent every later call).

## 5. Limits

- Edit detection is heuristic (shell is expressive): `mv`/`cp` over source,
  debuggers, and unquoted-variable `open()` targets are missed; `verifier/`
  basenames can false-positive. Persistence is checkable only where
  `verifier/agent.diff` exists (HAR-104/110; HAR-81 verifiers keep no diff
  → 44× `None`): True 20 / False 6 there.
- Loop onset flags long verification tails too (repeated passing tests
  after the fix); read it with the last-edit step. 28/82 runs show no
  ≥4/≥10 run — some still burn out on varied-but-fruitless commands.
- Terminal sizes are trajectory-stored chars; `est_tokens = chars // 4`
  is an assumption. Prompt series = metered agent steps; unreconciled
  reservations (002256 ×27, 002391 ×1) never appear in it.
- Savings are counterfactual estimates, not measurements: A/B assume the
  agent behaves identically on smaller prompts (it would not — that is the
  point, direction unknown); C/D assume the tail had no value (late passes
  exist: 2684-security-appsec fixed at call 78/84, 1271-media-games at
  91/95 — a stop rule needs a verifier/finish signal, not just a counter).
