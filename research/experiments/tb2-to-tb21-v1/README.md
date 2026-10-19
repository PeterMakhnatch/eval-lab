# tb2-to-tb21-v1

Version-differential scoring for the 26 PR #53 tasks and 26 format-only controls in `research/answer-keys/tb2-to-tb21-v1`. Packages are extracted with `git archive` from the frozen local mirrors, never from their working trees:

- TB2: `~/Developer/lab-extras/upstream/terminal-bench-2-tb20` at `69671fbaac6d67a7ef0dfec016cc38a64ef7a77c`, task at `<task_id>/`
- TB2.1: `~/Developer/lab-extras/upstream/terminal-bench-2-1` at `7131e4375048a0e408a8fb404b5f499d726b695b`, task at `tasks/<task_id>/`

`materialize.py` checks every package with `evallab.registry.compute_task_digests` and writes `runs/tb2-to-tb21-v1/materialization.json`. A digest mismatch or missing archive is recorded and excludes that package.

## Rules

- Controls are oracle and nop on both versions, through `evallab.vcheck.run_controls` and `grade_submission`.
- The cheat ladder and both Codex arms run on the TB2 package only. Codex uses `PinnedCodex` via `submit`, `approve --actor rh-agents-2026-10-17/tick`, and `tick`, with at most 3 attempts and failed-attempt memory.
- Every claim is replayed in a fresh container on both versions. A modified-task finding counts only when that replay scores 1.0 on TB2 and 0.0 on TB2.1. A replay of at least 1.0 on TB2.1, on a modified task or a format-only control, is a false discovery.
- `score_tb2_to_tb21.py` writes `qa.finding/v1` findings using the key's category and root cause, scores the test split (52 pairs from `pairs.jsonl`), then scores the unexposed subset after excluding `breaker_bench_dev_exposure` items and any pair that touches one. Both results append scoreboard rows with those disclosure notes.

## Commands

Materialize and print the plan:

```bash
.venv/bin/python research/experiments/tb2-to-tb21-v1/run_tb2_to_tb21.py --materialize --status
```

Launch controls, the TB2 ladder, all Codex rounds, and fresh replays only after both local Docker slots are free:

```bash
.venv/bin/python research/experiments/tb2-to-tb21-v1/run_tb2_to_tb21.py --controls --floor --all-rounds --replay --status --actor rh-agents-2026-10-17/tick
```

Score completed evidence:

```bash
.venv/bin/python research/experiments/tb2-to-tb21-v1/score_tb2_to_tb21.py
```

## amd64 and Modal

Controls pin `linux/amd64` on the staged execution copy (`--platform ''` keeps the daemon default). The answer-key package is not edited. Modal is opt-in, requires `--approve` and `--max-usd`, and this runner refuses a cap above $1:

```bash
.venv/bin/python research/experiments/tb2-to-tb21-v1/run_tb2_to_tb21.py \
  --controls --cells install-windows-3.11 \
  --environment modal --approve "tb2 native amd64" --max-usd 1
```

Modal controls use the dedicated `mimo-clean-census-tb2-amd64` billing app so
the native runner accepts the requested CPU/memory limit policy. The earlier
`tb2-amd64-modal` name was refused before sandbox creation. The $1 flag limits
reservations within one invocation, not provider billing or cumulative spend
across invocations; keep an external campaign receipt and do not reset spent
reservations when retrying.

TB2 `build-pmars` prefers the complete Debian `20251101T000000Z` snapshot dependency set, not only `dpkg-dev=1.22.21`: current trixie's `libc6-dev` breaks its required `libdpkg-perl`. This restores the frozen oracle's dependency versions inside the execution container without editing the source task or oracle. TB2 `rstan-to-pystan` installs certificates, then pins `curl=8.5.0-2ubuntu10.6` from an Ubuntu snapshot. Both `mcmc-sampling-stan` versions install cmake before the oracle, because RcppParallel's configure step requires it and the frozen script does not. `caffe-cifar-10` uses each version's own agent timeout (TB2 1200s, TB2.1 3600s). `protein-assembly` is not fixed here: the recorded oracle fetches live RCSB sequences that still contain chromophore `X`, then DnaChisel fails before writing `gblock.txt`. Local Docker does not disable Rosetta, so `install-windows-3.11` and a TB2 caffe retry that must finish inside 1200s need Modal.
