## QuixBugs → Harbor Adapter

## Overview

This adapter turns the Python half of the pinned
[QuixBugs](https://github.com/jkoppel/QuixBugs) benchmark into 40 deterministic
Harbor repair tasks. Each task starts with one upstream defect, exposes only
the defective Python implementation to the agent, and scores `1` only when the
separate upstream pytest suite passes.

The upstream source is pinned to commit
`4257f44b0ff1181dedaedee6a447e133219fcebf` and is MIT licensed. This
repository intentionally adapts only the 40 Python programs to comply with the
repository-wide language policy in `AGENTS.md`.

## What is QuixBugs?

QuixBugs is a program-repair benchmark based on classic algorithm challenges.
Each Python implementation contains a small defect and has an upstream test
suite plus a corrected reference implementation. The Harbor reward is binary
upstream-test success. Passing demonstrates compatibility with the supplied
tests, not semantic equivalence for every possible input.

## Adapter Features

- A self-contained `uv` package created with `harbor adapter init`.
- Exact source pinning and clean-checkout validation.
- Stable IDs of the form `quixbugs-python-<program>`.
- Full or bounded generation through `--output-dir`, `--limit`, `--task-ids`,
  and `--overwrite`.
- Python 3.13 with a separate, hash-locked pytest verifier.
- Agent images contain the target implementation but not tests or the Oracle
  solution.
- Deterministic `generation_manifest.json` with the source ref, selected IDs,
  task count, and content-tree SHA-256.
- Binary rewards, per-test verifier output, and bounded timeouts.

## Generated Task Structure

```text
adapters/quixbugs/
├── generated/
│   ├── generation_manifest.json
│   └── quixbugs-python-<program>/
│       ├── LICENSE
│       ├── task.toml
│       ├── instruction.md
│       ├── environment/
│       │   ├── Dockerfile
│       │   └── python_programs/<program>.py
│       ├── solution/solve.sh
│       └── tests/
│           ├── Dockerfile
│           ├── test.sh
│           └── python_testcases/
├── src/quixbugs/
│   ├── adapter.py
│   ├── main.py
│   └── task-template/
├── pyproject.toml
└── uv.lock
```

The evaluated agent edits `/app/python_programs/<program>.py`. Harbor transfers
only that declared artifact to the separate verifier environment.

## Run Evaluation / Harness

The checked-in configuration is model-free and uses the local Oracle:

```bash
harbor run -c adapters/quixbugs/run_quixbugs.yaml
```

For one free local control:

```bash
harbor run \
  -p adapters/quixbugs/generated/quixbugs-python-gcd \
  -a oracle -k 1 -n 1 \
  -o runs/quixbugs-adapter/smoke
```

No model or billed provider is configured. Model-backed evaluation requires an
explicit operator choice and the standing-policy queue.

## Usage: Create Task Directories

From the adapter directory:

```bash
uv sync --frozen
uv run quixbugs --output-dir generated --overwrite
```

The default fetch verifies the exact pinned Git commit. A local source checkout
is accepted only when it is clean and at that commit:

```bash
uv run quixbugs \
  --source-dir /path/to/QuixBugs \
  --output-dir generated \
  --overwrite
```

Bounded examples:

```bash
uv run quixbugs --output-dir /tmp/quixbugs-five --limit 5
uv run quixbugs --output-dir /tmp/quixbugs-selected \
  --task-ids quixbugs-python-gcd quixbugs-python-minimum_spanning_tree
```

Generation is staged. An error before the atomic swap preserves the previous
output. A non-empty destination requires `--overwrite`.

The committed full manifest contains 40 tasks and tree digest
`23cebf7f3c641e27afade09d3886dc4de8f55ac72027e900d079b2f49e3789eb`.

## Comparison with Original Benchmark (Parity)

**Status: deferred; cost: `$0`.** No model-backed Harbor run has been performed.
Oracle/no-op controls validate adapter wiring but are not evidence of agent
capability or parity with published repair systems. A valid comparison must
first pin the repair system, revision, task subset, tests, search budget, and
repeated-run protocol in both harnesses.

## Notes & Caveats

- Upstream tests are public, although hidden from the evaluated process by the
  Harbor environment boundary.
- Rewards measure the supplied tests; behavior outside covered inputs can still
  be wrong.
- The adapter follows the upstream Python suite's default exclusions and adds a
  per-test-file timeout.
- Initial generation and image builds require network access. The source commit,
  container image, and Python verifier dependencies are pinned.
- Prior free controls covered `quixbugs-python-gcd`,
  `quixbugs-python-breadth_first_search`, and
  `quixbugs-python-minimum_spanning_tree`: Oracle attempts passed and no-op
  attempts failed as expected. `verification_evidence.json` records their
  provenance. These controls are harness checks, not capability results.

## Installation / Prerequisites

- Python 3.11 or newer
- Git
- `uv`
- Harbor `0.21.0` or a compatible newer version
- Docker Desktop/Engine

```bash
cd adapters/quixbugs
uv sync --frozen
uv run quixbugs --help
```

## Citation

```bibtex
@inproceedings{lin2017quixbugs,
  title     = {QuixBugs: A Multi-Lingual Program Repair Benchmark Set Based on the Quixey Challenge},
  author    = {Lin, Derrick and Koppel, James and Chen, Angela and Solar-Lezama, Armando},
  booktitle = {Companion Proceedings of SPLASH 2017},
  year      = {2017}
}
```

## Authors & Contributions

Adapter implementation: Peter Makhnatch. Original benchmark authorship and
source files remain attributed to Derrick Lin, James Koppel, Angela Chen, and
Armando Solar-Lezama. Contributions must preserve the Python-only policy,
source pin, deterministic generation, separate-verifier boundary, and
Oracle/no-op controls.
