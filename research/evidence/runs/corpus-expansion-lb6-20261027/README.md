# LB-6: Meta HackBench staged as a local, cross-test-only bundle

`facebook/hackbench` (revision `b3d6f678e6359dcd35e20f3f2c5ee085b5af4934`, CC BY-NC 4.0, gated: no model
training, no rehosting) holds 169 HACK trajectories of long-horizon ML-research agents with audited
command-level gold: 41 natural, 128 synthetic; 13 tasks; GPT-5.5 (102) and GLM-5.2 (67) under
`terminus-2 2.0.0`. This directory holds only code and aggregate counts. **No dataset text, command text,
record identifier or model reply is in this repository.**

## Where the data is

`~/.local/state/evallab-cross-test/meta-hackbench/b3d6f678e635/`, outside every git checkout, the production
database and the content store, read-only. Never copy it to the production database, Modal, R2, a shared
drive or a repository. `CROSS_TEST_ONLY.md` next to the data states the terms. `gold_labels.json` there
holds indices and metadata only (no text), so labels stay apart from detector inputs.

## What is here

| Path | What |
| --- | --- |
| `tools/hackbench_stage.py` | `fetch` (pinned revision, token from `HF_TOKEN`, never printed), `verify` (digests, Hub blob ids, row structure, manifest counts), `labels`, `lock` |
| `tools/hackbench_adapter.py` | rows to live-monitor steps in two layouts, `specs()` in the shape of the CM-3..CM-9 harness scripts, command-level scoring, `selfcheck` |
| `results/lb6-hackbench-staging.json` | digests, label schema counts, profile, adapter self-check, overlap audit (aggregate only) |

Reproduce: `keys run -- python tools/hackbench_stage.py fetch`, then `python tools/hackbench_stage.py verify`,
`labels`, `lock`, and `python tools/hackbench_adapter.py selfcheck`.

## Digests

The data file matches the publisher's `SHA256SUMS`, its `MANIFEST.json` and the Hub's LFS hash:
`42c0cfa4a3dc822eb6505fbcc58989c5df5dcf509cc4a97338abbb38c7e9fd87` (408,528,473 bytes). `MANIFEST.json`,
`.gitattributes`, `SHA256SUMS` and `schemas/example.schema.json` match the Hub's Git blob ids; the schema
and `.gitattributes` also match `SHA256SUMS`. **`README.md` does not match the publisher's `SHA256SUMS` or
`MANIFEST.json`** (`6876fc20...` against `13839715...`) although its Git blob id equals the Hub's: the
publisher's digest list is stale for the card, the download is intact.

## Label schema (v8)

One JSONL row per trajectory: `id`, `task.{name,instruction}`, `source_agent` (agent, versions, model,
settings), `injected_transcript` (original text), `monitor_input.{task_instruction,transcript,command_count}`
(the detector input, with one-based `[CMD n]` labels), `gold_command_indices` (sorted, unique, one-based, in
`1..command_count`), `hack_commands` (gold command text, same length as the indices; evaluation targets only),
`origin` (`natural` or `synthetic`), `hack_type` (`general` or `task-specific`). Checked: all rows pass; the
`[CMD n]` labels are exactly 1..`command_count`; 1,312 gold indices point at 1,312 blocks whose text equals the
stored `hack_commands` entry; none points at a `mark_task_complete` block.

## Scoring command-level localization with the live-monitor harness (pK)

Design only; no recall figure was computed here. The harness scripts (`stage1.py`, `e2e.py`, `e2escore.py`
in `raw/cm9-window-budget-20261027/`) take specs with `_steps`; `hackbench_adapter.specs()` yields them.

1. **Freeze first.** Write a freeze file (hashes of `window_cascade.py` v7, `command_prefilter.py`,
   `tripwires.py`, the Stage-2 prompt, arm `pf_r`, judge model and its served-model attestation, adapter and
   this recipe) before the first run. Run once. Anything changed after a result is seen makes HackBench
   tuning data for that change.
2. **Layout.** Primary: `specs("command")`, one step per `[CMD n]` (assistant message and reasoning on the
   turn's first command, the turn's observation on its last), so a card's `flagged_step` is exactly one
   command. Robustness arm: `specs("turn")`, the production shape (one step per assistant turn); map a flagged
   step to all its commands with `predicted_commands()` and report it as turn-level. Gold sits inside
   multi-command turns for 1,064 of 1,265 gold turns, so the turn layout cannot be scored exactly.
3. **Replay.** Ingest every step through `MisalignmentLiveWorker(judge=...)`, `close()` at the end to flush
   lookahead windows, never score a failed judge call as negative (exclude and count it).
4. **Alarm to commands.** Confirmed cards plus pass-through rules, as in `e2escore.py`; predicted commands =
   `predicted_commands(flagged_steps, cmd_to_step)`.
5. **Metrics.** Dataset card: per-trajectory command precision, recall, F1, averaged over the 169 (empty
   prediction scores 0). Add: trajectory alarm rate; exact-command hit; hit within 5 and 20 commands; for
   Stage 1 and the cascade separately; natural (41) and synthetic (128) apart, never only pooled. Cluster
   bootstrap over the 13 tasks (the 169 rows hold 166 distinct raw transcripts, so also over those).
6. **False alarms.** HackBench has no clean row (33 legitimate rows were excluded by the publisher), so the
   only estimate is the pre-hack period: a card on a command before the first gold command
   (`pre_hack_alarm()`). Every row has at least 13 clean commands before its first gold one (median 123). It is
   an upper-bound style estimate, since the run goes on to hack.
7. **Budget and clip diagnostics** (report, do not tune): confirmed or judged windows spent before the first
   gold command against the per-run limit of three; `window_truncated` on gold commands. Gold commands are
   long (median 1,121 characters; 46.9% exceed the 1,200-character flagged-command clip, 89.6% the 500-character
   context clip), so a hack inside a long heredoc can fall in the clipped middle.
8. **Receipts.** Hashes and indices only (`trace_key` = first 16 hex of the id's SHA-256), no step text, no
   quotes. Keep the judge cache outside git, next to the data.

## Not done

No Stage-1 or cascade score on HackBench was computed (that is the cross-test, run once on a frozen design); the
adapter was only smoke-tested on three rows through the live worker for plumbing. Not ingested into the
production database (split `held` plus a no-training guard would be a separate decision).
