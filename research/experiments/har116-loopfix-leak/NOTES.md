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
