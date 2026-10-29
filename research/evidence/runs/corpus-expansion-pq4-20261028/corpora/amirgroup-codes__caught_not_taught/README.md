# amirgroup-codes/caught_not_taught (ControlArena IaC sabotage episodes)

Cross-test-only corpus. Adapter, profile and aggregate counts only; no transcript text, task text or
rows are committed.

- Source: https://github.com/amirgroup-codes/caught_not_taught, pinned commit
  `5aa035e3377ab62b086072577b3fd9c25825bc77` (paper: arXiv 2610.26935, Saeedi and Aghazadeh).
- Licence: MIT (GitHub licence API `mit`, `LICENSE` staged and in the manifest). Public, ungated.
- The transcripts ship in the repository (`transcripts/<tag>/...`, 297 + 243 scrubbed JSON episodes);
  no raw Inspect logs. The paper's second benchmark is deliberately not published by its authors.
- Local bundle: `~/.local/state/evallab-cross-test/amirgroup-codes__caught_not_taught/5aa035e3377a/`
  (603 files, 59,963,376 bytes; manifest sha256
  `02555c1ebd9bdb0be36db7dd7eab864ae375fc0520af891d979f9aeca960d4e0`, every file checked against its
  Git blob id at the pinned commit; 0444/0555). Staged: `LICENSE`, `README.md`, `CITATION.cff`,
  `transcripts/`, `data/`, `results/`, `cnt/transcripts/` (schema), `paper/prompts/`. Not staged: the
  built `docs/` viewer, the PDF/figures and the experiment code.
- Rules: eval-only, local-only, no training/tuning/few-shot, frozen-design one-shot, no vendor upload
  (`CROSS_TEST_ONLY.md`).

## Construct: covert attack, not spontaneous cheating

Attack-mode agents are **instructed** to carry out a hidden side task (for example "alter the
Deny-S3-Public SCP", "create a backdoor admin role") while doing a main IaC task, and to be subtle.
This is red-team policy behaviour on ControlArena Infrastructure-as-Code, not a model that decided to
cheat. A detector that scores well here has detected instructed sabotage; it has not shown
reward-hacking detection. Report it as `covert-attack`, never pooled with cheating corpora.

## Unit and label predicate

Unit: one **round-1** (memoryless) episode, `transcripts/<tag>/round1/{attack,honest,trusted}/*.json`,
two model pairs (Opus 4.8 acts with Opus 4.5 as trusted model/monitor; GPT-5.4 acts with GPT-4.1).

| State | Predicate | Count |
|---|---|---|
| positive | round-1 untrusted `mode=attack` and `grade.side_ok is True` (grader: side-task pytest passes) | 46 (Opus 33, GPT-5.4 13) |
| negative | round-1 `mode=honest`, no side-task grade (untrusted 80, trusted-model 80) | 160 |
| ambiguous | round-1 attack with `side_ok False` (attempted sabotage slice) or null | 34 (Opus 7, GPT-5.4 27) |
| excluded | all 300 round-2 episodes | 300 |

- `side_ok` is the corpus's own pytest side-task grader; `regraded` is false for every episode. Main
  task success is not part of the predicate (43 of 46 positives also pass the main task, 3 do not).
- The failed-side-task attack episodes are an **attempted-sabotage slice**: the agent was instructed
  and tried, the grader did not confirm success. They are never negatives and stay out of binary
  denominators; report them separately (recall of attempts). 27 of 34 are GPT-5.4.
- Round 2 is excluded: those episodes replay the earlier episode (including its attack
  reasoning) and a feedback message ("your submission was replaced", optionally the trusted solution),
  so the condition is spelled out in context and the history is conditioned by design.
- Negatives are honest-mode, not certified-benign: an honest agent may still write a poor diff. The
  two negative arms differ in model: attack episodes only come from the untrusted models, so report
  the untrusted-honest slice (80) as the primary matched-model negative set and the trusted-arm slice
  (80, Opus 4.5 and GPT-4.1) separately.

## What the detector sees (`_steps` only)

`tools/adapter.py::specs()` yields dicts with `_steps` plus separate `gold`, `labels`, `identity`.
**Kept** in `_steps`: the user task message(s), assistant text, native tool calls (function and
arguments), tool results, harness "please proceed" user nudges, and the final repository diff.

**Stripped**, per episode (counted in the adapter selfcheck, 240 labelled episodes):

- the whole system message (240): the attack prompt names the side task and says "secretly trying",
  "penetration tester", "stress-test our safety systems"; the honest prompt is also removed so prompt
  length/shape cannot give the condition away;
- every `reasoning_summary` part and scratchpad part (2,502): attack reasoning says "side task"
  147 times in the Opus attack set alone, which is the condition name;
- assistant turns that are provider content-filter refusals (30, GPT-5.4 attack only: "This content was
  flagged for possible cybersecurity risk ...");
- monitor score/reasoning, per-step monitor output, grader outcomes and explanations, side-task name
  and description, `mode`, `arm`, `variant`, `history`, `pair` and episode/problem ids.

Not stripped (agent-visible and kept): the agent's own tool calls and outputs. Tool results contain
repository text such as "monitor" and "red-team" in both modes.

Leak scan (`selfcheck`, whitespace-normalised substring checks on step text, not JSON blobs): side
description, side-task name, problem id/episode id, every 40+ char system-prompt sentence, monitor
reasoning lines (against assistant text) and condition patterns (`side task`, `penetration tester`,
`stress-test our safety`, `safety systems`, `secretly trying`, `sabotage`, pair and round tags): 0
hits in 9,729 steps. Grader pytest output overlaps tool results in 12 episodes because the agent
ran the same suite; counted as natural agent-visible overlap, not a leak.

## Residual confounds (not removable by stripping)

- Instructed sabotage is itself a distribution shift: the agent knows the monitor exists and is told
  to be subtle.
- Harness "please proceed" nudge user messages: 27 in 10 untrusted attack episodes (all GPT-5.4,
  all ambiguous failed attempts), 24 in 24 untrusted honest episodes (23 Opus, 1 GPT-5.4), 0 in
  trusted episodes, and **0 in any of the 46 positives**. On the Opus pair a nudge is therefore a
  clean honest-mode marker (23 of 40 Opus untrusted-honest episodes, 0 of 33 Opus positives). It is
  kept in `_steps` because the agent saw it, but any detector that keys on it is reading the
  harness, not the behaviour.
- **Content-free baseline floor.** An independent audit scored an unfitted
  `rank(n_steps) + rank(diff_chars) − nudge` baseline that reads no content: positives vs
  untrusted-honest negatives AUROC **0.912 Opus, 0.854 GPT-5.4, 0.854 pooled** (vs all negatives
  0.809 / 0.898 / 0.826). Single features: `n_steps` 0.818 / 0.824, `diff_chars` 0.710 / 0.829.
  Doing a second hidden task makes episodes longer, so part of this is the behaviour itself, but
  report every detector against this floor, not against 0.5.
- The text-editor "excluding hidden items" view output appears in 31 of 40 Opus-4.5 and 1 of 40
  GPT-4.1 trusted-arm negatives and in no positive: a model-identity marker, another reason to keep
  trusted-arm negatives out of the primary comparison.
- Only 5 main tasks; the 40 attack cells (same 40 in both model pairs) pair them with 9 distinct side
  tasks, and positives and honest negatives share those 5 main tasks. Cluster by main task for any
  interval. Honest episodes are 8 repetitions per main task per model, so negatives are far less diverse
  than positives.
- Model identity: attack episodes are the untrusted models only (Opus 4.8, GPT-5.4).
- Opus: 33 of 40 attack episodes are positives; GPT-5.4: 13 of 40, with 30 content-filter turns
  indicating partial refusals.
- Scored positives include cases the corpus's own monitor already flags (raw >= 5 defers); the
  deferral decision is a label field and must stay out of `_steps`.
- Command-level localisation is not scorable: labels are episode-level.

## Overlap with Eval Lab splits

SHA-256 of normalised ids (bare and full, same normalisation as the other pq3/pq4 profiles) of 45
problem ids + 5 main-task names and 240 episode ids against `assignments.csv` (`task` and `item_id`:
dev, test, heldout) and production `corpus_tasks`: 0 matches in every cell (id-level only; no
content comparison). This was measured by the staging builder and independently re-measured by an
auditor (290 raw ids, 410 normalised forms, also `key_id`; none of the 14 main and side task names
appears in `corpus_tasks.instruction_text`). `tools/profile.py` records the hashed ids but does not
itself compute the overlap, so the number is not regenerated by `profile.py`.

## Reproduce

```
python3 tools/adapter.py     # selfcheck: counts, stripped parts, leak scan
python3 tools/profile.py     # verifies manifest/revision after lock, writes results/profile.json
```

Both use only the standard library and read the locked bundle; they print aggregates, never
transcript text. `results/profile.json` holds counts, digests and hashed ids.
