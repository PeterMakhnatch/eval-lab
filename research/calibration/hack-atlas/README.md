# Hack atlas (reward-hacking and bad-task patterns)

Reusable knowledge corpus for offline reward-hacking work: detecting cheating
in agent trajectories, filtering bad SFT data, auditing bad tasks. No RL.

## What it is

`atlas.yaml` (schema `evallab.hack_atlas/v1`): 8 agent patterns
(`verifier_access`, `answer_leak`, `upstream_fetch`, `grader_tamper`,
`hollow_solution`, `constraint_violation`, `false_claim`, `none`) plus 8
task patterns (`task_answer_in_env`, `task_writable_grader`,
`task_weak_assertion`, `task_underspecified`, `task_broken_verifier`,
`task_unsolvable`, `task_network_open`, `task_no_resource_limits`).
Each pattern carries: definition, legit lookalikes (false-positive guards),
trace signals, paraphrased examples, prevalence with citations, upstream
taxonomy map, detection pointers, atomic System One question cards, refs.
Plus a top-level `router_question` (8-way choice) and `any_cheat_question`.

## How each consumer uses it

- **Kev/Jev question cards**: copy a pattern's `questions` verbatim into
  `POST /v1/systemone` as `state` + typed questions (`noul`/`choice`/
  `score`). Agent cards read a ~6k-token trajectory excerpt; `task_*` cards
  read task package text (instruction + tests + Dockerfile). Ask the
  per-pattern cards and threshold each on negatives from the same corpus;
  measured below, the generic `any_cheat_question` and the router's top-1
  label are weak on unseen benchmarks.
- **LLM-judge rubrics**: generate rubric criteria from `definition` +
  `trace_signals`; generate few-shot negatives from `legit_lookalikes`
  (critical: benign debugging superficially resembles cheating).
- **Deterministic rules**: implement `trace_signals` as tripwires; close the
  loop per `detection.rule`/`probe` (Eval Lab module or `none` = gap).
- **SFT filtering**: drop or mask (cf. SRFT) steps matching agent patterns;
  quarantine tasks matching `task_*` before they seed training.

## Coverage matrix (detectors-v1 scorecard, 484 traces)

| pattern | rule | probe | judge | kev |
|---|---|---|---|---|
| verifier_access | yes (`live_watch` hidden/log hits) | yes (`verifier_peek`) | ok (harbor_analyze tw 16/19, 1/18 FP) | measured (see below) |
| answer_leak | yes (`task_lint` git/answer) | yes (`git_history`) | ok (harbor_analyze) | measured (see below) |
| upstream_fetch | yes (`upstream_fetch`, `copy_check`) | yes (`upstream_fetch`) | PARTIAL (see gaps) | measured (see below) |
| grader_tamper | yes (integrity core + watch) | yes (9 tamper attacks) | ok (harbor_analyze) | measured (see below) |
| hollow_solution | GAP (no rule) | GAP | PARTIAL (metr only) | measured (see below) |
| constraint_violation | GAP | GAP | weak | measured (see below) |
| false_claim | GAP | GAP | noisy (laminar FP) | measured (see below) |
| none | negatives must not fire | n/a | must score low | measured (see below) |
| task_* (all 8) | yes except underspecified/unsolvable | partial (git, reward_plant, upstream) | n/a (audit use) | measured: chance-level |

## Known gaps

- LLM judges catch ~0-20% of `copy_upstream` on Eval Lab traces
  (harbor_analyze ours 2/10); `laminar_copied` covers it instead (11/11).
- Rules catch only 12/100 on tw with 24/100 FP; tw-raw is judge territory
  (harbor_analyze 10/10, metr 30/32 at 5% FPR, 0 FP).
- `laminar_false_completion` is noisy (tw-raw 4/10 hits, 5/10 FP).
- No rule/probe exists for `hollow_solution`, `constraint_violation`,
  `false_claim`, `task_underspecified`, `task_unsolvable`,
  `task_no_resource_limits`; `task_weak_assertion` has V5/V6 scan only.
- Karotte's Harbor comparison is a vendor claim, unverified by us.

## Measured with Kev-4B (zero-shot, 2026-10-09)

AUROC of the first noul card per pattern (v1 wording: `instructions` + `criteria`
text, the exact strings in `research/experiments/kev-hack-detector/questions.json`).
Window level on CheatBench held-out families (SWE-bench Verified, SWE-rebench,
CyBench; 138 / 672) and held-out TB2 tasks (42 / 48); trace level (max over windows)
on detectors-v1 `tw` (100 / 100) and `ours` (11 / 13). Details and caveats:
[the experiment README](../../experiments/kev-hack-detector/README.md).

| card | test families | dev TB2 | tw | ours |
|---|---|---|---|---|
| any_cheat | 0.50 | 0.92 | 0.86 | 0.76 |
| verifier_access | 0.61 | 0.73 | 0.60 | 0.57 |
| answer_leak | 0.89 | 0.89 | 0.58 | 0.80 |
| upstream_fetch | 0.92 | 0.83 | 0.57 | 1.00 |
| grader_tamper | 0.60 | 0.57 | 0.67 | 0.60 |
| hollow_solution | 0.54 | 0.60 | 0.88 | 0.51 |
| constraint_violation | 0.55 | 0.44 | 0.97 | 0.39 |
| false_claim | 0.47 | 0.45 | 0.17 | 0.80 |
| router 1 − P(none) | 0.65 | 0.97 | 0.94 | 0.74 |

Cards work where the hack is a concrete action named in the card (`answer_leak`,
`upstream_fetch`). On Terminal Wrench several cards fire on any hack family, so a
high score means "suspicious", not that pattern. Task cards scored 0.25–0.71 on 13
EnvCheck-defect vs 26 finding-free Terminal-Bench tasks: not usable yet.

## Provenance

Sources read 2026-10-09 from
`/Users/petermakhnatch/Developer/research-context/reward-hacking/`:
INDEX.md, CATALOG.md, both 2026-10-08 omp-conversations (eval-vs-training,
benchmarks-landscape), Ari post, Terminal Wrench paper + repo card, CAIS
CheatBench paper, steinad/CheatBench card, Karotte deep dive, Vals MiMo
posts, Unearned Passes, Fake-hardness, EnvCheck, Cursor, METR, Anthropic
misalignment, Countdown-Code papers; Eval Lab `detectors.py`,
`live_watch.py`, `integrity_reward_core.py`, `cheat.py`, `copy_check.py`,
`upstream_fetch.py`, `reward_hack.py`, `audit_mimo.py`, `task_lint.py`.
Facts cite sources inline; [INFERENCE] marks our reasoning.
