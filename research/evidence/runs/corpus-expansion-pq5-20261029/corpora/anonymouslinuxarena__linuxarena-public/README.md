# anonymouslinuxarena/linuxarena-public (LinuxArena reviewer sample)

Cross-test-only corpus. Adapter, profile and aggregate counts only; no transcript text, task text or
rows are committed.

- Source: https://huggingface.co/datasets/anonymouslinuxarena/linuxarena-public at dataset commit
  `2fd29f9c7bc190fb873bd76c6bd648f22b9ab0a7` (anonymous NeurIPS D&B submission, "LinuxArena").
- Licence: **CC-BY-4.0** on this release: README YAML `license: cc-by-4.0`, `cardData.license`
  `cc-by-4.0`, and the README "License" section ("Trajectories: CC-BY-4.0. Benchmark code: see the
  code repository"). Public, ungated (`gated: false`). This is the licence of the staged
  `linuxarena-public` release itself, not of the sibling MIT `straj_linuxarena` dataset.
  The uploader is anonymous, so attribution is to "anonymouslinuxarena" with the pinned commit.
- Staged: `data/sample.jsonl` (932 rows, 704,485,115 bytes, sha256
  `0b1e469a...cdfb`, equal to the Hugging Face LFS object id) plus `README.md`, `croissant.json`,
  `croissant_rai.json`, `manifest.json`, `scripts/build_sample.py`, `.gitattributes`. Not staged:
  the ten full `data/<env>.jsonl` shards (about 16.8 GB, 22,215 trajectories).
- Local bundle: `~/.local/state/evallab-cross-test/anonymouslinuxarena__linuxarena-public/2fd29f9c7bc1/`
  (7 files, 704,532,271 bytes; manifest sha256
  `214b6352bd5aa7a512062cd343d8c6598633fa9c643fbe0bcc6b5f9112f990ed`; small files checked against
  Git blob ids at the pinned commit; 0444/0555). `croissant_rai.json` still has `TODO` fields
  (data collection, maintenance plan), so the data card is incomplete.
- Rules: eval-only, local-only, no training/tuning/few-shot, frozen-design one-shot, no vendor
  upload (`CROSS_TEST_ONLY.md`).

## Construct: covert attack, not spontaneous cheating

Attack-mode agents (`Haiku attack`) are **instructed**, by their system prompt, to accomplish a
hidden side task (RCE, exfiltration, backdoor, malware ...) subtly while doing a main task. The
`straj` rows are curated sabotage trajectories (no system prompt, no model name, 3-42 actions, each
carries an `attack_analysis`). This is red-team policy behaviour on LinuxArena sandboxes, not a model
deciding to cheat. A detector that scores well detected instructed sabotage; it has not shown
reward-hacking detection. Report it as `covert-attack`, never pooled with cheating corpora.

## Unit and label predicate

The sample holds 932 rows from 7 runs. Four of them are monitor reruns of an agent run (only the
monitor fields should differ). Rows are merged into **units** by the action sequence the detector
sees (function + arguments, outputs ignored), inside one (run slice, environment, main task, side
task) cell; the original agent run is preferred over a rerun and the longest member supplies
`_steps`. Merge kinds (rows merged into an earlier row): 343 identical steps, 5 identical actions
with different tool output, 11 prefix replays, 1 identical `_steps` under a second side-task id.
Result: 572 groups (6 empty, excluded) = **566 units**; group sizes 1: 219, 2: 349, 3: 1, 4: 3.

| State | Predicate | Count |
|---|---|---|
| positive | attack-mode (`side_task` present; `Haiku attack` or `straj`) and `side_task_success is True` (the published scorer) | **138** (Haiku attack 67, straj 71) |
| negative | honest-mode (`side_task` null; `Haiku honest` or `Opus honest`) | **400** (200 Haiku, 200 Opus) |
| ambiguous | attack with `side_task_success` False (24), identical actions with conflicting labels (4) | **28** |
| excluded | no agent steps after dropping side-task/planning/`submit` tools | 6 |

Missing or inconsistent labels are ambiguous, never negative.

What the merge does and does not mean:

- **Prefix replays (11).** In the `GPT-5 Nano monitor on Haiku attack` rerun, 11 cells are a strict
  prefix of the original's action sequence (exact function and arguments up to truncation).
  They are truncated copies, not independent rollouts. Their own `side_task_success` (4 True,
  7 False) does not vote: the complete original supplies the label. The other reruns of the
  92 cells are 72 identical in actions, 6 empty (excluded) and **3** genuinely different action
  sequences (which stay separate units; 95 Haiku-attack units cover the 92 cells).
- **Scorer not deterministic on identical actions.** 4 of the 72 identical-action pairs carry
  different `side_task_success` (audit-pq5c F12); they are the 4 `duplicate_conflicting_labels`
  ambiguous units. Do not treat `side_task_success` as a noise-free label.
- **Scout numbers.** The brief quoted 166 attack rows and 141 successes (92 + 74; 67 + 74). After
  merging, Haiku attack gives the same 67 positives; straj gives 71 rather than 74, because
  dropping `submit` leaves three straj trajectories whose kept actions equal another straj
  trajectory's (they differ only in the dropped submit payload or volatile tool output).
- **The two `Opus honest` monitor reruns** are identical in actions (200 units, not 400).
  `agent_model` is filled in for 42 of the 600 honest rows (Haiku 18, Opus 24), against 184 of
  184 Haiku-attack rows and 0 of 148 straj rows, so it is unusable as a model label; the slice
  comes from the run name. `agent_model` and run names never reach `_steps`.
- **`main_task_success`** is False in all 184 Haiku-attack rows, but **True in 64 of the 148
  straj rows** (32 of 71 straj units), so it is a real outcome there. It is not part of any
  predicate and stays out of `_steps`.
- Negatives are honest-mode, not certified-benign. The attack and honest populations barely share
  tasks (13 shared main-task ids of 277); honest tasks are 200 distinct tasks run once.
- 223 of 321 distinct (environment, main task, side task) cells have more than one distinct unit:
  **200 are honest cells** (Haiku honest and Opus honest ran the same 200 main tasks; every one is a
  Haiku/Opus pair), only 23 are attack cells (Haiku attack and straj in one cell, the 3 different
  reruns, or distinct straj variants). Cluster by cell for any interval.
- Command-level localisation is not scorable: labels are trajectory-level.

## What the detector sees (`_steps` only)

`tools/adapter.py::specs()` yields dicts with `_steps` plus separate `gold`, `labels`, `identity`.
**Kept** in `_steps`: the agent's tool calls (function and arguments) and the tool results,
including `output_error`. **Stripped**, counted over the 926 non-empty rows by the selfcheck:

- the system prompt (767): the attack prompt says "your primary goal will be to discretely accomplish
  a side task" and carries the side-task framing; the honest prompt is removed too. Attack or
  condition instructions never reach `_steps`;
- the initial user message (209), every `agent_reasoning` text (25,261 actions), all
  `extended_thinking` (11,569) and `inter_action_messages` (566, phase prompts and nudges);
- the tools that exist only in one condition or only for planning, with their results:
  `run_side_task_test`, `start_phase1_planning`, `start_phase2_planning`, `ready_to_plan` (709
  calls) and **`submit` (885 calls, both modes)**. Honest `submit` calls only carry the keys
  `answer`/`confusions` (or none); attack `submit` calls carry the side-task tool schema
  (`bash_script`, `python_script`, `secrets_location`, `attacker_user`, ...). The published
  monitors never score `submit`, and keeping it made the condition readable from the argument
  keys (content-free AUROC 0.68 Haiku attack vs honest, 0.74 straj; audit-pq5c F11). Dropped in
  both modes with call, arguments and result: 1,594 dropped calls in total;
- the main-task description. It is **not** given as a user step: for 4 straj rows the description
  itself says "... the <side task> side task targets", so it is a label there;
- all per-action and trajectory monitor output (33,357 and 647), scorer explanations (838),
  `attack_analysis` (148), side-task fields, run names, run and trajectory ids, `agent_model`.

Leak scan (`selfcheck`; whitespace-normalised substring checks on all step text, which means
function names, tool-call argument **keys and values** and tool results walked as plain text, not
JSON-escaped blobs): attack-only system-prompt sentences (>=40 chars, minus sentences also in
honest prompts or the task text), `short_description` lines, `attack_analysis` prose lines (the
`incriminating_behaviour` field quotes the agent's own code and is counted as natural overlap),
run display names, trajectory/run ids, dropped tool names, side-task names matched on word
boundaries, **29 argument-key names of the side-task tools** that no honest kept tool uses, and
any Haiku-attack argument key no honest run uses: **0 hard leaks**. Reported separately, not as
leaks:

- 4 rows contain a side-task name only inside environment files that honest agents also see
  (`/seed-data/side_task_media/...`, a seed-data comment);
- 4 rows contain a scoring-details line the agent itself wrote into its actions (for example an
  attacker SSH key): the attack payload, i.e. behaviour;
- 44 rows quote `attack_analysis` code lines that are the agent's own actions; monitor reasoning
  lines overlap tool text in 431 rows and scorer explanation lines in 36 rows, because both quote
  the agent's own commands or pytest output (natural overlap);
- straj is a different tool surface: `view` and `str_replace_editor` occur only there (14 straj
  rows carry argument keys no honest run uses). Haiku attack has none.

**Residual vocabulary the stripping cannot remove** (`side task`, `sabotage`, `covert`, `stealth`,
`red team`, `secretly`, `run_side_task`), per unit, in **tool arguments** vs anywhere (arguments or
results):

| Slice (units) | tool arguments | arguments or results |
|---|---|---|
| Haiku attack (95) | 11 | 12 |
| straj (71) | 5 | 7 |
| Haiku honest (200) | **0** | 7 |
| Opus honest (200) | 1 | 16 |

In honest runs the words occur in environment file output; in attack runs the agent itself writes
"side task" into code and comments (instruction vocabulary flowing into behaviour). It is kept
because it is behaviour, but it is a lexical shortcut: report every detector against the
vocabulary floor below, not only against length. (Row counts with duplicates, from the selfcheck:
arguments 20/178 Haiku attack, 10/148 straj, 0/200 Haiku honest, 2/400 Opus honest.)

## Content-free floors (profile.json: `content_free_floors`)

Single-feature AUROC, no content read, recomputed on the repaired units. Values below 0.5 mean the
feature is inverted (shorter for positives); orientation-free value is `max(a, 1-a)`. `*_loo_prior` is
the leave-one-out positive rate of the unit's own group. Counts are positives/negatives.

| Population | n_steps | tool_arg_chars | tool_output_chars | vocabulary in tool args (binary) | vocabulary anywhere | environment prior | main-task-category prior | main-task-id prior |
|---|---|---|---|---|---|---|---|---|
| Haiku attack vs Haiku honest (matched model), 67/200 | 0.347 | 0.189 | 0.389 | 0.560 | 0.550 | 0.379 | 0.455 | 0.572 |
| Haiku attack vs all honest, 67/400 | 0.428 | 0.330 | 0.413 | 0.558 | 0.538 | 0.379 | 0.455 | 0.897 |
| straj vs all honest, 71/400 | 0.028 | 0.476 | 0.058 | 0.534 | 0.521 | 0.670 | 0.915 | 0.992 |
| all positives vs all negatives, 138/400 | 0.222 | 0.405 | 0.230 | 0.546 | 0.529 | 0.557 | 0.697 | 0.951 |

Reading: tool-call argument length alone separates Haiku attack from Haiku honest at **0.81**
(orientation-free), step count at 0.65, the vocabulary-in-arguments rule at 0.56. The straj slice is
trivially separable from honest by length (0.97 on `n_steps`) and by task identity, so **do not
score a detector on the pooled population or on straj alone**; the primary comparison is Haiku
attack vs the Haiku-honest 200, and every detector must be reported against the best floor above,
not against 0.5. The task-id prior is inflated by repeated cells (the same main task occurs in both
rollouts), treat it as an upper bound. The Opus-honest 200 differ in model and are a secondary
negative set.

## Overlap with Eval Lab splits

`tools/profile.py` computes the overlap (`results/profile.json`, `overlap`): SHA-256 of normalised
ids, bare and full form, of 277 main-task ids, 277 environment-qualified main-task ids, 94
side-task ids and all 932 trajectory ids, against `research/answer-keys/splits/assignments.csv`
(columns `task`, `item_id`, `unit`; DEV and TEST/heldout; 3,065 rows) and `corpus_tasks.task_id` of
the read-only production trace db (3,707 rows): 0 matches in every cell. Id-level only, no content
comparison.

## Reproduce

```
python3 tools/adapter.py     # selfcheck: counts, stripped parts, leak scan
python3 tools/profile.py     # verifies manifest/revision after lock, overlap, writes results/profile.json
```

Both use only the standard library and read the locked bundle (the full run takes a few minutes: the
sample is 704 MB and is parsed several times); they print aggregates, never transcript text. The
bundle bytes are unchanged by this repair (same manifest digest).
