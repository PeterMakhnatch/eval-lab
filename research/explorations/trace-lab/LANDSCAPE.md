# Trajectory-analysis tools and method: landscape (2026-09-29)

Question (Peter, 2026-09-29): which open-source or cheap tools *analyze* agent
trajectories (not just display them), how do they work, do they work with
Harbor, and what is the rigorous process (anchor: arXiv 2605.08545).

Spend: 3 Exa searches ($0.021). Everything else $0: no uploads, no model
calls, no launches. Evidence basis per claim: **executed** (run here on our
Harbor trials), **source-read**, **docs-read**, or **[INFERENCE]**. Research
agents: MeridianStack, TransluceHAL, ResearchAnalyzers, IndustryInsight,
MethodPapers, HarborCompatSmoke (session 2026-09-29).

## Bottom line

- The field has one clear centre: **Inspect Scout** (Meridian Labs / UK AISI)
  and **Docent** (Transluce). The anchor paper names these two plus Apollo's
  closed engine as "the 2025 wave" of log-analysis tools, and its own case
  study ran on Docent. Both are free to start and both import Harbor runs.
- Everything else is either (a) a research analyzer that needs a small
  Harbor converter, (b) a trace store with optional LLM judges
  (observability, not insight), or (c) closed and sales-gated.
- No tool handles two Harbor *recording options* our MiMo runs use:
  Terminus-2 `trajectory_config {raw_content: true, linear_history: true}`,
  which Eval Lab sets for SFT export (`docs/execution-tiers.md:308-313`;
  every HAR-81/HAR-90 trial `config.json`). `raw_content` stores the raw
  model reply and writes no `tool_calls` (harbor 0.21.0
  `terminus_2.py:218-219, 1433-1498`); `linear_history` splits a run that
  hits context summarization into `trajectory.json` +
  `trajectory.cont-N.json`. Neither is MiMo-specific: any model on these
  settings produces the same shape, and stock Terminus-2 runs (defaults
  off) carry `bash_command` tool calls. The files are valid ATIF-v1.7.
- The shared pre-processor now exists: `normalize/harbor_normalize.py`
  (see "Harbor compatibility" below).
- Method matters more than tooling: the paper's four principles and the
  validation numbers below are what make a finding credible.

## How the field splits

| Layer | What it answers | Examples |
| --- | --- | --- |
| Viewer | What happened in this run? | `harbor view`, Inspect View, SWE-agent inspector, OpenHands visualizer |
| Trace store (observability) | Store, search, count tokens/cost, replay | Phoenix, Langfuse, MLflow, Opik |
| Analyzer (insight) | Why did runs fail, how often, is the score valid? | Scout, Docent, AgentRx, AgentPex, CLEAR, Kura, Hodoscope |
| Eval generator | Turn a suspected behaviour into a repeatable test | Petri, Bloom, Petri Dish |
| Method | How to make any of the above trustworthy | 2605.08545, UK AISI "Seven steps", Mohl et al. |

## Tools that matter for Harbor

| Tool (org, licence) | How it works | Harbor fit | Cost | Verdict |
| --- | --- | --- | --- | --- |
| **Inspect Scout** (Meridian Labs + UK AISI, MIT) | Imports transcripts into a local Parquet DB. A *scanner* is a Python function per transcript: `grep_scanner` (regex, free) or `llm_scanner` (asks one question, gets a typed answer with `[M12]` message citations). Results are Parquet; *validation sets* score scanners against your labels (precision/recall/F1); `scout view` UI. | **Native** `scout import atif` (executed, 0.5.3 = latest). Continuations become separate transcripts, linked in metadata; summaries inlined; command text visible. | Local; grep free; LLM scanners cost per transcript x scanner | **Use** as the local scanner layer |
| **Docent** (Transluce; SDK licence text Apache-2.0, GitHub says "Other") | Upload runs to a collection. An *analysis plan* = DQL steps (read-only SQL to pick runs) + *Reading* steps (an LLM reads each picked run, answers in a JSON schema with citations) + clustering. Label sets measure agreement with humans. A Claude Code plugin drafts the plan. | **Native but strict** (executed, 0.1.87 = latest): rejects any trial with more than one file under `agent/` (3 of our 6 sample trials; 4 of 10 HAR-90). Our `trace-lab/docent/export_harbor.py` stitches them. | Hosted; reading models free within an unpublished weekly quota. Self-host "not generally supported" (docs). | **Use** for deep dives, after upload approval |
| **Petri / Bloom** (Meridian + UK AISI, MIT) | Generate *new* tests: an auditor model plays user and environment against a target model; a judge scores the transcript on 35+ dimensions (1-10). Bloom writes scenarios for one behaviour. | Not an analyzer of existing runs. But Petri's judge `audit_judge` is itself a Scout scanner (source-read, `_judge/judge.py:31-39`), so it can score imported Harbor transcripts with our own dimensions. Its prompt assumes an auditor-vs-target chat. | Judge calls per transcript; full audits are expensive | **Later**: reuse judge with custom dimensions; Bloom to turn a validated failure into a repeatable eval |
| **Petri Dish** | Petri run against real CLI scaffolds (Claude Code, Codex, Gemini CLI) | None for Terminus-2 | Model + sandbox | Skip |
| **Inspect Harbor** (Meridian, MIT, v1.0.0 2026-09-23) | Runs Harbor tasks inside Inspect, so logs are native Inspect logs that Scout/Docent/Petri read with no glue | Re-run only; cannot import existing job dirs; Terminus-2 is not a supported solver, so it is a different harness (docs/source-read) | Normal run cost | Option for future runs only; not comparable with Terminus-2 results |
| **UK AISI check-trajectories workflow** (inspect_evals skill) | Step-by-step SOP on Scout: default scanners (outcome summary, external failure, formatting failure, reward-hacking success, refusal) then a validity table crossing flags with pass/fail | Via Scout | Scanner cost | **Copy** as the SOP skeleton |
| **Harbor built-ins** | `harbor view`: local run browser. `harbor analyze <trial-or-job>`: runs an evaluator agent (default Claude Code + claude-haiku-4-5) in a container with a rubric (default: reward_hacking, task_specification). `harbor check`: task-quality rubric. | Native. `view` executed; `analyze` exists in 0.21.0 and 0.23.0 (help output executed), not run | view free; analyze = agent + model + container per trial | view: **use**; analyze: cheap candidate for the grader/task audit, needs a spend OK |
| **Hodoscope** (AR-FORUM, MIT, 121 stars) | Split runs into actions, LLM-summarize each, embed, 2-D map, then show where group A's density differs from group B's (e.g. MiMo vs another model) | Glue or via a Docent collection. Executed extraction only: 0036-e 167 messages -> 83 actions. Summarize/embed need paid APIs. | One LLM call per action + embeddings | Watch: good for model-vs-model contrast later |
| **AgentPex** (Microsoft, 11 stars; paper "Willful Disobedience") | Extracts rules from the system prompt and tool schemas, then 8 LLM evaluators check each trace (plan, output spec, forbidden transitions, argument grounding) | Glue: flatten ATIF to `{messages}` JSON | ~$0.019/trace in paper; OpenAI-compatible endpoints | **Trial**: the one tool aimed at passing-but-wrong runs |
| **AgentRx** (Microsoft, MIT) | Synthesizes per-step constraints, logs violations, then a judge names the first unrecoverable step and a 10-category cause | Glue (ATIF -> its IR); Azure default | LLM-heavy per step | Watch |
| **CLEAR agentic** (IBM, Apache-2.0) | Per-step + per-trace LLM critiques, clustered into issue categories, local dashboard | Glue via OTEL spans or CSV | Per step + per trace; any LiteLLM model | Watch |
| **Kura / OpenClio** (MIT) | Clio-style: summarize each run, embed, cluster, label clusters | Glue to `{role, content}` messages | Per run + embeddings | Later, once we have >100 runs |
| **Phoenix** (Arize, Elastic 2.0: source-available, not OSI open source) | Trace store with an official ATIF importer; judges are yours to add | **Native** (executed, client 3.5.0 pure converter): merges continuations when the ref chain is intact (A: 197 spans, 1 trace) | Self-host free | Viewer/store only |
| **MLflow** (Apache-2.0), Langfuse, Opik, Laminar | Trace stores with judge/"signal" features | Via Harbor's `atif2otel` plugin (docs-read) | Self-host free + your judge calls | Not needed now |

Reference label sets for testing any judge: TRAIL (Patronus), MAST
(Berkeley), Who&When, METR MALT.

Screened out or closed: Apollo Watcher and its log engine (closed,
partnership-gated; the paper's "Apollo Revealer"), Fulcrum Lunette (closed;
first 40 investigations free), LangSmith Insights (closed; Harbor has a live
plugin), Braintrust, Patronus Percival, Datadog, HAL harness (archived; reuse
its method, not code), datadog-labs/trajectory (live CLI sessions only), and
small single-author repos from the Exa sweep (TraceEval, TraceForge,
agent-triage, TraceBrain, AMDM).

## Harbor compatibility, executed on our trials

Sample: A = har81-l-d-a4-arvo-42496599 and B = har81-p-d-candidate-2684
(each: parent + cont-1 + 3 summarization files); C = har81-p-d-format-code-000240
and D = har81-l-d-a2-arvo-18737 (one file each); E = har90-mimo-0758-c
(5-step parent + unlinked 213-step cont-31); F = har90-mimo-0036-e (one file).

| Trial | Scout 0.5.3 transcripts | Docent 0.1.87 converter | Phoenix client 3.5.0 spans |
| --- | --- | --- | --- |
| A | 2 (split) | ConversionError | 197, one trace (merged) |
| B | 2 (split) | ConversionError | 171, one trace (merged) |
| C | 1 | OK, 41 messages | 41 |
| D | 1 | OK, 235 messages | 235 |
| E | 2 | ConversionError | parent only when following refs (cont-31 is orphaned) |
| F | 1 | OK, 167 messages | 167 |

In all three tools the MiMo command text survives verbatim; none derive
tool calls from it. The tail of E is invisible to any loader that follows
`continued_trajectory_ref`, and duplicated by any loader that imports every
file.

**Shared fix, built 2026-09-29:** `normalize/harbor_normalize.py` writes
one stock-shaped ATIF file per trial to `normalized/<set>/<job>/<trial>/`
(gitignored; raw files untouched):

- Stitches continuations with probe-03's own assembly
  (`capabilities.assemble_trial`: duplicate, cumulative superset, new
  session, missing head), so the 0758-c cont-31 tail is kept.
- Restores stock Terminus-2 `tool_calls` (`bash_command`
  {keystrokes, duration}, `mark_task_complete`, `call_<episode>_<n>` ids,
  observation `source_call_id` for single commands). Source per step in
  `extra.trace_lab.tool_calls_source`: `recorded` (the harness's
  `extra.step_layers` record, HAR-81), `replay` (the Eval Lab MiMo parser
  the harness ran, HAR-90), `native` (already present), or no calls
  (`rejected`, `harness_standin`, `none`).
- Keeps the raw model message verbatim and the probe-03 step ref in
  `extra.trace_lab.ref`, so probe-03 labels map onto normalized steps.

Executed on all 54 trials: 54/54 validate against harbor 0.21.0
`Trajectory`; assembled step counts and per-step accepted/rejected splits
match probe-03 on 54/54. HAR-81: 3,378 recorded agent turns, 135 rejected,
10 empty; 3,759 bash calls. HAR-90: 389 replayed turns, 1,888 rejected,
31 harness stand-ins, 3 empty; 427 bash calls.

```sh
cd research/explorations/trace-lab  # from the eval-lab checkout
E=~/Developer/eval-lab/.worktrees
U="uv run --no-project --python 3.12 --with harbor==0.21.0 python"
$U normalize/harbor_normalize.py $E/har81-dispatch-528/runs/har81-p-d-* \
  $E/har81-dispatch-531/runs/har81-* --out ~/Developer/eval-lab/derived/trace-lab/normalized/har81 \
  --evallab-src $E/har81-dispatch-531/src \
  --check probe-03-capabilities/har81/capabilities.jsonl
$U normalize/harbor_normalize.py \
  $E/har90-modal-mimo/runs/har90-mimo-{0036,0036-b,0036-c,0036-d,0036-e,0036-f,0758-a,0758-b,0758-c,0758-d} \
  --out ~/Developer/eval-lab/derived/trace-lab/normalized/har90 --evallab-src $E/har90-modal-mimo/src \
  --check probe-03-capabilities/har90/capabilities.jsonl
```

## The rigorous process

### Anchor: Kirgis et al., arXiv 2605.08545 (Princeton, UK AISI, Meridian, Transluce, Apollo; 2026-05-08)

Threats that outcome-only scores miss (Table 1): **internal validity**
(score vs capability: benchmark lookup, reward hacking, infrastructure
manipulation, sandbagging, environment barriers, grading artifacts,
refusals); **external validity** (capability vs real use: scaffold limits,
budgets, persistent failure modes, hidden intermediate progress, output
quality); **safety** (costly actions, constraint violations, dangerous
reasoning).

Four principles (Appendix A, Table 2):

1. **Define a validity target.** Fidelity, real-world transfer, or safety.
   It sets the burden of proof: rare safety incidents need few, hand-checked
   positives and low false negatives; claims about what drives success need
   a calibrated classifier with both error rates known.
2. **Confirm log coverage.** Instructions, every action, outcome, grader
   inputs. Missing context is the most common blocker.
3. **Build and validate a rubric.** Start broad, read transcripts, narrow
   to necessary-and-sufficient conditions ("funnel"). Validate on a balanced,
   held-out set with human review; report precision, recall, accuracy.
4. **Link labels to outcomes.** Prevalence by outcome first (a failure mode
   that only appears in failed runs does not threaten the score), then risk
   ratios, then mixed-effects or hierarchical models. No causal claims
   without an intervention.

Case study (tau-Bench Airline, HAL harness, Docent with GPT-5 and Sonnet 4.5
judges): 25 of 50 tasks were flawed (9 policy conflicts, 8 ambiguous, 8
database/grading errors); mean pass^5 over 13 models went from 20.8% to 40.0%
on the clean subset. Against the authors' manual labels the LLM judges had
high recall (0.92, 0.96) and low precision (0.66, 0.73): they are good at
shortlisting, and humans still confirm. A second rubric measured resistance
to user persuasion, which separated models with equal pass^5 (GPT-4 Turbo
~4x more persuadable than Gemini 2.5 Flash).

### What other recent papers add (read by agents; numbers as reported)

- **Seven simple steps** (Dubois et al., UK AISI, 2604.09563): the detailed
  SOP behind principle 3 (stratified manual reads, narrow scanners, blind
  multi-rater validation).
- **Mohl et al.** (2607.27518): scanner sensitivity ranges 0.17-0.93 by
  scanner and judge; ~120 scanner-graded transcripts to rule out a <5% rate;
  always sample non-flagged runs or recall is unknown.
- **Willful Disobedience / AgentPex** (2603.23806): 48 of 58 (83%)
  perfect-reward Claude traces broke at least one procedural rule. Audit
  passes, not only failures.
- **Agentic CLEAR** (2605.22608): automatic clustering recovers TRAIL error
  categories with a strong judge (12/12 with GPT-5) but judge choice changes
  what you find.
- **Reasoning Consistency Scanning** (2607.07229): P 0.96 / R 0.71 overall
  but recall 0.0 on the "contradictory reasoning" subtype. Report per-subtype.
- **Partial Compliance** (open-weight judges on refusals): judges mislabel
  partial refusals; a judge's written rationale is not evidence of why it
  decided.
- **SynCA** (2609.30290): a production judge at kappa 0.04 with humans
  while gating data. Calibrate every judge.
- **Precise Records, Unstable Meanings** (hermes-labs, 2026-07-30): telemetry
  counts are exact but the constructs they are used for often are not; keep
  a per-claim ledger (unit, denominator, disposition).
- **Apollo, "What makes a good monitoring prompt?"** (2026-07-23, read
  directly): a written reasoning procedure is the most important part of a
  judge prompt; a 1-10 rubric and one worked example fix calibration.

### Checklist, and where probe-03 stands

| Step | probe-03 status | Smallest next action |
| --- | --- | --- |
| 1 Validity target stated | Done (five dimensions never collapsed) | State the target in one line per summary |
| 2 Log coverage, honest denominators | Done (assembly rules, token-attributed shares) | None |
| 3 Full inputs assembled | Partial: task and grader checked; Terminus-2 system prompt not joined | Add system prompt + normalizer version per row |
| 4 Deterministic rules first | Done (first-firing rule table) | None |
| 5 Blind held-out validation with P/R | Partial: 8/11, 14/14, 2/2 first scorings; readers saw the rule definitions; step refs not scored; no P/R or intervals | Per-tag precision/recall; one reader classifies 20 failures without the rules |
| 6 Calibrate the auditor | Partial: reader-vs-reader agreement never reported | Compute it from existing keys (free) |
| 7 Audit the grader | Done (nop/qual controls, 2684 taint, HAR-97) | None |
| 8 Audit passes too | Partial: pass caveats only | Run existing detectors over the 7 counted passes |
| 9 Labels vs outcomes | Partial: prevalence yes, no risk ratios | P(pass given tag) vs P(pass without tag), within task |
| 10 Interventions before causal claims | Partial: attribution rules, no intervention | One matched-pair rescoring on existing data |
| 11 Reasoning vs action consistency | Missing | Hand-check whether plans in the 9 false-claim runs anticipated the failing tests |
| 12 Claims ledger / threat registry | Partial: strong provenance, no ledger | One-page ledger for the headline numbers |

## Hands-on trial (HAR-101, 2026-09-29, $0)

All executed on the 54 HAR-81/HAR-90 runs, raw vs normalized. Details:
`docent/README.md` + `docent/har81_reading_scoring.md`, `scout/README.md`,
`viewers/NOTES.md` (screenshots under each).

| Tool | Extra install | Raw MiMo runs | Normalized runs | Shows our analysis? |
| --- | --- | --- | --- | --- |
| `harbor view` | none (ships with harbor; env ~410 MB) | first file only (long run: 61 of 102 steps), 0 commands | whole run, commands as chips (102 steps / 97 with calls) | No labels |
| `evallab report run` | none (Eval Lab) | stitches, replays in-text calls (117/117) | same numbers | No labels |
| `evallab traj outline/card` | none (Eval Lab) | 0 tool calls | 117 / 129 calls | No labels |
| Inspect Scout 0.5.3 | one pip package, local | 71 transcripts for 54 runs, 0 tool calls | 54 transcripts, 4,236 tool calls | **Yes**: probe-03 labels as scan results with clickable `[Mn]` citations; validation view (WEDGE 53/54 vs hand key, in-sample) |
| Docent (SDK 0.1.87) | SDK only; hosted | stock converter rejects 17 of 54 | 54/54 accepted | AI readings with citations + DQL; our hand labels scored offline |
| Phoenix 20.16.0 | ~688 MB server | 0 TOOL spans | 4,236 TOOL spans (54/54 accepted) | Annotations work via API but crash the trace page |

Docent reading (one reading, 44 HAR-81 runs, blind metadata, scoring map
frozen before the run, 36 s, 2.35M input tokens, free): terminal stuck
P 0.25 / R 0.875 (21 false alarms: repetition loops and the "are you sure"
handshake read as stuck); false completion claim agree 0.864, P 0.75 /
R 0.60 (it re-verified work and overrode reward < 1); failure owner agree
0.773, blamed the model on all 36 failed runs (never harness, task/grader
or unclear); first mistake within +/-5 steps on 13 of 26 comparable runs.
probe-03 on the same maps: 43/44, 44/44, 43/44 (partly in-sample). This
matches the anchor paper: LLM readings shortlist (high recall), humans or
rules decide.

Known stitching difference: probe-03 and the normalizer keep a new
session's copied-context steps (system prompt dropped as duplicate; the
summary handoff, questions and answers kept, flagged
`is_copied_context`), so the long run has 102 steps; Eval Lab `traj`
drops all copied-context steps (99).

Pass-through check: on 3 stock Terminus-2 runs (har71 Qwen2.5, terminus
GLM-5.3-flash) the normalizer leaves `tool_calls` byte-identical.

## Recommended stack

1. **Eval Lab** stays the store (raw job dirs + Parquet/DuckDB projection);
   no separate trace store. Its projection reads only structured
   `tool_calls`, so MiMo runs need the normalizer (or a run-time change
   that writes `tool_calls` from `extra.step_layers`).
2. **probe-03** stays the deterministic source of truth for per-run labels.
3. **Normalizer** before every tool.
4. **Inspect Scout** as the local analysis UX: probe-03 labels with
   citations, validation sets, free grep scanners.
5. **`harbor view`** (normalized) to read single runs.
6. **Docent** as a second opinion for questions without a rule yet; always
   score readings against hand labels before using them.
7. Skip Phoenix and other trace stores for now. Trial **AgentPex** and
   **`harbor analyze`** only with a judge-call budget.

## Sources

- Anchor: https://arxiv.org/abs/2605.08545 (HTML read: §2 Table 1, §3.1, §4, App A-B)
- Scout: https://meridianlabs-ai.github.io/inspect_scout/ , db_importing, validation
- Petri judge: https://github.com/meridianlabs-ai/inspect_petri (`src/inspect_petri/_judge/judge.py`)
- Inspect Harbor: https://github.com/meridianlabs-ai/inspect_harbor
- Docent: https://docs.transluce.org/ingestion/integrations/harbor.md , https://docs.transluce.org/self-hosting.md
- UK AISI workflow: https://github.com/UKGovernmentBEIS/inspect_evals (`.claude/skills/check-trajectories-workflow`)
- Harbor plugins: https://docs.harborframework.com/core-concepts/plugins/existing-plugins.md
- Phoenix ATIF: https://arize.com/docs/phoenix/tracing/how-to-tracing/importing-and-exporting-traces/importing-atif-trajectories
- AgentPex: https://github.com/microsoft/agentpex ; AgentRx: https://github.com/microsoft/AgentRx ; CLEAR: https://github.com/IBM/CLEAR
- Hodoscope: https://github.com/AR-FORUM/hodoscope ; Kura: https://github.com/jxnl/kura
- Apollo: https://www.apolloresearch.ai/monitoring/what-makes-a-good-monitoring-prompt
- Papers: 2604.09563, 2607.27518, 2603.23806, 2605.22608, 2607.07229, 2609.30290
- Earlier source-read catalogue (viewers, trace stores, bridges): `../harbor/TOOLS.md`
