# HAR-116 part A: loop-fix harness variant

Two Terminus-2 harness trees for the self-hosted MiMo student, identical
except for the loop fix. A spec selects one by the two fields it already
uses for any harness tree:

| field | baseline | loop fix |
|---|---|---|
| `harness_tree_path` | `research/experiments/har116-loopfix-leak/harness-baseline` | `research/experiments/har116-loopfix-leak/harness-loopfix` |
| `harness_tree_sha256` | `sha256:433d5d2946e317b0213438ea4aa1852f2aaffa5c3a81a3bbdeb42d4aa028ecf3` | `sha256:06e5712c153f41bda8dfab38736847438135fc0c5693dc147be12d8207869281` |

The baseline digest is the HAR-104 / HAR-110 tree's own digest: the bytes
are the same tree, copied here so this experiment pins its own path. The
loop-fix tree adds two keys to `terminus/config.json`, `loop_break: true`
and `output_cap_chars: 2000`, and nothing else.

Both keys are Terminus behavior knobs the lab admits, so they ride the
normal path: the tree's config becomes `--agent-kwarg` values
(`loop_break=true`, `output_cap_chars=2000`) and the adapter applies them.
Leave them out and the agent behaves exactly as before.

## What the loop fix does

**Loop break.** The detector is the HAR-114 one, shared with the offline
replay so the two agree: a run of 4 identical normalised commands with no
edit in it, or 10 identical messages. On the call the run reaches that
length the agent is told, once, "you are repeating; change approach or
finish". If the repetition is still going 5 calls later, the agent phase
ends and the verifier runs. One call that differs, or one edit, ends it:
the agent keeps going and the nudge is recorded as broken.

It records, in the agent metadata and in each trajectory's
`final_metrics.extra.loop_break`: whether it fired, the call it fired on,
the detector, the call it stopped on, and what the model did on the call
after the nudge. A stop sets `stop_reason: loop_break`.

**Output cap.** At most 2,000 characters of terminal output come back per
step, head and tail, with a marker naming the file that holds the rest.
The full output is written to `/logs/agent/evallab-output/step-NNNN.txt` in
the sandbox, as the session user, so the agent can grep it. Output under
the cap comes back unchanged.

## The agent user: root, in both trees

The question was whether the agent can run as the unprivileged `agent`
user, the way the FineEnvs reference agent does, so it cannot rewrite
`/etc/hosts` and bypass the answer-leak blocklist.

Harbor can do this cheaply, and already does, for a task that asks: the
trial sets the environment's default user from `[agent].user` in
`task.toml`, and Terminus-2 starts its tmux session as that user. Daytona
honors it. The cyber and general tasks declare `user = "agent"` and their
images have that user, so those already run unprivileged.

The HAR-116 tasks do not. They are code tasks, and the code-task packages
declare no `[agent].user` and never create a user: `setup.sh` runs as root
and leaves the repo root-owned. The reference agent's own account agrees —
it drops to `agent` only for cyber and general tasks, reading
`/var/lib/mimo/agent_user`, which the code tasks never write. A real
HAR-110 trial shows the shell prompt as `root@<id>:/testbed`.

Running these as `agent` anyway is not cheap. The passes come from the
agent editing the repo, and that repo is root-owned with nothing chowning
it to an unprivileged user. Making it work means adding a user and handing
over the repo in every task image — a task change, not a harness one, and
a different one per image. So both arms stay root, and they stay
comparable. The consequence is unchanged: a root agent can still rewrite
`/etc/hosts` and undo the blocklist. The leak study should keep reporting
`/etc/hosts` edit attempts.

## Replay

`replay_loopfix.py` runs the rule over the 82 HAR-114 trials, read-only.
Of the 82, 54 would have been nudged and 40 of those stopped; the other 14
broke the repetition inside the 5-call window and would have been left
alone. The 40 stops save 57,348,616 input tokens between them (median
1,674,350 per stopped run).

Of the 15 runs that passed, the rule would have lost 2 and kept 13:

| run | earned the pass at | nudged | stopped | verdict |
|---|---|---|---|---|
| HAR-81 2684 (security-appsec) | call 78 of 84 | 46 | 51 | cut |
| HAR-81 1271 (media-games) | call 91 of 95 | 41 | 46 | cut |
| HAR-104 002391 (the plain pass) | call 8 of 102 | 19 | 24 | kept |

2684 and 1271 earned their passes after the stop, so the rule would have
ended them first. 002391 earned its pass at call 8, before the loop ever
started, so the stop at call 24 keeps the pass and only drops the loop
that followed it. Per-run figures are in `replay.csv`.

## Limits

The replay assumes the model would not have changed course because of the
nudge. It might have. The savings count the calls after the stop as never
happening, which is what the rule does, but the nudge itself could have
broken the loop earlier and saved more, or the model might have repeated
in a way the detector does not catch. Neither is credited.

The output cap's file holds the output the terminal session returned. A
command that timed out still gets upstream's timeout report rather than
the raw pane, because that report is what the agent needs.

The agent stays root, so the `/etc/hosts` bypass remains possible. Nothing
here closes it.

# HAR-116 third arm: completion-fix harness variant (prepared, NOT run)

## The tree

A third Terminus-2 harness tree for the self-hosted MiMo student,
byte-identical to the baseline except for the completion fix:

| field | baseline | completion fix |
|---|---|---|
| `harness_tree_path` | `research/experiments/har116-loopfix-leak/harness-baseline` | `research/experiments/har116-loopfix-leak/harness-completionfix` |
| `harness_tree_sha256` | `sha256:433d5d2946e317b0213438ea4aa1852f2aaffa5c3a81a3bbdeb42d4aa028ecf3` | `sha256:7d44c9f492659913eeb58147f68f04e4ec361f152eabe82e8e5a8b5ff1e7644a` |

The only byte difference is `terminus/config.json`, which gains the knob
`completion_fix: true`. Like the loop-fix keys it rides the normal path:
the tree's config becomes the `--agent-kwarg` value `completion_fix=true`
and the adapter applies it. Leave it out and the agent behaves exactly as
before.

## What the fix does

HAR-96's rule, literally: once the confirm prompt ("Are you sure ...")
is pending, accept a native `task_complete` tool call — lone or beside
other calls — or `"task_complete": true` inside a native call (JSON
argument or `<parameter=task_complete>`), as the confirmation. Upstream's
pending logic ends the episode from there.

The rule is pending-only on purpose. A lone `<function=task_complete>`
call already confirms live (#526), as do prose and Terminus-JSON claims,
so those shapes are untouched. A native-shaped turn with no prompt pending
is still a parse error, exactly as today: the fix never manufactures a
first claim, so it cannot confirm a turn prematurely.

The live check and the offline replay share one function,
`evallab.mimo_tool_calls.has_native_completion`, so the two cannot drift.
A shell `echo "task_complete"` is NOT a native signal (it names no tool
call); neither is prose that merely mentions the word, nor a bare
Terminus object with no native markup.

## Replay: the literal rule changes nothing on the observed runs

`replay_completionfix.py` replays the rule, read-only, over the 82
HAR-114 runs (38 HAR-119 + 44 HAR-81), simulating upstream's pending flag
turn by turn on the recorded raw text. Per run it reports the turn each
rule would accept, the per-step prompt tokens after it, and whether a
pass would be lost (accept before HAR-114's last useful edit, reward-1
runs). A1 is the first harness-accepted completion (HAR-100's prompt
turn); A3 is the first echo-done turn at/after the first claim-shaped
turn. Full per-run rows: `replay_completionfix.jsonl` (`.csv` beside it).

**Verdict: the literal rule recovers 0 runs and 0 tokens on all 82.**
No post-prompt turn carries a native completion signal the live harness
did not already accept — the observed claim loops are shell echoes
(`echo "task_complete"`), prose re-claims, and re-verification commands.
This matches HAR-100's HAR-81 finding and extends it to the 38 HAR-119
runs. The prepared third arm is therefore expected to behave exactly
like the baseline on these shapes; running it is Research-Harbor's and
Peter's call.

Because 0 is less than half of what A1 recovers (below), the comparison
stays prominent per the ticket, but no A1/A3 tree is built here.

### The 8 completion-claim loops (literal vs A1 vs A3)

Steps are agent step ids; saved is per-step prompt tokens after the accept
turn; pass compares the accept against the last useful edit.

| run | reward | last edit | prompt at | literal | A1 (turn, saved, pass) | A3 |
|---|---|---|---|---|---|---|
| HAR-110 000226 cNYdqfo | 1.0 | 10 | 16 | — | 16, 2.29M, kept | — |
| HAR-110 001832 CFCbfps | 1.0 | 51 | 87 | — | 87, 0.18M, kept | — |
| HAR-110 001896 7RJJeFg | 0.0 | 30 | 37 | — | 37, 1.55M | — |
| HAR-110 002391 8FqvKUU | 0.0 | 32 | 36 | — | 36, 1.82M | — |
| HAR-104 000226 | 1.0 | 12 | 14 | — | 14, 1.88M, kept | — |
| HAR-104 000383 | 0.0 | 22 | 30 | — | 30, 1.74M | — |
| HAR-104 002391 | 1.0 | 9 | 81 | — | 81, 0.91M, kept | — |
| HAR-104 002864 | 1.0 | 13 | 20 | — | 20, 2.25M, kept | — |
| **claim-loop totals** | | | | **0 runs, 0 tokens** | **12.63M** | **0 runs, 0 tokens** |

A3 fires nowhere on the 38: these runs echo `COMPLETE_TASK_AND_STOP` /
`task_complete` (shell) or re-claim in prose, never HAR-100's
`echo done`-shaped turns.

### All 82 for context

| group | literal recovered | literal saved | A1 saved | A3 saved | pass lost (lit/A1/A3) |
|---|---|---|---|---|---|
| 38 HAR-119 | 0 | 0 | 14.57M | 0 | 0/0/0 |
| 44 HAR-81 | 0 | 0 | 21.46M | 15.65M | 0/2/0 |

A1/A3 reproduce HAR-100's published pilot figures (21.46M; 15.66M) to the
dollar. A1's two lost passes are both 1271-media-games variants (a2: first
claim step 32 before the passing edit 36; a3: first claim 54 before edit
63). On the 8 claim loops above, A1 keeps every pass.

## Third-arm specs

`make_specs.py` takes an optional `--completionfix-tree/--completionfix-digest`
pair producing `har116-a-<task>-completionfix` specs for the same 10 Part A
tasks on the same staged task bytes. Without the pair the existing outputs
are byte-identical. The command (from the repo root, after staging):

```bash
uv run --no-sync python research/experiments/har116-loopfix-leak/make_specs.py \
  --baseline-tree research/experiments/har116-loopfix-leak/harness-baseline \
  --baseline-digest sha256:433d5d2946e317b0213438ea4aa1852f2aaffa5c3a81a3bbdeb42d4aa028ecf3 \
  --loopfix-tree research/experiments/har116-loopfix-leak/harness-loopfix \
  --loopfix-digest sha256:06e5712c153f41bda8dfab38736847438135fc0c5693dc147be12d8207869281 \
  --completionfix-tree research/experiments/har116-loopfix-leak/harness-completionfix \
  --completionfix-digest sha256:7d44c9f492659913eeb58147f68f04e4ec361f152eabe82e8e5a8b5ff1e7644a \
  --out-dir research/experiments/har116-loopfix-leak/specs
```

Route (from the retained HAR-110 base spec): terminus-2 +
selfhosted `XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` on Daytona, est $1.85
per trial.

## Behaviour tests

- `tests/test_harbor_terminus_completionfix.py`: pending prompt plus a
  native `task_complete` call confirms;
  `test_pending_prompt_plus_embedded_task_complete_true_confirms`;
  without the knob nothing changes
  (`test_without_the_knob_a_native_completion_turn_does_not_confirm`);
  a verification command without a signal never confirms; a native-shaped
  turn with no pending prompt does not confirm early; a clean claim with
  no pending prompt still reaches the prompt;
  `test_non_boolean_completion_fix_refuses`.
- `tests/test_mimo_tool_calls.py::test_has_native_completion_marks_only_native_signals`:
  the shared detector (lone/mixed/embedded true; bare object, prose
  mention, shell echo, plain call false).

## Limits

The replay is a counterfactual on recorded text: it assumes the model
writes the same turns under the new rule. It credits only turns already
written, never a nudge-changed course. Pending is simulated from recorded
acceptances; a turn the live parser rejected clears it, exactly as
upstream does.
