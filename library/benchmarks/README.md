# Frontier benchmark library (INGEST)

Pinned, Harbor-runnable copies of public 2026-cite benchmarks. Survey and
rejections live in [`SURVEY.md`](SURVEY.md). Each materialized bench has
`library/benchmarks/<name>/MANIFEST.md` (source, pin, license, counts, lane,
resources, sample verification).

**Never `@latest`.** Re-download only at the version in the manifest:

```bash
harbor download aime@1.0
harbor download gpqa-diamond@1.0
harbor download humanevalfix@1.0
harbor download terminal-bench-sample@2.0
```

Jobs stay in the INGEST worktree `./runs/` (gitignored). Verification is
free `oracle` / `nop` only (`-k 1 -n 2`). These numbers test the **task +
harness**, not a model.

## Materialized this pass

| Bench | Hub pin | Tasks on disk | Sample verified | License | Lane |
| --- | --- | --- | --- | --- | --- |
| [aime](aime/MANIFEST.md) | `aime@1.0` (`414014c2…`) | 60 | 5 / 5 oracle 1.0, nop 0.0 | MAA contest; lab-internal | Hub |
| [gpqa-diamond](gpqa-diamond/MANIFEST.md) | `gpqa-diamond@1.0` (`1983ac5c…`) | 198 | 5 / 5 oracle 1.0, nop 0.0 | CC-BY-4.0 | Hub |
| [humanevalfix](humanevalfix/MANIFEST.md) | `humanevalfix@1.0` (`ab02ff13…`) | 164 | 5 / 5 oracle 1.0, nop 0.0 | HumanEvalPack research | Hub |
| [terminal-bench-sample](terminal-bench-sample/MANIFEST.md) | `terminal-bench-sample@2.0` (`7e917f35…`) | 10 | 4 / 4 CPU oracle 1.0, nop 0.0 | Apache-2.0 | Hub |

## Canary nominees

Cheap, deterministic, already sample-verified. Complementary to CURATOR’s
TB3 task cards (those stay in `library/curated/`).

| Priority | Path | Why |
| --- | --- | --- |
| 1 | `aime/aime/aime_60` | Smallest exact-match math canary; integer to `/app/answer.txt`; seconds |
| 2 | `humanevalfix/humanevalfix/python-0` | Code-repair + pytest; oracle applies official fix; nop fails |
| 3 | `gpqa-diamond/gpqa-diamond/0` | Science MCQ letter; checks the QA image/verifier path |
| 4 | `terminal-bench-sample/terminal-bench-sample/regex-log` | Agentic terminal slice distinct from CURATOR TB3 cards; prebuilt GHCR image |

Do **not** put qemu-*, SWE instance images, OSWorld, or MLE-bench on the
canary suite.

## First experiment targets (`registered/*` — Peter-gated)

| Target | Why first | Notes |
| --- | --- | --- |
| AIME full 60 | Standard 2026 math cite; already on disk | Exact match; no tools |
| GPQA-Diamond full 198 | Standard 2026 science cite | Shuffled choices; no web |
| HumanEvalFix full 164 | Compact debug/repair vs SWE-bench | CPU pytest |
| LiveCodeBench v6 (not yet ingested) | Contamination-aware codegen | Next INGEST continuation |
| `bfcl_parity@1.0` (123) | Tool-calling without the 3.6k dump | Next adapter/Hub slice |

TB-sample is a **canary / smoke** set, not a scoreboard. Full TB2 (89) and
TB3/Frontier stay with CURATOR’s cards + the existing local clones.

## Explicitly not nominated

| Bench | Why |
| --- | --- |
| SWE-bench Verified / Pro | Multi-GB instance images |
| OSWorld | Desktop / cloud / GUI |
| MLE-bench / ML-dev | GPU / long jobs |
| GAIA / GAIA2 | Live web; weak frozen oracle |
| HLE | Access + answer-key handling |
| Full BFCL (3641) | Scale; start from parity |

## Layout

```
library/benchmarks/
  README.md                 this file
  SURVEY.md                 ≥12 candidates + rejected list
  <name>/MANIFEST.md        pin + license + sample table
  <name>/<hub-folder>/      harbor download output (tasks)
```

INGEST does not edit `policy/canary-suite.yaml`. BUILDER / Peter register
canaries from the nominees above.

`ruff.toml` in this directory sets `lint.select = []` so Hub task sources
are not restyled (same reason `adapters/quixbugs/generated` is excluded at
the root). Do not “fix” upstream Python; it would change `task_checksum`.
