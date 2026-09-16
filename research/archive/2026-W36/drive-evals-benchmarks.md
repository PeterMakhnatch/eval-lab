---
source_url: "drive:.Agents/Evals-Benchmarks (Google Drive; Peter's curated doc, exported 2026-08-19)"
source_type: drive
retrieved: 2026-08-19
license_note: Peter's own distillation — freely usable in-repo; upstream Terminal-Bench docs it summarises are paraphrased, not reproduced
status: raw
feeds:
  - library/curated/standards/instruction-rules.md
  - library/curated/standards/verifier-antipatterns.md
  - library/curated/standards/task-debugging.md
  - library/curated/standards/authoring-workflow.md
---

# Drive export — "Evals / Benchmarks" (Peter's curated notes)

Provenance: Google Drive > .Agents > "Evals / Benchmraks" doc, exported
2026-08-19 by Claude session. Peter's own distillation of TB craft docs,
discussion #224, arXiv 2607.12217, and practitioner threads. Input for
LOOP-HARVEST distillation; overlaps repo docs partially — the debugging
heuristics and bad-task taxonomy phrasing here are the unique value.

## TB3 contribution workflow
Learn what makes a good task → task proposal → maintainer approval
(GitHub Discussions task-ideas / Discord) → fork, implement, PR, iterate
with reviewers. Rubric: verifiable / well-specified / solvable / difficult
/ interesting-realistic / outcome-verified. Target: <30% frontier solve
rate. Sources: terminal-bench CONTRIBUTING.md, rubrics/task-proposal.md,
discussion #224.

## Taxonomy of bad tasks (instruction side)
- Best instructions: short, well-specified, self-explanatory, realistic,
  state the end goal; "agent = smart human"; say things once, clearly;
  tell expectations of the RESULT, not how it's tested — but sufficient
  that meeting them implies passing.
- Anti-patterns: AI-generated attention-grabbing instructions;
  over-prescription (telling the agent HOW); describing failure modes of
  the system; clerical difficulty (formatting traps, many mini-
  deliverables — "if a task is hard for SOTA models it shouldn't be
  because they can't spell strawberry"); restrictions (time/resource caps
  agents can't see); making tasks bigger as fake difficulty.

## Hidden knowledge (the classic verifier sin)
Solution displays insider knowledge the agent cannot possess (which record
is corrupted, which commit broke it, the preferred spelling of a name, the
exact bytes behind a golden file). Litmus: the author should be able to
walk from instruction.md to the solution from first principles. A proper
oracle for a diagnostic task INVESTIGATES (runs discovery commands) before
fixing; an oracle that jumps to the answer is using ground truth the env
never exposes. Underspecification often surfaces only when you read the
solution.

## Verifier principles
- Outcomes, not implementations; never coupled to the oracle's script.
- Usual bug: verifier built backwards from the author's own answer.
  Instead: start from the instruction's end state, ask "what would ANY
  correct finish look like?"
- Discovery process: if multiple agents produce genuinely-correct results
  the tests reject → widen the verifier; if wrong → keep the test.
- Prefer "agent builds something that computes the answer" (re-runnable)
  over "agent pastes a magic final answer".
- LLM-as-judge: TB3 allows only in extraordinary cases with evidence the
  judge essentially never errs (e.g., multiple different LLMs always
  agree).

## Debugging a task (three-way split)
1. Environment/solution broken — even the oracle can't pass.
2. Tests wrong — solution fine, checks picky/misaligned.
3. Agent failed for real — task fair, model didn't figure it out.
Techniques: run the oracle first; `harbor tasks start-env -p <task> -e
docker -a -i` to shell into the env instead of full runs; replay the
agent's commands as a script and see where reality diverges from tests;
ask the agent in plain language what it thought the goal was. Failure
triage: misunderstood goal → unclear instruction; reasonable-but-rejected
→ tests too narrow; stuck in nano → maybe real weakness, maybe bad UI
requirement; never had the info → hidden knowledge; almost solved →
possibly hard for good reasons (keep).
Over-specificity probe: temporarily TELL the agent the approach. Still
fails verifier → verifier broken. Passes → difficulty was approach-
finding, verifier fine.

## Reward hacking
Assume the model WILL hunt for leaks. Probe: include test signatures in
the prompt and check for unexpected success; run a "please hack" variant
and analyze trajectories (automated on TB PRs now). Environment must not
contain the answer in reachable form.

## Difficulty
Real difficulty is conceptual: can the agent find the approach, debug,
reason before executing. Evidence standard: multiple SOTA agents run,
interesting failure examples with debug output on file. If agents do 90%,
rewrite the task around the remaining 10%. Tasks trivial once the
instruction is well-written were context problems, not capability probes.

## TB3 task construction (the 9-step sequence)
1 domain expert picks a paid, realistic workflow with a meaningful failure
mode → 2 controlled synthetic world → 3 oracle proves solvable → 4
independent outcome/invariant verifier → 5 oracle=1, nop=0 → 6 run
frontier agents, inspect trajectories → 7 classify failures (capability
vs ambiguity/infra/leak/verifier bug) → 8 harden vs shortcuts, calibrate
tolerances → 9 automated checks + human review pre-merge.

## Eval construction ideas (Harbor-flavored)
Freeze all data (deterministic reproducibility); pre-install exact
toolchains in the image (restrict scope deliberately — no browser if
testing reasoning; mocked local search if testing search); hybrid grading
(programmatic for objective truths + judge TOMLs only for subjective
quality); sanity-check the eval itself.

## Benchmark links (fetch-candidates, register human-only)
Harbor-Index (harbor-index.org), TB3 (frontierbench.ai), RSI-benchmark
(rsi-benchmark.com), PostTrainBench (posttrainbench.com — tracing/
observability angle), FrontierCS (frontier-cs.org, cognition blog),
agentbehavior.dev (Basis & Braintrust). Harbor cookbook:
harborframework.com/news/harbor-cookbook.
