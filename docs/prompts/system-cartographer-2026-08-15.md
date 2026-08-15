# System Cartographer mission — 2026-08-15

This mission is for a long-context Codex agent. It is deliberately read-heavy and
product-oriented: its job is to make the system legible before another large feature wave.
It may start while M006/M007 repairs run because its write lease is documentation-only.

## Copy-paste prompt

```text
/goal Produce a repository-grounded system and product map that lets Peter explain what Eval Lab is, operate what exists, and decide how it evolves from evaluation R&D into a real-environment post-training data platform.

Lab: ~/Developer/eval-lab. Run: cd ~/Developer/eval-lab && git fetch origin && git worktree add .worktrees/system-cartographer -b role/system-cartographer origin/main && cd .worktrees/system-cartographer && uv sync --locked. Follow AGENTS.md and agents/{WORKFLOW,STRUCTURE,OWNERS,CHECKS}.md. This is a 3–6 hour read-heavy architecture mission. Record exact agent/model. Own only docs/checkpoints/2026-08-15-system-cartography.md, docs/system-cartography.html, and agents/handoffs/system-cartographer.md. Do not edit src, tests, policy, tasks, queue, runs, evidence, or existing architecture claims. Inspect open M006/M007 PR heads read-only and label them pending rather than merged.

Read architecture, analysis-loop, data-architecture, operations, operator-demo, observability, operating-manual, path-forward, the roadmap HTML, current CLI/modules/dashboard, task registry, experiment program, existing run evidence, and active handoffs. Trust executable code and persisted evidence over prose. Do not invoke a model, Docker, Harbor job, external service, paid action, deployment, or publication; --help and fixture-backed/read-only commands are allowed.

Answer concretely:
1. What can Peter do today from task selection through Harbor execution, persisted evidence, analysis, and next experiment?
2. Which components are proven live, fixture-proven only, pending in PR, blocked, or merely designed?
3. What artifact crosses each boundary, and what stable ID/digest joins task → experiment → job → trial → trajectory → analysis → proposal?
4. How do Peter, development agents, evaluated agents, Harbor, PostgreSQL/Parquet, Phoenix, GitHub, and future training infrastructure interact?
5. Where do current docs contradict source or current state?
6. If the end goal is real-environment post-training, which present components remain, what exact versioned data product bridges to training, and what is absent?
7. What should Peter accomplish in a one-hour operator session today, in 30 days, and in 90 days?
8. Evaluate a version-aware Harbor executable skill/contract and a Phoenix trace-evidence bridge. Recommend their exact form and timing; do not recommend a copied documentation wiki or Phoenix merely because it is installed.
9. Recommend no more than six 3–8 hour implementation missions suitable for Gemini/Grok, dependency-ordered, each with one outcome, exclusive path lease, executable acceptance, failure boundaries, and a stop condition.

Deliver a concise one-page overview followed by a source-cited deep map. The self-contained HTML view must make planes, joins, current status, and dependencies visually navigable without a server. Include: one end-to-end diagram; a capability matrix; artifact/ID ownership; trust and approval boundaries; exact safe demo commands; the current missing joins; a training-data boundary; a dependency graph for proposed missions; a “do not build yet” section; and a 20-minute teach-back checklist.

Every capability claim cites implementation plus a test or persisted evidence path, or is labeled unproven. Separate observation from inference. Do not call Oracle/Nop model evidence, a green test a live production proof, Phoenix canonical storage, or an open PR merged behavior. Challenge the premise if Eval Lab is not yet accurately called a post-training platform.

Acceptance: a mid-level engineer can answer “what ran, what is running, what comes next, what tasks exist, where analysis appears, who can authorize what, and how this could later produce training data” without reading the repository. Run reference/link checks and git diff --check. Rebase origin/main, open PR `SYSTEM-CARTOGRAPHER: map the evaluation R&D platform`, and stop at review; do not merge.
```

The prompt is just under the usual 4,000-character `/goal` budget.

## Candidate components the report must evaluate

These are hypotheses, not preapproved missions:

| Candidate | Zero-to-one outcome | Earliest dependency |
|---|---|---|
| Harbor Contract | Version-aware executable Harbor playbook, capability manifest, drift tests, and safe recipes | One current repair merged |
| TraceGraph | Phoenix spans correlated by experiment/job/trial/analysis IDs and linked from the UI | M006 plus a live flight |
| Experiment Studio | Compose a one-variable experiment, preview power/cost/elicitation, and emit only a proposed spec | M007 plus one registered task |
| Ladder | Deduplicated bounded experiment curricula with explicit evidence/power/budget stop conditions | Experiment Studio |
| Foundry Batch | Generate candidate tasks from saved responses, certify or retain exact rejection, never self-register | M007 plus one certified seed |
| Training Export | Rebuildable SFT/RLVR-ready trajectory datasets with held-out splits, provenance, licenses, and leakage controls | Valid non-control trajectories |

The likely product framing to test is: **Eval Lab is currently a real-environment
evaluation R&D control plane and evidence/data factory.** Harbor owns execution; Eval Lab
owns intent, authorization, provenance, measurement, analysis, proposals, and human gates.
The missing bridge to post-training is a reviewed, contamination-aware, reward-bearing
trajectory data product. Training infrastructure itself should remain a separate plane until
that data contract is proven.
