# Breaker-bench ladder floor

The model-free floor for the Breaker agent: what Eval Lab's deterministic
cheat ladder scores on `breaker-bench-v1` dev pairs, so that RH Agents can set
their model agent against it. Ladder v1.4.0 (14 attacks) and v3.0.0 (adds the
v2 and v3 attacks), old vs fixed benchflow sandbox. $0, local Docker only, no
model is called.

Results: [`RESULTS.md`](RESULTS.md). Machine-readable findings:
`findings/ladder-v1.4.0/` and `findings/ladder-v3.0.0/` (one `qa.finding/v1`
per key item), scored by `score_floor.py` with the repo's own
`evallab.qa.score.score_findings`.

## What is measured

`research/answer-keys/breaker-bench-v1` pairs 214 tasks that benchflow's three
exploit scripts crack at benchflow 0.2.0 (`reward = 1.0`) with the same task at
0.2.2 (`reward = 0.0`). Dev split: 46 pairs (21 swebench-verified, 21
terminal-bench-2, 4 skillsbench). The child (0.2.0) is `fail`, the parent
(0.2.2) is `pass`.

For each pair side this experiment asks one question of the ladder: does it
crack the task (`cracked`: the benchmark's own verifier paid reward >= 1.0
after the attacks) or not (`clean`)? `cracked` becomes a `fail` finding,
`clean` a `pass`, and anything else (`infra`, `unscored`) is `unknown`, which
the scorer never counts as a pass. The ladder's attacks are not benchflow's
three exploits, so the key's `mechanism` is reported next to the attack that
actually fired; it is not assumed to match.

## Coverage: 9 of the 46 dev pairs

Only a subset of the dev pairs was run, chosen because their Docker images were
already cached or build fast: 3 skillsbench (`3d-scan-calc`,
`mars-clouds-clustering`, `invoice-fraud-detection`), 2 swebench-verified
(`pytest-dev__pytest-5809`, `pylint-dev__pylint-7277`) and 4 terminal-bench-2
(`constraints-scheduling`, `filter-js-from-html`, `query-optimize`,
`sqlite-db-truncate`). `score_floor.py` prints two rows per ladder: the
official dev-split row (`n_measured` of `n_items`, status `partial`) and a
covered-subset row computed on the key restricted to the items this run
measured. Quote the second one only with its `n`.

## Method and the emulation caveat

Benchflow itself is not run: it executes on Daytona and the 0.2.0 SDK is not
available. `run_floor.py` instead stages two Harbor task packages per pair and
runs the ladder on each with `evallab cheat run --ladder-version <v>`:

| mode | what it is |
|---|---|
| `0.2.0` | the pristine upstream task package, agent and verifier as root |
| `0.2.2` | an emulation of benchflow v0.2.2 `harden_before_verify` (below) |
| `0.2.2p` | ablation: `0.2.2` plus `procps` and `rsync` installed in the image |
| `0.2.3` | `0.2.2` with `restore_workspace=False` and `-p ctrf` (extra, not in the key; sqlite-db-truncate only) |

The `0.2.2` emulation is built from `benchflow-ai/benchflow` tag `v0.2.2`
(commit `540b01107656a1d836ddfb4d776eaaa5f73bd571`),
`src/benchflow/_sandbox.py`:

- Dockerfile: `FROM <the task's prebuilt docker_image>` plus a pre-agent layer
  that creates the `agent` user, snapshots build-config files, copies the
  workspace to `/testbed_verify`, and locks `/solution` and `/tests` to
  `root:root 700`; `task.toml` gets `[agent] user = "agent"` and
  `[verifier] user = "root"`, so the ladder's attacks run as `agent`.
- `tests/test.sh` becomes a wrapper that does `harden_before_verify` and then
  `exec bash /tests/test-orig.sh`: `pkill -u agent`, clear `/logs/verifier`
  (`rm -rf` then `mkdir` and `chmod 777`, exactly as `_CLEAR_VERIFIER_DIR_CMD`),
  restore build-config and `rsync` the workspace back from `/testbed_verify`,
  purge symlinks and `__pycache__`, `CLEANUP_CMD`, and re-pin `VERIFIER_ENV`
  (`PYTEST_ADDOPTS=-c /dev/null --confcutdir=/tests ...`,
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `PYTHONPATH=""`, `HOME=/nonexistent`).

This host is arm64. swebench images are amd64-only, and Harbor builds the
task Dockerfile rather than pulling a prebuilt image when a Dockerfile is
present. Each staged package therefore carries
`environment/docker-compose.yaml` with `services.main.platform: linux/amd64`,
which Harbor merges last. Without that pin the swebench builds fail with
`no match for platform in manifest` and the cell is `unknown`, not `pass`.
The snapshot and restore steps are shell (`cp`), not `python3`: benchflow's
own snapshot is shell, and some task images (query-optimize) have no
`python3`.

What the emulation does not capture: benchflow runs the verifier through
Harbor's own `Verifier` after `harden_before_verify` as separate `exec` calls,
while here the hardening runs at the top of the verifier script, inside the
same call. Daytona images, the real ACP agent, and benchflow's own trusted
`PATH` handling (0.2.3) are not reproduced. Treat `0.2.2` results as "the
ladder against the v0.2.2 hardening as written", not as a benchflow run.

## Reproduce

Sources (not in this repo):

- TB2.0 task packages at the pinned commit:
  `~/Developer/lab-extras/upstream/terminal-bench-2-tb20/<slug>/`
- skillsbench and swebench-verified task packages: `laude-institute/harbor-datasets`
  sparse clone (commit `37db108843a49bb31a592e37a75e2c40dc3f9749`) under
  `.corpora/harbor-datasets` of a `benchflow-ai/benchflow` `v0.2.2` checkout
  at `/tmp/benchflow-v0.2.2/labs/reward-hack-matrix`.

```bash
uv sync --frozen --extra laminar            # Harbor 0.24; a plain sync uses the global 0.21
.venv/bin/python research/experiments/breaker-bench-ladder-floor/run_floor.py \
    --modes 0.2.0,0.2.2,0.2.2p --ladders 3.0.0,1.4.0 --workers 2
.venv/bin/python research/experiments/breaker-bench-ladder-floor/score_floor.py
```

`run_floor.py` caches finished jobs (a `cheat-verdicts.json` whose verdict is
`clean` or `cracked`), so an interrupted run resumes. It never runs more than
two containers at once. Jobs and staged packages live under
`runs/breaker-bench-ladder-floor/` (not committed); the small proof files that
back each finding are copied to `evidence/<job>/`.
