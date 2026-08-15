# Peter's operating manual — running a lab built by agents

How to direct this lab without drowning in its code. Written from the first
three days of real incidents, for Peter specifically. Everything here has
already earned its place the hard way. Iterate by editing; this is your doc.

## The one rule

**You read intents and outcomes, never diffs, unless you are deciding
something.** The swamp feeling comes from trying to "keep up with the code."
Stop. The code is the agents' medium; yours are: what was intended
(handoffs, PR titles), what happened (digests, events), and what it means
(DISCOVERIES, reports). You drop to code level only when an actual decision
needs it — and then only the specific file the decision touches.

## Your three surfaces, in order

1. **`digests/<today>.md`** — two minutes. What ran, what it cost, what
   drifted, what's waiting on you.
2. **`scripts/fleet-status.sh`** — ten seconds. The derived truth: the board's
   Now/Review/Next/Needs-Peter, active vs SPENT branches, unregistered or
   stale work, queue depth.
3. **`gh pr list`** — the delta. Each PR title starts with its mission ID (`M###:`) and says
   one thing. Open PRs are the only work that needs your eyes.

That is "keeping track." If something in those three surfaces confuses you,
ask an agent to explain it — don't go spelunking.

## The morning routine (~15 minutes, in order)

```
1. read digests/<date>.md              what happened overnight
2. scripts/fleet-status.sh             who's mid-flight, who's blocked
3. gh pr list                          what wants review
4. read digests/DISCOVERIES.md         accept/reject new entries (your real job)
5. tail queue/events.jsonl             only if something above looked wrong
```

Then decide the day's missions. Everything else is optional.

## How you issue work

One mission, one role, one prompt. Every prompt has five parts, always:

1. **Identity + worktree setup** — exact commands, so "where do I work" is
   never improvised.
2. **Owned paths** — the only places it may write. Disjoint paths are why
   seven agents don't collide.
3. **The mission** — goal, phases, and the *acceptance criteria* that define
   done. Acceptance criteria are the contract; vague missions produce swamp.
4. **Boundaries** — what it must not do, and what to do when blocked
   (record it and continue; never improvise around a rule).
5. **Handoff discipline** — update `agents/handoffs/<role>.md` every ~30 min.

Rank missions by difficulty and give the hardest to the strongest model.
Never launch a new wave while more than ~2 PRs sit unreviewed — unreviewed
work is inventory, and inventory is where you get lost.

## How you say yes and no

Your authority lives in files, not in vigilance:

- **`policy/standing-approvals.yaml`** — the money and execution authority.
  What may run unattended, which agents, cost ceilings. Agents may never
  loosen it; editing it is how you steer spending. Raising a ceiling IS the
  approval.
- **Ownership grants** — the code authority. Lanes own paths
  (`agents/OWNERS.md`); a mission writes only its leased paths on the board
  (`agents/missions/ACTIVE.md`). Expanding a lease happens in the prompt, explicitly,
  scoped to named files ("for this mission only, you may touch X, Y").
- **`queue/STOP`** — the brake. Create the file, dispatch halts after the
  current job. `evallab stop` / `resume`.
- **Escalation by construction** — anything outside policy lands in
  `queue/waiting/` and shows in the digest. You approve with
  `evallab approve <id>` or by editing policy so the class is standing.

You never need to watch agents to feel safe; the policy file, the ownership
table, and the ceilings are watching. Your job is to keep those three
accurate.

## How you review

- Read the **handoff** first (their story), then the **PR description**, then
  — only if the work touches shared files or money paths — the diff of those
  specific files.
- Trust **verification, not prose**: acceptance criteria met? controls run?
  CI green (`gh pr checks`)? A claim without a run link is a draft.
- Attribution discipline: when something fails, the lab's own taxonomy
  applies — task defect, harness defect, or agent failure. Canaries exist so
  drift is a suspect before capability is. (The transaction-reconciliation
  0/3 was a changed verifier, not a dumber model — that lesson generalizes.)

## When you intervene

Only on these signals, all visible in your three surfaces:

- a handoff `Status: blocked` for more than a session
- a conflict recorded in a handoff (integrator work — you or a MEDIC-style
  mission, never "whoever gets there first")
- red CI on main
- spend or deferral anomalies in the digest
- two agents wanting the same paths (fix the ownership table, not the agents)

## Git hygiene for you personally

- **`git branch --show-current` before anything** in the main checkout. The
  worst incident so far was work landing on a leftover agent branch nobody
  noticed. Ten characters of typing prevents it.
- You don't hand-edit in the main checkout while integrator missions run —
  one writer per tree includes you.
- `git fetch` before reading state; local views go stale fast.
- All merges — yours included — only with GitHub checks green
  (`gh pr checks <n>`), once MEDIC lands the premerge gate.

## Skills: encode your routines

Recurring workflows belong in Claude Code skills/commands, not in your
memory. Worth creating as the lab stabilizes (any agent can build these for
you):

- a **lab-status** skill: runs the three surfaces and summarizes — your
  morning routine as one command
- a **mission-launcher** skill: takes role + mission text, generates the
  five-part prompt with the setup/git boilerplate baked in
- a **review** skill: given a PR number, pulls handoff + description +
  checks + shared-file diff into one summary
- `/goal` for session-scoped objectives; `/loop` for recurring checks — you
  already use these; prefer them over re-explaining context each session

Rule of thumb: the third time you type the same instructions to any agent,
turn them into a skill or a doc and point at it.

## Principles that earned their place (the incident log)

1. **The repository is the record.** Sessions die; only commits, handoffs,
   events, and runs are real. Anything not landed didn't happen.
2. **One folder, one writer per tree, disjoint paths.** Every collision so
   far traces to a violation of one of these.
3. **Fail closed, attribute precisely.** An auth failure must produce
   "nothing ran," never a page of fake zeros. Reason codes
   (`missing_credential:`, `execution_failed`) are why 3 a.m. failures are
   debuggable at 9 a.m.
4. **Local green must equal CI green.** Environment-dependent tests and
   version drift are how "it worked for the agent" becomes red main.
5. **Agents propose, the executor disposes.** No agent runs paid work
   directly; the queue + policy file are the only path. This is what makes
   "unsupervised" safe.
6. **Pin everything a comparison depends on.** Task version, verifier
   digest, model string, judge calibration. An unpinned comparison is a
   vibe.
7. **Write the decision down where the next agent will look.** STRUCTURE.md
   for layout, OWNERS.md + the mission board for ownership, policy for permission, handoffs for
   state. A rule that lives in chat history doesn't exist.
