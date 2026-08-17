---
status: living
audience:
  - builder
  - analyst
---

# Synthetic task generation: blueprint

Role: DATA-STRATEGY. Date: 2026-08-15. Status: architecture, not
implementation. Zone 03 of `docs/data-architecture.md`; certification and
registration rules there are binding here. The quality bar is TB3's contract,
which this lab audited line-by-line (`docs/research/literature-survey.md` §2)
and whose failure modes (TB2's 89/89 curl exploit, Terminal Wrench's 331
hackable environments) define exactly what the generator must not reproduce.

## 1. Objective and non-goals

**Objective:** a pipeline that turns real software change events (commits,
issue-linked diffs) into executable Harbor tasks — `task.toml` + environment +
pytest verifier + oracle solution — certified before any evaluation use.

**Non-goals:** replacing curated benchmarks (synthetic tasks are Zone 03,
never presented as TB-grade without independent review); generating tasks with
LLM-judged verification (deterministic verifiers only in this blueprint); any
autonomous registration (humans own `registered/*`, always).

## 2. Why commit-derived tasks

A merged commit with tests is a naturally occurring (task, verifier, oracle)
triple: the parent state is the initial environment, the commit message/issue
is the instruction seed, the changed tests are the verifier seed, and the diff
itself is the oracle. This is the SWE-bench/SWE-smith recipe; the lab's
version differs in three ways: full TB3-style container contract instead of
patch-in-repo-harness, an explicit certification gate with recorded evidence,
and provenance sidecars from birth.

## 3. Pipeline

```text
 A. SOURCE          B. SYNTHESIS             C. CERTIFICATION        D. DISPOSITION
 commit/issue   →   task.toml            →   schema + policy     →   pass: staged in
 harvesting         environment/ (Docker)     static checks           library/synthetic/
 (licensed          instruction.md            oracle k=3 == 1.0       + sidecar + cert
  repos only)       tests/ (separate          nop == 0.0              record
                    verifier image)           cheat probes            fail: rejected/
                    solution/ (oracle         determinism run         with reason code
                    from the real diff)                               (kept as lineage)
```

### A. Source harvesting

- Inputs: repositories with OSI licenses compatible with redistribution;
  the license enters the sidecar and propagates (Zone rules).
- Selection filters: commit touches ≥1 test file and ≥1 source file; tests
  fail on parent and pass on child (verified by execution, not assumed —
  this is the FAIL_TO_PASS discipline); diff within size bounds; no secrets
  (scanned) and no vendored-blob churn.
- Everything recorded: repo URL, commit SHAs (parent+child), harvest filters
  version. `ProvenanceMetadata.transform = "taskgen@<ver>"`,
  `parent_digests = [sha256 of harvested bundle]`.

### B. Synthesis

Per TB3 contract (the floor, from the audit):

1. `task.toml`: resource declarations honest to measured needs (run the
   oracle under cgroup limits to calibrate `memory_mb`/`timeout_sec` rather
   than guessing); `[verifier] environment_mode = "separate"`.
2. `environment/`: parent-state checkout baked into the image, digest-pinned
   base, **no `tests/` or `solution/` anywhere in the agent image**
   (AGENTS.md hidden-input rule).
3. `instruction.md`: generated from commit message + issue text, rewritten to
   task form; the generator must strip solution leakage (issue threads often
   contain the fix — leakage screening is a named synthesis step with its own
   check, not a hope).
4. `tests/`: separate verifier image; test deps baked at build time; **no
   network fetches at verify time** (the TB2 lesson, mechanically checked);
   golden data independent of the oracle where the task shape allows —
   when the golden is oracle-derived, the certification record says so
   (the fin-saccr audit showed why this distinction matters).
5. `solution/`: the real diff applied by script — an oracle by construction.

### C. Certification gate (all mandatory, all recorded)

| Check | Rule | Catches |
|---|---|---|
| Schema/static | task.toml valid; canary GUID present; path/layout checks | malformed packages |
| **Oracle** | reward == 1.0 on **k=3 consecutive** runs | unsolvable/flaky tasks |
| **No-op** | reward == 0.0 | vacuous verifiers |
| Cheat probes | curl/tool-shim probe and trivial-artifact probe score 0.0 | TB2-class verifier exploits |
| Determinism | oracle twice from clean state → identical reward and verifier check vector | hidden nondeterminism |
| Leakage scan | instruction/environment contain no verbatim solution spans | self-answering tasks |

Exactly 1.0 and exactly 0.0 — not ≥, not ≈ (the mission's phrasing, adopted
verbatim, because tolerance here is how vacuous verifiers slip through).
Certification uses the local free path only (oracle/nop under policy);
nothing in this pipeline is billable.

### D. Disposition

- Pass → `library/synthetic/<batch>/<task>/` + sidecar + certification record
  (JSON: check vector, run IDs, evidence paths). Still Zone 03.
- Fail → `library/synthetic/<batch>/rejected/<task>/` with reason code;
  rejects are lineage, not garbage — they calibrate the generator.
- **Promotion to evaluation use** (`registered/*`) is a human act, per
  policy (`escalate_to_human: new_task_registration`) and the Zone 03 rule:
  agent-authored tasks never merge into `registered/*`.

## 4. Difficulty perturbation

Perturbations create controlled families from one certified base task —
the lab's instrument for measuring *what makes tasks hard* rather than
accepting difficulty as given:

| Perturbation | Mechanism | Measures |
|---|---|---|
| Noise injection | distractor files, stale docs, misleading comments added to environment | robustness to irrelevant context (context-rot sensitivity, survey §3) |
| Instruction degradation | precise spec → underspecified request (graded levels) | inference of intent |
| Tool constraint | remove/deny a tool class in agent config (no network, no editor, shell only) | adaptability, tool substitution |
| State corruption | pre-broken intermediate state added (wrong partial fix present) | diagnosis vs blind implementation |
| Scale stretch | same logic, larger repo slice | context budget management |

Rules: every perturbed variant is a **new task version** with its own full
certification pass (a perturbation can accidentally break solvability — the
oracle gate catches it); the perturbation operator and parameters live in the
sidecar `transform`; variants link to the base via `parent_digests`. A
certified family = a difficulty axis with a solvable anchor at each level.

## 5. Trust boundaries and failure modes (design-time honesty)

- **Generator gaming its own gate:** the certifier and generator must not
  share code paths that could co-evolve; certification uses the lab's
  standard runner, not generator-supplied scripts.
- **Monoculture risk:** commit-derived tasks inherit the harvested repos'
  distribution; the catalog must report per-batch repo/language/domain mix so
  nobody mistakes a Python-web-app batch for general capability.
- **Contamination:** harvested commits may be in model training data. Recency
  filters (harvest after model cutoffs) mitigate but don't eliminate;
  the sidecar records commit dates so analyses can condition on them.
- **Verifier weakness inheritance:** upstream tests may be weak (the
  fin-saccr lesson: passing tests ≠ correct behavior). The certification
  record notes verifier provenance (upstream tests as-is vs augmented) —
  weak-verifier tasks are usable for training signal, flagged for evaluation.

## 6. Build order (when implementation is approved)

1. Harvester + filters against one licensed repo; measure yield rate.
2. Synthesizer for the TB3 contract; certify 5 tasks end-to-end manually.
3. Certification runner as `evallab` command emitting the record schema.
4. Perturbation operators, one at a time, each with a certified family.
5. Only then: scale, batch reporting, and the catalog views.

Each step lands behind the existing queue/policy gates; no new approval
surface is created by this blueprint.
