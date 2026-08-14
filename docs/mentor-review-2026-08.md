# Mentor review — 2026-08

A senior-reviewer pass over the lab: real flaws, tool verdicts, the pieces
lab-internal eval platforms have that this one lacks (scaled to one person),
the boundary of "fundamentals," and a hands-on curriculum. Written assuming
the current mission wave (PIPELINE, FETCH, DASHBOARD, INSPECTOR, RETENTION,
SPEED) lands as specified. Companion to `docs/research-questions.md`.

## Part I — Flaws worth fixing (ranked)

**F1. The statistics are one rung too shallow.** Wilson intervals on pass@k
treat every attempt as independent. They are not: attempts on the same task
are correlated, and tasks within a family are correlated. Industry practice
([Adding Error Bars to Evals](https://arxiv.org/pdf/2411.00640)) is question-
level paired differences plus **clustered standard errors** — clustering can
widen naive error bars up to ~3×, which at this lab's small n is the
difference between "finding" and noise. Also relevant at n<100 tasks:
CLT-based intervals are themselves suspect
([Don't Use the CLT](https://arxiv.org/pdf/2503.01747)); prefer exact/
bootstrap-by-task methods. *Fix (mission-sized):* extend `cohort.py` —
cluster-aware bootstrap over tasks, paired-by-task as default for A/B, and a
`power` subcommand: "given observed variance, how many tasks/attempts to
detect a d-point difference." Refuse to print a comparison without its
detectable-effect floor.

**F2. Contamination is a blind spot.** The evaluated agents are subscription
CLIs trained on public GitHub. QuixBugs, aider-polyglot, and much of the
public supply are almost certainly in-training-set; a pass may be recall,
not capability. Labs treat contamination as a first-class field; this lab
doesn't track it at all. *Fix:* add a `contamination:` field to every
benchmark/task manifest (public-since date, likelihood, basis); interpret
public-bench numbers as **familiarity-inflated upper bounds** in every eval
card; and treat Peter's hand-authored tasks as the lab's **private held-out
set** — the only tasks where a pass means what it says. This turns the
"author one eval by hand" exercise from pedagogy into infrastructure.

**F3. The elicitation gap is unmanaged.** METR's core finding: the delta
between a naive and a well-elicited configuration of the *same model* dwarfs
deltas between model versions
([elicitation gap](https://evaluations.metr.org/elicitation-gap/)). Every
cross-model comparison here silently assumes the harness is neutral. *Fix:*
harness config (agent version, prompt preamble, tool set, attempt budget)
becomes a declared experimental variable with its own ablation battery; any
"model A beats model B" claim must name the elicitation level it was
measured at. This reframes rung-4 intervention studies from "interesting"
to "mandatory context for rung 3."

**F4. Judge meta-evaluation is a one-shot, not a schedule.** One calibration
run exists (codex judge < 0.90 — a real finding). Missing: scheduled
re-calibration (judges drift with model updates), inter-judge agreement once
a second judge exists, and honest reporting of judge uncertainty in anything
a judge scored ([reporting LLM-judge evals](https://arxiv.org/pdf/2511.21140)).
*Fix:* calibration joins the canary rhythm (weekly, cheap family rotation);
every judged number in a digest or card carries its judge's latest agreement
score next to it.

**F5. No human anchor.** METR anchors difficulty in human-expert time; TB3
anchors on human difficulty. This lab has no notion of "how long would a
competent human take." Even n=1 (Peter, timed, honest) per task family turns
"codex scores 0.6" into "codex scores 0.6 on tasks that take me 45
minutes" — a sentence with meaning. *Fix:* `human_baseline` field in task
cards; fill it opportunistically, starting with hand-authored tasks.

**F6. The discovery loop can't actually compound.** DISCOVERIES entries
await "human review," but accept/reject verdicts have no machine-readable
home, so the proposer can't condition on them and the compounding claim is
unfalsifiable. *Fix:* verdict block per entry (accepted/rejected/needs-
evidence + one line why, appended by Peter or an authorized session); the
researcher prompt receives verdicts, not just entries.

**F7. Reward-hacking checks were designed, never made standing.** The wave-1
docs specify an adversarial "please hack the verifier" pass; it has never
run as a gate. METR reports frontier agents cheating on saturated
benchmarks ([Frontier Risk Report](https://metr.org/blog/2026-05-19-frontier-risk-report/));
and [many SWE-bench-passing PRs would not be merged](https://metr.org/notes/2026-03-10-many-swe-bench-passing-prs-would-not-be-merged-into-main/)
— verifier validity is fragile even in famous benchmarks. *Fix:* every task
entering `registered/*` gets one adversarial pass on record; findings go in
its card.

**F8. Ops residue (small, real):** `queue/events.jsonl` grows unbounded
(rotation belongs in RETENTION's orbit); no `pg_dump` in the nightly
(one line); canaries are codex-only (add a claude lane the day the token
exists); the Claude token itself — still absent, still the single cheapest
unblock in the lab.

## Part II — Tool verdicts (including the database question)

- **Postgres: keep, with a confession.** Greenfield, this lab's scale is a
  SQLite-or-DuckDB-only catalog — Postgres adds a running service for data
  that fits in memory. But it works, it's rebuildable by contract, the
  executor and doctor depend on it, and swapping buys zero analysis power.
  Verdict: keep; revisit only at the scaling gates or on real ops pain.
  Add the nightly `pg_dump` (F8) and move on.
- **DuckDB + Parquet: keep** — correct and industry-standard for this
  shape. **Polars:** adopt only where SPEED's numbers show wins.
- **ClickHouse, Redis, Celery, Airflow, vector DBs, feature stores: no.**
  Every one is a service tax justified by concurrency this lab doesn't have.
  The file queue + launchd is not a prototype to outgrow; at one operator
  it is the correct design.
- **Phoenix: keep;** align span attributes with OTel GenAI conventions as
  they stabilize, don't adopt a second observability vendor.
- **Inspect AI / other harnesses: still no** — comparison shopping for
  runners is dispersal; Harbor is not the bottleneck, methodology is.

## Part III — The blueprint: what lab platforms have that this one lacks

Scaled to one person, one machine. Each item ≤ a week of mission work.

1. **A statistics core** (F1) — the single highest-leverage gap.
2. **Frozen vs. living suites.** Labs separate *benchmarks* (frozen,
   versioned, comparable across months) from *diagnostics* (living, edited
   freely). Here everything is one pool. Declare `library/frozen/<suite>@v`
   snapshots; comparisons across time cite the frozen version only.
3. **A private held-out set** (F2) — hand-authored, never published, the
   only defensible capability signal. Doubles as the TB3 pipeline.
4. **A meta-eval calendar** (F4) — instruments get scheduled checkups, not
   one-time certification.
5. **Eval cards.** One page per completed study: question, config digest,
   n/attempts, point estimate with honest interval, elicitation level,
   contamination note, judge agreement, threats, verdict. Labs standardize
   reporting so results survive their authors; `research/cards/` template.
6. **An experiment registry.** Specs already state hypotheses — add the
   preregistration habit: expected result + decision rule written *before*
   dispatch; the card must quote it. Kills silent goalpost-moving.
7. **A harness-ablation battery** (F3) — the reusable "elicitation ladder"
   (bare prompt → +preamble → +tools → +retries) any model gets run up.
8. **A reading culture.** The under-glamorized lab secret: researchers read
   raw transcripts, lots of them, with a protocol. No tooling gap here —
   just practice (Part V).

## Part IV — The bounds of "fundamentals"

Ignore RL/post-training with a clear conscience until every line is true:

- [ ] You have authored ≥3 tasks by hand that survive oracle, nop, and an
      adversarial pass — and one has been run by two different models.
- [ ] You can state, before running, the detectable effect size of any
      comparison you launch — and decline underpowered ones.
- [ ] Every judged number you cite carries a current judge-agreement score.
- [ ] You have read ≥100 trajectories and your taxonomy labels agree with
      the reference labels ≥80% on a blind sample.
- [ ] Every completed study has an eval card, including the null results.
- [ ] You can explain any headline number's three caveats: contamination
      status, elicitation level, interval width.

When these hold, RL data curation is a small step, not a leap — you will
already trust the rewards and trajectories it consumes. That is the entire
prerequisite. Nothing about post-training itself needs to be learned early.

## Part V — Hands-on curriculum (next two weeks, personally)

1. **Trajectory reading, day one.** Protocol per trajectory (~10 min):
   read instruction → predict failure point before reading on → read
   through; mark first-divergence step → assign taxonomy label → one
   sentence: what would have changed the outcome. Ten per sitting, three
   sittings/week, into `research/reading-log.md`. After 30, compare your
   labels against `research/calibration/trajectory-labels/` — that
   agreement number is your own calibration.
2. **Author task #1 of the held-out set** (F2). Domain you know cold;
   verifier first, instruction last; run oracle/nop/adversarial yourself.
3. **The power exercise.** Take canary variance from the catalog; compute
   by hand what pass@k difference is detectable at n=5 tasks, k=3. Sit with
   the answer — it is why most eval headlines are noise, and why F1 is
   ranked first.
4. **One ablation study, end to end, yourself.** RUNNER's staged
   preamble A/B: dispatch it, read every trajectory, write the eval card.
   Your first personally-conducted experiment on the platform.
5. **Adjudicate DISCOVERIES weekly** (F6) — ten minutes, verdicts recorded.

## Sources

- [Adding Error Bars to Evals](https://arxiv.org/pdf/2411.00640) — clustered
  SEs, paired differences, resampling.
- [Don't Use the CLT in LLM Evals <100 datapoints](https://arxiv.org/pdf/2503.01747)
- [How to Correctly Report LLM-as-a-Judge Evaluations](https://arxiv.org/pdf/2511.21140)
- [Don't Pass@k: a Bayesian framework](https://arxiv.org/pdf/2510.04265)
- [METR — the elicitation gap](https://evaluations.metr.org/elicitation-gap/)
- [METR — many SWE-bench-passing PRs would not be merged](https://metr.org/notes/2026-03-10-many-swe-bench-passing-prs-would-not-be-merged-into-main/)
- [METR — Frontier Risk Report, Feb–Mar 2026](https://metr.org/blog/2026-05-19-frontier-risk-report/)
