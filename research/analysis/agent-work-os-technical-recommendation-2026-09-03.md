---
status: proposed
audience:
  - operator
  - builder
  - analyst
repository_state_commit: 1d3985b
---

# Agent work OS: technical recommendation

## Decision

Pursue the direction, but do **not** treat it as a flat self-organizing company and do not rebuild Eval Lab around it.

The useful system is a constrained work-allocation loop:

```text
human goals and priority
        ↓
typed task graph
        ↓
eligibility-filtered offers
        ↓
worker bid / abstain
        ↓
atomic allocation and isolated execution
        ↓
deterministic checks + independent review
        ↓
outcome, cost, context, and intervention evidence
        ↓
conservative routing updates
```

The organizational rule is:

> Strategy and acceptance flow top-down. Leaf-task selection may flow bottom-up. Allocation remains mechanically serialized. Evidence flows back up.

This is worth a bounded pilot because it can reduce dispatch work while producing evidence about agent configurations on real work. It becomes waste if it turns into a new UI, a permanent role hierarchy, or a learned router before comparable outcome data exists.

Run Eval Lab and the work-OS pilot as separate tracks. Eval Lab's own current-state document says its bottleneck is runs and populated columns, not more methods or documentation (`docs/NOW.md:54-65`). Keep 70–80% of effort on real experiments and use no more than 20–30% on the work-OS pilot until the pilot passes its kill gates.

## What today's investigation actually established

### The good part

The day exposed real requirements rather than proving a complete system:

- unsolicited peer paging is an interrupt mechanism, not coordination;
- task state must outlive tabs and chats;
- pane names are not reliable worker identities;
- outputs need exact heads/digests and review dispositions;
- abstention is useful information;
- context continuity can help production but contaminates model comparisons;
- the review queue can become the human bottleneck even when dispatch is automated.

The pull-board trial also produced real repository work. That is enough to justify a controlled continuation.

### What is not yet proved

The current ledger does not establish that a model is better at a lane. It contains mostly one-off, heterogeneous tasks, mostly positive marks, inconsistent reviewers, self-selected tasks, different context histories, and almost no cross-arm overlap (`research/inbox/ledger.md`). It measures delivered artifacts; it does not yet identify a model effect.

The current pull layer is convention, not a control plane:

- `research/inbox/board.md` is a manually maintained projection;
- `research/inbox/claims/README.md` specifies plain claim files with no executable exclusion;
- `research/inbox/ledger.md` is handwritten and contains backfilled unknown identities;
- the proposed work-OS driver brief is not an implemented pipeline;
- the repository's executable queue, leases, identity, evidence, and verdict paths do not consume the board.

There is also a second task source: the machine-wide `board` CLI stores `~/.config/herdr/task-board.json`. It was empty while `research/inbox/board.md` reported active and review work. Two task truths already exist.

The machine-wide board must not be trusted for concurrent claims. Its source says atomic replacement makes concurrent use safe (`~/.local/bin/board:14`, `:67-72`), but each mutation is `load → modify → os.replace` without a lock or compare-and-swap (`:108-130`, `:175-191`). Two claimers can both read `open`, both print success, and the last replacement wins. It also has no dependencies, lease generation, heartbeat, owner fence on `done`, append-only events, result manifest, or repository scope.

By contrast, Eval Lab already has good primitives for its evaluation execution path: O_EXCL submission and transitions, a flocked single executor, generated leases, heartbeats, immutable run metadata, content-addressed evidence, bounded evidence packs, provenance-typed labels, and human-only verdicts (`src/evallab/queue.py`, `runner.py`, `analysis_worker.py`, `profiles.py`, `labels.py`, `verdicts.py`). Those patterns should inform the work OS; the experiment queue itself should not be repurposed as a general task board.

## Correct the conceptual model

### 1. A model is not the experimental unit

The unit is an **arm**:

```text
arm = model + provider + harness/version + system/profile
    + tools/permissions + reasoning budget
    + context policy + persistence mode
```

`GPT-5.6 in OMP with a warm Analyst transcript` and `GPT-5.6 in Codex CLI with a fresh task packet` are different arms. So are the same OMP model with different sticky rules or tool permissions.

A **worker** is one running session of an arm. A worker may be disposable. The arm identity is stable and digestible.

### 2. Task type, contribution role, and worker identity are different

- **Task type** is an objective feature of work: implementation, evidence audit, trajectory analysis, architecture decision, operations, and so on.
- **Contribution role** is an action within one task: draft, verify, challenge, reproduce, integrate, or abstain.
- **Worker identity** is the arm and session executing that action.

A persistent tab named `Architect` collapses all three and causes role priming. An agent can appear architect-like because its system prompt, accumulated transcript, and offered work keep steering it there. That is useful specialization in production, but it is not evidence that the underlying model is intrinsically the best architect.

Roles may emerge only in the weak statistical sense: after repeated comparable tasks, an arm has better observed outcomes on a set of task features or contribution stages. A self-written `role: skeptic` line is metadata, not proof.

### 3. The sequential paper and backlog pulling address different mechanisms

The paper discussed in today's chats studies agents contributing sequentially to the **same shared task**, seeing predecessors' outputs, then choosing a complementary contribution or abstaining. It does not validate autonomous selection across an unrelated backlog.

Use the ideas in the right places:

- independent leaf tasks in the task graph may run in parallel;
- a consequential task may use a short sequential chain: draft → challenge/verify → resolve;
- each successor reads the prior artifact, not the predecessor's full conversation;
- do not serialize an entire `analysis` lane merely because tasks share a label.

Task pulling and within-task sequential contribution can coexist, but they solve different problems.

### 4. “What models want” is not a routing objective

A bid or abstention reports perceived fit under the current prompt and context. It may reflect real competence, familiarity, role conditioning, risk aversion, or preference for easy work. Treat it as one feature. Outcome evidence and controlled exposure decide whether the bid was calibrated.

## Target architecture

### A. One task store, with a human UI as a view

Do not extend either markdown claiming or the current custom `board` JSON script into a scheduler.

Trial **Beads in shared-server mode** as the local task graph and **Perles** as the human TUI. This preserves the desired shape:

- local and self-hosted;
- CLI/JSON access for every OMP pane, without MCP;
- dependencies and a ready queue;
- one task source for all worktrees;
- a human Kanban/search/dependency UI over the same data.

Beads' embedded mode is single-writer; multiple panes require its shared server. Atomic claiming must be proven against the exact installed version before adoption. `amplifier-work-tracker` is useful as a custody/conformance reference, but its contracts are still draft and it is too young to make the foundation without a local bake-off.

The adoption test is not “does the UI look good?” It is:

1. twenty concurrent claim attempts produce exactly one owner;
2. a killed worker's lease is reclaimed;
3. a stale worker cannot close after reclaim;
4. worktrees see the same dependency graph;
5. every mutation appears in an audit trail;
6. a task can carry the envelope below;
7. Perles renders and edits the same state without a second database.

If Beads fails that test, repair the smallest existing local board with a real flocked transaction and event log; do not build a web app.

### B. Typed task envelope

Every claimable task needs enough structure to support both execution and later analysis:

```yaml
id: work-...
parent_goal: ...
objective: ...
task_kind: implementation | research_audit | trajectory_analysis | architecture | operations
risk: low | medium | high
priority: 0..4
base_revision: <git sha>
scope:
  paths: [...]
  writable_paths: [...]
acceptance:
  commands: [...]
  observations: [...]
  reviewer_questions: [...]
non_goals: [...]
dependencies: [...]
required_capabilities: [browser, docker, gpu, network, write, ...]
context_refs: [...]
budget:
  wall_minutes: ...
  max_turns: ...
  max_cost_usd: ...
evaluation_mode: production | calibration | replay
created_by: human | agent-proposal
```

An agent may propose a task, but it must inherit a parent goal, acceptance bar, and budget. A task without those fields is an inbox report or idea, not ready work. This prevents autonomous task minting from becoming busywork.

### C. Arm registry

Maintain a small versioned registry of eligible arms. Bind each execution to a digest of:

- harness and version;
- model/provider pin;
- OMP profile or equivalent system configuration;
- enabled tools and permissions;
- reasoning level and token/cost ceilings;
- context policy (`fresh_packet`, `warm_program`, `warm_personal`);
- environment capabilities;
- qualification evidence and known blockers.

Eval Lab's `AgentProfile` and readiness ladder provide the right identity pattern, but the work-OS registry should remain outside the experiment queue initially. It may later emit Eval Lab-compatible profile facts.

### D. Bounded pull, not an unrestricted marketplace

The scheduler should be deterministic software, not a long-lived LLM keeper.

For an idle worker:

1. Filter ready tasks by dependencies, priority, required capabilities, permissions, budget, and writable-path conflicts.
2. Offer a bounded set—normally two or three tasks.
3. Worker returns one of:
   - bid for one task with a short fit rationale;
   - request one named missing context reference;
   - abstain with a reason code.
4. Scheduler atomically allocates one task with owner, generation, lease expiry, and worktree.
5. Worker heartbeats. Reclaim increments generation.
6. Every close, update, or result submission is fenced by owner and generation.

Why bounded pull:

- the human keeps global priority;
- the worker still contributes local fit judgment;
- avoided tasks age visibly;
- candidate sets and choices can be logged, making selection bias measurable;
- low-risk exploration can be injected without forcing the human to choose a model.

Do not infer ability from unconstrained self-selection. Easy-task cherry-picking otherwise makes a conservative arm look better than an ambitious one.

### E. Task lifecycle

```text
proposed
  ↓ triage/specification
ready
  ↓ eligibility filter
offered
  ↓ atomic allocation
claimed ──lease lost──> ready
  ↓
running
  ↓
submitted
  ├── deterministic failure → rework | failed
  └── checks pass → review
review
  ├── accepted_clean
  ├── accepted_after_rework
  ├── rejected
  └── needs_decision
```

Task state and execution evidence are separate records. A worker must not mark its own work verified. `done` is not `accepted`.

### F. Context as a manifest, not a transcript

A fresh worker receives a content-addressed context manifest:

```yaml
context_manifest_id: sha256:...
task_id: ...
base_revision: ...
required:
  - path: AGENTS.md
    digest: ...
  - path: docs/NOW.md
    digest: ...
  - artifact: <dependency result manifest>
    digest: ...
optional: [...]
excluded:
  - prior_worker_chat
selection_reason:
  <ref>: <why this reference is needed>
token_budget: ...
compiler_version: ...
```

Use the existing deterministic `contextpack.py` machinery as a starting compiler, not as the final packet. Its current unit is a broad mission type (`builder`, `analyst`, `runner`, `operator`) and living docs under a token budget. A work packet must also bind exact dependency outputs, base revision, scope, non-goals, and acceptance evidence.

Context policy is part of the arm:

- **production:** warm program workers are allowed when context continuity improves throughput;
- **calibration/replay:** fresh sessions with the same frozen packet are required;
- **personal advisors:** long-lived conversations are allowed but are not scored as interchangeable workers.

There is no reliable “remove context” operation on a long-lived session. Start a fresh worker when clean context matters.

### G. OMP worker adapter

OMP already exposes the required launch surfaces: explicit profile/model selection, non-interactive `-p`, JSON/RPC modes, `--cwd`, `--session-dir`, tool filtering, reasoning level, configuration overlays, and time limits.

The adapter should:

1. create an isolated worktree at the frozen base revision;
2. materialize the context manifest and task envelope;
3. launch the exact arm;
4. renew claim custody independently of model output;
5. capture session/events, process status, token usage, cost, and elapsed time;
6. enforce cancellation and limits;
7. submit a structured result manifest;
8. never parse conversational prose to decide completion.

Result manifest:

```yaml
schema_version: 1
task_id: ...
claim:
  owner: ...
  generation: ...
arm_id: sha256:...
session_id: ...
context_manifest_id: sha256:...
base_revision: ...
head_revision: ...
status: submitted | blocked | failed | abstained
changed_files: [...]
verification:
  - command: ...
    exit_code: ...
    output_digest: ...
artifacts: [...]
blocker:
  code: ...
  detail: ...
remaining_risks: [...]
usage:
  wall_seconds: ...
  turns: ...
  input_tokens: ...
  output_tokens: ...
  cost_usd: ...
```

### H. Verification and review

Use an outcome vector, not one vague quality score:

- acceptance checks passed/failed;
- review disposition: clean, minor rework, major rework, rejected;
- defect counts by severity and class;
- scope or policy violations;
- unsupported claims or missing evidence;
- elapsed time, cost, and turns;
- human interventions and review minutes;
- infrastructure/harness failure versus valid agent attempt.

For code, deterministic checks dominate. For research and architecture, require a structured rubric, cited evidence, rejected alternatives, and an independent review from a different arm; sample consequential outcomes for human adjudication. Agent review remains advisory. Eval Lab's human-only verdict boundary must not be silently bypassed.

## Learning without fooling yourself

### Production observations

Log every candidate set, choice, abstention, assignment, context policy, result, review, and intervention. This measures operational usefulness. It does **not** by itself compare models causally because tasks and context differ.

### Controlled replays

Promote selected completed tasks into frozen replays:

```text
base revision + task envelope + context manifest + environment
+ acceptance checks + limits + hidden evidence policy
```

Run the current champion and one challenger from the same state. Code tasks can use behavioral checks. Research/architecture tasks use blinded pairwise review plus defect extraction and periodic human calibration. Never require an “ideal patch” when observable acceptance is sufficient.

### Exploration policy

Start with static eligibility rules. On low/medium-risk work, reserve a small exploration share for challengers. Log the probability/candidate set used for allocation. Do not fit a learned router until there is meaningful overlap between arms on comparable task strata.

A lane-by-model raw success table is not enough. Report at least:

```text
arm × task_kind × risk × context_policy
n, clean-pass rate, rework rate, failure mix,
median cost per accepted result, median latency, human minutes
```

Show uncertainty and refuse rankings with inadequate overlap. Five unrelated tasks are not five comparable trials.

## Pilot design

### Fleet

Keep three populations distinct:

1. **Personal advisors:** two or three long-lived sessions Peter queries directly. Never pull fleet work and never enter model rankings.
2. **Production workers:** three or four arm-labelled workers. One warm Eval Lab worker is reasonable; the rest should usually be fresh or campaign-scoped.
3. **Review workers:** fresh, read-only where possible, and from a different model family than the author.

Retire persistent role-labelled tabs by attrition. Before closing one, require: no open claim, no uncommitted branch work, and a short durable program snapshot with goals, decisions, open questions, and artifact references. Do not export entire transcripts as routine context.

### Two-week crossover

Do not compare “two push lanes versus two pull lanes”; lane differences would be confounded with allocation policy.

Instead stratify ready tasks by kind/risk/estimated size and alternate comparable tasks between:

- **manual assignment:** Peter chooses an arm;
- **bounded pull:** scheduler offers an eligible shortlist and worker bids/abstains.

Use approximately 12–20 real tasks across at least three kinds. Pair or replay at least five informative tasks on champion and challenger arms. Keep acceptance bars fixed before execution.

Primary operational measures:

- Peter minutes spent dispatching;
- ready-to-claim latency;
- accepted results per day;
- clean-pass and rework rates;
- median Peter review minutes;
- duplicate/lost/stale claims;
- cost and turns per accepted result;
- number and reason of abstentions/no-bids.

Secondary learning question: did evidence change at least one arm/task allocation decision?

### Three real-work walkthroughs

#### Implementation

Task: repair one bounded queue or data-quality defect with a focused reproduction. Eligibility requires write access, Python, and the relevant tool. Worker receives exact base revision, paths, failure evidence, acceptance command, and non-goals. Deterministic check gates review. A different arm reviews the exact head.

#### Research/claim audit

Task: decide whether a capability claim is supported by named artifacts. Candidate arms need read-only research capability. First worker produces structured claims and citations. A second sequential contribution challenges unsupported statements. Human only adjudicates remaining disagreements or consequential recommendations.

#### Trajectory analysis

Task: classify an observed failure across a frozen cohort. Deterministic extraction runs first. Worker receives a bounded evidence pack, taxonomy, denominator, and source digests. Output is a structured sidecar. It is rejected if it silently treats harness failure as model failure or omits counterexamples.

## Failure modes and controls

| Failure | Control |
|---|---|
| Two agents believe they claimed the same task | transactional claim, read-back, owner+generation fence |
| Worker dies or idles indefinitely | heartbeat, observable reaper, stale-holder close refusal |
| Agents choose only easy work | bounded priority shortlist, aging/escalation, log candidate sets |
| Valuable task receives no bids | reason-coded abstentions, decompose or route to baseline arm after threshold |
| Persistent role prompt masquerades as capability | neutral arm labels; context policy recorded; fresh replay workers |
| Context packet is insufficient | named context request; packet revision creates a new manifest, never silent expansion |
| Keeper becomes a bottleneck or invents work | deterministic scheduler; agent proposals require parent goal/bar/budget |
| Review queue overwhelms Peter | deterministic checks first, independent defect extraction, only consequential human adjudication |
| Agent games visible checks | hidden checks where appropriate, mutation/negative controls, random audit |
| Scoreboard rewards survivorship | include failures, abandoned claims, no-bids, infrastructure invalids, and fixed denominators |
| Board and repo drift apart | one task store; board is projection; append-only events and reconciliation |
| Model/harness/context effects are conflated | digest exact arm and context manifest; controlled replays change one variable |

## Rollout

### Phase 0 — stop multiplying truths

- Freeze new features in `research/inbox/board.md`, the custom `board` JSON script, and the proposed scoreboards.
- Choose one task store for the pilot.
- Keep Eval Lab's experiment queue separate.
- Snapshot and reconcile current open/claimed/review work once; then make old boards read-only projections.
- Do not schedule “every pane must claim or pass every two hours.” Idleness is correct when no valuable task fits.

### Phase 1 — prove one vertical slice

Using Beads+Perles if the conformance test passes:

1. Peter creates a typed low-risk task in the TUI.
2. It becomes ready after dependencies clear.
3. One eligible OMP worker receives a bounded offer and atomically claims it.
4. The adapter creates a worktree and launches the frozen arm/context packet.
5. The worker emits a result manifest.
6. Focused checks run.
7. An independent reviewer records defects/disposition.
8. Peter sees task, diff, evidence, cost, and review in one place.
9. Killing a second worker demonstrates reclaim without duplicate closure.

Do not build a router before this works.

### Phase 2 — instrument real delivery

Run the two-week crossover. Keep direct relationships for high-context work. Collect complete event data. Convert only the most informative completed tasks into replays.

### Phase 3 — conservative routing

Add static eligibility, a champion/challenger policy, aging/no-bid escalation, and uncertainty-aware reports. Recommendations may change automatically; high-risk allocations should not until evidence is sufficient.

### Phase 4 — only after measured demand

Consider learned contextual routing, recursive task decomposition, or tighter Eval Lab ingestion only if the simpler system repeatedly changes decisions and reduces human effort.

## What not to build

- a custom Kanban or web application;
- another markdown claim protocol;
- a permanent LLM keeper that polls and pages everyone;
- a hierarchy of VP/director/manager agents;
- a learned router before controlled overlap exists;
- a universal “best model” score;
- a full transcript memory system;
- autonomous task generation without inherited goal/bar/budget;
- an integration that turns Eval Lab's paid experiment queue into a general work scheduler;
- automatic human verdicts;
- more synthetic methodology before current Eval Lab campaigns have durable executed cohorts.

SPADE is relevant to future synthetic-world curriculum and difficulty control. It does not solve task assignment, worker identity, context packaging, claim custody, or real-work evaluation. Do not make SPADE the work-OS architecture.

## Eval Lab direction in parallel

Eval Lab should continue. Its architecture is coherent and unusually strict about evidence, provenance, controls, and validity. The problem is that implementation has outrun exercised evidence.

The highest-value next work remains what current repo sources already say:

- generate durable control evidence for the synthetic packages;
- run the bounded 12-task FuncDAG campaign and an approved economical model cohort;
- project those actual trajectories;
- populate semantic/capability columns with named consumers and denominators;
- use observed failures to select the next perturbation family;
- defer the RL/SPADE recursion until held-out lift and task validity are demonstrated.

Do not let the work-OS project become a reason to avoid running the evaluation machinery already built.

## Kill gates

Stop or reduce the work-OS effort if any of these holds after two weeks:

- more than 20% of Peter's project time goes to board/protocol/tool maintenance;
- median dispatch time does not fall by at least half from a recorded baseline;
- accepted throughput does not improve and review time rises;
- any duplicate or silently lost claim occurs after the custody layer is declared ready;
- task/context/arm identity cannot be reconstructed for at least 95% of completed work;
- there is insufficient overlap to support even one changed routing decision;
- the system encourages agents to create or claim low-priority work merely to stay busy.

Success is not “the agents organized themselves.” Success is:

> Peter states goals and acceptance; useful tasks flow to eligible workers with little interruption; results arrive with reproducible evidence; and measured outcomes make future allocation decisions better.

## Immediate next actions

1. Continue Eval Lab delivery with a deliberately small trusted crew; prioritize real runs and populated evidence.
2. Preserve two or three personal advisor sessions and stop treating them as interchangeable workers.
3. Label new workers by arm, not role. Use fresh sessions for calibration and replay.
4. Run a local Beads shared-server + Perles conformance bake-off outside the Eval Lab execution queue.
5. Implement only the vertical slice after the task store passes atomicity/custody tests.
6. Run the two-week matched crossover; do not publish a model ranking from today's ledger.
7. Decide whether to continue only from dispatch, throughput, review, integrity, and overlap evidence.
