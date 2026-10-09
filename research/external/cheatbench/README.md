---
status: living
audience:
  - analyst
  - operator
---

# CheatBench external corpus (public cheating traces)

**State: pending until fetched.** The 500 MB parquet is never committed
(`.gitignore`); a fresh checkout holds only this README, `PIN`, and
`fetch.py`. Run the fetch once to materialize it here.

## What it is

`steinad/CheatBench` (MIT, https://huggingface.co/datasets/steinad/CheatBench,
pinned in `PIN`): 4,472 labelled agent traces from public benchmark runs —
Terminal-Bench 2, SWE-bench Verified, CyBench, SWE-smith, HAL-USACO,
MLE-bench, BountyBench, SWE-rebench, KernelBench. Labels: `cheating` (844),
`attempt` (41), `benign` (3,587). Every positive row carries a
`cheating_mechanism` and line-level `loc_*` localization.

**These are other people's runs from public benchmarks, not Eval Lab runs.**
Imported outcomes stay theirs, flagged external, never recomputed into ours.

## Contamination class — binding

Behaviour-study material only. Model exposure on these public tasks is
unknown and unknowable; nothing derived here enters a capability claim, a
reliability curve presented as agent capability, or an eval card's Result
section. Never train on a split that is also evaluated. `fetch != register`:
acquiring this corpus never registers a task — registration is human-only
via `library/registry/`.

## Viewer subset (194 rows)

Every `cheating`/`attempt` row with a MiMo-relevant mechanism
(`secret_discovery_in_environment`, `secret_discovery_via_web_search`,
`verifier_exploitation` — future git history, upstream fetch, grader
exploitation). Counts (benchmark × mechanism × label):

| Benchmark | Mechanism | Label | N |
|---|---|---|---|
| bountybench | verifier_exploitation | cheating | 2 |
| cybench | secret_discovery_via_web_search | cheating | 17 |
| kernelbench | verifier_exploitation | cheating | 3 |
| mle-bench | secret_discovery_in_environment | cheating | 1 |
| mle-bench | verifier_exploitation | cheating | 2 |
| swe-bench-verified | secret_discovery_in_environment | cheating | 95 |
| swe-bench-verified | verifier_exploitation | cheating | 2 |
| swe-rebench | secret_discovery_in_environment | cheating | 8 |
| swe-smith | secret_discovery_in_environment | attempt | 3 |
| swe-smith | secret_discovery_in_environment | cheating | 1 |
| swe-smith | verifier_exploitation | attempt | 6 |
| swe-smith | verifier_exploitation | cheating | 5 |
| terminal-bench-2 | secret_discovery_in_environment | attempt | 1 |
| terminal-bench-2 | secret_discovery_in_environment | cheating | 1 |
| terminal-bench-2 | secret_discovery_via_web_search | attempt | 5 |
| terminal-bench-2 | secret_discovery_via_web_search | cheating | 13 |
| terminal-bench-2 | verifier_exploitation | attempt | 25 |
| terminal-bench-2 | verifier_exploitation | cheating | 4 |

The full parquet stays stored for later corpus work; only this subset is
imported into the viewer.

## Re-import ($0)

```bash
python research/external/cheatbench/fetch.py   # pinned download + sha256, into this dir
uv run --with inspect-scout==0.5.4 python \
  research/explorations/trace-lab/scout/import_cheatbench.py \
  --db <scout-transcripts-dir> --staging <staging-dir>
```

Converter: `src/evallab/cheatbench.py` (`cheatbench.raw_trace.v1` → staged
ATIF; label, mechanism, rationale and localization carried as transcript
metadata plus a clearly-marked final annotation message stating the ATIF
step and transcript message number of the cheat). Tests:
`tests/test_cheatbench.py`.

## How to open

Throwaway Scout view (never on 7576/8100/8501):

```bash
scout view <project-dir-with-scout.yaml> -T <db> --port <free-port> --no-browser
```

Go-live on the always-on Scout viewer (`http://127.0.0.1:7576`) is a
parent-run step: see the PR report for the exact post-merge commands.
Our 8501/8100 run counts are untouched — nothing is written to
`~/Developer/eval-lab-results`.
