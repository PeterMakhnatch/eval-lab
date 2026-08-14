# Frontier benchmark survey (INGEST, 2026-08-14)

Quality bar: a frontier-lab eval team would **cite** the benchmark in 2026, not every GitHub repo with “bench” in the name.

**Ingestion lanes** (preference order):

1. **Hub** — Harbor legacy registry / Hub, **pinned version** (never `@latest`)
2. **Adapter** — `harbor/adapters/<name>` with `--limit` slice
3. **Thin local adapter** — only if 1–2 do not exist

Harbor registry pins below come from `harbor dataset list --legacy` plus `harbor-framework/harbor` `registry.json` (local clone). Git SHAs are those recorded for each dataset’s tasks.

---

## Accepted for materialization (this pass)

### 1. AIME (2024 / 2025-I / 2025-II)

- **Measures:** contest math; integer answers 0–999; exact match.
- **Who cites it:** default frontier math slice (with AMC/HMMT/etc.) in 2025–2026 model cards.
- **Why top-tier:** official MAA contest; unambiguous verifier; 60 items; small CPU.
- **Lane:** Hub `aime@1.0` (60 tasks, harbor-datasets commit `414014c23ce4d32128073d12b057252c918cccf4`). Adapter `harbor/adapters/aime` exists.
- **License diligence:** contest items are MAA-copyrighted; Harbor packaging is for eval. Do not republish problem text outside this lab. Oracle writes the integer only.
- **Resources:** tiny Ubuntu/Python images; no GPU.
- **Status:** materialized — `library/benchmarks/aime/`

### 2. GPQA-Diamond

- **Measures:** graduate-level science MCQ (bio/physics/chem), intended to be Google-resistant.
- **Who cites it:** GPQA / “Diamond” is a standard 2024–2026 reasoning suite item (often with no tools).
- **Why top-tier:** expert-written; published paper ([arXiv:2311.12022](https://arxiv.org/abs/2311.12022)); 198 Diamond items.
- **Lane:** Hub `gpqa-diamond@1.0` (198 tasks, harbor-datasets commit `1983ac5c4d43f43cb7a9af9f89c54d09025589ec`).
- **License:** CC-BY-4.0 (dataset card).
- **Resources:** CPU; scientific Python stack in adapter images.
- **Caveat:** shuffled choices (adapter seed); not a substitute for lab practicals. Web tools change the original “Google-proof” claim.
- **Status:** materialized — `library/benchmarks/gpqa-diamond/`

### 3. HumanEvalFix (HumanEvalPack)

- **Measures:** repair of buggy Python HumanEval functions; pytest/pass@k style.
- **Who cites it:** OctoPack / code-repair papers; still a compact agent debug canary (not SWE-bench).
- **Why keep:** 164 tasks, oracle exists, CPU, Harbor adapter + Hub pin.
- **Lane:** Hub `humanevalfix@1.0` (164 tasks, harbor-datasets commit `ab02ff13250fae8d91b93a6e4c11ce0bdcb78215`).
- **License:** HumanEval / HumanEvalPack terms (research use; not a free-for-all republish).
- **Status:** materialized — `library/benchmarks/humanevalfix/`

### 4. Terminal-Bench 2.0 sample

- **Measures:** agentic terminal work (the TB2 public sample, 10 tasks).
- **Who cites it:** Terminal-Bench is the lab’s home benchmark family; TB2 is the citable 2025 public set; TB3/Frontier is the 2026 successor (already in CURATOR’s task library).
- **Why ingest here:** Hub-pinned **sample** (`terminal-bench-sample@2.0`, 10 tasks, commit `7e917f35c281188532772312d4ad91ca9274febc` on `laude-institute/terminal-bench-2-0-sample`) is a legal, small, Harbor-native slice. Full `terminal-bench@2.0` is 89 tasks at commit `69671fbaac6d67a7ef0dfec016cc38a64ef7a77c`.
- **License:** Apache-2.0 (TB repos).
- **Status:** materialized — `library/benchmarks/terminal-bench-sample/` (4 CPU tasks oracle 1.0 / nop 0.0)

---

## Assessed, not materialized this pass

### 5. Terminal-Bench / Frontier-Bench (full)

- **Measures:** long-horizon terminal jobs, outcome-verified, TB3-hard.
- **Why top-tier:** this lab’s primary agent bench; Frontier-Bench is the 2026 successor name.
- **Lane:** already cloned locally (`frontier-bench` `3d694e91`, `terminal-bench` `4e77c91d`). CURATOR holds 19 verified **task** cards. Full Hub `terminal-bench@2.0` (89) is ingestable later.
- **Not duplicated here** to avoid a second copy of 74 heavy TB3 tasks.

### 6. SWE-bench Verified

- **Measures:** real GitHub issues → patches; official tests.
- **Why top-tier:** default 2024–2026 coding-agent citation (500 verified).
- **Lane:** Hub `swebench-verified@1.0` (500). Adapter `harbor/adapters/swebench`.
- **Skip this pass:** per-instance Docker images are multi-GB; not a laptop canary. **Not GPU**, but **cloud/disk-heavy**.
- **License:** Apache-2.0 (benchmark); instance repos have their own licenses.

### 7. SWE-bench Pro

- **Measures:** harder, often multi-file, more languages than Verified.
- **Why top-tier:** 2025–2026 “Verified is saturated” follow-on.
- **Lane:** Hub `swebenchpro@1.0` (731). Adapter `swebenchpro`.
- **Skip:** same image/disk class as Verified; plus Scale licensing diligence.

### 8. LiveCodeBench (release v6 subset)

- **Measures:** contamination-aware competitive programming (time-split).
- **Why top-tier:** standard 2025–2026 code-gen cite (vs HumanEval contamination).
- **Lane:** Hub `livecodebench@6.0` (100 sampled). Adapter `livecodebench`.
- **Skip this pass:** hidden tests / judge runtime can be heavy; continue later as experiment target (CPU possible).

### 9. BFCL (Berkeley Function-Calling Leaderboard)

- **Measures:** tool/function calling (simple, parallel, multiple, irrelevance).
- **Why top-tier:** default tool-use leaderboard through 2025–2026.
- **Lane:** Hub `bfcl@1.0` (3641) and `bfcl_parity@1.0` (123). Adapter `bfcl`.
- **Skip this pass:** 3.6k tasks; many need tool schemas / AST judges. `bfcl_parity` is the right next slice.

### 10. GAIA / GAIA2

- **Measures:** multi-step assistant questions (web, files, tools).
- **Why top-tier:** GAIA is the canonical “general assistant” cite; GAIA2 is the 2025+ refresh.
- **Lane:** Hub `gaia@1.0` (165). Adapters `gaia`, `gaia2`.
- **Skip:** **live web + files**; not a frozen local oracle/nop canary. Many items are not “oracle = 1 without a model.”

### 11. HLE (Humanity’s Last Exam)

- **Measures:** extremely hard closed-ended academic questions.
- **Why top-tier:** 2025–2026 “still hard for frontier” exam suite.
- **Lane:** adapter `hle` exists; no small Hub pin used here.
- **Skip:** closed answers + often LLM/exact judges; dataset access terms; not a 5-task free oracle story without dumping answers.

### 12. OSWorld

- **Measures:** desktop/computer-use (GUI, apps).
- **Why cited:** leading computer-use bench.
- **Lane:** adapter `osworld`.
- **Skip:** **not locally runnable as a canary** (display, often cloud VMs, sometimes GPU). Explicit GPU/cloud skip.

### 13. tau-bench / τ²-bench class

- **Measures:** multi-turn tool-use with a user simulator (retail/airline).
- **Why cited:** 2024–2026 agent-harness papers (Sierra / τ-bench).
- **Lane:** no first-class Hub name in this registry dump; would be a **thin adapter** later.
- **Skip this pass:** needs a user-sim + policy; not oracle-from-`solve.sh` in the TB sense.

### 14. MLE-bench / ML-dev-bench class

- **Measures:** Kaggle-style ML engineering (train, submit).
- **Why cited:** OpenAI MLE-bench; 2025–2026 “agents that do ML.”
- **Lane:** adapters `ml_dev_bench`, `mlgym-bench`.
- **Skip:** **GPU/cloud/time**; say so. Not a free local 3-task oracle.

### 15. Extra find: ARC-AGI-2 (Hub `arc_agi_2@1.0`, 167)

- **Measures:** abstract grid puzzles.
- **Why cited:** 2025–2026 “still hard” visual/abstract reasoning.
- **Lane:** Hub + adapter `arc_agi_2`.
- **Not materialized:** lower priority than AIME/GPQA for *this* terminal-agent lab; grids are a different modality.

### 16. Extra find: Aider Polyglot (Hub `aider-polyglot@1.0`, 225)

- **Measures:** Exercism-style edits across languages.
- **Why cited:** aider leaderboard; coding-agent staple.
- **Skip this pass:** many language toolchains; Java/etc. conflicts with lab Python-only *application* policy for new code — generated tasks in other languages are a policy question (AGENTS.md: ask Peter before TS/Java **in imported tasks**). Recorded as **accepted conceptually, not copied**.

---

## Rejected (or deferred) — the valuable half

| Candidate | Decision | One-line reason |
| --- | --- | --- |
| SWE-bench Verified (full 500) | defer | Multi-GB instance images; not a laptop canary |
| SWE-bench Pro | defer | Same + extra license/size |
| SWE-Lancer Diamond | defer | Large; preparedness/OpenAI packaging |
| SWE-smith | defer | Synthetic SWE; not first-line cite |
| GAIA / GAIA2 | defer | Live web / files; weak frozen oracle |
| OSWorld | reject-for-local | Desktop/cloud/GUI; not local CPU canary |
| HLE | defer | Access + answer-key handling |
| BFCL full (3641) | defer | Scale; start from `bfcl_parity` later |
| tau-bench class | defer | Needs user simulator; no Hub pin used |
| MLE-bench / ML-dev | reject-for-local | GPU/cloud/long jobs |
| LiveCodeBench v6 | defer | Next experiment target; not this slice |
| SimpleQA | reject | Short factoid QA; not a 2026 *agent* frontier cite |
| DS1000 / BigCodeBench | defer | Useful code benches; not the four we needed first |
| Unnamed GitHub “xyz-bench” | reject | Does not clear the citation bar |
| `@latest` Hub refs | reject | Forbidden; every ingest is version-pinned |

---

## What we did **not** do

- No unpinned sources.
- No GPU/cloud verification.
- No billable model runs.
- Did not vendor 500 SWE images or 3.6k BFCL tasks into git.
