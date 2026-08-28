---
type: correction-errata
topic: citation-integrity-audit
requested_by: Analyst (wK:p5)
reviewed: 2026-08-28
status: verified-corrections-applied
method: every arXiv ID resolved via export.arxiv.org Atom API; bindings cross-checked against local paper manifests; method claims quoted from paper body text
machine_ledger: research/inbox/CITATION-VERIFICATION-LEDGER-2026-08-28.json
---

# Citation Integrity Audit and Correction

## 1. Root cause, stated plainly

The Analyst reported 4 of 8 IDs in commissioned surveys were bound to the wrong
benchmark name, and that the literature map carried a bad ID for
AgentProcessBench. Both reports are confirmed. The audit found the defect is
**more severe than a name-to-ID binding failure** in the artifacts I authored.

I asserted 31 distinct arXiv IDs across my literature map, method catalog, and
synthetic funnel handoff. Resolving all 31 against the arXiv API:

| Defect class | Count | Meaning |
|---|---:|---|
| `FABRICATED_ID` | 20 | The asserted ID resolves to an entirely unrelated paper. The ID was invented, not mis-bound. |
| `TITLE_INVENTED` | 5 | The ID is correct for a real catalogued paper, but the descriptive title I wrote around it was invented. |
| `BINDING_OK` | 6 | ID and name agree with the resolved record. |

The fabricated IDs carry a visible signature: repeated `12345` and `09876` digit
groups (`2510.12345`, `2605.12345`, `2407.12345`, `2511.12345`, `2502.12345`,
`2510.09876`, `2504.09876`, `2604.09876`, `2512.09876`, `2511.09876`). Those are
placeholder digits. They were never resolved against anything.

Representative failures — asserted ID resolves to unrelated subject matter:

| Asserted ID | I labelled it | It actually is |
|---|---|---|
| `2502.12345` | AgentProcessBench | Uncertainty quantification for stationary and time-dependent PDEs |
| `2512.09876` | MemGym | Homological Milnor-Witt modules and Chow-Witt groups over general bases |
| `2512.18902` | Graphectory | Speaker Recognition — Wavelet Packet Based Multiresolution Feature Extraction |
| `2604.12876` | FuncBenchGen | Fueter trees for Dunkl-regular functions over alternative *-algebras |
| `2605.04321` | MetaAgent-X | AI and Suicide Prevention: A Cross-Sector Primer |
| `2510.12345` | ContextBench | Carleman Estimates for Backward Anisotropic Stochastic Parabolic Equations |

## 2. What was trustworthy, and is now the authority

The **local paper corpus is real** and each download group carries a
`manifest.json` recording `arxiv_id`, `title`, `authors`, `published`, and
`abstract` as fetched from the arXiv API at download time.

- Manifest sources: **131** (125 unique arXiv IDs)
- Manifest IDs re-resolved against the arXiv API: **125 / 125 resolve**
- Manifest-vs-API title mismatches: **5**, all benign (truncation, or LaTeX `$\tau$`
  rendered as `Tau`)

**The manifests, not any prose I wrote, are the citation authority.** Every
correction below is drawn from them and independently re-confirmed against the
API.

## 3. Corrected bindings

The specific correction the Analyst asked for first:

> **AgentProcessBench** — the map carried a bad ID. The verified ID is
> **`2603.14465`**, *"AgentProcessBench: Diagnosing Step-Level Process Quality in
> Tool-Using Agents"*. Neither `2508.02060` nor `2502.12345` is correct.

Full corrected table. All 36 verified IDs re-resolved against the API in a final
confirmation pass (36 / 36 resolve):

| Benchmark / paper | Verified arXiv ID | Resolved title (exact) |
|---|---|---|
| AgentProcessBench | `2603.14465` | AgentProcessBench: Diagnosing Step-Level Process Quality in Tool-Using Agents |
| AgentRx | `2602.02475` | AgentRx: Diagnosing AI Agent Failures from Execution Trajectories |
| TrajDebug | `2608.06346` | TRAJDEBUG: Tracing Error Lifecycle to Identify Critical Failures in Long-Horizon Agents |
| GroundEval | `2606.22737` | GroundEval: A Deterministic Replacement for LLM-as-Judge in Stateful Agent Evaluation |
| AgentCheck | `2607.11098` | AgentCheck: A Reproduce-Intervene-Mitigate Workbench for LLM Agents over MCP |
| ToolMaze / When Tools Fail | `2606.05806` | When Tools Fail: Benchmarking Dynamic Replanning and Anomaly Recovery in LLM Agents |
| ToolMisuseBench | `2604.01508` | ToolMisuseBench: An Offline Deterministic Benchmark for Tool Misuse and Recovery |
| ToolBench-X | `2606.25819` | Beyond Function Calling: Benchmarking Tool-Using Agents under Tool-Environment Unreliability |
| ToolPRMBench | `2601.12294` | ToolPRMBench: Evaluating and Advancing Process Reward Models for Tool-using Agents |
| FuncBenchGen | `2509.26553` | Towards Reliable Benchmarking: A Contamination Free, Controllable Evaluation |
| LOCA-bench | `2602.07962` | LOCA-bench: Benchmarking Language Agents Under Controllable and Extreme Context |
| ContextBench | `2602.05892` | ContextBench: A Benchmark for Context Retrieval in Coding Agents |
| MemoryAgentBench | `2507.05257` | Evaluating Memory in LLM Agents via Incremental Multi-Turn Interactions |
| BEAM | `2510.27246` | Beyond a Million Tokens: Benchmarking and Enhancing Long-Term Memory in LLMs |
| MemGym | `2605.20833` | MemGym: A Long-Horizon Memory Environment for LLM Agents |
| MemoryArena | `2602.16313` | MemoryArena: Benchmarking Agent Memory in Interdependent Multi-Session Agentic Tasks |
| Graphectory | `2512.02393` | Process-Centric Analysis of Agentic Software Systems |
| tau-bench | `2406.12045` | $\tau$-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains |
| tau2-bench | `2506.07982` | $\tau^2$-Bench: Evaluating Conversational Agents in a Dual-Control Environment |
| ALCE | `2305.14627` | Enabling Large Language Models to Generate Text with Citations |
| MiniCheck | `2404.10774` | MiniCheck: Efficient Fact-Checking of LLMs on Grounding Documents |
| Meta-Task | `2607.27929` | Meta-Task: Turning Terminal Task Synthesis into a Terminal Task for Scalable Agents |
| VPR | `2605.10325` | Verifiable Process Rewards for Agentic Reasoning |
| AgentAbstain | `2607.10059` | AgentAbstain: Do LLM Agents Know When Not to Act? |
| Trust or Escalate | `2407.18370` | Trust or Escalate: LLM Judges with Provable Guarantees for Human Agreement |
| Clarification Timing | `2605.07937` | Ask Early, Ask Late, Ask Right: When Does Clarification Timing Matter |
| SWE-Pruner | `2601.16746` | SWE-Pruner: Self-Adaptive Context Pruning for Coding Agents |
| SWE-bench | `2310.06770` | SWE-bench: Can Language Models Resolve Real-World GitHub Issues? |
| MetaAgent-X | `2605.14212` | MetaAgent-X : Breaking the Ceiling of Automatic Multi-Agent Systems |
| SAFARI | `2606.24626` | SAFARI: Scaling Long Horizon Agentic Fault Attribution via Active Investigation |
| AgentAtlas | `2605.20530` | AgentAtlas: Beyond Outcome Leaderboards for LLM Agents |
| WebClipper | `2602.12852` | WebClipper: Efficient Evolution of Web Agents with Graph-based Trajectory Pruning |
| TASTE | `2605.28556` | A Matter of TASTE: Improving Coverage and Difficulty of Agent Benchmarks |
| Failure as a Process | `2607.09510` | Failure as a Process: An Anatomy of CLI Coding Agent Trajectories |
| Span-Level Error Localization | `2606.02060` | Where Do Deep-Research Agents Go Wrong? Span-Level Error Localization in Agent Trajectories |
| CodeTracer | `2604.11641` | CodeTracer: Towards Traceable Agent States |

### Names with no locatable arXiv ID — must not be cited with one

| Name | Correct citation form |
|---|---|
| **Recovery-Bench** | Repository only: `letta-ai/recovery-bench`. No arXiv paper located. My asserted `2602.14922` resolves to *ReusStdFlow*, unrelated. |
| **CooperBench** | UNVERIFIED. No arXiv ID located in manifest or API. Do not cite. |
| **TraceJudgeBench** | Hugging Face dataset `samp0rt/TraceJudgeBench`, not an arXiv paper. |
| **AgentDiagnose** | Manifest entry carries no arXiv ID. Cite as toolkit. |
| **Proxy State Evaluation** | Manifest entry carries no arXiv ID. |

## 4. The two load-bearing claims

Both were previously sourced to `2604.11641`. That ID resolves to *CodeTracer:
Towards Traceable Agent States*, which does not contain either design. The
Analyst's refutation is correct. Both claims are re-established below **from
different papers, with body-text quotes**.

### (a) Does published work condition recovery rate on a fault-opportunity denominator?

**ESTABLISHED.** Source: **ToolMaze**, *"When Tools Fail: Benchmarking Dynamic
Replanning and Anomaly Recovery in LLM Agents"*, **arXiv:2606.05806** (Zhu et al.;
Shanghai AI Laboratory, ECNU, Soochow, Shandong, Baidu). Local PDF:
`papers/agentic-capabilities/sources/when-tools-fail.pdf`.

Verbatim from the paper body:

> "PRR evaluates the conditional probability of resolving an encountered
> perturbation:
> $$\text{PRR}_m = P(\text{Recovered} \mid \text{Perturbation}) = \frac{\sum_{\tau \in T_m} I_{\text{recov}}(\tau) \cdot I_{\text{pert}}(\tau)}{\sum_{\tau \in T_m} I_{\text{pert}}(\tau)}$$
> where $I_{\text{recov}}(\tau) = 1$ if the agent successfully executes a valid
> recovery strategy: (1) retrying for transient faults, (2) utilizing an
> alternative path, or (3) wisely aborting unsolvable tasks. PRR strictly
> evaluates error recovery independently of final task success."

Supporting definitions quoted from the same section:

> "For each trajectory $\tau \in T_m$, let $I_{\text{succ}}(\tau) \in \{0,1\}$
> indicate successful task completion, and $I_{\text{pert}}(\tau) \in \{0,1\}$
> indicate exposure to the injected perturbation."

**This is exactly a fault-opportunity denominator.** The denominator is
$\sum_\tau I_{\text{pert}}(\tau)$ — the count of trajectories actually exposed to
the injected fault, not the count of all trajectories. Zero exposure yields a
zero denominator and an undefined rate, which matches our null-on-zero rule.

The paper also defines a companion cost metric on the same exposure indicator:

> "$C_{\text{rec}}(\tau) = 1 - \frac{c^*(\tau)}{\max\{c(\tau), c^*(\tau)\}} \cdot I_{\text{succ}}(\tau)$"
> and "$\text{RC}_m = \frac{1}{|T_m|}\sum_{\tau \in T_m}\left[I_{\text{pert}}(\tau) \cdot C_{\text{rec}}(\tau)\right]$"

Note the asymmetry worth flagging to the Analyst: PRR normalises by exposure
($\sum I_{\text{pert}}$) whereas RC normalises by the full set ($|T_m|$). RC is
therefore not an exposure-conditioned mean.

### (b) Does published work use a paired clean-twin / unintervened control arm?

**ESTABLISHED in a paper — and REFUTED for `letta-ai/recovery-bench`.**

**Established.** Source: **AgentCheck**, *"AgentCheck: A
Reproduce-Intervene-Mitigate Workbench for LLM Agents over MCP"*,
**arXiv:2607.11098**. Local PDF:
`papers/agentic-capabilities/sources/agent-check.pdf`.

Verbatim from the paper body, Section 4 System Description:

> "The system centers on controlled comparison over an agent run. A clean run is
> first recorded over a task and its tools, then that run is replayed with
> selected tool response changed. This yields a clean run and faulted run that can
> be compared. Resulting trajectories are judged with fault-specific checks."

The causal-attribution sentence:

> "An MCP-proxy runner holds every tool response constant except one, so the
> divergence is attributable to the injected fault."

The replay mechanism, which is what makes the twin a twin:

> "AgentCheck runs an agent against its real tools and records every tool
> response, then re-runs the agent with the response perturbed by a fault (12
> types) injector. Matching tool calls are replayed from cache, and later tool
> calls go live after the agent diverges."

Algorithm 1 line 1, quoted:

> "$\tau_{\text{clean}} \leftarrow \text{RUN}(A, q, T)$, caching each response in
> $K$ &nbsp;&nbsp;$\triangleright$ $K$: $(\text{tool}, \text{args}, i) \mapsto r$"

Scope limit the paper states about its own verdict, which should be carried
forward verbatim rather than paraphrased:

> "A fix_confirmed verdict states that one injected fault, under one scenario, no
> longer trips its checks; it is not a certificate of general robustness or
> safety."

**Refuted for recovery-bench.** The Analyst suggested the harness source might be
better evidence than a paper. I read it. It is better evidence — and it shows the
**opposite** of a clean-twin design.

Source read: `recovery_bench/pipeline.py` and `recovery_bench/utils.py`
(`letta-ai/recovery-bench`, local checkout).

Arm construction, `pipeline.py::run_recovery`:

```python
if task_ids is None:
    task_ids = get_unsolved_tasks(traces_folder)
...
rc = generate_recovery_traces(
    traces_folder=traces_folder, model=model, task_ids=task_ids, ...
)
```

Selection rule, `utils.py::get_unsolved_tasks`:

```python
# reward > 0 means resolved
verifier_result = results.get("verifier_result") or {}
rewards = verifier_result.get("rewards") or {}
reward = rewards.get("reward", 0.0)
if reward > 0:
    continue          # solved tasks are excluded
task_name = results.get("task_name", task_id)
unsolved_ids.append(task_name)
```

The recovery arm runs **only on tasks the initial model failed** (`reward == 0`).
There is no arm in which the *same recovery agent* attempts the *same task* from a
clean, unintervened start. Consequently the harness cannot separate:

- recovered from inherited corrupted state, from
- would have solved this task anyway from scratch.

This is selection on outcome. **Any recovery-causality claim sourced to
recovery-bench remains UNVERIFIED**, and the confound is structural, not a
reporting gap. To obtain a clean twin the harness would need a matched arm running
the recovery agent on the same task set from the unmodified initial environment.

## 5. Claims that remain UNVERIFIED

Per the Analyst's instruction, these are reported as unevidenced rather than
dropped. Absence of evidence is itself the finding.

| Claim | Status | Reason |
|---|---|---|
| Cohen $\kappa$ = 0.87 / 0.90 / 0.93 | **UNVERIFIED** | Attached to papers whose abstracts do not evidence the figure; no body-text quote located. Must not seed a judge-agreement floor. |
| Krippendorff $\alpha$ = 0.78 | **UNVERIFIED** | Same. |
| Recovery causality via clean twin in recovery-bench | **REFUTED** | Harness selects on `reward == 0`; no unintervened arm exists. |
| Any method claim attached to a `FABRICATED_ID` | **VOID** | The cited paper does not exist as cited. Re-derive from the corrected ID or drop. |
| Method associations for corrected IDs not yet body-quoted | **BINDING-VERIFIED, METHOD-UNQUOTED** | ID↔title is confirmed; the specific method sentence has not been quoted from the body. Do not treat as method evidence yet. |

The last row matters and I want it explicit: correcting an ID proves the paper
exists and is named correctly. It does **not** prove the paper contains the method
I attributed to it. Only claims (a) and (b) above carry body-text quotes in this
pass.

## 6. Mechanism fix, not effort fix

The failure was a process gap: I wrote citations from memory into prose and never
resolved them. Corrective mechanism, now applied in this artifact:

1. **No ID enters an artifact unless it came from a manifest or an API resolution
   in the same session.** The manifests already existed; I bypassed them.
2. **A machine-checkable ledger ships with the prose.**
   `CITATION-VERIFICATION-LEDGER-2026-08-28.json` records every asserted ID, its
   resolved title, defect class, and correction. It is re-runnable.
3. **Method claims require a body quote, not an abstract.** Claims (a) and (b) are
   quoted from paper body text; everything else is marked METHOD-UNQUOTED.
4. **Repositories are cited as repositories.** Recovery-Bench gets a repo pin, not
   a fabricated arXiv ID.

## 7. Three genuinely new papers worth extraction

The Analyst flagged these; all three are verified-title and now pinned.

| ID | Title | Why it matters |
|---|---|---|
| `2607.09510` | Failure as a Process: An Anatomy of CLI Coding Agent Trajectories | Treats failure as a temporal process — onset, evolution, recovery — rather than a final outcome. Same thesis as the Analyst's objection to outcome-contaminated process metrics. |
| `2606.02060` | Where Do Deep-Research Agents Go Wrong? Span-Level Error Localization in Agent Trajectories | Semantic span localization; ships TELBench and DRIFT, a claim-centric audit method. Directly relevant to the empty `process_step_facts` table. |
| `2601.16746` | SWE-Pruner: Self-Adaptive Context Pruning for Coding Agents | Context vertical. |

A full body extraction on `2607.09510` and `2606.02060` is queued as the next
unit of work and is deliberately **not** attempted in this artifact — mixing an
unverified extraction into a correction artifact would repeat the original defect.
