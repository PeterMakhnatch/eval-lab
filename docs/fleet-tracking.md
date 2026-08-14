# Fleet tracking: how the human keeps up with many agents

> Claude, 2026-08-13, at Peter's direction. Companion to
> `docs/design-additions.md` (adds **brief 12**) and `docs/parallel-work.md`
> (adds the machine-readable HANDOFF header). BUILDER: commit this file and
> `scripts/fleet-status.sh`; `scripts/` is BUILDER-owned from now on.

## Principle

**The repository is the record.** Agent sessions are ephemeral — tmux panes
close, terminal scrollback dies, chat contexts compact. Work that has not
landed as a commit, a HANDOFF entry, a queue record, an event line, or a run
directory did not happen, and no tracking UI may claim otherwise. Every layer
below renders repo state; none of them stores anything of its own.

This means tracking five agents and tracking fifty is the same problem: more
branches, same sources.

## The five sources of truth

| Source | Answers | Exists since |
|---|---|---|
| Git branches / worktrees / PRs | who changed what, when, merged or not | now |
| `HANDOFF.md` per role directory | intent: status, next step, blockers | now (protocol) |
| `queue/` directories + `events.jsonl` | what the lab executed and why (policy rule) | brief 05 |
| `runs/` + Postgres catalog | experiment results, rewards, cost, exceptions | now / brief 05 |
| `digests/YYYY-MM-DD.md` | the daily rollup of all of the above | brief 06 |

### Machine-readable HANDOFF header (protocol addition)

Every role's `HANDOFF.md` begins with four greppable lines, then free prose:

```
Status: building | blocked | review-wanted | done
Last: <one line — most recent completed step>
Next: <one line>
Blockers: <one line or "none">
```

`fleet-status.sh` and later `harbor-lab fleet` parse exactly these. Agents
update them at every stopping point; a stale header is treated as "unknown,
investigate."

## What Peter sees, and when

**Anytime, ten seconds:** `scripts/fleet-status.sh` — one screen: main's
state, every role's branch (commits ahead, last activity, uncommitted work,
HANDOFF header), PRs, queue counts, latest digest head, last events.
Exists today; `--since 6h` narrows the window.

**Daily, pushed to the phone:** the nightly job (brief 06) already commits
`digests/<date>.md`. Brief 12 extends it with a **Fleet** section and then
**delivers** it: `gh` updates a single pinned GitHub issue titled
`📊 Lab daily` (edit body to today's digest, add yesterday as a comment).
Subscribing to that one issue turns GitHub mobile/email notifications into the
delivery channel — zero new infrastructure, renders Markdown and Mermaid,
readable from anywhere. This is the "constant automated reports delivered to
me" mechanism.

**Live, when drilling in:** the Streamlit app (brief 11) gains a **Fleet**
tab — same sources, refreshed on load: role table, queue funnel, spend vs.
ceiling, canary trend, links into digests. Phoenix (brief 08) remains the
trajectory-level view; `harbor view` the single-trial view. Three panes, three
altitudes: fleet → experiment → trajectory.

**Weekly:** the digest's Sunday edition appends a system-delta paragraph and
regenerated diagrams (below).

## Diagrams that cannot go stale

Hand-drawn architecture diagrams rot. All diagrams are **generated from
observed state** into Mermaid blocks (GitHub renders Mermaid natively in
Markdown — no image pipeline):

1. **Component map** — from `compose.yaml` services actually up, queue
   presence, Phoenix reachability: boxes for executor, Postgres, Phoenix,
   dashboards, with live/down annotation.
2. **Fleet graph** — from `git for-each-ref` + worktree list: main plus role
   branches, commits-ahead counts, PR states.
3. **Experiment funnel** — from queue directory counts + catalog totals:
   proposed → approved → running → done/failed, with today's numbers on the
   edges.

Generator lives in `src/harbor_lab/diagrams.py` (brief 12); output goes into
the daily digest and `docs/diagrams/` (overwritten, committed, therefore
diffable — a diagram diff in a PR is itself a system-change report).

## Brief 12 — fleet reporting and delivery (BUILDER, after 05–07)

Build `harbor-lab fleet` (render the fleet-status sections from the five
sources; `--json` for the dashboard), `harbor-lab report --daily` (digest +
fleet section + Mermaid diagrams), delivery step in the nightly (`gh issue`
pinned-issue update; issue number in `.env` as `LAB_REPORT_ISSUE`), the
Streamlit Fleet tab, and `diagrams.py`. Absorb `scripts/fleet-status.sh`
(keep it as a thin wrapper calling `harbor-lab fleet` so the ten-second path
never needs the venv warm). Acceptance: with two role branches active and one
queued job running, `harbor-lab fleet` shows all three correctly; the morning
after an unattended night, the pinned issue contains the digest with all three
diagrams rendered; deleting Postgres and re-ingesting reproduces the same
report (rebuildability holds for reporting too).

## External tools, assessed

Evaluated 2026-08-13 for the layer they'd occupy. The lab's record layer is
non-negotiable; tools below are optional cockpits on top of it.

- **firstmate** (github.com/kunchenguid/firstmate, ~3.5k stars, active) — the
  closest match to this lab's needs and worth a trial. A liaison "first mate"
  agent dispatches crewmate agents into tmux/zellij sessions and isolated git
  worktrees; ship tasks (code → PR) and scout tasks (investigation reports);
  `/bearings` session digests; AFK mode with batched escalations. Verified
  support includes Claude Code, Grok, Codex, and Cursor Agent CLI — Peter's
  exact mix. Overlap with `parallel-work.md` is real (worktrees, PR delivery,
  handoffs): if adopted, firstmate becomes the *dispatch and live-supervision*
  layer, our protocol stays the *repo contract* (roles, ownership, HANDOFF,
  merge rules), and its worktree conventions must be reconciled with ours.
  Recommendation: do not retrofit the five agents mid-mission; trial it for
  the next wave (or for ad-hoc scout tasks) in a scratch repo first.
- **Session managers** (Conductor, Claude Squad, Vibe Kanban, Crystal) — UIs
  for running many local coding-agent sessions in worktrees. Same layer as
  firstmate with less autonomy; coverage of Cursor/Grok varies; assessments
  may be stale — re-check before adopting any.
- **Phoenix / LangSmith** — trace observability, already planned (brief 08).
  They answer "what did the agent do inside a trial," not "what is my fleet
  doing." Different layer; both stay.
- **GitHub itself** — already the shared UI: branch list, PR list, commit
  graph, and the pinned daily issue. The mobile app makes it the push
  channel. Costs nothing; works for non-technical review from a phone.

## Interim discipline (current wave, effective immediately)

No prompt changes needed. Two additions agents pick up from this doc when they
next read the repo:

1. Add the four-line header to your `HANDOFF.md` now (Status/Last/Next/
   Blockers) and keep it current at every stop.
2. When you open a PR, prefix the title with your role in caps —
   `CURATOR: add first 8 verified tasks` — so the PR list reads as a fleet
   report by itself.
