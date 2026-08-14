# What this lab studies

The focus document. When "what are we even analyzing?" fog sets in, start
here. Companion to `docs/operating-manual.md` (how to operate) and
`docs/architecture.md` (how it's built).

## The object of study

At this stage the **eval is the research object, not the model.** You cannot
learn anything from a model's score until you trust the instrument that
produced it — and this lab's first three real findings were all instrument
findings (a migrated verifier zeroing a solvable task; a judge below its
calibration floor; a canary suite catching its own migration). That ordering
is not a detour from studying agents; it is the prerequisite.

## The atomic datum: a trial

Everything analyzable here is a function over trials. One trial =
(task@version + environment + agent + model + verifier@digest) → four data
streams, all captured today:

1. **Trajectory (ATIF)** — the agent's own timeline: reasoning text, tool
   calls, observations, per-step tokens/cost. The agent's *point of view*.
2. **Environment truth** — what actually happened: artifacts and their
   digests, verifier outputs, reward dimensions, exit codes. The world's
   point of view. (A trajectory alone is the flight recorder without the
   weather; conclusions need both.)
3. **Judgment data** — rubric verdicts, calibration records, answer keys.
   Evidence about the *graders*.
4. **Lab telemetry** — queue events, policy decisions, spend, drift
   baselines. Evidence about the *lab itself*.

Closed lane, by construction: model internals (logprobs, attention) are not
available through subscription CLI agents. Behavioral analysis only.

## The question ladder

Ordered by what must be trusted before the next rung means anything. Rungs
1–2 are answerable today with data already on disk.

**1. Instrument questions** (validity — the current focus):
Does the verifier discriminate (oracle 1.0 / nop 0.0)? Does it accept
alternative correct solutions, or only the oracle's? Is the judge calibrated
(agreement vs. answer keys ≥ floor)? Did the task/verifier drift (canaries)?
Can the verifier be gamed (adversarial controls)?

**2. Behavior questions** (one agent, described not ranked):
Where in the trajectory does failure begin? Which taxonomy category
(task_invalid / harness / planning / tool_use / …)? Loops, recovery after
errors, verification-before-declaring-done, steps and cost to solution.

**3. Comparison questions** (agents ranked — requires rungs 1–2 plus n≥5):
pass@k with intervals, paired by task; cost-capability frontier; failure-
category profiles per model. Blocked today mainly by having one working
agent credential.

**4. Intervention questions** (the harness-improvement loop):
Hold task+verifier fixed, change ONE thing about the agent's setup —
instruction preamble, available tools, model, attempt budget — and measure.
This is "improving reasoning without touching weights," and it is where
understanding of *how agents act* actually accumulates.

**5. Training-data questions** (parked, deliberately):
Filter verified-successful trajectories, export SFT/RL datasets, distill.
The lab already manufactures both raw ingredients (verifiable rewards +
trajectories) as byproducts; rung 5 becomes cheap exactly when rungs 1–4 are
trustworthy. DeepSeek-R1's recipe = rung 5 standing on someone's rungs 1–2.

## Tools: have / don't need yet

| Have (wired) | Purpose |
|---|---|
| Harbor + ATIF | execution, sandboxing, trajectory capture |
| Postgres + Parquet/DuckDB | catalog + step-level analytics |
| Phoenix (+atif2otel) | visual trace inspection |
| Reward Kit + calibration corpus | judged dimensions with measured trust |
| DSPy (staged) | judge/prompt optimization experiments |
| Queue + policy + canaries + digests | unattended operation with attribution |

| Don't need yet | Trigger to revisit |
|---|---|
| Inspect AI / other eval frameworks | never, unless leaving Harbor |
| E2B / Modal / cloud sandboxes | local Docker saturation (scaling.md gate) |
| vLLM / TRL / Axolotl / VeRL | rung 5 |
| LanceDB failure memory | sidecar volume (design-additions brief 10) |

## The standing personal exercise

One rung nobody can delegate: **author one eval by hand** — task, environment,
oracle, verifier, controls — without an agent writing it. Everything else in
this lab measures understanding; that one produces it.
