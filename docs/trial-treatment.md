---
status: living
audience:
  - analyst
  - runner
---

# Trial treatment keys and capture records

A pass rate or a learnability call may only pool trials that ran under the same setup. `evallab.trial_treatment` makes that check mechanical. It gives every trial:

- a **treatment key**: a hash of every setting that changes behaviour;
- a **capture record**: which evidence the trial left behind, and how complete it is.

Both are catalog tables next to `task_qualification.parquet`, under `derived/parquet/external/task_catalog/`:

| Table | One row per | Written by |
|---|---|---|
| `trial_treatment.parquet` | trial | `evallab tasks treatment-collect <job_dir>...` |
| `trial_capture.parquet` | trial | the same command |

`treatment-collect` merges into the existing tables, replacing rows for trials it has seen before. The trial identity is `(job_name, trial_name)`. `evallab attach` exposes both tables as views.

## Pool check

```
uv run evallab tasks pool-check <job name or dir>... [--same-task] [--accept-unknown] [--json]
```

The command exits 0 and prints `POOLABLE` only when every setup field agrees. Otherwise it exits 1, prints `REFUSED`, and lists each differing field with the trials behind each value.

- **Unknowns refuse the pool.** A field is unknown when the trial's files cannot establish it. `--accept-unknown` lets unknowns count as equal to each other, and they are still reported. A known value never equals an unknown one.
- **Tasks don't split the pool by default.** A cohort of different tasks run under one setup passes; the compared fields are `setup_key`'s. `--same-task` also requires a single task version.

## Values

- **Unknown is not zero.** A value the trial's files cannot establish is `null`, and its name is listed in `unknown_fields`. `complete` is true only when that list is empty.
- `"n/a"` marks a setting that does not apply. A nop or oracle agent has no model, sampling, parser or request ceilings. The queue also sizes its wall ceiling per task, so its time limits are `"n/a"` too.
- `"default"` marks a sampling parameter the harness did not send, so the provider's default applied.
- `treatment_key` hashes all key fields. `setup_key` hashes all of them except the task fields `task_version_digest` and `agent_timeout_seconds`.
- Key-field columns store JSON text, so a number, `"n/a"` and `"default"` fit one column. `read_treatments()` decodes them. `field_sources` records where each value came from, and `harness_config` holds the hashed harness settings.

## Key fields and their sources

| Field | Source |
|---|---|
| `task_version_digest` | `lab-metadata.json` → `task_staging.source_package_digest` (the catalog's task version) |
| `backend` | trial `config.json`, `result.json` or `lock.json` environment (`daytona`, `docker`, …) |
| `agent` | trial `config.json` → agent import path or name |
| `harbor_version` | `lab-metadata.json` → `tools.harbor` |
| `model` | trial `config.json` → `agent.model_name` |
| `model_revision`, `serving_image`, `serving_context_tokens` | MiMo self-hosted route only: `MODEL_REVISION`, `SGLANG_IMAGE` and `CONTEXT_LENGTH` in `tools/modal-mimo-serve/serve.py`, read at the run's commit. Other routes don't record serving pins, so these are unknown. |
| `temperature`, `top_p`, `top_k`, `thinking` | Requested values come from the agent kwargs (`temperature`, and `llm_call_kwargs.top_p` / `top_k` / `extra_body.reasoning_effort` / `chat_template_kwargs`). On the MiMo route, the proxy's shaping wins whenever every call in the trial's proxy ledger (`provider_usage.calls`) has `shaping_applied`. The forced literals (`x["temperature"] = 0.6`, `template["enable_thinking"] = True`, …) are read from `containers/zai_openapi_secret_proxy.py` at the run's commit. |
| `max_tokens` | agent kwargs `llm_call_kwargs.max_tokens` |
| `harness_config_digest` | Agent kwargs minus sampling. Terminus-2 constructor defaults are filled in (parser `json`, `enable_summarize`, `proactive_summarization_threshold` 8000, `max_turns`, `interleaved_thinking`, `trajectory_config.raw_content` / `linear_history`), so an omitted kwarg and its explicit default hash alike. Also hashed: the digests of the job's `harness-tree/` files other than `config.json` (rules, skills), and the spec's extra-instruction, toolbox and preamble digests. |
| `parser_digest` | MiMo route: a docstring-free AST digest of `src/evallab/mimo_tool_calls.py` at the run's commit, or `"none"` if that commit lacks the file. Other routes: `"none"`, since they have no normalizer. |
| `agent_timeout_seconds` | `agent.override_timeout_sec`, otherwise the task's `task.toml` `[agent] timeout_sec` × `agent_timeout_multiplier` (a task field) |
| `agent_timeout_multiplier`, `agent_timeout_override_seconds` | trial or job `config.json` |
| `timeout_seconds`, `max_requests`, `max_input_tokens`, `max_output_tokens`, `max_total_tokens`, `cost_limit_usd` | the job's prepared `experiment-spec.json` |

**Code at the run's commit.** Code-derived fields are read with `git show <commit>:<path>` from the local object store, at `lab-metadata.json` → `repository.commit`.

- If the tree was dirty at the run, or the commit is not in the object store, those fields are unknown. The run could have used uncommitted code.
- The repository commit itself is not a key field. A recording-only change therefore does not split a key:
  - source files are hashed as ASTs without docstrings, so comments, docstrings and layout don't count;
  - `harbor_terminus.py` also writes the trajectory, so it is deliberately not hashed.
- **Limitation:** a behaviour change in `harbor_terminus.py`'s MiMo wiring outside `mimo_tool_calls.py` would not split the parser digest. Keep MiMo response handling in `mimo_tool_calls.py`.

## Capture record

| Column | Meaning |
|---|---|
| `trajectory_head` | `agent/trajectory.json` exists |
| `continuation_indices`, `continuation_count` | which `trajectory.cont-N.json` files exist |
| `summarization_count` | `result.json` `agent_result.metadata.summarization_count`, or null |
| `continuations_missing` | continuation indices 1..`summarization_count` with no file, or null when the count is unknown |
| `summarization_files` | `trajectory.summarization-*.json` files |
| `n_episodes`, `trajectory_steps`, `trajectory_steps_unmetered` | Harbor's episode count, and the unique steps across head and continuations. Identical steps repeated in several files count once. Unmetered steps are agent steps without `metrics`. |
| `verifier_stdout`, `verifier_reward_file`, `recording_cast` | files present |
| `rollout_details` | length of `agent_result.rollout_details`. It is null when the key is absent; 0 means Harbor wrote an empty list. |
| `proxy_ledger`, `proxy_calls`, `proxy_calls_shaped`, `proxy_unresolved_requests` | from the proxy's per-call ledger (`lab-metadata.json` `provider_usage`), or null without one |
| `result_*_tokens` | `agent_result.n_input_tokens` / `n_output_tokens` |
| `trajectory_*_tokens` | sum of per-step `metrics` over unique main-trajectory steps plus summarization trajectories |
| `proxy_*_tokens` | ledger totals |
| `input_tokens_unattributed` | ledger input minus trajectory-attributed input. This is traffic the saved trajectories don't account for, such as continuation files that were overwritten. |

## Example (HAR-90, 2026-09-29)

The six `har90-mimo-0036*` trials are refused as a pool. They ran under four parser treatments:

- 0036 and 0036-b: dirty trees, so the parser digest is unknown;
- 0036-c and 0036-d: the #512 branch at 864d4555;
- 0036-e: e2d9a935, the Qwen3-Coder XML fix;
- 0036-f: #515 at a6a78026.

They also differ in `max_tokens`, the summarization threshold and the request and token ceilings.

`har90-mimo-0758-c` saved only `trajectory.cont-31.json` of 31 continuations. Its trajectories account for 5.5M of the ledger's 26.2M input tokens.
