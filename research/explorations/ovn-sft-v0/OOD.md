# Later OOD evaluation: SWE-bench Verified Python slice

**HAR-134 · Library · 2026-10-01 · $0 / no trials**

## Decision

Recommend **the 20 SWE-bench Verified instances below** for a later, separately admitted stock-versus-LoRA comparison using **Harbor + Terminus-2 `lf2` + Daytona**, with the MiMo distill served on Modal. This is a **completed source-level qualification and cost assessment**, not runtime certification or approval to execute. The candidate has real task/package/verifier/image identities; its remaining admission conditions are explicit below.

This does **not** replace tonight's G1/G5 FineEnvs evaluation, consume its $30 allocation, resume HAR-61, or authorize a benchmark sweep. No cloud environment, model call, oracle/nop trial, image build/pull, training job, or paid search was launched for this qualification. Docker registry **manifest metadata** was fetched, not image layers.

Use the label **“repository-screened, 20-task SWE-bench Verified subset”**, not “SWE-bench Verified score.” OOD here means a later shift from the current FineEnvs training tasks to human-written issue-resolution tasks. It does not establish absence of base-model pretraining exposure, and source-family difference alone does not establish training/evaluation disjointness.

## Why this benchmark rather than Terminal-Bench 2

SWE-bench Verified provides Python repository repair tasks with issue descriptions, base commits, reference patches, and executable test graders. It matches the current model/domain while changing task provenance and problem formulation. A broad terminal benchmark would confound transfer with shell/system administration and non-Python work.

The [pinned Harbor adapter README][adapter] and [parity record][parity] now contain stronger Daytona evidence than the old image-size-only deferral:

- Three registry-backed **Daytona** sweeps with `mini-swe-agent==2.1.0` + GPT-5-mini resolved **265/499, 275/499, and 276/499** comparable tasks: mean **0.545**, reported SEM **0.007**. A recurring scikit-learn timeout was excluded. These are upstream reports, not Library measurements.
- A separate, older **Terminus-2 + Claude Sonnet 4.5** comparison covers all 500 tasks. The two records support the individual components, but **do not prove their exact conjunction with MiMo, `lf2`, the current proxy, or this LoRA adapter**.
- The current adapter pins the regenerated task snapshot, including the verifier `uv` PATH fix, to **`harbor-datasets@86723674f04e4209ac479d0fb75d9d9f44b4377e`**.
- There is a source inconsistency: [adapter metadata][metadata] still summarizes a 10-task parity subset, whereas the detailed README/JSON records the three full Daytona sweeps. This report cites the detailed dated record rather than claiming that all metadata agrees.

Terminal-Bench 2 remains an alternative, not the recommended primary slice. Its inspected historical snapshot `69671fbaac6d67a7ef0dfec016cc38a64ef7a77c` has 89 task configurations spanning 16 categories and heterogeneous resource needs. It adds domain changes we do not need for this question. Its current README advertises Apache 2.0, but that historical tree does not contain a root license file; present-day licensing text must not be retroactively asserted as a file at the old pin. Neither benchmark's public availability grants permission to train on its held-out evaluation tasks.

## What was actually exercised

| Check | Observed result | What it does not establish |
|---|---|---|
| Installed Harbor CLI | `harbor 0.21.0`; legacy registry lists `swebench-verified@1.0`, 500 tasks | Registry availability is not a passing trial |
| Native source intake | `harbor datasets download` materialized all **20/20** selected packages at the pinned commit, using a temporary subset registry | No container was built or started |
| Real Harbor task loading | `harbor.models.task.task.Task` loaded all **20/20** packages; each normalized to Linux, 1 CPU, 4096 MiB RAM, 10240 MiB storage, no GPU; source `memory='4G'`/`storage='10G'` were actually consumed | No measurement of peak RAM/disk use |
| Native Eval Lab preparation | A real `evallab tasks prepare` CLI invocation, then the same `prepare_task` API on all 20, accepted `terminus-2` + `selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` + `daytona`; package/verifier digests frozen; all `spec_id`/`submitted_at` remained null | No submit, approval, tick, paid model response, or `lf2`/adapter runtime proof |
| Public OCI metadata | All **20/20** tags resolved to a Linux/amd64 manifest; immutable platform-manifest digests recorded | Images were not pulled; no guarantee of Daytona image admission or unpacked disk fit |
| Selection replay | Full 500-ID source frame, exclusions, salted ranking and round robin reproduced the listed 20 IDs exactly | Declared project labels are not canonical repository identity |

Preparation used Eval Lab revision `5f372233ff862baa0b88d9b271576b53f20b9fb1`. The scratch specs used a positive transport cost cap solely to exercise preparation; **that field is not a cloud-compute spending cap**. They are not promoted run specs. No queue submission was made.

Durable machine-readable artifacts:

- [`OOD-SLICE.csv`](OOD-SLICE.csv): ordered 20-task manifest, exact original package/verifier digests, base image tags, observed immutable amd64 manifests, compressed layer totals, and resources/timeouts.
- [`OOD-QUALIFICATION.json`](OOD-QUALIFICATION.json): selection/source pins, source-check receipt, exclusions, price inputs and calculated scenarios. It contains no credentials, prompts, solutions, or executable approval.

## Exact candidate slice and selection rule

Start with the **500 task directory IDs** at the [pinned task snapshot][tasks]. Before looking at any MiMo outcome:

1. Read all **1,180** rows of the Python task ledger, including all **1,047 train rows**. Ledger SHA-256: `0d4ffc0fcd3cfa4ddf1b783287464c6e6d34bda17ded81836ecbbeef769ca13f`.
2. Conservatively exclude the complete SWE repositories `astropy/astropy`, `matplotlib/matplotlib`, `pydata/xarray`, and `scikit-learn/scikit-learn`, including the ledger alias `sklearn`. This removes 110 source tasks. Matches include train tasks `000306`, `000680`, `001946` (sklearn), `002135` (xarray), `002672` (astropy), and `002963` (matplotlib), regardless of whether a row was marked usable or review.
3. Exclude all seven [upstream-known oracle issues][adapter]: astropy `8872`, `7606`, `8707`; Django `10097`; scikit-learn `14710`; Sphinx `8595`, `9711`. Four already belong to excluded repositories, leaving **387** candidates.
4. Set salt to **`har134-sweverified-python-v1`**. Rank a string by the hexadecimal SHA-256 of its UTF-8 bytes after `salt + "\0"`. A repository key is the task ID with its final `-<issue-number>` removed.
5. Sort repository keys by that hash; sort instances within each repository by the same hash. Take one task per repository per round, at most three rounds, stopping at 20. Do not sort the selected list again.

| Order | Task ID | Repository |
|---:|---|---|
| 1 | `mwaskom__seaborn-3069` | mwaskom/seaborn |
| 2 | `django__django-14373` | django/django |
| 3 | `pytest-dev__pytest-6197` | pytest-dev/pytest |
| 4 | `pylint-dev__pylint-4604` | pylint-dev/pylint |
| 5 | `pallets__flask-5014` | pallets/flask |
| 6 | `sphinx-doc__sphinx-10466` | sphinx-doc/sphinx |
| 7 | `psf__requests-1766` | psf/requests |
| 8 | `sympy__sympy-13091` | sympy/sympy |
| 9 | `mwaskom__seaborn-3187` | mwaskom/seaborn |
| 10 | `django__django-15957` | django/django |
| 11 | `pytest-dev__pytest-5840` | pytest-dev/pytest |
| 12 | `pylint-dev__pylint-4661` | pylint-dev/pylint |
| 13 | `sphinx-doc__sphinx-7757` | sphinx-doc/sphinx |
| 14 | `psf__requests-6028` | psf/requests |
| 15 | `sympy__sympy-13615` | sympy/sympy |
| 16 | `django__django-16819` | django/django |
| 17 | `pytest-dev__pytest-7571` | pytest-dev/pytest |
| 18 | `pylint-dev__pylint-8898` | pylint-dev/pylint |
| 19 | `sphinx-doc__sphinx-7440` | sphinx-doc/sphinx |
| 20 | `psf__requests-2317` | psf/requests |

**Disjointness limit:** no exact declared-project match was found for these eight repositories in the train ledger. That is a screening result, not clearance. The overnight G1 audit already demonstrated that a project census label can name a dependency instead of the target repository. Before admission, compare the eventual G3 manifest's canonical repository identities, original dataset IDs, test paths, issue/text/code overlap, and any prior optimization/evaluation exposure. An unresolved identity is not evidence of non-overlap. If the screen changes, version the candidate and refreeze it before any outcome; do not substitute failures after evaluation begins.

## Runtime and verifier risks to close before admission

### Images, resources, and network

All 20 source Dockerfiles use x86_64 SWE-bench images tagged `:latest`, install `uv` from the versioned `0.7.13` installer URL, and work in `/testbed`. Registry metadata resolves their amd64 manifests now; compressed layer totals range from **0.945 to 1.206 GiB per image**. These totals include shared layers per image and are **not** unique download size, expanded filesystem size, or a proof of the 10-GiB storage ceiling. There are 20 task-specific images.

The original package digests in the CSV bind the source Dockerfiles, **not the future value of mutable image tags**. Before a run, pin the observed platform image digests (or an equivalently evidenced Daytona snapshot), preserve the original-to-effective mapping, and recompute package/spec digests if Dockerfiles change. Record build dependencies and installed versions; source pinning alone does not freeze network downloads.

Harbor resolves this source's environment network policy to **public**; agent and verifier have no explicit phase overrides. The source verifier installs package dependencies. Therefore do not promise offline operation or silently apply a network rule that makes the verifier fail. A later canary must prove the intended agent/host-proxy/verifier routing and isolation on the exact Daytona backend. Use the approved host-side inference proxy; do not expose the provider credential to the task container. The current overnight serving implementation is **SGLang**, despite older plan wording saying vLLM; compare arms on the same actual backend and configuration.

### Grader integrity and controls

The inspected Seaborn package activates its conda environment, installs the repository editable with development dependencies, restores test files, applies the withheld test patch, invokes pytest, and evaluates the SWE-bench result. Its parser environment requests `swebench==4.0.3`, `datasets==2.16.1`, and `fastcore<1.11`. This package-level evidence is more specific than the adapter README's general `swebench>=4.1.0` installation advice.

The score is executable-test based, not an LLM judge. That is useful, but not proof against agent interference with an editable repository, installation hooks, test tooling, or grade artifacts. Preserve hidden `tests/` and `solution/` outside the agent image, verify the grader runs in its intended context, retain clean provenance/trace review, and validate the Lab's countability verdict. Upstream oracle success does not certify this exact runtime.

With separately approved spend, require **oracle pass and nop fail on the admitted 20-task package set**, and a bounded exact-route canary before the model sweep. A canary alone does not clear all 20 verifiers. Save timeout/infra outcomes separately; do not replace them with favorable tasks or treat them as proof the model cannot code. No such controls were executed under HAR-134's $0 scope.

## Cost assessment, not a latency forecast

Prices read on 2026-10-01 from [Modal][modal-price] and [Daytona][daytona-price]:

- Modal **A100 80GB**: `$0.000694/s`. A serving container with 4 physical CPU cores (`$0.0000131/core/s`) and 16 GiB memory (`$0.00000222/GiB/s`) costs **$2.814912/hour**. This is the assumed serving shape, not a claim about a future reservation.
- Daytona 1 vCPU + 4 GiB + 10 GiB disk: `$0.0504 + 4×0.0162 + 5×0.000108` = **$0.11574/hour**, using the published 5-GiB free disk allowance. Account-specific credits, storage lifetimes, and billing details can differ.
- Illustrative one-session warm overhead: 208 seconds cold start plus 300 seconds tail, **$0.3972**. The 208 seconds came from the [HAR-90 serving smoke receipt](../../experiments/har90-modal-mimo/README.md), not an OOD measurement. Actual startup/shutdown is billable and must be observed.

Use actual server billable time and sandbox lifetimes:

```text
cost = 2.814912 × Modal_server_hours
     + sum(0.11574 × selected_sandbox_live_hours)
     + other billable build/storage/egress/retry costs
```

The table conservatively leaves the shared server up during setup and verification. “Effective concurrency 4” assumes that throughput really supports four task lifetimes in parallel; merely requesting concurrency 4 does not establish it. Shared-GPU contention can increase each task's latency. **No MiMo runtime was measured for these tasks.**

| Scenario: agent + setup + verifier | Effective concurrency | Amortized trial, excluding one warm overhead | 20 trials / one arm, including one warm overhead | 40 trials / paired arms, including one warm overhead |
|---|---:|---:|---:|---:|
| 600 + 300 + 300 s | 4 | $0.27 | $5.86 | $11.32 |
| 1200 + 600 + 600 s | 4 | $0.55 | $11.32 | $22.25 |
| Declared phase ceilings: 3000 + 1800 + 3000 s | 4 | $1.78 | $35.91 | $71.42 |
| 600 + 300 + 300 s | 1 | $0.98 | $19.93 | $39.47 |
| 1200 + 600 + 600 s | 1 | $1.95 | $39.47 | $78.55 |
| Declared phase ceilings: 3000 + 1800 + 3000 s | 1 | $6.35 | $127.39 | $254.39 |

These are **scenarios**, not observed means, confidence intervals, approved budgets, or hard account ceilings. Phase timeouts can overlap differently; upload, teardown, TTL, retries, and storage retention add cost. Verify the backend's actual lifecycle limit covers setup + agent + verifier rather than assuming an agent timeout is an end-to-end deadline. Oracle/nop qualification costs are additional and require Daytona even without a model call. A one-task smoke can cost much more than the four-way amortized figure.

**Budget recommendation:** do not authorize the full paired sweep from the optimistic $11–22 scenarios alone. First admit a bounded controls/route canary with independent provider-side spending/auto-stop limits, measure cold build, verifier, server and token throughput, then freeze the full-sweep cap and identical per-arm limits. If an allocation cannot cover the measured envelope, reduce the authorized design *before outcomes* or do not run. Tonight's shared allocation is not permission for this later study.

## Later execution contract

Admission should produce one receipt covering:

1. **Identity and holdout:** cleared canonical overlap against the final G3/GEPA exposure set; this versioned 20-ID list; no outcome-dependent substitution or training on these tasks.
2. **Task/runtime identity:** original and effective package/verifier/image digests; Harbor/runtime revision; exact `lf2` digest; network/grader isolation; all 20 control verdicts.
3. **Model identity:** same pinned MiMo base and tokenizer; fixed stock and LoRA adapter revisions; stock selector `selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` and tuned selector `selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:har129`; actually loaded adapter and same Modal/SGLang serving configuration. Selector admission in PR #604 is not a successful model smoke.
4. **Budget and design:** separately authorized canary and sweep limits, measured resource envelope, auto-stop, same per-arm harness/inference/timeout/concurrency settings, and counterbalanced arm order. Adding a GEPA arm is a new design/budget decision, not implicit in this two-arm recommendation.
5. **Analysis:** frozen countability/infra-exclusion rules, per-task paired outcomes and denominators, effect size and uncertainty, exact paired test, and arm-blind behavioral analysis. At 20 tasks, one task is five percentage points; report repository clustering and the narrow selected population. See [METHODS.md](METHODS.md) for the small-sample cautions; a later experiment needs its own preregistration, not post-hoc reuse of tonight's test family.

**Final verdict:** SWE-bench Verified is the best-supported later Python OOD candidate among the inspected options. Its source intake, resource parsing, Lab preparation, deterministic slice and OCI metadata are verified at $0. **Exact MiMo/Terminus-2/Daytona execution, canonical disjointness, grader controls, and budget admission remain deliberately unclaimed.**

## Primary sources

[adapter]: https://github.com/harbor-framework/harbor/blob/99218a4611e3bd76e49ce3b5f977e8c8134ad3ac/adapters/swebench/README.md
[parity]: https://github.com/harbor-framework/harbor/blob/99218a4611e3bd76e49ce3b5f977e8c8134ad3ac/adapters/swebench/parity_experiment.json
[metadata]: https://github.com/harbor-framework/harbor/blob/99218a4611e3bd76e49ce3b5f977e8c8134ad3ac/adapters/swebench/adapter_metadata.json
[tasks]: https://github.com/harbor-framework/harbor-datasets/tree/86723674f04e4209ac479d0fb75d9d9f44b4377e/datasets/swebench-verified
[modal-price]: https://modal.com/pricing
[daytona-price]: https://www.daytona.io/pricing

**Licensing boundary:** the Harbor adapter README's blanket “Apache 2.0 (benchmark and dataset)” statement conflicts with the [upstream SWE-bench repository's MIT license](https://github.com/SWE-bench/SWE-bench/blob/main/LICENSE). Distinguish Harbor packaging, the benchmark code/data terms, and each underlying repository's license. Preserve the applicable notices and verify dataset/redistribution terms before republishing tasks or traces; this report does not relicense the packages or grant benchmark-training permission.
