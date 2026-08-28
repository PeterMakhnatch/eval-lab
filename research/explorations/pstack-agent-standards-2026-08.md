---
status: completed
reviewed: 2026-08-27
subject: cursor-pstack-adoption
sources:
  - https://github.com/cursor/plugins/tree/main/pstack
  - https://github.com/can1357/oh-my-pi/blob/main/docs/skills.md
  - https://metr.org/blog/2025-07-10-early-2025-ai-experienced-os-dev-study/
  - https://metr.org/blog/2026-02-24-uplift-update/
  - https://blog.google/innovation-and-ai/technology/developers-tools/dora-report-2025/
---

# Pstack practices for sustainable agent work

## Decision

Do not install or mirror Pstack as a whole. Adapt two narrow mechanisms into native OMP skills and fix one concrete enforcement contradiction already present in eval-lab:

1. change-impact analysis that follows semantic callers and non-code contracts, then proves the safety-critical invariant;
2. repository-health work that converts repeated corrections into deterministic checks and safety-gates worktree cleanup;
3. exact parity between the local and CI typecheck gates.

Pstack is a useful source of workflow ideas. Its repository does not establish that the plugin, as a package, improves defect rate, review time, or maintainability. Its claims about "fearless parallelism" and operating like an engineering team are author positioning, not measured comparisons.

## Method

The review used:

- Pstack 0.14.5 source, including its manifest, skills, principles, playbooks, agents, and automation references;
- official OMP documentation for skills, context files, extensions, hooks, and custom tools;
- an audit of eval-lab's existing governance, checks, engineering standards, CI, worktree workflow, and review skill;
- a source review by the OMP Librarian;
- parallel Pstack source, external-evidence, and eval-lab-control audits;
- an adversarial Grok review focused on Cursor lock-in, prompt bulk, unsafe autonomy, and duplicated policy;
- primary METR and Google DORA publications, plus a directional vendor analysis from GitClear.

No Harbor run, cloud environment, plugin installation, or paid evaluation was performed.

## What Pstack contains

Pstack is an MIT-licensed Cursor plugin by Lauren Tan. The manifest registers `skills/` and `agents/`; most of the portable material is Markdown. The package also assumes Cursor-specific capabilities and, in some playbooks, Graphite, `/loop`, cloud agents, Bun, and TypeScript helpers.

| Layer | Examples | Portability to OMP |
|---|---|---|
| Engineering principles | prove the real artifact, subtract before adding, encode lessons in structure | Portable as ideas; many already exist in OMP or eval-lab |
| Task playbooks | bug fix, investigation, performance, refactor, worktree cleanup | Adaptable when scoped to eval-lab's commands and approval gates |
| Review workflows | blast radius, heterogeneous interrogation | OMP already has LSP, `task()`, reviewers, and a repository review skill |
| Cursor routing | slash commands, sticky `poteto-mode`, `.cursor/rules` model map | Do not port; OMP discovers skills and routes subagents differently |
| Executable helpers | Graphite/PR watchers and orchestration scripts | Do not import; Cursor-specific and partly TypeScript/Bun |
| Autonomous operations | overnight PR and merge flows, background triage | Reject where they bypass eval-lab's approval or merge boundaries |

## Existing eval-lab controls

Eval-lab already has stronger deterministic coverage than a wholesale Pstack import would add:

- `AGENTS.md` defines language, evidence, security, and paid-execution boundaries.
- `agents/WORKFLOW.md` defines isolated worktrees, leases, handoffs, and merge discipline.
- `agents/CHECKS.md` defines exact-head green and deterministic-test requirements.
- `docs/engineering.md` defines boundary models, I/O seams, immutable evidence, checks, and measured performance claims.
- `.claude/skills/review/SKILL.md` rejects unevidenced acceptance claims and checks the PR head, diff, lease, and handoff.
- `src/evallab/governance.py` and CI enforce root structure and governance markers.

Copying Pstack's proof, clean-cutover, review, TDD, or model-routing instructions into another always-loaded catalog would create competing sources of truth.

### Concrete gap found during the audit

The local premerge script allowed 28 `ty` diagnostics while CI and `docs/engineering.md` required zero:

| Surface | Prior value |
|---|---:|
| `scripts/premerge.sh` | `TY_BASELINE=28` |
| `agents/CHECKS.md` | ratchet at 28 |
| `.github/workflows/typecheck.yml` | `TY_BASELINE=0` |
| `docs/engineering.md` | zero diagnostics |

That made the documented local reproduction weaker than CI. This change aligns the local script and check contract with the existing zero-diagnostic CI gate.

## Adoption matrix

| Pstack mechanism | Decision | Eval-lab implementation | Reason |
|---|---|---|---|
| `blast-radius` | Adapt | `.omp/skills/change-impact/SKILL.md` | Adds a scoped procedure for semantic callers, serialized contracts, SQL, CLI, and task/verifier boundaries without another sticky rule |
| `encode-lessons-in-structure` | Adapt | `.omp/skills/repository-health/SKILL.md` | Converts repeated corrections into types, lint, tests, governance checks, or idempotent commands before adding prose |
| worktree-cleanup safety gates | Adapt | `repository-health` skill | Dirty and in-use work remains protected; inventory comes from Git, mission state, handoffs, and PR state |
| evidence-first `why` archaeology | Fold into change impact | `change-impact` skill | Useful for surprising guards and old compatibility code; the full seven-investigator Cursor workflow is excessive here |
| prove the real artifact | Keep existing | OMP delivery contract, `agents/CHECKS.md`, review skill | Already authoritative; a copied skill would duplicate policy |
| heterogeneous review | Keep existing | OMP reviewer agents and `review` skill | Available when risk warrants it; mandatory panels would add cost and noise |
| verification-skill generator | Reject for now | Existing `scripts/premerge.sh`, focused tests, smoke commands | A generator does not solve a demonstrated missing verification path in this repository |
| sticky `poteto-mode` and principle index | Reject | None | Always-on prompt weight and citation ritual conflict with scoped OMP skill loading |
| `no-comments` and blanket prose cleanup | Reject | None | Comments and docstrings can carry mathematical, provenance, safety, and external-contract rationale |
| `.cursor/rules` model routing | Reject | OMP configuration and explicit task agent selection | Cursor model names and routing do not map directly to OMP |
| Graphite, `/loop`, cloud-agent, Bun, and TypeScript helpers | Reject | None | Toolchain mismatch, repository language rule, and approval-boundary conflicts |
| autonomous paid runs, external messages, or merges | Reject | Existing standing approvals and merge owner | Contradicts eval-lab's human authorization and exact-head review gates |

## External evidence and limits

### METR developer productivity study

METR's early-2025 randomized trial covered 16 experienced open-source developers and 246 issues in repositories they knew well. In that setting, AI-allowed work took 19% longer, while developers believed AI had made them faster. This is evidence that perceived speed is not a reliable quality or productivity measure for one early-2025 tool setting. It is not evidence that current agents generally reduce productivity.

METR's February 2026 update explicitly says the early result is out of date. Its later study had severe participation and task-selection effects, so METR describes the newer speedup estimates as unreliable. The durable implication for this project is narrower: measure the actual workflow and artifact rather than relying on agent or developer impressions.

### DORA 2025

Google's DORA report surveyed nearly 5,000 technology professionals. It reports 90% AI adoption, more than 80% self-reported productivity improvement, and 59% self-reported positive code-quality influence. It also reports limited trust and says the continuing challenge is ensuring software works as intended before delivery. These are broad survey associations and perceptions, not a causal test of Pstack or any particular agent workflow.

### GitClear

GitClear reports higher short-term churn, duplication, and lower moved/refactored code in its repository telemetry after widespread AI-assistant adoption. The analysis is observational, uses vendor-defined metrics, and cannot isolate AI use from changes in repository mix, developer population, or process. It is directional support for monitoring churn and duplication, not a basis for a hard LOC or refactoring quota.

### Pstack itself

The Pstack source demonstrates that the workflows exist and can be read or invoked in Cursor. It does not publish a controlled evaluation of defect rates, maintainability, or review outcomes. Eval-lab should evaluate any future skill change against a named behavior and fixed task set rather than treating plugin adoption as validation.

## Standards going forward

1. **One fact, one authority.** A quality rule has one canonical document or executable gate. Other surfaces link to it.
2. **Second correction triggers enforcement design.** Before adding repeated prose, ask whether a type, schema, lint, test, governance check, or script can make the failure impossible or visible.
3. **Cross-boundary changes require impact analysis.** Follow LSP references and non-code contracts before changing exported symbols, schemas, file layouts, SQL, CLI behavior, or verifier interfaces.
4. **Completion requires direct proof.** Exercise the actual changed surface or a focused check that would fail on the defect. Compilation and agent summaries are not behavioral evidence.
5. **Local gates cannot be weaker than CI.** The local premerge contract must reproduce the same diagnostic thresholds and required checks.
6. **Parallelism follows ownership.** One writer per worktree; dirty and in-use work is never a cleanup candidate without an explicit human decision.
7. **Skills stay scoped.** Project-specific procedures belong in `.omp/skills/`; avoid sticky mode catalogs and avoid duplicating `AGENTS.md`.
8. **Executable automation needs a demonstrated gap.** Do not add hooks, tools, extensions, or another language when a scoped skill or existing command is sufficient.

## Implemented changes

- Added `.omp/skills/change-impact/SKILL.md`.
- Added `.omp/skills/repository-health/SKILL.md`.
- Registered `.omp/` in the frozen root map.
- Changed the local `ty` baseline from 28 to 0.
- Updated `agents/CHECKS.md` to match CI and `docs/engineering.md`.

The skills are original eval-lab adaptations. Their provenance sections link to the relevant MIT-licensed Pstack sources; no Cursor code, TypeScript helpers, automation daemons, model routing, or sticky mode was copied.

## Primary sources

- [Pstack repository and README](https://github.com/cursor/plugins/tree/main/pstack)
- [Pstack plugin manifest](https://github.com/cursor/plugins/blob/main/pstack/.cursor-plugin/plugin.json)
- [Pstack blast-radius skill](https://github.com/cursor/plugins/blob/main/pstack/skills/blast-radius/SKILL.md)
- [Pstack encode-lessons-in-structure principle](https://github.com/cursor/plugins/blob/main/pstack/skills/principle-encode-lessons-in-structure/SKILL.md)
- [Pstack worktree cleanup playbook](https://github.com/cursor/plugins/blob/main/pstack/skills/poteto-mode/playbooks/worktree-cleanup.md)
- [OMP skills documentation](https://github.com/can1357/oh-my-pi/blob/main/docs/skills.md)
- [OMP context-file documentation](https://github.com/can1357/oh-my-pi/blob/main/docs/context-files.md)
- [METR early-2025 trial](https://metr.org/blog/2025-07-10-early-2025-ai-experienced-os-dev-study/)
- [METR February 2026 update](https://metr.org/blog/2026-02-24-uplift-update/)
- [Google DORA 2025 summary](https://blog.google/innovation-and-ai/technology/developers-tools/dora-report-2025/)
- [GitClear maintainability analysis](https://www.gitclear.com/the_ai_code_quality_maintainability_gap)
