---
status: living
audience:
  - builder
  - runner
  - operator
  - analyst
---

# Eval Lab system map

**Launch → harness → proxy/capture → trial → process-job → counts/pages → results home → ledger/analysis → training/evaluation.** This is the navigation map, not a live status board. [Linear](https://linear.app/petermakhnatch) owns assignments and approvals; [the research card index](../research/CARD-INDEX.md) locates committed results. Path ownership is defined in [OWNERS](../agents/OWNERS.md).

| Stage | Responsible surface | Main artifacts | Boundary that must hold |
|---|---|---|---|
| **Launch** | Platform: [CLI](../src/evallab/cli.py), [queue/Executor](../src/evallab/queue.py) | Experiment spec, approval events, `queue/events.jsonl` | A merge is not run authorization. Paid work needs recorded approval and applicable ceilings; [spend day](../src/evallab/spend_day.py) records known spend and unresolved amounts. |
| **Harness** | Platform + task supply: [runner](../src/evallab/runner.py), [Terminus adapter](../src/evallab/harbor_terminus.py), [task packages](../library/) | Pinned task/harness bytes, resolved model and execution configuration | Compare actual digests and settings, not treatment names. Keep the agent's environment separate from hidden verifier inputs. See [trial treatments](trial-treatment.md). |
| **Proxy/capture** | Platform: [model capture](../src/evallab/model_capture.py), [provider sidecars](../containers/) | Usage records; when capture is enabled, `calls.jsonl`, capture manifest and job-link receipt | Credential isolation and exact request/response capture are different contracts. Do not invent uncaptured turns or expose provider secrets; transport and tokenization must match the declared treatment. |
| **Trial** | Harbor through the Lab runner; [ATIF extraction](../src/evallab/evidence/atif.py) | `runs/<job>/<trial>/`: config, logs, trajectory, verifier output and `result.json` | Retain the original verifier reward and execution evidence. Distinguish model failure from setup, transport and verifier failure; a database row does not replace the job directory. |
| **Process-job** | Platform: [process_job](../src/evallab/process_job.py) | `<job>/processed/` trial/job JSON and Markdown, linked diagnostics | Read retained trials, produce reports, then publish the completed report set. Processing is not permission to rerun a trial or replace its raw reward. |
| **Counts/pages** | Research + trace quality: [counts](../src/evallab/counts.py), [trial decision pages](../src/evallab/trial_decision.py) | `counts` verdict (`counted_pass`, `counted_fail`, `excluded`), reasons/evidence, decision sections | One counts verdict per trial; consumers do not invent another. Keep raw reward, deterministic facts, and diagnostic judgments distinct; unknown evidence stays explicit. |
| **Results home** | Platform: [results_home](../src/evallab/results_home.py) | `~/Developer/eval-lab-results/<date>/<card>-<job>/`, provenance, `INDEX.md` and `INDEX-all.md` | Byte copies survive worktree retirement. Preserve source commit/dirty provenance and collision identity; backfilled unknown provenance is not a reconstructed fact. |
| **Ledger/analysis** | Task health: [task_health](../src/evallab/task_health.py), [task ledger](../research/experiments/python-task-ledger/); Research: [cohort](../src/evallab/cohort.py), [curve](../src/evallab/curve.py), [SQL](../sql/) | Task-health ledger, provenance-bound facts/features, comparisons, spend ledger | Task validity is not model capability. Use the stated eligible denominator and counted outcomes; retain source IDs and distinguish observations from inferred causes. Model judgments are not disposable caches. |
| **Training/evaluation** | Data + training infrastructure: [split manifests](../src/evallab/sft_split.py), [training pool](../src/evallab/training_pool.py), [SFT records](../src/evallab/sft_records.py), [Modal SFT tooling](../tools/modal-mimo-sft/) | Frozen train/held-out manifests, reviewed examples, dataset/adapter hashes, paired evaluation results | Keep held-out tasks and their repository groups out of selection/training. Bind examples to reviewed counts and recorded model-visible content. Pin harness and limits across evaluation arms; training/serving still needs its own approval. |

## Source and runtime boundary

[Storage paths](../src/evallab/storage/paths.py) owns output resolution. Default live state is the primary checkout's sibling `<primary>-state` (for example `~/Developer/eval-lab-state`): `derived/parquet/` for live projections and `reports/` for status, lessons and daily digests. Linked worktrees share that store. Existing `EVALLAB_DERIVED_ROOT` and explicit output arguments override their respective destinations.

Tracked `derived/parquet/` snapshots, `research/lessons.md`, `docs/STATUS.md` and historical digests are reviewed source artifacts, not unattended write targets. Snapshot lessons checks remain independent of the live store; deliberate promotion uses explicit output paths and a reviewed change. Raw runs and evidence CAS keep their existing preservation rules. See [generated/cache policy](GENERATED-CACHE-POLICY.md), [operations](operations.md), [verification gates](../agents/CHECKS.md) and [delivery/worktree rules](../agents/WORKFLOW.md).
