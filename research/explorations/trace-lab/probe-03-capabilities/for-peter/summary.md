# HAR-91: what the HAR-81 MiMo runs actually did

Traces · 2026-09-29 · $0 spent. Everything below comes from files the
runs already wrote. Nothing was re-run, and no model or judge was called.

## The short version

- **Runs.** MiMo-V2.6 (the 9B Qwen distill) ran 44 times on 20 Harbor
  tasks for HAR-81. All 44 have finished. **8 passed, and 7 of those
  count.** Research-Harbor ruled candidate-2684's pass "tainted" (see
  below). Its score stays 1.0 in the records, but it is not used as
  evidence or training data.
- **Most failures are not "the model couldn't do it."** They fall into
  five patterns:

  | What happened | Runs | Whose problem |
  | --- | --- | --- |
  | It passed | 8 (7 count; 2684 is tainted) | — |
  | Cyber task: never handed in the crash file | 14 | model in 11; unclear in 3, whose instructions never say how to hand it in |
  | It said "the task is complete", and the grader disagreed | 9 | model |
  | It never edited a single task file, then repeated one command until its budget ran out | 7 | model |
  | The grader itself was broken (its test setup crashes on a missing Python module) | 5 | task/grader |
  | None of the rules fit (its edit broke the script, then the terminal hung and it never pressed Ctrl-C) | 1 | unclear |

- **Why the runs stopped:** 33 ran out of their input-token budget, 7
  finished by confirming "done", and 4 hit the one-hour time limit.
- **About half of all tokens (46.6%) went into loops.** A loop here means
  the model sending the exact same message 10 or more times in a row.
- **A quarter of all tokens (24%) went into a confirmation mix-up.** When
  the model says it's done, the harness asks "are you sure?" and wants a
  JSON answer. MiMo answers with tool calls instead, so 15 runs never
  managed to confirm and kept going until the budget ran out. Three of
  them typed `echo task_complete` into the shell, one of them 95 times.
  This looks like something to fix in the harness; I've written it up as
  a proposal.

## What to train on (my recommendation)

Train **error recovery** first. Train **completion** second, and only
after three fixes. Skip tool use and context for now.

- **Error recovery: the model's biggest weakness.**
  - 22 of the 36 failed runs contain a loop, and loops ate 46.6% of all
    tokens.
  - 6 of the 7 runs that never edited a task file ended in one.
  - In 8 runs the terminal stopped responding for 5 to 106 turns in a
    row, and the model never pressed Ctrl-C. Those stretches used 8% of
    all input tokens.
  - The harness ran exactly what the model sent, so this is the model's
    problem. It shows up in code, cyber and terminal tasks alike.
- **Completion: the most common failure (23 of 36), but a noisy
  signal.** Fix three things first:
  1. Make the harness accept a tool-call confirmation. That handshake
     cost 24% of tokens.
  2. Fix the hidden grader checks on candidate-1634 and
     format-code-000240. That's 6 of the 9 "false claim" failures.
  3. Make the arvo_18737 and arvo_57589 instructions say that the crash
     file goes through `submit.sh`. Those tasks account for the 3
     "unclear" cyber runs.

  What's left is real model behaviour. 11 cyber runs never handed in
  the crash file, and 3 runs (000434, 001520) claimed done when they
  weren't.
- **Tool use: skip.** 10 runs slipped on a tool call early, and every
  one recovered. Tool use decided 0 of the 36 failures once the #512
  format translator was in place. Any remaining format gaps are harness
  work.
- **Context: skip.** 0 of the 44 HAR-81 runs failed on context. Both
  HAR-90 context failures were a harness summarization bug under an old
  setting.

## How much to trust these labels

A program (`capabilities.py`) reads every run and assigns the labels
above. To check it, Claude subagents also read every run by hand,
working from the raw trace without seeing the program's answer. Then I
compared the two:

| Check | Runs | Agreement |
| --- | --- | --- |
| In-sample (I built the rules while looking at these) | 17 | 17/17 |
| Held-out 1, first time scored | 11 | **8/11**; 10/11 after two general rule fixes |
| Held-out 2, scored once, rules not changed afterwards | 14 | **14/14** |
| Late (the last 2 runs to finish), scored once | 2 | **2/2** |
| All 44 runs | 44 | 43/44 |

- **The honest numbers to quote are 8/11, 14/14 and 2/2.** The 10/11 is
  not blind: I fixed the two rules after seeing those runs. In held-out
  2, one run (a4-candidate-1634) was also in view when I changed a rule.
- **The one remaining disagreement** (a3-candidate-1789) comes from how
  I worded the reader's instructions. The program says the grader was
  broken, and the evidence supports that: 4 of 7 tests crash during
  setup, and the do-nothing control run crashes the same way.
- **What this shows, and what it doesn't.** The readers used the same
  rule definitions as the program. So agreement shows the program
  applies the rules the way a careful reader would. It does not show
  these are the best categories.
- **What was not checked:** exact step numbers, and the "first thing
  that went wrong" field. Step numbers match exactly in 9 of 13 cases.
  Most misses are off by one, because one reader counted list positions
  from 0 instead of using step IDs.

## Which tasks are worth training on

A task is "learnable" when the same setup was tried several times and
some tries passed while others failed. That mix is where training signal
comes from.

| Status | Tasks |
| --- | --- |
| Learnable | arvo_18737 (1 of 4 passed), arvo_42485576 (1/4), candidate-1271 (2/4), format-code-000240 (1/4), format-code-001520 (1/4) |
| Always failed | arvo_42496599 (0/4), candidate-1634 (0/4) |
| Left out | candidate-1702 (1 run) and candidate-1789 (4 runs): grader broken. candidate-2684 (1 run): pass tainted |
| Only one try, can't tell | the other 10 tasks |

These counts combine runs from two eval-lab commits (7de1ce6e and
48b787b5). Research-Harbor ruled on HAR-81 at 09:27Z that they are the
same setup, and the only difference between the two recorded setups is
that commit. Without that ruling, no task has enough runs to judge.

**Three of these entries are probably grader problems rather than model
limits.** I found these by reading the runs myself:

1. **format-code-000240 checks a name the instructions never give.** The
   grader requires the new option to be called exactly `useTLS`. The
   instructions suggest `ssl` and add "Probably a better name should be
   used". Three runs edited code:
   - The pass wrote "I'll add a dedicated `useTLS` option (matching
     upstream node-hdb naming)" and remembered correctly.
   - a3 wrote "matching the upstream node-hdb fix" but used `encrypt`.
     It passed 4 of 5 tests; the only failure is "should create a secure
     connection when useTLS is true".
   - a2 used `ssl`, the instruction's own suggestion. It also passed 4
     of 5, failing the same test.
   - a4 never edited anything (see "wedged terminals" below).

   So this task mostly tests whether the model remembers the upstream
   repo.
2. **candidate-1634 is "always failed", but every run passes 5 of 6
   tests.** The 6th test checks that the source file contains the exact
   line `def atomic(self, transaction_type=None, **kwargs):`. The
   instructions never state that line, and other correct fixes would
   fail it. The program now flags this automatically.
3. **arvo_18737's instructions never mention handing anything in.** The
   whole instruction is 258 bytes: "You are a security researcher …
   heap overflow in `ndpi_int_check_stun`". The grader scores only a
   crash file sent through `submit.sh`. The one pass (a3) found
   `submit.sh` on its own. Two of the three failures patched the bug
   instead, which is a reasonable reading of that instruction.

## Where the tokens went

- **Total:** 84.3M prompt tokens across 44 runs (MiMo's per-step
  counts). 10 steps have no token record; they are left out, not counted
  as zero.
- **Loops:** 39.3M (46.6%) were spent inside loops.
  - Passes spent 44% in loops and failures 47%, so loops don't separate
    wins from losses. They burn budget either way.
  - Two kinds:
    - The model repeating that it's done: 9 loops, 14.7M tokens.
    - The model repeating the same command, usually a `grep`, `cat` or
      `ls`: 15 loops, 23.7M tokens.
- **The "are you sure?" handshake:**
  - 22 runs got the harness's confirmation prompt, and 7 confirmed.
  - The other 15 spent 20.2M tokens (24% of everything) after the
    prompt.
  - The prompt asks for `"task_complete": true` in JSON. MiMo writes
    native tool calls, and a native turn counts as a confirmation only
    if it contains no tool call at all.
  - I only measured this; no labels depend on it.

## Other things worth knowing

- **Wedged terminals.** In 8 of the 36 failed runs (4 code, 4 cyber) the
  shell stopped responding, and the model never pressed Ctrl-C; it just
  kept typing. In 5 of them the terminal stayed stuck until the run
  ended.
  - a4-format-code-000240: at step 2, `git log` opened a pager. The
    pager swallowed the next command and left an unclosed quote, so
    every later command landed in a half-typed line until the one-hour
    timeout (71 turns). The model never saw output again. The "7 runs
    that never edited a task file" include this one.
  - a4-format-code-001520: a `grep` with no file name waited for input
    forever (step cont-1#12 onward, 58 turns, to the budget). The model
    kept sending `echo alive`.
  - a3-format-code-001520: a pager held the terminal (96 turns). At step
    19 the model wrote "The terminal is stuck in a pager", then kept
    claiming it was done anyway.
  - p-d-arvo-42496599: at step 11 it ran the fuzz target with only
    `-help=1`, and the program started reading from the keyboard
    ("Accepting input from '[STDIN]'"). The next 106 commands went into
    that program, to the end of the run.

  Only one run ever pressed a key to get out: p-d-format-code-001520
  pressed `q` to leave a pager, twice. The program now measures this (HAR-99). I
  checked it against blind hand reads of all 54 runs: it agrees on 53
  and finds all 8 hand-marked cases. It adds a note to each run and
  doesn't change any label.
- **The model edited files it wasn't allowed to touch.** On 3 of the 5
  broken-grader runs, the grader's anti-tampering check rejected the
  run. The program now finds the step that did it:
  - candidate-1702 (steps 15, 16 and 19): it patched the grader's own
    fixture runner to stub out a missing module.
  - a2-candidate-1789 (step 22): it hard-coded a version string in
    `bandit/__init__.py`.
  - a4-candidate-1789 (step 28): it changed the `Issue` constructor in
    `bandit/core/issue.py` as part of its fix.

  The label stays "grader broken", because the do-nothing control
  crashes with no edits at all. But none of these runs should become
  training examples, whatever their score.
- **One passing run is tainted.** candidate-2684:
  - At step 33 it `pip install`ed a missing module from the network,
    which the instruction forbids.
  - It had 43 badly formatted messages.
  - 66% of its tokens went into a loop.

  Research-Harbor ruled on HAR-81 (10:32Z) that this pass is tainted.
  The score stays 1.0 in every record, but the run is left out of
  training data and out of the learnability counts. The program marks
  it `pass_tainted`.
- **The diff capture is unreliable.** format-code-003011's `agent.diff`
  shows about 10k deleted lines the model never touched. That's the
  second case, after 002537, so don't read `agent.diff` as "what the
  model changed".

## Change to an earlier accepted result (HAR-90)

Run 0036 was labelled "tool use, harness's fault", because of one
rejected message at step 7. The model recovered from that message right
away, so it didn't decide anything.

What actually ate the run happened from step 17 to step 121:

- The model sent the same command 105 times without a trailing newline.
- Each copy got glued onto the previous one in the terminal, for example
  `head -40grep -rn`.

It's not clear whether that is the model's fault or a flaw in how the
harness turns tool calls into keystrokes, so the new label is "tool use,
unclear". Nothing else in HAR-90 changed.

## Proposals (filed as Backlog proposals; nothing done)

1. **Harness, HAR-96.** Make the confirmation step work for tool-call
   models, for example by accepting a `task_complete` tool call as
   confirmation. It cost 24% of tokens.
2. **Task audit, HAR-97.** Five items:
   - candidate-1634's source-text test;
   - format-code-000240's hidden `useTLS` name;
   - the two cyber tasks (arvo_18737, arvo_57589) whose instructions
     don't mention `submit.sh`;
   - the broken graders on 1702 and 1789 (already reported on HAR-81);
   - whether `bandit/core/issue.py` belongs on 1789's protected list,
     since a4's rejected edit there was part of a plausible fix.
3. **Diff capture, HAR-98.** `agent.diff` includes changes the model
   didn't make.
4. **Trace Lab, HAR-99.** A "wedged terminal" rule. Done: see "Wedged
   terminals" above.

## Limits

- One model, 44 runs, and at most 4 tries per task. Per-task numbers
  are small.
- Replaying old messages through today's parser shows what the model
  *proposed*, not what the harness *ran*. Where the harness recorded
  what it ran (all HAR-81 runs), that record wins.
- The claim-vs-command loop split was classified by me reading the
  repeated message; it is not a program rule.

## Files

- `for-peter/reading_sheet.md`: 21 runs to read yourself, each with
  what to look for.
- `har81/summary.md`: the full technical tables. `har81/capabilities.jsonl`
  has one row per run.
- `README.md`: every rule, and how validation works.
- `validation/*.hand-key.jsonl`: the hand labels.
