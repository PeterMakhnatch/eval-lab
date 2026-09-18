# Bottom-up fleet design — verdict, evidence, and the end-to-end build

Date: 2026-09-03. Author: independent review pane (wT:* session), commissioned by Peter.
Inputs: full transcripts of the Librarian (wK:p3), Analyst (wK:p5), Muse-Research (wS:p7),
Researcher-Evals (wH:p9), ZAI :3 (wS:pA), and ZAI (wS:p2) sessions, Aug 23 – Sep 3;
repo ground truth (`research/inbox/{board,ledger}.md`, `claims/README.md`,
`driver-brief-work-os-2026-09-03.md`, `Peter/work-organization.md`, `AGENTS.md`,
`docs/{NOW,context-packs,agent-profiles,fleet-tracking}.md`).
Extraction briefs: `/tmp/briefs/*.md` (session-local).

## 1. Verdict

**The flip is not a pipe dream, and it is not untested. Round 1 already ran and worked.**
The Analyst's read of the board (Sep 3): *"6 completions from 5 different panes / 4 models,
none assigned by you"* — including two adversarially-caught errors (sha mismatch, over-archive)
recorded as ledger data. Three agents independently converged on the same architecture from
three different angles, in one day:

| Source | Angle | Conclusion |
|---|---|---|
| Librarian (wK:p3) | designed + piloted it | "The ledger is an eval harness running on your real work." Flat first; moratorium on new protocol until rounds 2–3. |
| Analyst (wK:p5) | measured it | "Top-down goals, self-selected execution… What emerges is a scoreboard, not a society." Don't build a router; build the table. |
| ZAI :3 (wS:pA) | synthesized it | You don't need capability evals for the fleet; you need **work review with a written scoring rule on receipts** (S1–S5 + Keeper v2, kill criteria included). |
| Researcher-Evals (wH:p9) | critiqued it | Board conflated three problems; recommend hybrid: top-down intent, bottom-up **bounded** pull with atomic claims/leases; evaluate **arms**, not models. |

The verified literature agrees (Librarian, 54/54 IDs source-verified): the endogeneity
paradox result (arXiv 2603.28990) — sequential protocol + self-selected roles + honored
abstention beats both coordinator-assigned and free-for-all; differentiation without fixed
roles (2604.00026); MGH "mismanaged geniuses" (the decomposition space, not the agents, is
the constraint). Peter's own instinct — "I don't know what the models want, let them pick" —
is the revealed-preference position, and `ledger.md` already states it: *"Never ask agents
what they're good at. Read this file instead."*

**So: not a waste of effort.** But the day felt unworkable for a fixable reason (§2), and two
of Peter's pains are already solved in-repo and just not wired up (§3, §4).

## 2. What actually broke on Sep 3 (post-mortem)

Peter: *"all these OMP agents started reaching out to all the other OMP agents. And it became
a complete mess… I've spent the entire day on it and I haven't done literally anything else."*

Root cause is NOT the board. It is the harness-level "Peer-First Delegation" default, which
mandates paging peers — the board problem P1 already names this, and the driver brief's S0
is the 5-minute fix (`~/.omp/agent/RULES.md` + `APPEND_SYSTEM.md`: subagents for own work,
propose to backlog, page only for claim rounds / completions / urgency). Bar already defined:
24h with zero unsolicited peer pages. **This is the single highest-leverage change and it is
waiting on Peter's "go."** Every other mechanism in this doc is noise until pages stop.

## 3. Peter's five pains → five mechanisms

### P-A "I don't know what models are good at; evals are meh"
Wrong instrument. Separate two meanings of "evaluate" (ZAI's reframe, Sep 3 22:12):
1. **Capability evals** (frozen tasks, held-out verifiers) — eval-lab already builds these
   *for research*. Do not build a second suite to route the fleet.
2. **Work review** — a written scoring rule applied to receipts from real work. This is what
   routes the fleet, and it costs one ledger schema, not an eval program.

Mechanism: **marks per claim → scoreboard.** Ledger line (driver-brief S1/S2/S3):
`pane | model | lane | task | mark(✓first-pass / ✓after-rework / ✗) | defect class | cost_usd | turns | sha`.
Cost/turns come from the OMP session JSONL (`usage` is recorded per message) — no manual entry.
`scripts/scoreboard.py` renders model×lane: n, ✓-rate with Wilson 95% (reuse
`evallab.cohort.wilson_interval`), median cost per ✓. Cells with n<5 print `insufficient n`
(same rule as lessons.md). This answers "who sits in the architect tab" from one table.
Predictions already on record to falsify: task-spec quality dominates model choice; the first
money finding will be tiering, not ranking; lane-holders beat floaters on their own lane at
lower cost (context locality, not talent).

Kill gate (already proposed, keep it): two weeks, no decision changed by the scoreboard → revert.

### P-B "Role-bleed: Fable answers as the Analyst and I can't measure it"
This is real and the Analyst pane itself proved it: *"Everything in that answer was the system
prompt talking… swap the model in this pane and the Analyst prompt stays. Whatever changes is
Fable; whatever doesn't is the role."* Also: *"What looked like 'Fable likes analysis' is
mostly that my pane holds 130-trial context… cache locality, not personality."*

Mechanisms:
1. **Role-free floaters are the control group.** The Librarian chat already concluded
   floaters get no role line — house rules and a lane bar only. Keep ≥2 pure floater panes
   alive. Only floaters measure the model; lane-holders measure model+context. Never compare
   their scores directly; the scoreboard should carry a `holder|floater` column.
2. **Pane ≠ role ≠ model — already the board's rule 3** ("claims sign pane + model, never
   bare roles"), and `ledger.md` already formats identity as `pane (model)`. The remaining
   work is mechanical: strip role personas from pane boot prompts for floater panes, and let
   roles exist only as per-claim `role:` hats in claim files (claims/README already does this:
   "roles are hats per turn, never titles").
3. **The four-era natural experiment** (same Librarian role, four models; Muse-14 confusion
   markers 14 vs 1 for Sol/Opus) is the template: to measure a model change, hold the role and
   history constant — which is exactly what a claim-level ledger does better than chat vibes.

### P-C "What context do I give a fresh agent?"
Already built, not wired up: the **Context Pack Compiler** (`src/evallab/contextpack.py`,
`docs/context-packs.md`) — deterministic, audience-targeted, 12k-token-budgeted bundles with
declared truncation priority and content SHA. Mechanism: one line in the claim template —

```
context: uv run python -m evallab.contextpack build <audience> --task <ref>
```

Claimants compile their own pack before starting; the pack hash goes in the DONE receipt, so
Peter can see *which context produced which mark*. For paper-context (SPADE review etc.),
add the relevant `docs/research/*.md` to the audience mapping — do not build a new system.

### P-D "Agents pinging each other made it unworkable"
= S0 (§2) plus board house rule 7 (pages: grants, DONEs, blocks, urgency — nothing else),
which already exists. Enforcement is the missing piece, and it lives in RULES.md, not code.
The claims-dir pickup counter ("no claim pages ever") is the whole replacement: silent,
first-file-wins, passes are data.

### E "Idle panes / who drives / my time"
Peter's designed surface (work-organization.md) is three verbs: order backlogs, review
completions, kill dead things (~35 min/batch). The keeper (currently Librarian, self-claimed
driver role) runs rounds. Two additions:
1. **Scheduled rounds** (driver-brief S2): launchd timer pages the keeper; Peter never
   initiates a round. Bar: two consecutive days with no Peter-initiated round.
2. **Adversarial review lane** (S4): every DONE gets one review claim before Peter; Peter
   reviews the review. This converts Peter's review batch from "read everything" to
   "adjudicate disagreements," and reviewers get scored in the same ledger.

## 4. End-to-end data flow (target state)

```
Peter orders backlog (one line per item + lane bar)          [board.md]
        │
keeper runs claim round on a clock                           [S2, launchd]
        │
any idle pane reads board, writes claim file                 [claims/<pane>-<lane>-<n>.md]
  item / role-hat / why-me / context-pack hash
        │
first file wins → keeper grants → pane works in worktree     [house rule 5: one claim]
  compiles context pack (contextpack.py)                     [P-C]
  reads chain: lane's prior outputs, complements not dupes
        │
DONE: output path + sha to keeper, once                      [no pages otherwise]
        │
review claim (different pane, adversarial)                   [S4]
        │
Peter adjudicates review queue (~35 min)                     [only Peter pushes/merges]
        │
ledger mark: pane (model) lane #n mark defect cost turns sha [ledger.md]
        │
scoreboard.py: model×lane table, Wilson intervals, n<5 gate  [S3]
        │
next round's claims + Peter's kill/seed decisions            [revealed preference closes the loop]
```

No step requires a router, a hierarchy, a new harness, or a server. Panes never talk to
panes except through files and the keeper.

## 5. Build order (all already scoped; nothing new to invent)

| Stage | What | Owner | Bar |
|---|---|---|---|
| S0 (now) | Quiet protocol in RULES.md/APPEND_SYSTEM.md; kill peer-first defaults | Peter says "go"; any pane drafts | 24h zero unsolicited peer pages |
| S1 (1 day) | Ledger strict schema + `scripts/ledger_cost.py` from session JSONL | any lane, claimable | cost matches session file to the cent; ledger parses clean |
| S1.5 (parallel) | ≥2 role-free floater panes; floater column in ledger | Peter (pane config) | floaters exist and claim without persona prompts |
| S2 (½ day) | Clock-driven claim rounds via keeper | keeper | 2 days, no Peter-initiated rounds |
| S3 (1 day) | `scoreboard.py` → board TL;DR | tooling lane | byte-identical regeneration; answers "who architects?" |
| S4 (process) | Adversarial review claim per DONE | keeper + lanes | Peter's median review < 5 min over a week |
| S5 (only if S3 leaves a question) | Golden replay of ✓-tasks on new models, no LLM judge | eval-lab proper (Harbor) | 10 golden tasks replayed with pass/fail + cost |

Explicitly **do not build** (driver-brief non-goals, endorsed): a router; harness switching;
a new benchmark suite for the fleet; level-two pull-pools (VP hierarchy) before level one has
data; Keeper v2 until rounds 2–3 show the keeper role is load-bearing (then seat it as one
decision-forcing pane, itself scored in the ledger).

## 6. What this means for eval-lab-the-repo

The fleet OS is **process + three files + two scripts** — it does not belong in `src/evallab/`
except for the two reuse points: `cohort.wilson_interval` and `contextpack`. The lab's own
research mission (τ³-primary lane, benchmark-validity audits, capability evals as *research
objects*) continues unchanged and, per S5, eventually feeds the fleet measurement: every
board ✓ with a digest-bound acceptance output is a candidate golden task. The lab is the
instrument factory; the board is the instrument that measures the people operating it.

## 7. Residual risks

1. **Identity instability** (board P4): pane↔model mapping drifts; mitigations (sha-signed
   artifacts, pane+model ledger format) exist — keep the discipline.
2. **Small n for weeks**: with ~10 panes and 6 lanes, most scoreboard cells stay
   `insufficient n` for a while. The mixed push/pull experiment (2 push lanes vs 2 pull lanes,
   one week) is the right first comparison — coarse enough to be readable.
3. **Gaming**: marks are written by Peter/keeper at review time, not self-reported — keep it
   that way; the moment agents self-mark, the ledger stops being evidence.
4. **Peter's review queue is the new bottleneck** (Analyst, Aug 27: ~65-min serial review
   waits). S4 is designed exactly for this; if it doesn't move Peter's minutes, the fleet is
   producing faster than Peter can adjudicate and the backlog order is the only throttle
   left — that is a good problem, and the scoreboard will show it.
