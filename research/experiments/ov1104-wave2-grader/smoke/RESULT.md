# Defensive smoke census: result

## Answer

The executed controls find **0 weak graders among 36 evaluated tasks**.
The 95% Wilson interval is **0.0–9.64%**.
This is an **executed-subset result**, not a complete X07 result or grader certification.
The requested exit-0 injection probe is not implemented or executed.

| Control | Accepted / evaluated | 95% Wilson interval |
|---|---:|---:|
| Null, no candidate changes | 0/36 | 0.0–9.64% |
| Actual 4096-byte artifact grid | 0/1 | 0.0–79.35% |
| Both supported controls, complete cases | 0/1 | 0.0–79.35% |

All 36 eligible task-image sets receive a valid null observation: **36/36**, Wilson **90.36–100%**.
Only `photonic-waveguide-routing` supports an inert artifact and has both required local images.
All 35 eligible code-edit-only tasks have an unsupported grid interface. No unrelated text-file probe substitutes for it.

## Coverage and missingness

The immutable admitted population has **55 tasks**: 49 from the prior strict-DEV inventory and 6 scorecard extras.
This is not the full current DEV registry. The runner never opens answer-key split files or constructs `CorpusRegistry`.
The manifest records the admission source hashes and exact package digests.

- Complete cached task-image sets: **36/55 = 65.45%**, Wilson **52.25–76.64%**.
- Missing complete task-image sets: **19 tasks**. `cad-model` has an agent image but lacks its verifier image.
- Declared inert-artifact interfaces: **8 tasks**. Only **1** has complete cached images; the other **7** remain unavailable.
- Unsupported inert-artifact interfaces: **47 tasks**.
- Final valid observations: **37 controls**, each with native reward **0.0**. There are **0** final infrastructure failures.
- Historical attempts: **2 infrastructure failures** and **1 invalid grid attempt**, preserved and excluded from final control counts.

The frozen Docker enumeration contains **242 image tags and 242 unique image IDs**.
The manifest selects **38 unique agent/verifier image IDs**, including the agent-only `cad-model` image.
The other **204 image IDs** are not assumed DEV. Their task content is not opened for admission.
`image-enumeration.json` records the raw enumeration path and hash.

## Exact mutation-audit comparison

The prior scorecard contains **24/37** mutation-weak tasks.
The exact joint denominator with this smoke census is **31 tasks**.
These 31 tasks have valid null observations but no supported grid interface.
The prior mutation audit finds **20/31** weak, Wilson **46.95–78.88%**.

| Null acceptance on the joint set | Mutation weak | Mutation not weak | Total |
|---|---:|---:|---:|
| Null reward 1 | 0 | 0 | 0 |
| Null reward 0 | 20 | 11 | 31 |
| Total | 20 | 11 | 31 |

The null control detects **0/20** mutation-weak tasks, Wilson **0.0–16.11%**.
This shows the null control's limited coverage. It does not refute the mutation results.
The 6 mutation-audited tasks without local images are `000484`, `000581`, `001793`, `001954`, `002212`, and `003005`.
Full task IDs and all 4 cross-tab cells appear in `summary.json`.

## Actual artifact and boundary evidence

The valid grid reaches the declared artifact `/app/routing_result_1.json`.
The exported artifact is exactly **4096 bytes**.
Its SHA-256 matches the preregistered payload:

`b880e4e6a9a7399fc8b4ca54a1edaac0e6fcc437cdb4ba8d1f4fbaaf8e5474b0`

The native artifact manifest reports `status: ok`.
The original grader rejects the actual content with `JSONDecodeError: Extra data`, then emits reward 0.
This is not an artifact-transfer failure.

The 37 valid trials retain **39 environment-start receipts**: 35 shared environments and 4 separate-role environments.
All **39/39** confirm the official network lock, empty actual mount lists, and exact declared CPU/memory limits.
The runner extends `evallab.harbor_docker.LockedDockerEnvironment`; it does not replace its security checks.
It uses no host bind mounts, host credentials, model inference, image builds, image pulls, or paid compute.
Native Harbor is **0.24.0**, under Python **3.12.11** from the prior grader worktree environment.

Some cached images emit existing Python startup warnings. Their target tests still run and reject missing requested behavior.
Missing newly requested task symbols remain genuine null rejections, not missing third-party infrastructure.
The raw verifier logs retain these distinctions. No protected grader or test files change.

## Runner defects and preserved receipts

1. Initial admission waits 300 seconds with FIFO capacity 1 and never obtains a container.
   Amendment 1 uses the documented shared maximum of 2 and a longer admission wait.
2. The first admitted run confirms network isolation and zero mounts but cannot download a reward.
   The inherited capability incorrectly reports `mounted=True` after this runner disables mounts.
   Amendment 2 reports `mounted=False`, so Harbor uses its native verifier-log download path.
3. The first grid attempt uploads its script but leaves `qa_command` unset.
   No artifact reaches the verifier. Its native reward 0 is an **invalid grid observation**, not a successful grid rejection.
   Amendment 3 binds the script command and requires the exported artifact hash to match the frozen payload.
   Only the affected grid reruns. The 36 valid null observations remain unchanged.

`protocol-history.json` records every freeze, failure receipt, and amendment.
The initial and amended protocol/runner snapshots remain outside Git.
No failed receipt is overwritten or reported as reward 0.

## Commands actually run

Run from `/Users/petermakhnatch/Developer/eval-lab/.worktrees/ov1104-wave2-grader`.

```sh
python3 research/experiments/ov1104-wave2-grader/smoke/smoke_runner.py prepare
python3 research/experiments/ov1104-wave2-grader/smoke/smoke_runner.py freeze
```

The following command runs the initial attempt and each pre-registered transport repair of `000079`:

```sh
env OV1104_SMOKE_RUNTIME=1 PYTHONPATH=src \
  python3 /tmp/nov2-line/coordinator/heavy.py -- \
  /Users/petermakhnatch/Developer/eval-lab/.worktrees/ov1104-grader/.venv/bin/python \
  research/experiments/ov1104-wave2-grader/smoke/smoke_runner.py run \
  --task format-code-task-000079
```

The complete eligible sweep runs once after Amendment 2. It resumes the completed valid `000079` control:

```sh
env OV1104_SMOKE_RUNTIME=1 PYTHONPATH=src \
  python3 /tmp/nov2-line/coordinator/heavy.py -- \
  /Users/petermakhnatch/Developer/eval-lab/.worktrees/ov1104-grader/.venv/bin/python \
  research/experiments/ov1104-wave2-grader/smoke/smoke_runner.py run
```

The repaired artifact control runs after Amendment 3:

```sh
env OV1104_SMOKE_RUNTIME=1 PYTHONPATH=src \
  python3 /tmp/nov2-line/coordinator/heavy.py -- \
  /Users/petermakhnatch/Developer/eval-lab/.worktrees/ov1104-grader/.venv/bin/python \
  research/experiments/ov1104-wave2-grader/smoke/smoke_runner.py run \
  --task photonic-waveguide-routing
python3 research/experiments/ov1104-wave2-grader/smoke/smoke_runner.py summarize
```

Each repair receives a new `freeze` command before execution.
The final freeze SHA-256 is `b99ace1afe3462ca5ae17c3d8905d2977291a8e982f52cb6c2872ebcab181fa1`.
The manifest stays unchanged at `ea77ebadd7130ff308b5a35b4fceee89e6c0c44beddadaa3e9b744c5b74ad24c`.
The final summary SHA-256 is `a29de5c64187b8a32cebea386bc3cef3c15e7942811b3ff8759e549b8d85f357`.

## Files and limits

- `smoke_runner.py`: reusable frozen census, runtime, and aggregation commands.
- `manifest.json`, `PREREG.md`, `freeze.json`: admission, probes, protocol, and hashes.
- `summary.json`: per-task outcomes, denominators, Wilson intervals, and exact cross-tab.
- `receipt-manifest.json`: 37 valid trial directory hashes, grid hash, and boundary counts.
- Raw evidence: `/private/tmp/ov1104/wave2-grader/workers/smoke/`.
  The raw index covers **555 files**, **1,487,858 bytes**, for the valid trials.
  Its SHA-256 is `05d4799d30221c98f40785223ea7c41b1bdaad1b564f860b46743ac37d26e4bf`.
- Spend: **$0**. Hosted generation calls: **0**. No tests, lint, formatter, or build checks run by this worker.

The runner resumes this fixed manifest. A new population requires a new frozen experiment and fresh evidence directories.
The 300-second watchdog, cached-image selection, and admission snapshot limit this result.
No fresh positive reference controls run here. This is negative-control acceptance measurement, not task-solvability certification.
The third X07 probe remains unexecuted. **Full X07 remains incomplete.**
