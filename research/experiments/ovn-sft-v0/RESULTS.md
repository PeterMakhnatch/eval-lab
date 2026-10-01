# ovn-sft-v0: results

**No detectable difference for either method at this size.** The five-trajectory,
ten-step SFT run tested the end-to-end process, not the general effectiveness
of SFT. The GEPA prompt arm likewise did not demonstrate an improvement.
Both preregistered exact McNemar tests have Holm-adjusted p=1; non-rejection
is not equivalence or proof of no effect.
Both blind G6 raters judged the tuned arm's single counted pass copied from
upstream, although the deterministic counter could not bind the unpack/read
evidence. That concern is explicit below; neither the original primary table
nor the separately labelled judgment sensitivity shows a detectable effect.

Date: 2026-10-01. Integrator: Cdx 3 / HAR-133 under the 17:16Z assignment.
[PREREG.md](PREREG.md) remains immutable. This report integrates the protected
[G5 run record](G5-RUN.md), GEPA gate and G6 review, and scoped spend receipts.
[VALIDITY.md](VALIDITY.md) gives the one-page scientific verdict.

## 1. Chronology, frozen digests and family admission

The final [HAR-126 disposition](https://linear.app/petermakhnatch/issue/HAR-126)
at **16:42:43Z** supersedes the 16:06:28Z replacement permission:
**60 original physical attempts; 55 counted outcomes; five infra-missing cells;
no replacements**. The original exclusions remain visible and are never zeros
in the primary analysis. The two-test family was frozen before G5: LoRA-SFT
minus stock and GEPA minus stock. Tuned versus GEPA is exploratory only.

**Neither primary test rejects. This does not establish equivalence or no effect.**
Only three discordant task pairs inform each exact test. The nonrandom,
light-image cohort, one stochastic run per cell, clustered missingness and
three serving sessions limit attribution and generalization.

PREREG merged 04:42:26Z (#598); executed eval v2 merged 05:22:12Z (#605),
before G2/eval outcomes. The five-trajectory export and ten-step adapter were
fixed before G5. GEPA was admitted 11:03:47Z, fixing the family at two before
the first G5 outcome. One planned serving session became three authorized
cold sessions without changing the treatment assets or per-task arm order.

| Segment | Runtime revision | Deploy / healthy | Physical cells | Stop |
|---|---|---|---:|---|
| g5r | 55366a45 | 12:57:07 / 13:01:08 | 13 | 13:27:09 |
| g5r2 | b1619aa3 | 14:07:16 / 14:11:05 | 27 (7 recovered tuned, then 20) | 15:26:12 |
| g5r3 | b1619aa3 | 15:32:08 / 15:35:26 | 20 | 16:36:45 |

An earlier URL-parse abort on 0eb5903d dispatched no physical trial.
[G5-RUN.md](G5-RUN.md), merged #673/0b862e07 at 17:24:55Z, retains the
app IDs where available, manifests, budget snapshots, 60-cell record and
incident chronology. Its merged bytes match reviewed snapshot 7dc5bd93.

| Frozen input | SHA-256 |
|---|---|
| PREREG | `225ee5e1ce054dc89661b625228ac13f333995c54e9624db6e1b5e060b630a72` |
| Executed eval CSV | `3b997fdcff048061fd8a05d446948d4d6425bf670eb0d6710989c50dcd0a9219` |
| G5 cohort | `ecfb2613ecfe0fdc941b28a07fdf67fc0ee8110bf05234c758b244330bce80d3` |
| 60-spec aggregate | `a8551c185172e7ad5b6c54acd50b4e41a8fd4a7d558cf6992e819b22c8838029` |
| lf2 harness | `f18091f344b075230bf99744fb92dd75c1e9ebe67f5cb15027a0d6ce791456be` |
| Final adapter | `e96209f2a15108e50076e24d7ecaeba43365af055e020f2ab526bb0b270c04a2` |
| GEPA addendum | `b55a90cdf5e07719150c5642043bebfa54ce7ff68f28aff2009d4f738e7f9470` |
| SFT conversations | `sha256:71a9f7073a0ce4e2a12865fc2a0a74881986ec30067a18bf7e78a528a3875c94` |
| SFT manifest | `sha256:567d64a4b78a3775dae89596787851e8e04578a111774932c6d3494af07a522b` |

Full identities, source receipts and verification limits are in
[har133_sources.json](har133_sources.json). These identify the executed v2
cohort, not the superseded v1 list.

## 2. Training, adapter and GEPA selection

Base: **XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B**, revision
`2367e865d009c13ac81713a2878291d33ab28177`. No model-family/domain change; no RL.

Five clean counted-pass training trajectories (000552, 000941, 002416, 002555,
002938) were cut at reviewed completion points and expanded into **145
per-turn examples**, not 145 independent trials. The export has 1,569,955
sequence tokens, of which 39,913 are supervised last-assistant target tokens.
All 145 API-prefix and loss-mask checks passed; no eval-task data was selected.
Source gate/trim details are in the audit receipts and [G3 data card](g3/data_card.md).

| Final fit setting | Value |
|---|---|
| Trainer | TRL SFTTrainer, token-mean assistant-target loss; checkpointed lm-head chunks 4096 |
| Epochs / optimizer steps | 1 / 10 |
| Micro-batch / accumulation | 1 / 16 |
| Seed | 42, applied before adapter initialization |
| Learning rate / schedule | 5e-5 / cosine; warmup ratio 0.03 |
| LoRA | Rank 16, alpha 32, dropout 0.05; q/k/v/o/gate/up/down projections |
| Dtype / gradient checkpointing | bfloat16 / enabled |
| Sequence length / packing | 65,536; over-length refusal, no truncation / disabled |
| Checkpoint selection | Final checkpoint, no eval-driven selection or sweep |

The pinned stack includes torch 2.14.0, transformers 5.12.1, TRL 1.14.0 and
PEFT 0.21.0. Producer receipt (#663/3844e46b) records 1,330.3 training seconds,
ten steps and nonzero LoRA-B max magnitude 2.5146e-4; mean logged loss
0.164033 is a training diagnostic, not held-out improvement. The adapter is
`har129-g4/adapter`, served as `:har129`. GPU execution and on-volume
adapter hashes remain producer-receipt evidence, not an independent download
or retrain. Earlier deleted dry-run/greedy-text claims are not used.

### GEPA prompt arm

The training-only gate began with the first 10 G2 attempt-1 seeds. Three
excluded tasks were removed symmetrically before candidate evaluation: 001647
infra, 000341 and 001870 copied/tainted under that frozen gate policy. The
retained **seven-task** seed scored 1 pass / 6 failures; C1 scored 3 / 4.
The successful-fetch amendment did not retrospectively reselect that gate.

One candidate was selected (no C2): the 1,564-byte b55a90cd addendum. Seed
measured tokens were 10,153,367 versus C1's 11,931,159, a 1,777,792-token
increase. This is a small selected prompt intervention, not a general claim
about prompt optimization. The [C1 handoff](gepa-har135/candidate-one-handoff.json)
and #664 receipt bind the candidate and 11:03 admission. G5 GEPA uses **stock
weights**, no adapter, plus only that addendum; section 5 gives its held-out test.

## 3. Actual serving, harness, prompt and execution parity

All arms used LoRA-capable SGLang app `evallab-mimo-v26-9b-lora` and the
same pinned base weights. Stock/GEPA request the base name; tuned uses
base plus `:har129`. The source specifies one A100-80GB, SGLang 0.5.20
and context 65,536. Base and tuned share the server command, with LoRA flags
and a read-only adapter volume added. Actual request/response names and
sampler were verified from captures; weight residency remains bounded by the
producer/runtime evidence, not merely an echoed name.

| Controlled setting | All arms |
|---|---|
| Harness | lf2, digest in section 1 |
| Sampling | Temperature 0.6, top-p 0.95, top-k 20, n=1, stream=false |
| Thinking | chat_template_kwargs.enable_thinking=true |
| Per-call output | 4,096 tokens |
| Per-trial ceilings | 120 requests; 2,500,000 input and 131,072 output tokens; 3,600s timeout |
| Tool output / loop break | 2,000 characters; command-run 8, message-run 20, grace 15 calls |
| Sandbox | BoundedDaytonaEnvironment, TTL 70min, auto-stop 5min, auto-delete 0 |
| Submission concurrency | 20, serial position waves; native per-task order checked |

GEPA's exact addendum appears once in the first task prompt on GEPA routes
only, not as the entire first message. Parent verified all 20 final-wave
routes and 1,709 HTTP200 records, every sampler/model binding and per-route
settled usage; prior 13+27 proofs are retained. Every task's native intervals
are sequential in its frozen arm order. Final-wave native trial starts span
15:36:04.892178–15:36:05.630134Z, observed peak 20. Spec-plus-lease file
counts are not concurrency; GPU utilization was not measured.
The successive native-trial concurrency peaks were 13 (initial stock/GEPA),
7 (recovered tuned), 20 (position 2), and 20 (position 3). Only the configured
parallelism was common; realized contention was not matched throughout.

Identical repository revision and one uninterrupted session are **not**
claimed: repairs changed execution heads and three cold sessions ran.
Effective treatment/harness/limits/sampling parity was separately audited;
session and shared-server effects remain caveats.

## 4. All tasks, physical identities, counts and exclusions

Here 1 is counted pass, 0 counted fail, and missing is excluded infra.
Each of the 20 recorded repository/project keys occurs once. This does not
prove independence from shared serving failures or establish every image's
Git remote identity.

| Arm | Planned | Raw passes | Counted passes | Counted failures | Infra-missing | Counted coverage |
|---|---:|---:|---:|---:|---:|---:|
| Stock | 20 | 2 | 2 | 15 | 3 | 17/20 |
| LoRA-SFT | 20 | 1 | 1 | 18 | 1 | 19/20 |
| GEPA | 20 | 3 | 3 | 16 | 1 | 19/20 |

These marginal denominators are **not** the denominators used to compare arms.
All trial IDs, physical spec IDs, raw rewards, counts reasons and source hashes
are in the input CSV.

| Task suffix | Recorded repository/project | Stock | LoRA-SFT | GEPA |
|---|---|---:|---:|---:|
| 002302 | powerline | 0 | 0 | 0 |
| 001695 | github.com/kinnala/scikit-fem | 1 | 0 | 0 |
| 000521 | pyabm | 0 | 0 | 0 |
| 001797 | github.com/linode/linode-cli | 0 | 0 | 0 |
| 001809 | localstack | 1 | 0 | 1 |
| 001769 | lenstronomy | 0 | 0 | 0 |
| 001285 | pyftpdlib | 0 | 0 | 0 |
| 001765 | lbry | missing | 0 | 0 |
| 000568 | youtube_dlc | 0 | 0 | 0 |
| 001241 | pyalgotrade | missing | 0 | 0 |
| 002578 | maestral | 0 | 0 | 0 |
| 001606 | deepdow | 0 | 0 | 0 |
| 002017 | github.com/napari/napari | 0 | 0 | 0 |
| 001626 | dulwich | missing | 0 | 0 |
| 001355 | magika | 0 | 0 | 0 |
| 000332 | peekingduck | 0 | missing | 1 |
| 000842 | libsaas | 0 | 0 | 0 |
| 000169 | hpccm | 0 | 1 | 1 |
| 001136 | piccolo | 0 | 0 | 0 |
| 001833 | safedelete | 0 | 0 | missing |

### Every physical trial

Each cell is **trial name (raw reward / counted verdict)**. Full native UUIDs,
physical spec IDs, exclusion reasons and hashes are in the linked CSV. No
retry row or replacement identity is present.

| Task | Stock | LoRA-SFT | GEPA |
|---|---|---|---|
| 002302 | `ovn-g5-002302-stock__83xFDnj` (0 / fail) | `ovn-g5-002302-tuned__5j77iHJ` (0 / fail) | `ovn-g5-002302-gepa__sFGLATE` (0 / fail) |
| 001695 | `ovn-g5-001695-stock__PveqguL` (1 / pass) | `ovn-g5-001695-tuned__HSdBDkF` (0 / fail) | `ovn-g5-001695-gepa__q4rVPoo` (0 / fail) |
| 000521 | `ovn-g5-000521-stock__pjKC2mS` (0 / fail) | `ovn-g5-000521-tuned__TdWLAib` (0 / fail) | `ovn-g5-000521-gepa__2rqo6EY` (0 / fail) |
| 001797 | `ovn-g5-001797-stock__yGw88CW` (0 / fail) | `ovn-g5-001797-tuned__L32LiEr` (0 / fail) | `ovn-g5-001797-gepa__W7cKEVT` (0 / fail) |
| 001809 | `ovn-g5-001809-stock__PZ8sw7f` (1 / pass) | `ovn-g5-001809-tuned__AUVzrvM` (0 / fail) | `ovn-g5-001809-gepa__nxdBvhh` (1 / pass) |
| 001769 | `ovn-g5-001769-stock__qSPHRnf` (0 / fail) | `ovn-g5-001769-tuned__pHJnuZq` (0 / fail) | `ovn-g5-001769-gepa__XKhJTpk` (0 / fail) |
| 001285 | `ovn-g5-001285-stock__JvtxRFm` (0 / fail) | `ovn-g5-001285-tuned__WgPbz8K` (0 / fail) | `ovn-g5-001285-gepa__waX6njT` (0 / fail) |
| 001765 | `ovn-g5-001765-stock__Tigdd3B` (null / excluded:infra) | `ovn-g5-001765-tuned__rrXKUgY` (0 / fail) | `ovn-g5-001765-gepa__yHdbtvs` (0 / fail) |
| 000568 | `ovn-g5-000568-stock__Ri7YYN5` (0 / fail) | `ovn-g5-000568-tuned__Zcbk6Ub` (0 / fail) | `ovn-g5-000568-gepa__XXS7kMp` (0 / fail) |
| 001241 | `ovn-g5-001241-stock__FGtdyNH` (null / excluded:infra) | `ovn-g5-001241-tuned__xKbAg3N` (0 / fail) | `ovn-g5-001241-gepa__6LNRk7d` (0 / fail) |
| 002578 | `ovn-g5-002578-stock__ZVWT2gB` (0 / fail) | `ovn-g5-002578-tuned__Zyd7L6j` (0 / fail) | `ovn-g5-002578-gepa__kzhAXLU` (0 / fail) |
| 001606 | `ovn-g5-001606-stock__jePg8nP` (0 / fail) | `ovn-g5-001606-tuned__MnCArfo` (0 / fail) | `ovn-g5-001606-gepa__ySwezhm` (0 / fail) |
| 002017 | `ovn-g5-002017-stock__be4xo9x` (0 / fail) | `ovn-g5-002017-tuned__SWc4xbr` (0 / fail) | `ovn-g5-002017-gepa__qQJWeCc` (0 / fail) |
| 001626 | `ovn-g5-001626-stock__6P6YXKD` (null / excluded:infra) | `ovn-g5-001626-tuned__fSkoQL7` (0 / fail) | `ovn-g5-001626-gepa__3kGXxof` (0 / fail) |
| 001355 | `ovn-g5-001355-stock__ZHNXfvD` (0 / fail) | `ovn-g5-001355-tuned__8UJMiQN` (0 / fail) | `ovn-g5-001355-gepa__bJkSJkD` (0 / fail) |
| 000332 | `ovn-g5-000332-stock__Xn3uN4Q` (0 / fail) | `ovn-g5-000332-tuned__zevQhXu` (null / excluded:infra) | `ovn-g5-000332-gepa__YyfJfPU` (1 / pass) |
| 000842 | `ovn-g5-000842-stock__RUUQHiz` (0 / fail) | `ovn-g5-000842-tuned__GjjMiqx` (0 / fail) | `ovn-g5-000842-gepa__7p54K7C` (0 / fail) |
| 000169 | `ovn-g5-000169-stock__npH4TqV` (0 / fail) | `ovn-g5-000169-tuned__CxBpceW` (1 / pass) | `ovn-g5-000169-gepa__Hmxf7wb` (1 / pass) |
| 001136 | `ovn-g5-001136-stock__jjgSG57` (0 / fail) | `ovn-g5-001136-tuned__QTMzsEL` (0 / fail) | `ovn-g5-001136-gepa__rexYV6K` (0 / fail) |
| 001833 | `ovn-g5-001833-stock__UTVAfnN` (0 / fail) | `ovn-g5-001833-tuned__rNpMkcb` (0 / fail) | `ovn-g5-001833-gepa__aci6NkF` (null / excluded:infra) |

### Discordant-pair evidence

Local report links below retain the original trial IDs and source metadata;
the CSV also records their hashes. A pass is the counted verifier outcome,
not an inferred completion claim.

| Contrast | Task | Intervention | Stock | Published reports |
|---|---|---:|---:|---|
| gepa_minus_stock | 001695 | 0 | 1 | [gepa](file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-001695-gepa/processed/trial-ovn-g5-001695-gepa__q4rVPoo.json) / [stock](file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-001695-stock/processed/trial-ovn-g5-001695-stock__PveqguL.json) |
| gepa_minus_stock | 000332 | 1 | 0 | [gepa](file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-000332-gepa/processed/trial-ovn-g5-000332-gepa__YyfJfPU.json) / [stock](file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-000332-stock/processed/trial-ovn-g5-000332-stock__Xn3uN4Q.json) |
| gepa_minus_stock | 000169 | 1 | 0 | [gepa](file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-000169-gepa/processed/trial-ovn-g5-000169-gepa__Hmxf7wb.json) / [stock](file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-000169-stock/processed/trial-ovn-g5-000169-stock__npH4TqV.json) |
| tuned_minus_stock | 001695 | 0 | 1 | [tuned](file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-001695-tuned/processed/trial-ovn-g5-001695-tuned__HSdBDkF.json) / [stock](file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-001695-stock/processed/trial-ovn-g5-001695-stock__PveqguL.json) |
| tuned_minus_stock | 001809 | 0 | 1 | [tuned](file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-001809-tuned/processed/trial-ovn-g5-001809-tuned__AUVzrvM.json) / [stock](file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-001809-stock/processed/trial-ovn-g5-001809-stock__PZ8sw7f.json) |
| tuned_minus_stock | 000169 | 1 | 0 | [tuned](file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-000169-tuned/processed/trial-ovn-g5-000169-tuned__CxBpceW.json) / [stock](file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-000169-stock/processed/trial-ovn-g5-000169-stock__npH4TqV.json) |

## 5. Preregistered paired inference and sensitivity

Difference is **intervention minus stock**. Each contrast uses its own
complete pairs, not unpaired marginal denominators or a common-three-arm subset.

| Contrast | Complete pairs | Intervention passes | Stock passes | Difference | Approximate marginal 95% paired-score interval | Raw exact p | Holm p |
|---|---:|---:|---:|---:|---:|---:|---:|
| LoRA-SFT minus stock | 16/20 | 1/16 (6.25%) | 2/16 (12.50%) | -6.25 pp | [-30.81, +18.39] pp | 1.000 | 1.000 |
| GEPA minus stock | 16/20 | 3/16 (18.75%) | 2/16 (12.50%) | +6.25 pp | [-16.73, +29.43] pp | 1.000 | 1.000 |

| Contrast | Both pass (a) | Intervention only (b) | Stock only (c) | Neither (d) |
|---|---:|---:|---:|---:|
| LoRA-SFT / stock | 0 | 1 | 2 | 13 |
| GEPA / stock | 1 | 2 | 1 | 12 |

Exact two-sided McNemar uses only b+c. Both contrasts have b+c=3, so even
three discordances in one direction could only attain p=0.25. The intervals
above are **approximate marginal descriptive intervals**, not an exact inversion
of McNemar and not simultaneous family-wise intervals. Holm at family alpha
0.05 decides rejection; both decisions are **do not reject**.

Omitted pairs:

- LoRA-SFT / stock: **000332, 001241, 001626, 001765**.
- GEPA / stock: **001833, 001241, 001626, 001765**.

### Missing-outcome sensitivity

The preregistered **sharp bounds over the planned 20 tasks** retain every known
outcome and let each missing cell independently range over 0/1:

| Contrast | Minimum difference | Maximum difference |
|---|---:|---:|
| LoRA-SFT minus stock | -20 pp | 0 pp |
| GEPA minus stock | -10 pp | +10 pp |

For LoRA, the known net difference is -1; its missing tuned cell can contribute
0 or +1 and the three missing stock cells together 0 through -3:
`(-1 + [0,1] - [0,3]) / 20`. For GEPA, replace the known -1 with +1.
These are finite-cohort missing-data bounds, **not confidence intervals**.
They answer a different question from the paired-score intervals.

The final ruling additionally requested these hypothetical scenarios:

| Assumption | N per contrast | LoRA difference | GEPA difference | LoRA raw / Holm p | GEPA raw / Holm p |
|---|---:|---:|---:|---:|---:|
| All five missing cells fail | 20 | -5 pp | +5 pp | 1 / 1 | 1 / 1 |
| All five missing cells pass | 20 | -15 pp | -5 pp | 0.453125 / 0.90625 | 1 / 1 |
| Only the three missing stock cells fail; other two stay missing | 19 | -5.2632 pp | +5.2632 pp | 1 / 1 | 1 / 1 |

**Uniform all-fail/all-pass scenarios do not attain the sharp bounds.** Both
arms have missing outcomes, so mixed assignments can be more extreme.
Across all 32 joint completions, neither contrast rejects: the smallest
hypothetical raw / Holm p-values are 0.21875 / 0.4375 for LoRA and
0.625 / 0.6875 for GEPA (these minima need not occur in the same assignment).
These calculations are descriptive sensitivity checks, not extra confirmatory
tests, replacement trials, or edits to primary counts.

Exploratory only: tuned minus GEPA has 18 complete pairs, a/b/c/d=1/0/1/16,
difference -5.56 pp, raw exact p=1. The common three-arm complete subset has
**15**, not 18, tasks and was not substituted for either primary comparison.

### Reproduce the analysis

From a synced Eval Lab checkout:

```bash
uv run python research/experiments/ovn-sft-v0/har133_analysis.py
```

Inputs: [har133_outcomes.csv](har133_outcomes.csv). Output:
[har133_analysis.json](har133_analysis.json). The script calls the existing
`evallab.analysis_statistics.exact_paired_binary_contrast`, then applies
Holm to the fixed family of two. It enumerates all 32 joint binary completions
of the five missing cells, sharing each missing stock value across contrasts.
An independent rational-arithmetic calculation reproduced the exact p-values;
an additive per-task calculation reproduced the sharp bounds without using
that enumeration.

- Input CSV SHA-256: `ea23e08a004c1050f3e39bf4e7beadb3bff2cae8342443f97eee001f10702af3`.
- Executed eval list: `3b997fdc…`; cohort: `ecfb2613…`; frozen specs: `a8551c18…`.
- All 60 published report hashes, counts and raw rewards matched the frozen
  steward receipt `666556cb…`; all 60 native result hashes also matched.
- Full digests, source paths, configuration and parent verification receipts:
  [har133_sources.json](har133_sources.json). CSV report paths are relative to
  `~/Developer/eval-lab-results/2026-10-01/`.

The older v1 eval list `504b913a…` is **not** the executed cohort. It was
superseded before G2; see the chronology below.

## 6. Descriptive secondaries and frozen G6 review

[har133_secondary.json](har133_secondary.json) includes each contrast's exact
16 tasks, per-arm totals/median/Q1/Q3 and paired per-task differences for input
tokens, output tokens and physical model requests. Q1/Q3 are medians of the
lower/upper eight observations. Parent recalculation from all 60 settled ledgers
matched both tables. The two excluded HTTP503 routes are absent from these
paired tables, so their successful-response usage coverage is complete.

| Complete-pair contrast | Quantity | Intervention total | Stock total | Total paired difference |
|---|---|---:|---:|---:|
| LoRA / stock | Input tokens | 34,611,239 | 33,847,421 | +763,818 |
| LoRA / stock | Output tokens | 237,481 | 226,377 | +11,104 |
| LoRA / stock | Physical requests | 1,439 | 1,402 | +37 |
| GEPA / stock | Input tokens | 35,298,063 | 33,870,179 | +1,427,884 |
| GEPA / stock | Output tokens | 193,855 | 239,268 | -45,413 |
| GEPA / stock | Physical requests | 1,445 | 1,392 | +53 |

Sources are **settled proxy usage and raw HTTP capture**, never ATIF step sums
or Harbor agent-summary totals, which omit some length-capped responses.
The three sealed captures contain 1,194 + 2,442 + 1,709 = **5,345 HTTP attempts**:
5,291 HTTP200 and 54 HTTP503. Request-plus-response model-name observations
are not calls. Provider usage on those 54 HTTP503 attempts is **unknown**;
ledger zero is recorded accounting, not proof of zero compute or zero cost.

| Stop reason, denominator 20 planned per arm | Stock | LoRA-SFT | GEPA |
|---|---:|---:|---:|
| Input-token ceiling | 13 | 12 | 15 |
| lf2 loop break | 4 | 4 | 3 |
| Completion confirmed by harness | 0 | 3 | 1 |
| Unknown / infra | 3 | 1 | 1 |

Stop reasons do not decide verifier success: for example, 000169-GEPA passed
while stopped at its input-token ceiling. Display-only identical-loop labels
occur on 20/20 stock, 20/20 tuned and 19/20 GEPA records; they are **not validated
G6 behavioural incidence estimates**. G6's frozen blind review is integrated below; its judgments do not change counts. Fewer output tokens or
a shorter failed run is not by itself an improvement.

### Frozen G6 blind behavioural review

Sources: [G6_RESULTS.md](../../explorations/trace-lab/har128/g6/G6_RESULTS.md),
[G6_TABLES.md](../../explorations/trace-lab/har128/g6/G6_TABLES.md) and the
G6 freeze/score artifacts. Two groups of **native agent raters, not humans**,
reviewed all 60 arm-stripped packs. Commit b957af3b at 17:18:46Z contains
120 labels and blind metrics; d92cc9c4 at 17:21:06Z adds the map and joined
reports, without modifying the frozen labels/metrics. FROZEN_AT 17:18:24Z
is an operator assertion; the Git ordering is independently observable.
All 121 manifest entries and the 60-cell map join reproduced. Map 5fcfcc8e
matches the sealed copy. Rater compliance with off-limits files is self-reported,
not cryptographic proof of blinding.

Job/trial/model identifiers, the GEPA addendum, timestamps and agent metadata
were removed. First-prompt task bodies match after accounting for dynamic
sandbox hostname UUIDs; literal full-prompt byte equality is not claimed.
Task and behavioural content can still suggest an arm.

Supported descriptive results, without new significance tests:

- Agreed repetition-loop labels: stock 9, tuned 6, GEPA 8; completion-claim
  labels 3, 3, 4. Scored denominators are 17, 19, 19; disagreements remain
  visible. These observations do not establish that either method reduces loops.
- Loop-break nudges fired on 33/55 scored cells, but only 11 stopped with the
  canonical loop-break reason. Detection, termination, completion handshake and
  verifier reward are distinct. All four confirmed handshakes (three tuned,
  one GEPA) occur on verifier failures.
- Raters agree on stop 60/60, first failure within two steps 42/60, blame
  59/60, and loop kind 51/60. Against agreed labels, Eval Lab stop is 59/60
  and first failure 9/42; Scout stop 59/60, first failure 2/42, blame 53/59.
  These are agreement measurements with agent labels, not human ground truth.
- The copied-pass field agrees between raters on 6/6 counted-pass cells. Only
  000169-tuned is rater-positive. Eval Lab's 5/6 agreement is five negatives
  and that one disagreement, not five detected copied fixes.

**Metric limitations found in integration:** the initial G6 claim that
unparseable was zero throughout is contradicted by 8/55 scored cells (stock 3,
tuned 3, GEPA 2) with nonzero values, including 62 and 90. Median/IQR zero
does not mean all zero. The first-edit detector also labels g6-33 step 7 an
edit, although it is a read-only awk comparison containing `>`; the first
recorded real repo-write command is step 52. The same detector feeds
never-edited and post-edit-token metrics. Their values are not validated
editing measurements, so we do not claim that GEPA edits later or spends less
after its last edit. Time to first real edit is **unavailable, not zero**.
HAR-128 withdrew these claims and corrected the parse-error and prompt-parity
wording in protected **#676/da500218**, merged 17:53:56Z, without changing
the frozen labels or metrics. We integrate that corrected interpretation.

**Copied-pass distinction:** both G6 raters judge 000169-tuned copied. Native
step 21 lists the requested wheel after pip. In the **capped ATIF view**,
step 22's middle omission hides the unzip command echo; the frozen consumer
returns acquisition unknown. That explains the published counted pass,
but is not proof of an independent or uncopied solution.

**Late retained-source correction (18:18Z):** the omission marker points to
`agent/evallab-output/step-0021.txt` (3,691 bytes, SHA-256 `3fd855a9…`).
Its full lines 28–41 contain the exact unzip echo, `&&`-gated upstream
`recipe.py` grep output and extracted-file listing. The earlier absence-of-
bound-evidence assessment was therefore too broad: the evidence exists in
the retained native bundle, although the original capped-view consumer did
not use it. Supplying the exact retained text in memory restores 11 bound
lines, but the frozen extractor still returns unknown: its source-content
recognizer does not accept grep's path/line prefixes, and the trailing `ls`
is semicolon-joined rather than `&&`-gated. These are observed coverage
limits, not a reason to dismiss the full extraction/read evidence. We preserve
the historical counter result and this stronger source evidence separately;
no live verdict or primary input is hand-edited.

Research-Harbor's **17:48:36Z final disposition** accepts this canonical primary
designation and supersedes the 17:32Z proposed corrected-primary interpretation.
The requested **secondary, judgment-based sensitivity** excludes 000169-tuned
rather than counting it as a failure:

| Judgment-based assumption | Complete pairs | Tuned-only / stock-only | Difference | Raw exact p | Holm p |
|---|---:|---:|---:|---:|---:|
| Exclude the pass judged copied by both raters | 15/20 | 0 / 2 | -13.33 pp | 0.5 | 1.0 |

GEPA versus stock remains N=16, raw/Holm p=1. The analysis script reproduces
this scenario without changing the CSV or primary counts. Separately, the
raters' descriptive “genuine pass” view is stock 2/17, tuned 0/19 and GEPA
3/19; that denominator treats the judged copied pass as nongenuine, **not**
as the PREREG exclusion unit, so it is not the paired test above.

Any forward-looking HAR-131 matcher expansion and its symmetric G5 replay
are sensitivity evidence only, not an amendment to this primary result or
a blocker for RESULTS. No unobserved replay outcome is asserted here.


## 7. Reconciled spend and estimation limits

At the **16:37:18 stored Modal billing snapshot**, read locally at 17:11Z:

- LoRA-app total $8.79548180 minus pre-G5 $0.83892826 = **$7.95655354 billed delta**.
- All 60 native trial lifetimes total 69,301.478367 seconds. Daytona at
  $0.23094/hour gives **$4.44568984 estimated**, including all five infra attempts.
- G5 accounting total: **$12.40224338 (about $12.40) against the $16 cap**.
  Engineering's headline was about $12.43. The roughly 3-cent Modal difference
  is unresolved; provider revision is a hypothesis, not an observed cause.
- Calendar-day canonical accounting: **$29.15707294**. The independently retained
  pre-04 component is $5.00611592, leaving **$24.15095702 after 04:00**, before
  direct model calls. The calendar override was $35, not the overnight cap.
- Traces disclosed direct Z.ai spend of $2.31 whole-day, about $1.73 after 04:25,
  outside the proxy. Its 17:32 receipt also discloses a **$0.31 overrun of the
  $2 subcap for #610**; this is distinct from the overall overnight budget.
  No invoice/per-call timestamp receipt was available. Charging the entire
  disclosed $2.31 as a conservative overnight allowance yields **$26.46095702
  against $30**. This is recorded-bill-plus-estimate accounting, **not an
  unconditional upper bound or a fully settled invoice**. Billing lag and the
  exact direct-call split remain limits.

All three local round manifests target the LoRA app and record it stopped.
Engineering additionally reported zero running Modal apps; this audit made no
remote status calls. Generic per-job sidecars naming the base app are not used
as LoRA teardown evidence. HAR-133 performed **$0 new experimental execution**.

The coordinator's canonical overnight ledger at **16:43Z** records **$24.18**
(Modal $17.29 + Daytona $6.89), explicitly a Modal-plus-Daytona scope. It is
a different rounded snapshot from the local 16:37:18 reconstruction above.
It also excludes disclosed direct Z.ai. Adding the conservative whole-day
$2.31 allowance gives **about $26.49**, not $24.18 all-inclusive. Neither
view is an unconditional upper bound on delayed bills.


## 8. Dated deviations, limitations and interpretation

All times UTC on 2026-10-01. Sources and the full dated audit snapshot are bound
in [har133_sources.json](har133_sources.json); owner decisions are on
[HAR-126](https://linear.app/petermakhnatch/issue/HAR-126),
[HAR-127](https://linear.app/petermakhnatch/issue/HAR-127),
[HAR-129](https://linear.app/petermakhnatch/issue/HAR-129) and
[HAR-135](https://linear.app/petermakhnatch/issue/HAR-135).

| Time | Event / deviation | Disposition and interpretive effect |
|---|---|---|
| 04:08; first G1 commit 04:09:33 | Dry-run SFT sample selection preceded the first committed eval freeze. | The literal committed-before-any-selection requirement was not met. Dry-run samples were outside both eval cohorts and the dry-run adapter was deleted. Accepted with this chronology caveat, not silently called compliant. |
| 04:38:28 working freeze; 05:22:12 merge | Initial cohort contained pandas code mislabeled fastparquet (002209); v2 replaced it with safedelete (001833). | Refreeze occurred before G2 and any eval outcomes. Parent reproduced the v2 bytes, split/digest checks, module-based disjointness and instruction scan. The contaminated v1 was never the executed G5 cohort. Some package identities remain module-derived, not measured image Git remotes. |
| 04:42:26 | PREREG merged (#598). | Fixed paired analysis and conditional two-contrast Holm rule before G2 data. |
| 09:45:34 | Counts amendment after G2: command-only failed-fetch findings no longer exclude raw passes without deterministic successful-fetch evidence. | Protected implementation and uniform reprocessing before G5. Raw failures remain counted failures regardless of fetch findings. Frozen GEPA selection exclusions were not silently changed. |
| Before final G4 fit; bound at 10:54:15 and 11:53:20 | G3 admitted five clean counted-pass training trajectories, expanded into 145 per-turn rows; final fit was ten optimizer steps. | Exact teacher/API prefix and target-only mask checks passed for all 145 rows; 39,913 supervised tokens. Context 65,536, over-length refusal, seed42, rank16/alpha32, last checkpoint. These are five trajectories, not 145 independent trials. Remote execution and adapter-on-volume identity remain producer-receipt evidence, not an independent retrain/download. |
| 11:03:47, before first G5 trial | GEPA candidate b55a90cd was admitted from its training-only gate. | Family fixed at two; same stock draw shared across contrasts. No held-out-driven prompt or checkpoint selection in the audited evidence. |
| First launch; 13:01:35 to 14:36:30 recovery | Initial pretrial launch failure and seven tuned dispatch refusals exposed missing adapter-profile admission. | Seven refused dispatches were not physical trials. Retained 13 physical stock/GEPA attempts; ran the seven missing tuned cells before their next-position arms. Exact input/spec treatment assets and per-task order held. Runtime source changed for the repair, so identical repository revision is not claimed. |
| g5r, g5r2, g5r3 | Three cold serving sessions replaced the preregistered single-session ideal. | Explicit authorized deviation. Frozen model/adapter/addendum/harness/limits/sampling held, and per-task native intervals are sequential, but session/segment effects and shared failures constrain a weights-only causal interpretation. Final-wave native peak was20, not inflated spec-plus-lease counts; GPU utilization was not measured. |
| 15:26 stop; 15:27:47 amendment | The old $12 guard refused a $12.1168 next-wave projection; coordinator raised G5 to $16 (app-day $11.50) after 40 physical attempts. | Prospective stopping amendment for the remaining original20, not retroactive compliance with12. No task, treatment, per-trial limit, family or counts change. |
| 15:25-15:26; 15:50:56-15:51:27 | Two HTTP503 exclusions and three last-position stock sandbox losses. | Five missing outcomes, never primary zeros. The stock losses cluster by arm/position. BoundedDaytona configured auto-stop5min/delete0/TTL70; long model calls were255-281s, but subsequent recorded tool observations and missing provider activity logs prevent causal identification. Actual idle-stop or a platform incident is **unknown**. |
| 16:06:28 permission; 16:42:43 rescission | Three once-only stock replacements were briefly authorized by a benign-command/cluster proxy, then explicitly canceled. | No replacement ran. Possible behaviour-linked loss made that proxy unsafe. Primary analysis remains the original60; requested failure assumptions are labeled post hoc sensitivity only. |
| 16:36:29 final native finish; 16:38 report | Last original cell was001695-stock. Gate exits skipped automated final link/freeze/reconcile in two segments; Engineering completed those operations manually. | Actual sealed captures, links and reconciled records are verified evidence. Automation defect is owner-assigned; manual invocation does not change outcomes or add a new scientific acceptance gate. |
| 16:34 correction; 17:11 local reconstruction | CLI proxy spend omitted disclosed direct Z.ai calls; calendar-day, overnight and G5 cost lenses differed. | Earlier $28.0759 launch figure was a GPU+Daytona+candidate subtotal, not an all-inclusive overnight bound. Use the component accounting below; do not call invoice revisions or idle-stop causality proven. |
| 17:18 freeze; 17:48:36 final ruling | Both blind G6 raters judged 000169-tuned copied; the existing deterministic consumer could not bind the unpack/read evidence. | Canonical primary counts remain fixed. The 17:48 ruling supersedes the proposed 17:32 corrected-primary framing. Excluding the cell under the rater judgment is separately reported at N=15, raw p=0.5; no hand override or rule change. |
| 17:37 findings; 17:53:56 merge | G6 first-edit detection mistook an awk comparison for a write; zero medians were misreported as all-zero parse errors. | Protected #676 withdraws edit-timing, never-edited and post-edit-token claims, corrects eight nonzero parse-error cells and qualifies hostname-normalized prompt parity. Labels/metrics stay frozen; no post-freeze replacement metric is promoted. |
| 18:18 retained-source correction | The native capped-output sidecar contains the unzip echo and gated upstream source-read evidence omitted from ATIF's middle. | Earlier “no bound native evidence” language is superseded. The frozen consumer still returns unknown after exact in-memory recovery because of remaining output-format coverage limits. Full-source evidence strengthens the copied-pass sensitivity; canonical primary remains fixed under the 17:48 ruling, and future symmetric replay is not silently promoted. |

### Interpretation boundary

The loop ran and is reproducible; neither intervention demonstrated improved
counted pass rate in the preregistered tests. Do not turn a nonsignificant pilot
into equivalence, declare GEPA better from its positive point estimate, infer
LoRA's general capability from five training trajectories, or attribute unknown
sandbox losses to a proven platform mechanism. Missingness bounds concern these
20 tasks; the approximate intervals retain their explicit assumptions and do
not license population-wide claims. Engineering's run record and the GEPA/G6 reports retain their own source
provenance; this RESULTS document owns the integrated interpretation.
