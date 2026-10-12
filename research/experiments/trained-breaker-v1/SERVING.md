# Serving trained-breaker-v1 (baseline vs L1) — prepared remote recipe

Status: **locally checked, NOT GPU-qualified.** No GPU run, deployment, weight
download or paid inference was performed. Remote steps below await a new budget
approval; the local Modal SDK definition and native transport were exercised.
Primary-source facts are marked **[verified]**; capacity/throughput estimates are
**[assumption]** or **[unmeasured]**. Reading a model card is not a runtime validation.

## 1. What the two arms are [verified]

| | baseline | l1 |
|---|---|---|
| HF repo | `Qwen/Qwen3.6-35B-A3B` | `rewardhack/qwen3.6-35b-a3b-hacksft-thinkoff-873rows-ep3` |
| Pinned revision (immutable commit; verified against HF API 2026-10-12 — use verbatim, never re-resolve main) | `995ad96e…31597b0` | `56955cb7…bd825` |
| Artifact | full BF16 weights, 26 shards | **full merged** BF16 weights, 26 shards |
| Params / stored bytes | 35,951,822,704 BF16; 71,921,347,477 B (~67.0 GiB) | 35,951,822,704 BF16; 71,916,583,934 B (~67.0 GiB) |
| Arch | `Qwen3_5MoeForConditionalGeneration` | same (card: "loads with transformers or vLLM like the base model") |
| License | Apache-2.0 | CC-BY-SA-4.0 (SETA-derived task bodies) |
| served_model_name | = HF repo ID (parent contract) | = HF repo ID (parent contract) |

- **L1 is full merged weights [verified].** The main-branch file list
  (`https://huggingface.co/api/models/rewardhack/qwen3.6-35b-a3b-hacksft-thinkoff-873rows-ep3`)
  contains `model-00001-of-00026` … `model-00026-of-00026.safetensors` plus
  the index, and **no** `adapter_config.json` / `adapter_model.*`. The card
  states: "Full merged weights (bf16 safetensors …) of a LoRA fine-tune
  (r=32, alpha=32, all-linear)", merged with
  `tinker_cookbook.weights.build_hf_model` (scale alpha/r = 1).
- **No adapter of either kind is published [verified].** Not standard PEFT
  (no adapter files in the repo) and not Tinker-native (no `tinker://`
  sampler URI on the card). The LoRA lived inside Tinker during training
  only. `merge_check.json` shows the released weights reproduce the
  training run's own Tinker sampler within implementation noise
  (mean |Δlogprob| 0.114/0.091, delta-over-base correlation 0.969/0.970).
- **Matched rendering:** the L1 card requires thinking OFF
  (closed `<think></think>`, `enable_thinking=false`), a 65,536-token window,
  a 16,384-token reply cap and Terminus-2. Our prepared comparison additionally
  pins both arms to temperature 1.0, top_p 1.0 and top_k -1. The native secret
  proxy enforces those values; this is our matched protocol, not a reproduced score.

## 2. Primary path: Modal + vLLM, one app per arm

**Decision:** two matched single-GPU vLLM servers (`modal_serve.py`),
not SGLang, not Tinker.

- vLLM is listed by the model card and has a published
  Qwen3.6-35B-A3B recipe requiring **vLLM ≥ 0.17.0**, with BF16 hardware
  guidance of **1× H200** (or 2× H100):
  `https://recipes.vllm.ai/Qwen/Qwen3.6-35B-A3B`.
  The recipe's think-off switch is exactly our protocol:
  `--default-chat-template-kwargs '{"enable_thinking": false}'`.
  We pin the **linux/amd64 v0.28.0 image digest** in `model_manifest.json`,
  not a moving image tag, and use `--generation-config vllm` so checkpoint
  generation defaults cannot silently change the matched sampling protocol.
- Sizing [verified arithmetic on pinned `config.json` + HF `usedStorage`]:
  stored weights are 71,921,347,477 B (base) / 71,916,583,934 B (L1),
  i.e. ~67.0 GiB per arm. Label capacities converted, NOT measured usable HBM:
  H200-141GB is nominal 141 GB (≈131.3 GiB by conversion) → ~64 GiB nominal
  headroom for the 65,536-token KV (~1.3 GiB full-attention KV: 10 layers ×
  2 KV heads × 256 dim × K/V × bf16), activations, CUDA graphs and runtime;
  A100-80GB is nominal 80 GB (≈74.5 GiB by conversion) → ~7.5 GiB nominal
  headroom — tight and unmeasured, so H200 is the plan (also the recipe's BF16
  guidance). Every headroom figure here is an estimate; actual usable HBM is
  unmeasured and A100 fit is not ruled out.
- Use one app per arm to keep model identity and weight volumes separate.
  Run them sequentially to reduce concurrent resource use. Both share the same
  runtime, GPU and settings; only weights and deployment identity differ.
- Auth: vLLM `--api-key` from Modal secret
  `evallab-breaker-<arm>-api-key` (key `VLLM_API_KEY`); every `/v1` call
  needs the bearer key. BUT the Modal endpoint is `unauthenticated=True`:
  any hit, with no proxy auth, can wake the container and cold-start billed
  GPU time. max_containers/token caps bound rate, not total dollars.
  Host-side value lives in
  `BREAKER_SELFHOSTED_API_KEY`; per-arm URLs in parent specs map
  `breaker-selfhosted/<full HF repo id>` → the matching app URL
  (parent env `EVALLAB_BREAKER_SELFHOSTED_UPSTREAM`).
- Bounds: `max_containers=1`, `min_containers=0`, 300 s scaledown,
  `--max-num-seqs 8` (fixed matched profile; no resource environment overrides).
  Stop the app after the campaign; scale-to-zero alone is not a spending fence.

## 3. Tinker comparison (honest)

- Eval Lab already sampled the base through
  `tinker/Qwen/Qwen3.6-35B-A3B` in cheater-recall-v1 wave 2. That native
  Terminus OpenAI-compatible route is usable; its recorded uncached rates
  were $0.54 input / $1.335 output per million tokens.
- Tinker's weight documentation describes exporting training-run weights for
  external serving. The inspected public interfaces did not establish how to
  import this released full HF checkpoint as a Tinker sampler.
- No public L1 `tinker://.../sampler_weights/...` URI was found on its card.
  Re-training to obtain another sampler would create a different artifact,
  not evaluate the released L1.
- **Decision:** use Modal+vLLM for both arms. A Tinker-base/Modal-L1 comparison
  changes serving/rendering along with weights and is not the primary matched test.
  Recheck Tinker's current pricing if using it later for SFT.

## 4. Concrete commands (run only after budget approval)

Run from the owned Eval Lab worktree. `keys has BREAKER_SELFHOSTED_API_KEY`
checks presence without exposing the value; if absent, configure it with
`keys set BREAKER_SELFHOSTED_API_KEY` before the authorized deployment. The
secret-creation command below reads the value from the environment inside
Python, not a secret-bearing shell argument. It refuses existing secret names
instead of overwriting another deployment's credentials.

```bash
# $0 local preflight; constructs the SDK definitions but makes no remote calls.
uv run --no-project --with modal==1.5.4 python research/experiments/trained-breaker-v1/modal_serve.py plan --arm both

# REMOTE SETUP BELOW: only after Peter approves the new budget.
keys run -- uv run --no-project --with modal==1.5.4 python -c '
import json, os, modal
from pathlib import Path
m = json.loads(Path("research/experiments/trained-breaker-v1/model_manifest.json").read_text())
for app_name in m["serving"]["app_per_arm"].values():
    modal.Secret.objects.create(app_name + "-api-key", {"VLLM_API_KEY": os.environ["BREAKER_SELFHOSTED_API_KEY"]})
'

# Confirm the pinned HF commits still exist; never replace them with main.
# CPU-only weight download (~67 GiB/arm), before allocating any GPU:
EVALLAB_BREAKER_ARM=baseline uvx --from modal==1.5.4 modal run research/experiments/trained-breaker-v1/modal_serve.py::download_weights
EVALLAB_BREAKER_ARM=l1 uvx --from modal==1.5.4 modal run research/experiments/trained-breaker-v1/modal_serve.py::download_weights

# Deploy only the arm about to run; save its printed bare HTTPS origin.
EVALLAB_BREAKER_ARM=baseline uvx --from modal==1.5.4 modal deploy research/experiments/trained-breaker-v1/modal_serve.py
# After baseline finishes and is stopped:
EVALLAB_BREAKER_ARM=l1 uvx --from modal==1.5.4 modal deploy research/experiments/trained-breaker-v1/modal_serve.py

# Mandatory shutdown / emergency cleanup for these dedicated experiment apps:
uvx --from modal==1.5.4 modal app stop evallab-breaker-base
uvx --from modal==1.5.4 modal app stop evallab-breaker-l1
```

The CPU function resumes the fetch, verifies every index-named shard and writes
an atomic repo+revision-bound completion marker. GPU startup checks the marker;
a refused startup can still bill. The native dispatcher performs readiness
before model dispatch; real server behavior has not yet been qualified. Follow
[README.md](README.md) for per-arm approvals, origin selection and the scoped
launch command. Its `finally` stops the selected app; abrupt process/machine
death still requires explicit operator cleanup.

Multi-GPU alternative (NOT the plan): 2× H100 per arm would also fit but
doubles GPU spend and adds tensor-parallel complexity for no protocol gain.

## 5. Official-price cost estimate (both models)

Rates [verified]: Modal list (`modal.com/pricing`, mirrored 2026-10-08 in
`research/price-bench/prices.yaml`): H200-141GB $0.001261/s ($4.5396/h);
CPU $0.0000131/core-s; memory $0.00000222/GiB-s. Per-second billing.

| Line | Math | Cost |
|---|---|---|
| Server rate per arm | 4.5396 + 4×0.04716 + 16×0.007992 | **≈ $4.8561/h** |
| Both arms concurrently | 2 × 4.8561 | **≈ $9.71/h wall** |
| Idle tail per warm period | 300 s × hourly/3600 | **≈ $0.41/arm** |
| Weight download (separate CPU-only function; GPU billed only from server start) | ~67.0 GiB/arm snapshot fetch | CPU+memory per-second for an **[unmeasured]** duration; HF egress / Modal ingress rates **[unverified]** — never download while a GPU container is allocated |
| Volume storage retained | 143,837,931,411 B total (~134.0 GiB) | **$0 only within** Modal's 1 TiB/mo volume include (per price-book note); otherwise $0.09/GiB-mo ≈ **$12/mo [assumption]**; egress assumptions **[unverified]** |
| Cold GPU load (weight load + CUDA graphs) | — | **[unmeasured]**; allow the 18–20 min startup/timeout budget, measure on first deploy |

Wall-clock GPU cost uses total server-seconds, including cold start, drain and
idle periods. Sequential apps lower concurrent burn, not necessarily total cost:

```
campaign_gpu_$ ≈ hourly_rate × (n_trials × mean_trial_h / concurrency + cold_h + drain_h + idle_h)
```

The actual pilot is **24 model trials**: two arms × (five leaky + seven historical
repaired versions), one attempt each. `build_specs.py` calculates the scenario
from the committed `cost_assumptions`: two concurrent trials, a 600-second agent
budget plus 600 seconds for other trial phases, and a 20-minute cold-start /
five-minute idle allowance per campaign. It adds the dated Daytona sandbox
rate and a 20% planning margin to each draft budget. This is a conservative
timeout scenario, **not measured throughput or an enforced total-dollar fence**;
download and persistent-storage costs are separate. No historical $15 or $5
approval authorizes these new campaigns.

## 6. Validation status and limits

- Validated $0: both HF identities, SHAs, file lists, licenses, card recipe,
  pinned config.json architecture/context, merge_check.json semantics, vLLM
  recipe version/hardware/think-off flag, Tinker models/pricing/docs,
  Modal list prices.
- Local runtime proof: Modal SDK **1.5.4** constructs the app definitions; the
  native Harbor/LiteLLM client reaches the new proxy and a scripted loopback
  peer with both exact model IDs and forced think-off sampling. Security and
  campaign tests run in the parent checkout; see the preparation receipt.
- NOT validated (no GPU): the digest-pinned vLLM image boots these weights,
  peak HBM, 65,536-token stability, real template outputs, throughput/latency,
  cold-start/download durations or the live readiness response. Deployment
  URLs cannot be known before the approved deployment.
- No paid API, cloud deployment, weight download or local model inference was
  performed. CPU-only protocol fixtures are not model results.

## Sources

- L1 model API + card + config + merge evidence:
  `https://huggingface.co/api/models/rewardhack/qwen3.6-35b-a3b-hacksft-thinkoff-873rows-ep3`,
  `https://huggingface.co/rewardhack/qwen3.6-35b-a3b-hacksft-thinkoff-873rows-ep3/raw/main/README.md`,
  `…/raw/main/config.json`, `…/raw/main/merge_check.json`
- Base model: `https://huggingface.co/api/models/Qwen/Qwen3.6-35B-A3B`
- vLLM recipe (≥0.17.0, 1×H200 BF16, think-off flag, v0.28.0 measurement):
  `https://recipes.vllm.ai/Qwen/Qwen3.6-35B-A3B`
- Tinker training/sampling/weights/serverless + pricing:
  `https://tinker-docs.thinkingmachines.ai/tinker/quickstart/`,
  `https://tinker-docs.thinkingmachines.ai/tinker/models/`,
  `https://tinker-docs.thinkingmachines.ai/tutorials/core-concepts/weights/`
- Prices: `https://modal.com/pricing`; repo mirror
  `research/price-bench/prices.yaml` (modal.* + `tinker.hosted_training.qwen3-5-9b` notes);
  Tinker SFT defaults `src/evallab/sft_tinker.py`; Modal pattern
  `tools/modal-mimo-serve/serve.py` + `README.md`

Local source copies live in the research-context library:
- `sources/links/recipes.vllm.ai/2026-10-12-qwen-qwen3-6-35b-a3b-vllm-recipes.md`
  — https://recipes.vllm.ai/Qwen/Qwen3.6-35B-A3B
- `sources/links/modal.com/2026-10-11-pricing-pay-per-second-for-gpus-and-cpus-modal.md`
  — https://modal.com/pricing
- `reward-hacking/omp-conversations/2026-10-10-huggingface-breaker-model-inventory.md`
  contains the checkpoint inventory and links to archived model cards;
  `reward-hacking/datasets/2026-10-12-breaker-training-sources.json` pins the
  broader training-data inventory without storing bulk weights/corpora in git.
