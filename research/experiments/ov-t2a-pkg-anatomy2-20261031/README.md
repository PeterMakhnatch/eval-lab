# ov-t2a-pkg-anatomy2-20261031

Benchmark driver for `evallab.package_anatomy` (`evallab tasks anatomy`): per-package latency on the 35 strictly-DEV MiMo `format-code` packages, the C1-C5 impossible-task detection check, localization against the 30 `task_quality.repair_qualified` repairs, and the compact cards for the frozen OQ-6 DEV cohort.

```bash
EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run --no-sync python research/experiments/ov-t2a-pkg-anatomy2-20261031/run_bench.py --out <receipts dir>
```

DEV only: package bytes, vendored DEV reference patches, the journal's repair events (read-only) and the OQ-6 plan's package paths gated by `CorpusRegistry`. No answer key, TEST split, model call or container. The preregistration, gates, receipts and report live in the research-context bridge (`2026-10-31-overnight/ag-sonnet/raw/T2A-PKG-ANATOMY2/`), outside the repository.
