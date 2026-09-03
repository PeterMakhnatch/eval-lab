# Upstream adoption scorecard

**Snapshot basis.** Repository facts below are read from the pinned commits in each citation; TRACE is pinned to arXiv **2510.00415v3** because its linked canonical GitHub repository was empty when fetched (no `HEAD`, source files, or license to pin). “Offline” means a backend can train/convert an already stored record without regenerating policy actions; it does **not** waive Eval Lab’s Harbor/CAS admission and split gates.

## SPADE (`spade-rl/spade`)

**(a) License / pin.** MIT; read at `ebd40ec872fc5630cac299cb5e38e7c89743bef5` ([LICENSE](https://github.com/spade-rl/spade/blob/ebd40ec872fc5630cac299cb5e38e7c89743bef5/LICENSE#L1-L5); [project metadata](https://github.com/spade-rl/spade/blob/ebd40ec872fc5630cac299cb5e38e7c89743bef5/pyproject.toml#L1-L35)).

**(b) Input contract.** Its reusable fixed-environment boundary is exact Gym-like text I/O: `reset(seed) -> (str, dict)` and `step(action: str) -> (str, float, bool, bool, dict)`, wrapped as an `EnvInstance`; an adapter must list environments, create instances, expose difficulty range, and category ([`env_adapter.py`](https://github.com/spade-rl/spade/blob/ebd40ec872fc5630cac299cb5e38e7c89743bef5/spade/core/envs/env_adapter.py#L15-L21), [L57-L158](https://github.com/spade-rl/spade/blob/ebd40ec872fc5630cac299cb5e38e7c89743bef5/spade/core/envs/env_adapter.py#L57-L158)). Its RL input is instead a `Trajectory` containing OpenAI-format `messages`, `tokens`, `loss_mask`, `rollout_log_probs`, reward, status, and metadata ([`types.py`](https://github.com/spade-rl/spade/blob/ebd40ec872fc5630cac299cb5e38e7c89743bef5/spade/core/types.py#L22-L58)).

**(c) Stored vs live.** **Not a direct stored-trajectory trainer.** Fixed-env mode can re-run saved environment files through `SyntheticGameAdapter`, but self-play trains on current rollouts: the orchestrator records generated response token log-probabilities into its `Trajectory` ([`fixed_env_orchestrator.py`](https://github.com/spade-rl/spade/blob/ebd40ec872fc5630cac299cb5e38e7c89743bef5/spade/core/fixed_env_orchestrator.py#L296-L375); [`synthetic_game_adapter.py`](https://github.com/spade-rl/spade/blob/ebd40ec872fc5630cac299cb5e38e7c89743bef5/spade/core/envs/synthetic_game_adapter.py#L1-L20)). A Harbor trace lacks this native object/tokenization/logprob contract; an importer plus a compatible off-policy objective would be new work, not a supported path.

**(d) Runtime / heavyweight imports.** Base dependencies include `transformers`, `openai`, `gem-llm`, `math_verify`, and `weave`; the Tinker training extra explicitly adds `torch` and `tinker` ([`pyproject.toml`](https://github.com/spade-rl/spade/blob/ebd40ec872fc5630cac299cb5e38e7c89743bef5/pyproject.toml#L38-L77)). The environment timeout uses POSIX `SIGALRM` ([`env_adapter.py`](https://github.com/spade-rl/spade/blob/ebd40ec872fc5630cac299cb5e38e7c89743bef5/spade/core/envs/env_adapter.py#L24-L54)), so do not promise Windows portability. Source does not make a GPU mandatory for the generator/validator, but its token-logprob self-play backend is not the lightweight/offline bundle path.

**(e) Evidence/provenance store.** It has a competing **curriculum memory**, not an immutable evidence store: `EnvironmentMemory` keeps source path, truncated executable code, win rate, regret, and metadata, then serializes a mutable JSON list ([`env_memory.py`](https://github.com/spade-rl/spade/blob/ebd40ec872fc5630cac299cb5e38e7c89743bef5/spade/core/env_memory.py#L18-L46), [L144-L174](https://github.com/spade-rl/spade/blob/ebd40ec872fc5630cac299cb5e38e7c89743bef5/spade/core/env_memory.py#L144-L174)). Replace it with Harbor/CAS references and derived digests; never let it become evidence authority.

**(f) TRACE-specific.** N/A.

**(g) Generation/validation and separability.** Relevant paths are `spade/core/game_generator.py` (`GameSpec`, `SyntheticGameGenerator`), `spade/core/generate_and_validate_games.py` (stand-alone generator/retry/difficulty runner), `spade/core/utils/game_files.py` (load/reset/step structural validation), and `spade/core/env_validator.py` (LLM solvability/winnability rejection sampling). The stand-alone runner imports the generator and synthetic environment rather than a trainer ([`generate_and_validate_games.py`](https://github.com/spade-rl/spade/blob/ebd40ec872fc5630cac299cb5e38e7c89743bef5/spade/core/generate_and_validate_games.py#L1-L80)); these pieces are therefore separable from self-play. Hint-regret paths are `spade/core/hint_generator.py` (generates a model hint from environment source) and `spade/core/env_memory.py` (`high_regret_seeds` ranks medium-win-rate environments by regret) ([`hint_generator.py`](https://github.com/spade-rl/spade/blob/ebd40ec872fc5630cac299cb5e38e7c89743bef5/spade/core/hint_generator.py#L1-L18); [`env_memory.py`](https://github.com/spade-rl/spade/blob/ebd40ec872fc5630cac299cb5e38e7c89743bef5/spade/core/env_memory.py#L76-L100)). **Security gate:** its structural validator dynamically loads generated Python and invokes `reset`/`step` ([`game_files.py`](https://github.com/spade-rl/spade/blob/ebd40ec872fc5630cac299cb5e38e7c89743bef5/spade/core/utils/game_files.py#L10-L24)); Eval Lab must retain its own quarantine/sandbox admission rather than execute candidate code in-process.

## TRACE — *Towards Self-Evolving Benchmarks* (`arXiv:2510.00415`)

**(a) License / pin.** The source of truth is paper **v3, 24 Mar 2026**, not a distributable implementation ([paper header](https://arxiv.org/pdf/2510.00415), p.1). The paper’s linked `titanwings/trace-benchmark-evolving` repository returned an empty repository at fetch time; therefore license and commit are **unavailable**, rather than inferred.

**(b) Input contract.** TRACE defines a seed benchmark as task/trajectory pairs $B_0=\{(q,\tau)\}$ and an execution step as $\langle c_{i-1},r_i,a_i,o_i\rangle$—context, reasoning, external action, observation ([§3.1–3.3](https://arxiv.org/pdf/2510.00415), pp.4–5). Its output contract is the pair **(evolved problem, validatable trajectory)**, not a training batch ([§4](https://arxiv.org/pdf/2510.00415), p.5).

**(c) Stored vs live.** Stored seed traces are inputs, but TRACE is **not offline-only**: Algorithm 1 calls `TaskEvolve(E,q,Δ,T)` for test-time exploration, and validation re-executes each tool call against its environment ([§4.2–4.4 / Algorithm 1](https://arxiv.org/pdf/2510.00415), pp.6–7). Thus cached Harbor evidence can ground proposal mining, but a TRACE reproduction requires live tool/environment access and reproducible replay.

**(d) Runtime / heavyweight imports.** No implementation-level OS/GPU dependency is specified in the paper. Its reported loop is nevertheless heavyweight: Proposer, Executor, and Validator use `Qwen3-Coder-480B-A35B`; an auxiliary validator uses `Qwen3-235B-A22B-Instruct`; evaluation uses an `inspect_eval` ReAct agent capped at 100 interaction turns ([§5.1](https://arxiv.org/pdf/2510.00415), p.8). [INFERENCE] Serving those declared models is operationally external/GPU-scale, but this is not a stated installation requirement.

**(e) Evidence/provenance store.** TRACE makes the trajectory a first-class auditable artifact, but the paper supplies no persistence/CAS implementation ([§3.2](https://arxiv.org/pdf/2510.00415), pp.4–5). It therefore does not compete with Harbor in code; Harbor must supply immutable records, replay inputs/outputs, and admission lineage.

**(f) Deficit → environment/task methodology and what “methodology only” excludes.** Stage 1 performs bottleneck-aware pre-exploration of a seed task and trace, then emits targeted evolution proposals (longer evidence chains, more tool interaction, deeper reasoning). Stage 2 injects a proposal at a solution-path fork, performs free tool-enabled exploration, records thought/action/observation, and formulates the new task **after** obtaining the solution trace. Stage 3 requires schema/lightweight checks, step-by-step replay, global logical/solvability audit, answer determinism/accessibility, difficulty assessment, and a blind trajectory-agnostic ReAct solver with tool parity ([§4.1–4.3](https://arxiv.org/pdf/2510.00415), pp.5–7).

The full loop is `select seed → R proposal/exploration retries → formulate q′ from τ′ → validate → accept (q′,τ′)` ([Algorithm 1](https://arxiv.org/pdf/2510.00415), p.7). It is an **evolution/evaluation loop, not a weight-update training algorithm**. “Methodology only” therefore excludes claiming reproduction of their agents/prompts, live tools, replay substrate, retry budgets, difficulty judge, blind solver, 480B backend, or GAIA/AIME experiment; it permits only the deterministic deficit-to-candidate principles after Harbor admission gates.

**(g) SPADE-specific.** N/A.

## Agent Lightning (`microsoft/agent-lightning`)

**(a) License / pin.** MIT; read at `218f1f7c0bac0800de4d5a4e5e6f61cf7b5038b4` ([LICENSE](https://github.com/microsoft/agent-lightning/blob/218f1f7c0bac0800de4d5a4e5e6f61cf7b5038b4/LICENSE#L1-L17)).

**(b) Input contract.** A rollout request is `RolloutCreate(input: Any, is_train: bool=True, config, metadata, rollout_id)`; events are ordered per rollout. Gateway-captured `model_request` has model/version, original request, full response, status/usage, while a reward event has required scalar `value` ([`schemas.py`](https://github.com/microsoft/agent-lightning/blob/218f1f7c0bac0800de4d5a4e5e6f61cf7b5038b4/agentlightning/schemas.py#L12-L66), [L104-L143](https://github.com/microsoft/agent-lightning/blob/218f1f7c0bac0800de4d5a4e5e6f61cf7b5038b4/agentlightning/schemas.py#L104-L143)). Its verl bridge turns successful model-request events into `Triplet(prompt={token_ids}, response={token_ids, log_probs}, reward)` and attaches final reward to the final triplet ([`agl_rollout_manager.py`](https://github.com/microsoft/agent-lightning/blob/218f1f7c0bac0800de4d5a4e5e6f61cf7b5038b4/agentlightning/verl/agl_rollout_manager.py#L448-L509)).

**(c) Stored vs live.** The protocol can represent events, but the supplied RL bridge is live/on-policy: it creates rollout requests, polls them to a terminal state, fetches events, and deletes completed rollout state ([`agl_rollout_manager.py`](https://github.com/microsoft/agent-lightning/blob/218f1f7c0bac0800de4d5a4e5e6f61cf7b5038b4/agentlightning/verl/agl_rollout_manager.py#L358-L426), [L517-L567](https://github.com/microsoft/agent-lightning/blob/218f1f7c0bac0800de4d5a4e5e6f61cf7b5038b4/agentlightning/verl/agl_rollout_manager.py#L517-L567)). A Harbor→event importer could be built, but is not an offline trainer in this pin.

**(d) Runtime / heavyweight imports.** Core requires Python ≥3.12, FastAPI/Uvicorn, Pydantic, HTTPX, Hydra/OmegaConf and `kr8s`; project metadata says OS independent ([`pyproject.toml`](https://github.com/microsoft/agent-lightning/blob/218f1f7c0bac0800de4d5a4e5e6f61cf7b5038b4/pyproject.toml#L1-L38)). The optional verl integration adds `verl`, `torch`, `ray`, `tensordict`, `datasets`, NumPy and tqdm; its own comment says this is not a training environment and calls for GPU-torch verl separately ([`pyproject.toml`](https://github.com/microsoft/agent-lightning/blob/218f1f7c0bac0800de4d5a4e5e6f61cf7b5038b4/pyproject.toml#L104-L128)). No core GPU/Linux requirement; the execution backend imposes it.

**(e) Evidence/provenance store.** Its server is explicitly module-level in-memory dictionaries, not durable provenance ([`server/store.py`](https://github.com/microsoft/agent-lightning/blob/218f1f7c0bac0800de4d5a4e5e6f61cf7b5038b4/agentlightning/server/store.py#L1-L17)). It must be a disposable live-rollout transport; Harbor remains the persisted evidence system.

**(f) TRACE-specific.** N/A.

**(g) SPADE-specific.** N/A.

## ADP — Agent Data Protocol (`neulab/agent-data-protocol`)

**(a) License / pin.** Project README declares MIT (while warning individual datasets may differ); read at `040a279b46b2388ae42b43449f8645b9781c7bf7` ([README](https://github.com/neulab/agent-data-protocol/blob/040a279b46b2388ae42b43449f8645b9781c7bf7/README.md#L227-L230)). This is the canonical repo corresponding to paper arXiv:2510.24702, not an ambiguous unrelated “ADP.”

**(b) Input contract.** ATIF version is exactly `ATIF-v1.7`. An `ATIFTrajectory` requires at least one sequentially numbered `Step`; each step has source (`system|user|agent`), message, optional tool calls, observation/results, metrics (including optional token IDs/logprobs), and `extra`; agent steps alone may have tools/reasoning ([`schema/atif.py`](https://github.com/neulab/agent-data-protocol/blob/040a279b46b2388ae42b43449f8645b9781c7bf7/schema/atif.py#L9-L105), [L120-L162](https://github.com/neulab/agent-data-protocol/blob/040a279b46b2388ae42b43449f8645b9781c7bf7/schema/atif.py#L120-L162)). The documented pipeline is newline-delimited JSON: `full_raw.jsonl → full_atif.jsonl → full_std.jsonl → agent-specific full_sft_*.jsonl` ([README](https://github.com/neulab/agent-data-protocol/blob/040a279b46b2388ae42b43449f8645b9781c7bf7/README.md#L46-L70)).

**(c) Stored vs live.** **Offline conversion only:** its documented inputs are saved JSONL and its purpose is conversion to SFT-ready data; it does not implement a rollout runner or optimizer ([README](https://github.com/neulab/agent-data-protocol/blob/040a279b46b2388ae42b43449f8645b9781c7bf7/README.md#L1-L31)).

**(d) Runtime / heavyweight imports.** No GPU/Linux requirement is declared. Requirements include Pydantic, pandas, `browsergym-core`, `openhands-sdk`, `openhands-tools`, and `transformers` ([`requirements.txt`](https://github.com/neulab/agent-data-protocol/blob/040a279b46b2388ae42b43449f8645b9781c7bf7/requirements.txt#L1-L13)); those agent/browser dependencies are too heavy to import in Eval Lab’s portable exporter.

**(e) Evidence/provenance store.** The repo converts files and defines local dataset metadata/tool specs; it supplies no CAS/provenance database. `ATIFTrajectory.extra` can carry extensions, but source digests, redaction state, split/cluster identity, and Harbor lineage are not required fields ([`atif.py`](https://github.com/neulab/agent-data-protocol/blob/040a279b46b2388ae42b43449f8645b9781c7bf7/schema/atif.py#L120-L139)). Preserve Eval Lab’s contract as the outer envelope rather than making ADP authoritative.

**(f) TRACE-specific.** N/A.

**(g) SPADE-specific.** N/A.

## TRL (`huggingface/trl`)

**(a) License / pin.** Apache-2.0; read at `312727b3ef44400e60032be1122fbce7865ff24d` ([`pyproject.toml`](https://github.com/huggingface/trl/blob/312727b3ef44400e60032be1122fbce7865ff24d/pyproject.toml#L1-L37); [LICENSE](https://github.com/huggingface/trl/blob/312727b3ef44400e60032be1122fbce7865ff24d/LICENSE#L1-L16)).

**(b) Input contract.** `SFTTrainer` accepts an offline `datasets.Dataset`/`IterableDataset`: language-modeling rows have `text` or conversational `messages`; prompt-completion rows have `prompt` and `completion`; pre-tokenized rows require `input_ids` and may carry labels/assistant/completion masks ([`sft_trainer.py`](https://github.com/huggingface/trl/blob/312727b3ef44400e60032be1122fbce7865ff24d/trl/trainer/sft_trainer.py#L847-L879)). The collator rejects other shapes and explicitly selects `messages|text` or `prompt+completion` ([`sft_trainer.py`](https://github.com/huggingface/trl/blob/312727b3ef44400e60032be1122fbce7865ff24d/trl/trainer/sft_trainer.py#L639-L670)).

**(c) Stored vs live.** **Adoptable offline SFT path:** SFT consumes a supplied stored dataset and tokenizes it; the contract contains no rollout-logprob requirement. Do not generalize that to TRL RL algorithms: this recommendation is specifically SFT until an on-policy backend is separately approved.

**(d) Runtime / heavyweight imports.** Metadata says OS independent; base dependencies are `accelerate`, `datasets`, and `transformers` ([`pyproject.toml`](https://github.com/huggingface/trl/blob/312727b3ef44400e60032be1122fbce7865ff24d/pyproject.toml#L12-L39)). But SFTTrainer directly imports `torch`, `accelerate`, and Transformers ([`sft_trainer.py`](https://github.com/huggingface/trl/blob/312727b3ef44400e60032be1122fbce7865ff24d/trl/trainer/sft_trainer.py#L25-L56)); optional extras add DeepSpeed, bitsandbytes, PEFT, and vLLM ([`pyproject.toml`](https://github.com/huggingface/trl/blob/312727b3ef44400e60032be1122fbce7865ff24d/pyproject.toml#L45-L88)). GPU is operationally appropriate for substantive weight updates, but not declared as a package OS requirement.

**(e) Evidence/provenance store.** The SFT interface consumes a dataset and yields a Trainer; it defines no Harbor-like evidence store or required provenance columns. [INFERENCE] Treat TRL only as the terminal consumer of a digest-pinned rendered dataset and write its result manifest back into Harbor.

**(f) TRACE-specific.** N/A.

**(g) SPADE-specific.** N/A.

## verl (`volcengine/verl`)

**(a) License / pin.** Apache-2.0; read at `e42d6af6e85dd37c907af5ea99326355c376bd97` ([LICENSE](https://github.com/volcengine/verl/blob/e42d6af6e85dd37c907af5ea99326355c376bd97/LICENSE#L1-L16)).

**(b) Input contract.** `RLHFDataset` loads one or more `.parquet` files (and its implementation also accepts `.json`/`.jsonl`), defaults `prompt_key` to `prompt`, and builds raw chat prompts from that field ([`rl_dataset.py`](https://github.com/volcengine/verl/blob/e42d6af6e85dd37c907af5ea99326355c376bd97/verl/utils/dataset/rl_dataset.py#L55-L122), [L150-L172](https://github.com/volcengine/verl/blob/e42d6af6e85dd37c907af5ea99326355c376bd97/verl/utils/dataset/rl_dataset.py#L150-L172), [L279-L420](https://github.com/volcengine/verl/blob/e42d6af6e85dd37c907af5ea99326355c376bd97/verl/utils/dataset/rl_dataset.py#L279-L420)). PPO reward input additionally needs `ground_truth` and `data_source` in Parquet non-tensor fields; custom score signature is `my_reward_fn(data_source, solution_str, ground_truth, extra_info=None)` ([reward docs](https://github.com/volcengine/verl/blob/e42d6af6e85dd37c907af5ea99326355c376bd97/docs/preparation/reward_function.rst#L15-L56)).

**(c) Stored vs live.** It consumes stored **prompts/reward fields**, not stored completed trajectories for its PPO path. The PPO loop repeats prompt batches, calls `generate_sequences`, scores the generated responses, updates actor, then updates rollout weights ([`ray_trainer.py`](https://github.com/volcengine/verl/blob/e42d6af6e85dd37c907af5ea99326355c376bd97/verl/trainer/ppo/ray_trainer.py#L1466-L1514), [L1563-L1716](https://github.com/volcengine/verl/blob/e42d6af6e85dd37c907af5ea99326355c376bd97/verl/trainer/ppo/ray_trainer.py#L1563-L1716)). It is therefore live/on-policy (and uses/recomputes rollout probabilities depending configuration), not a Harbor-trace PPO importer.

**(d) Runtime / heavyweight imports.** Loader imports `datasets`, NumPy, `torch`, OmegaConf, PIL and Transformers ([`rl_dataset.py`](https://github.com/volcengine/verl/blob/e42d6af6e85dd37c907af5ea99326355c376bd97/verl/utils/dataset/rl_dataset.py#L18-L36)); requirements add Ray, TensorDict, PEFT, PyArrow, WandB and others ([`requirements.txt`](https://github.com/volcengine/verl/blob/e42d6af6e85dd37c907af5ea99326355c376bd97/requirements.txt#L1-L29)). Its supported uv GPU workflow targets Linux Python 3.12 on x86_64/aarch64 with CUDA; docs separately permit a CPU extra for CI/quick checks, not the production RL path ([install docs](https://github.com/volcengine/verl/blob/e42d6af6e85dd37c907af5ea99326355c376bd97/docs/start/install.rst#L7-L8), [L39-L58](https://github.com/volcengine/verl/blob/e42d6af6e85dd37c907af5ea99326355c376bd97/docs/start/install.rst#L39-L58), [L244-L270](https://github.com/volcengine/verl/blob/e42d6af6e85dd37c907af5ea99326355c376bd97/docs/start/install.rst#L244-L270)).

**(e) Evidence/provenance store.** It can optionally dump rollout generations as JSONL ([`ray_trainer.py`](https://github.com/volcengine/verl/blob/e42d6af6e85dd37c907af5ea99326355c376bd97/verl/trainer/ppo/ray_trainer.py#L479-L546), [L1720-L1724](https://github.com/volcengine/verl/blob/e42d6af6e85dd37c907af5ea99326355c376bd97/verl/trainer/ppo/ray_trainer.py#L1720-L1724)); this is a run artifact, not a content-addressed evidence/provenance system. Keep it external and ingest only an explicit result manifest into Harbor.

**(f) TRACE-specific.** N/A.

**(g) SPADE-specific.** N/A.

## Recommendation

| project | adopt/adapt/reject | one-line reason | hard blocker if any |
|---|---|---|---|
| SPADE | **ADAPT** | Borrow the Gym text-env boundary plus generation/validation and regret-selection ideas; keep Harbor ownership and sandboxing. | Native trainer requires current-policy token/logprob trajectories; generated Python must not execute outside a quarantine sandbox. |
| TRACE | **ADAPT** | Adopt only deterministic deficit→candidate and replay/admission principles. | No pinned executable implementation/license; full method requires live tool exploration, replay, validators, and large external model serving. |
| Agent Lightning | **ADAPT** | Its rollout/event schemas can be mapped at an external execution boundary. | Supplied verl bridge is live and deletes transient state; its in-memory server cannot be Harbor provenance. |
| ADP | **ADAPT** | Map/validate ATIF-style offline records at export boundaries, retaining Eval Lab’s richer lineage/redaction/split envelope. | Root protocol does not require the Harbor provenance fields; importing browser/agent dependencies into core would violate the lightweight boundary. |
| TRL | **ADOPT** | `SFTTrainer` directly consumes the portable stored `messages` or `prompt`/`completion` export. | External weight-capable runtime/accelerator still required; no Harbor/model/network invocation tonight. |
| verl | **ADAPT** | Use only as a future external on-policy RL backend behind a rendered-Parquet adapter and result manifest. | Linux CUDA/Ray-scale deployment plus live rollouts/current-policy probabilities; incompatible with stored-trajectory-only training. |


---

## ADDENDUM (2026-09-03, Track F lead) — TRACE disambiguation, per wK:p3

The TRACE section above reviews only **TRACE-Benchmark-Evolution** (Guo et al., arXiv:2510.00415), which is an **optional synthesis/validation reference** for task-evolution plans. It is NOT the Track B/C methodology source and must not be cited for capability-deficit metrics.

The Track B/C methodology source is **TRACE-Capability** — *Turning Recurrent Agent failures into Capability-targeted training Environments* (arXiv:2604.05336, ScalingIntelligence/TRACE @ `d2db23085409555b3f13ea426f42d62cf0bbc43d`): applicability-aware `NA`/`PRESENT`/`LACKING` label distinctions and deterministic post-label metrics `Cov`, `ER-`, `ER+`, `Delta` (`pipeline/aggregate_capabilities.py::compute_metrics`). Disposition: **ADAPT methodology only** (labels + metrics; not its LLM labeling as authority, `GameSpec` registry, environment generator, GRPO/LoRA pipeline, or MoE gate).

Invariant (binding, from `track-f-trace-disambiguation-20260903.md`)

**v1 verdict correction (2026-09-03, integration lock, supersedes the Recommendation table above for these two rows):** Agent Lightning = **REJECT for v1 / deferred** and verl = **REJECT for v1 / deferred** — no first-wave use, including adapter scaffolding; both become plan/result-boundary options only after the SFT-signal gate. TRL-SFT-only remains the sole ADOPT; SPADE, ADP, TRACE-Capability remain ADAPT methodology/boundary only.
: Tracks B/C follow TRACE-Capability (2604.05336); TRACE-Benchmark-Evolution (2510.00415) may inform an optional later task-synthesis validation plan only; neither becomes evidence authority, executable admission, trainer, or Harbor replacement. Use qualified names on every first mention; never bare "TRACE".
