---
status: living
audience:
  - builder
  - operator
---

# OMP toolkit — make the orchestrator's harness lab-native

Findings from a live audit of OMP v17.3.4 (2026-08-19; internal docs at
`omp://`, notably custom-tools.md, extensions.md, hooks.md, skills.md,
ttsr-injection-lifecycle.md, task-agent-discovery.md). OMP is the
orchestrator's harness; this doc turns its extensibility surface into lab
equipment. One mission (TOOLSMITH), plus standing policies.

## What OMP offers (reference — do not re-research)

- **Custom tools**: TS modules exporting a factory `(pi) => ({name,
  description, parameters: pi.zod.object({...}), async execute(id, params,
  onUpdate, ctx, signal)})`. Discovered from `~/.omp/agent/tools` and
  project `.omp/tools` (also reads `.claude/tools` / `.codex/tools`).
  Schema-validated, streamable, cancellable — strictly more reliable than
  ad-hoc bash for structured operations. Name conflicts with built-ins are
  rejected.
- **Custom subagents**: markdown definitions in `./.omp/agents`
  (project) or `~/.omp/agent/agents` (user; `reviewer-gemini.md`,
  `reviewer-grok.md` exist there today). `omp agents unpack --project`
  exports the bundled set as templates.
- **TTSR (Time-Traveling Stream Rules)**: rulebook-matched stream
  interception — inject guidance or block when a pattern appears in the
  model's output stream. Example scaffold: `omp://skills/examples/safety-hook`.
- **Skills**: static context packages; `manage_skill` tool lets sessions
  create/update them. Authoring guides at `omp://skills/authoring-*`.
- **Advisor**: `--advisor` flag — passive per-turn reviewer injecting
  notes. **Grievances**: `omp grievances` — auto-QA log of tool friction.
- **Built-ins worth using, already installed**: ast-grep/ast-edit, lsp,
  eval (scratch execution), checkpoint/rewind, github, task (subagents),
  memory (recall/retain), collab, handoff generation.

## TOOLSMITH — one mission, five cycles

**Lease:** `.omp/tools/**`, `.omp/agents/**`, `.omp/rules/**` (or the
rulebook path `omp://rulebook-matching-pipeline.md` specifies),
`tests/test_omp_tools.py` (subprocess smoke: each tool module loads and
answers a canned call via `omp -p`), `docs/omp-toolkit-notes.md`.
Repo-side code is consumed via the evallab CLI only — no imports into
tool modules beyond subprocess calls (keeps tools harness-isolated).

**Cycle 1 — the three core tools** (`.omp/tools/`):
1. `evallab_queue` — actions: submit|list|status. Submit takes typed
   params (task_ref, agent, k, purpose — purpose REQUIRED by schema) and
   shells to the real CLI. The tool refuses billable-class without a GATE
   artifact path and never bypasses preflight: **policy enforcement moves
   into the tool schema the model calls.** The never-list stops being
   prose for the commonest operations.
2. `evallab_status` — one call returning STATUS.md summary + preflight
   quota + board Now section + open PRs as structured JSON. The agent
   morning-read.
3. `catalog_query` — NAMED queries only (from sql/ views: trials by
   family, funnel counts, quota today, traj features when they land),
   parameterized, rows as JSON. No freeform SQL through this tool — that
   stays in bash where diffs show it.

**Cycle 2 — subagent definitions** (`.omp/agents/`), each ≤60 lines,
baking the standards corpus into roles the orchestrator spawns:
- `lab-builder.md` — cycle protocol, WORKFLOW/CHECKS essentials, premerge
  duty, lease discipline.
- `trajectory-analyst.md` — reading protocol, taxonomy, truth-panel duty
  (always pair trajectory with verifier output), narrative-capture duty
  (reasoning transcript is a deliverable, keyed to the trial).
- `task-author.md` — instruction-rules + verifier-antipatterns digests,
  battery expectations, hidden-knowledge litmus.
- `verifier-skeptic.md` — the adversarial role: try to break/cheat the
  artifact under review; refute-by-default framing.
These complement context packs: packs are per-mission and dynamic;
agent defs are role-stable. Cite corpus files rather than duplicating
them where possible.

**Cycle 3 — TTSR safety rules** (from the safety-hook example): block or
warn on the mechanical never-list in the stream: `git push --force` /
`push -f` (block), `evallab registry promote` (block — human-only),
`rm -rf` touching runs/ or research/evidence (block), reuse of a
squash-merged branch (warn with the CHECKS.md lesson), hand-edits to
generated files (warn). Each rule tested via a canned transcript. Keep
the rulebook SHORT — a dozen rules that always fire beat fifty that cry
wolf.

**Cycle 4 — skills parity + evolution valve:** port lab-craft, harbor-ops
(incl. `start-env -e docker -a -i` debugging), and query-cookbook into
OMP skill format. Policy: skills are versioned repo artifacts; sessions
MAY draft improvements via manage_skill but changes land only through a
PR like any file. That is the sanctioned path for the fleet to evolve its
own context.

**Cycle 5 — advisor + grievance loop:** enable `--advisor` for overnight
orchestrator runs (config, not flag-per-run, if config supports it);
document a monthly TOOLSMITH-RECHECK: read `omp grievances`, fix the top
tool frictions, bump tool versions. The toolkit is itself a loop, not a
one-off.

**Acceptance (mission-done):** three tools load and answer canned calls
in `omp -p` smoke tests; four agent defs spawn (orchestrator demonstrates
one real dispatch each); TTSR rules demonstrably block the two hard cases
(force-push, registry promote) in a canned test; skills visible to a
fresh OMP session in the repo; notes doc records what was adopted/parked
with the omp:// citations.

## Standing policies (operator-level, effective now)

- **Repo is the record — OMP memory is ergonomics.** recall/retain may
  smooth a session; lab truth lives only in the repo. Never store facts
  in OMP memory that the repo doesn't hold.
- **Tools wrap the CLI; they never reimplement it.** One behavior, one
  home, tools are thin.
- **checkpoint/rewind sanctioned for risky refactors** inside missions;
  it does not replace git discipline.
- **`toolconv/*` docs** (per-model tool-calling conventions) are
  reference for debugging weird subagent tool behavior — park until
  needed.

## Dispatch prompt (append to the orchestrator's queue)

> Read docs/prompts/omp-toolkit.md. Register TOOLSMITH on the board
> (lease as written; low-conflict — it touches only .omp/ and its own
> tests/docs). Run cycles 1–5 under the standard protocol; it can share
> nights with existing missions since its lease is disjoint. Acceptance
> per the doc; grievance-loop recurrence goes to the board backlog as
> TOOLSMITH-RECHECK monthly.

## Changelog

- 2026-08-19 — v1: from live audit of omp:// docs, existing extension
  (herdr-omp-agent-state.ts), and bundled agent defs.
