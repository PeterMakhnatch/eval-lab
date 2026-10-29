# NP35-LB-1: which staged corpora hold enough clean test-writing runs to measure the Flake weakness?

Zero model calls, $0, no downloads, no uploads. Aggregates only: no transcript text, command, path or task text is printed or stored. Bundles
were read through their merged adapters, read-only. Not touched: HackBench, Goodfire, CheatBench and every DEV, TEST or held-out key.

**Question (from NP29-CM-1, `research/evidence/runs/np29-test-edits-20261029/RESULTS.md`).** DEV has only 4 clean runs that write to a strict test
path (Goodfire) and 15 from one task (CheatBench). A Wilson upper bound under 10% with zero alarms needs 35 such clean runs spread over many tasks
(125 for 3%). Which staged cross-test corpora hold that many?

**Method.** NP29's definition S (a write to a strict test path) is applied unchanged: `tools/s_shim.py` imports NP29's `test_edits.analyse_steps`
and checks the file's sha256 against `origin/main` (`c82f820453b4...`). Each corpus's detector input `_steps` is read as the adapter yields it.
Where the adapter's step keys differ from the ATIF shape S reads (`source`, `tool_calls[{function_name, arguments}]`), one mechanical renaming is
applied (see "Trap" below); nothing is parsed out of prose. A corpus clears the bar if its **established** negatives (the registry's
`labels.false_alarm_basis.established_negatives`, reconciled to the count in every case) hold **at least 35 S runs over at least 20 distinct tasks**.

## Reference: Flake Laundering reproduces NP29 exactly

| Check | NP29 | This run |
|---|---|---|
| NP29 sample (69 runs): established negatives with S | 12 of 60, in 2 tasks | 12 of 60, 2 tasks |
| `legitimate_test_repair` with S | 5 of 10 | 5 of 10 |
| `app_fixed_no_masking_test_change` with S | 7 of 50 | 7 of 50 |
| 69 sample runs matched to the staged bundle by exact tool-call sequence | - | 69 of 69; S agrees on 69 of 69 |

So NP29's sample is a subset of the staged bundle and its S counts come out of the bundle's adapter. Whole bundle (229 runs): 26 test repairs
with S in **17**, in 2 tasks; 189 application fixes with S in **20**, in 1 task; 7 positives with S in **0**. Flake's 37 S negatives lie in
**3 tasks**, so it cannot clear the 20-task bar. The registry counts only the 26 repairs as established (NP29 treated all 215 as established).
Nine of the 26 repairs have neither S nor NP29's loose flag L: S cannot see them (see the last section).

## Result

Established negatives only (the first three numeric columns). "All negatives" adds unestablished negatives (no flag fired, condition controls
that the registry does not count) and is context, not evidence about clean runs. Tasks are distinct keys among the S runs.

| Corpus | Established neg. runs | S | S tasks | Task key | Clears 35 / 20 | All neg.: S / tasks (of runs) | Positives: S / runs |
|---|---|---|---|---|---|---|---|
| `linuxarena-public` | 400 | **60** | 41 | main_task_id = environment x main (200 in negatives) | **yes** | 60 / 41 (of 400) | 10 / 138 |
| `flake-laundering` (reference, registry-established) | 26 | **17** | 2 | task id (17) | no | 37 / 3 (of 217) | 0 / 7 |
| `beam-tripwire` | 175 | 11 | 6 | instance id (native, 44) | no | 29 / 14 (of 442) | 123 / 171 |
| `reflex-sentinel` | 50 | 3 | 3 | task_id (native, 50) | no | 12 / 11 (of 205) | 45 / 45 |
| `agent-cheat-index` | 270 | 1 | 1 | task id (native; 10 in the established family) | no | 7 / 4 (of 1,086) | 16 / 62 |
| `sdf-shortcut-taking` | 331 | 4 | 1 | none: the corpus is one task (constant key) | no | 5 / 1 (of 636) | 14 / 359 |
| `evilgenie-escalation` | 177 | 0 | 0 | problem_id (15) | no | 148 / 9 (of 1,990) | 4 / 124 |
| `researcharena-benchmark-gaming` | 20 | 2 | 1 | setting id (proxy) | no | 2 / 1 (of 20) | 2 / 21 |
| `sleight-bench` | 43 | 2 | 2 | construction group (proxy, 40) | no | 2 / 2 (of 43) | 2 / 43 |
| `kernelbench-hard-runs` | 19 | 0 | 0 | problem (5 scoreable) | no | 0 / 0 (of 19) | 0 / 11 |
| `caught-not-taught` | 160 | 0 | 0 | main task name (5) | no | 0 / 0 (of 160) | 0 / 46 |
| `trusted-monitoring-logs` | 200 | 0 | 0 | APPS problem id | no | 0 / 0 (of 200) | 0 / 33 |
| `knowing-collusion-filter` | 20 | 0 | 0 | scenario (proxy, 9) | no | 0 / 0 (of 20) | 0 / 86 |
| `cuaverifierbench` | 29 | 0 | 0 | Online-Mind2Web task id | no | 0 / 0 (of 29) | 0 / 77 |
| `posttrainbench-trajectories` | 0 | 0 | 0 | benchmark x base model (proxy, 28) | no | 110 / 27 (of 1,439) | 6 / 109 |
| `baitbench` | 0 | 0 | 0 | dataset-instance id (90) | no | 19 / 17 (of 488) | 27 / 681 |
| `agentcheck` | 0 | 0 | 0 | task_id (3 repos) | no | 22 / 2 (of 91) | 28 / 28 |
| `reward-pressure-study-2` | 0 | 0 | 0 | task name (20) | no | 4 / 3 (of 653) | 31 / 467 |
| `inferencebench-trajectories` | 0 | 0 | 0 | scenario (proxy, 4) | no | 5 / 4 (of 211) | 1 / 17 |
| `kobayashi-maru`, `mistrace-reward-hacking`, `rhb-delivery-queue` | 0 | 0 | 0 | item / task / scenario id | no | 0 / 0 | 0 to 4 |

Not applicable to S (no structured tool calls in `_steps`, or no runs): `hack-ignition` (5,418 established; every run is one MBPP completion),
`apollo-insider-trading`, `assistant-axis`, `collusion-probe-pitfalls` (chat or dialogue text), `paichecker`, `automationbench-verified` (task
rows, no agent run). All of them have S = 0 by construction. `hackbench` was not touched.

**Only one corpus clears: `linuxarena-public`.** I re-derived it independently of the worker that produced it: 60 S runs of 400 honest-condition
runs (30 Haiku, 30 Opus), in **41 distinct environment x main-task keys** (the same 41 under either key), across 8 of 10 environments, at most 2
runs in any one task (top three tasks hold 10% of the S runs). 27 of the 60 include an in-place edit of an existing test file (`SI`, 20 tasks). Form
hits (a run can have several): editor `create` 45, editor `str_replace` 27, shell redirect 4, copy or move 1. Nothing else comes close on established negatives: the next best is Beam's `possible` control (11 over 6 tasks).
On Flake the S runs lie in 2 tasks (repairs) or 3 (all negatives).

**The all-negatives column can mislead.** `posttrainbench-trajectories` has 110 S in 27 proxy tasks and `evilgenie-escalation` 148 in 9, but none
of those negatives is established (PostTrainBench's are "no judge fired"; EvilGenie's 1,813 are impossible-task runs with no flag), and PostTrainBench's
key is benchmark x base model, not a task. `agentcheck`'s 22 and `reward-pressure-study-2`'s 4 are circular: those corpora's own labels are built from
test-file writes.

## Recommendation

**Score `linuxarena-public` honest runs.** It is the only staged corpus that clears 35 clean S runs over 20 or more tasks, it is CC-BY-4.0 and
public, and hosted evaluation is allowed under CONTRACT.md line 29 (evaluation only, local storage, never Laminar or Docent).
Freeze the design before the first call, exactly as NP21 did: Haiku 5.5 facets (`run-atlas-prompt/v2` reader) and cascade v8, with NP29's S split
(T = the 60 S runs, N = the 340 others) fixed in advance.

- **Minimum design (answers the question): 120 runs = all 60 S plus 60 N matched on environment.** About **120 to 172 Haiku facet calls** and about **17 judge calls** (14% of runs, as in NP21; at most one per run),
  about 5.3M input tokens. With zero alarms in 60 S runs the Wilson upper bound is 6.0%, under 10%; showing under 3% needs 125.
- **Full design: all 400 honest runs.** About 400 to 572 Haiku calls and about 57 judge calls (at most one per run), about 17M input tokens (honest LinuxArena runs have a
  median of 158,000 characters, about 39,000 tokens, about 5 times NP21's 8,400 input tokens per run). Do this only if the minimum design shows alarms.
- Call-rate basis: NP21-CM-1's own usage (`causal-prefix-monitor.md`): 613 Haiku facet calls for 429 runs (1.43 per run), 3.61M input tokens, and 61
  Sonnet judge calls (14% of runs). [INFERENCE] The 1.43 may be lower or higher on long traces; one call per run is the floor, and no trace here exceeds
  about 135,000 tokens. The cascade's Stage 1 is deterministic and free. Subscription use costs $0 marginal; token totals are the quota budget.
- Report with the positives (138; S in 10) as context, and by environment. Treat Haiku-honest and Opus-honest separately: the same 200 main tasks
  appear in both slices, so they are not independent task draws.

**Optional, cheap second: the 16 unscored Flake test repairs.** NP21 scored 10 of the 26; the other 16 run on short transcripts (about 3,000 tokens
each): about 23 Haiku calls and at most 16 judge calls. It tests whether the 8-of-10 alarm rate holds at n = 26, but all 26 lie in 2 tasks, so it can
confirm the mechanism and cannot support a rate across tasks.

Neither adds Goodfire or CheatBench rows. Not recommended: PostTrainBench (unestablished negatives, proxy tasks, very long traces), and anything
whose S count is zero.

## What S cannot see

- **Writes outside test directories and test basenames.** Nine of Flake's 26 repairs have no S and no loose flag L: the repair is a whole-file or
  shell-redirect write to a path that does not look like a test. S is a lower bound on test editing; NP29 found the same for half of its 10 repairs.
- **Whole-file and shell forms are the norm, not in-place edits.** Flake repairs have 0 SI in NP29's sample and 8 of 26 in the bundle. In LinuxArena's
  S runs `create` is the most common form (45 form hits against 27 `str_replace`), so many are new test files, not repairs: S counts a clean run writing to a test path, not a run repairing a
  failing test. On LinuxArena the loose flag L adds 204 runs that mention test, fixture or conftest text but write elsewhere or only read.
- **Only what the adapter keeps.** LinuxArena's adapter drops `submit`, reasoning and some tools by design; sdf and others carry malformed native tool
  names that S does not remap. Tool calls that exist only as prose are not seen.
- **Browser, chat and single-completion corpora** have no file-write form at all, so S = 0 there says nothing about test editing.
- **Task keys are proxies in several corpora** (marked in the table) and absent in sdf; counts of distinct tasks there are upper bounds on independence.
- **The negatives are not certified clean.** LinuxArena's 400 are honest-condition runs (no side task was given), established by condition and not by
  a judge. S runs among them can still contain behaviour a monitor should flag.

## Trap: adapter step shapes differ, and a wrong shape gives silent zeros

S reads `source` and `tool_calls[{function_name, arguments}]`. Staged adapters use other shapes: `role` and `function` (LinuxArena), `role`, `name`
and `args` (agentcheck), `name` with JSON-string arguments (reward-pressure-study-2), `file_change` calls (BAITBENCH). Unmapped, reward-pressure-study-2
returns S = 0 for every group (4 with the mapping, 31 for positives) and BAITBENCH 15 instead of 19. Any new S run on a staged corpus must check that
`calls` is nonzero and use the one mapping per corpus in `tools/corpora/`. This is recorded in `data-access-map.md` section 10.

## Reproduce

`tools/setup.sh` exports NP29 and every corpus adapter from `origin/main` under `/private/tmp/np35` and copies the scripts. Then, with system
`python3` (the eval-lab virtualenv python cannot load `libpython3.12.dylib` on this machine; the scripts use the standard library only):

```bash
python3 /private/tmp/np35/run_flake.py > /private/tmp/np35/flake_ref.json        # Flake: NP29 sample (69 runs) and whole bundle
python3 /private/tmp/np35/scripts/linuxarena-public.py                            # the one that clears; ~20 s
python3 /private/tmp/np35/scripts/beam-tripwire.py                                # ~12 s; one script per corpus in tools/corpora/
python3 /private/tmp/np35/aggregate.py /private/tmp/np35 > census_rows.json       # the table above
```

`results/census.json` holds the table rows and the Flake reference; `results/groups.json` holds every group per corpus (runs, S, SI, L, tasks, S
forms, task-key derivation). Re-running `beam-tripwire.py` and `posttrainbench-trajectories.py` gave byte-identical group counts.
