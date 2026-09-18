---
status: draft
stage: 5
analysis_date: 2026-09-10
review_status: needs_human_review
---

# Stage 5 — FuncDAG composition and topological trace analysis

## Executive finding

**No prerequisite-order violation was found in the ten fully topology-checked model traces:** seven MCP trials and three adapted Codex synthetic trials. The observed failures are more specific:

- **MCP high-name-similarity seed2024:** correct tools and ordering, but `compute_unit_3` receives `factor=1` instead of the observed prerequisite value `-5`, producing a final23 rather than−39. Classify `tool_use` / value binding, not distractor selection or topological ordering.
- **Codex synthetic hard:** nine correct node records occur in a valid topological order. Reward0 comes from a verifier demanding one particular total order although the instruction requires only topological order. Classify **`verifier_false_negative`**, not capability failure.
- **Promoted synthetic easy Flash:** a diagnostic integer precedes the otherwise correct JSON object. This is an artifact-serialization `implementation` failure.

Depth-five MCP success is observed: GLM-5.3-Flash passes3/3 seeds and GLM-5.3 passes one seed42 canary. High-similarity depth-three Flash passes2/3. **These do not isolate a depth or name-similarity effect:** graph size, depth, names and tool-access route differ. All seven MCP attempts call zero distractor tools.

## Scope, validity, and denominators

The requested `runs/` tree has30 parseable FuncDAG trial results but no remaining mcp-funcdag trial directories. Those MCP jobs were promoted: `research/evidence/runs/zai-wave2-*/PROMOTION.json` records their original `runs/` paths. Following retained canonical bundles adds12 FuncDAG results. There are **42 distinct trial IDs** across these two roots:

| Disposition | N | Treatment |
|---|---:|---|
| Completed MCP model attempts | 7 | Full events/arguments/dependency-edge evaluation |
| Completed adapted synthetic Codex | 3 | Full output trace evaluation |
| Completed promoted synthetic easy Flash | 3 | Artifact/trajectory evaluation |
| Infrastructure/setup/cancellation | 16 | Excluded from capability denominator |
| Oracle/NOP controls | 13 | 11 Oracle rewards1; 2 NOP rewards0; not model capability |

Sixteen excluded local results comprise10 NonZeroAgentExitCodeError,3 CancelledError,2 Codex ValueError and1 Docker RuntimeError;14 OpenCode and2 Codex. A launch-failure zero is not a model failure, and exception class alone does not prove one common authentication cause. Codex F4mA4VR lacks a model name; Az2rApj rejects Docker's unsupported no-network mode.

The original `baseline-funcdag-easy-zai-opencode-k3` retains three trial directories without trial results: **incomplete, not three additional completed failures**. Its `-r2` contains three nonzero-exit results, all excluded. The inventory below lists all42 results and hashes. This bounded canonical-file cohort is not every historical FuncDAG record across all worktrees/Parquet partitions; aliases must not silently inflate it. All13 completed model attempts have Stage5 records below.

### Validation performed

- Parsed results; distinguished trial records from job roll-ups; checked UUID uniqueness.
- All **35 SHA-256 comparisons** for seven promoted MCP trials match PROMOTION.json: result, trajectory, target artifact, benchmark events and verifier stdout.
- Original MCP temporary task paths no longer exist. Retained `derived/harbor-tasks/mcp-funcdag/*/<task>/instruction.md` matches the promoted ATIF user-message SHA-256 in **7/7** cases. This proves instruction identity, not every regenerated environment/verifier byte. Historical task checksums and retained reference hashes are separately recorded.
- Checked MCP required-node coverage, tool mapping, arguments/results, distractor calls and predecessor availability; retained truth agrees with all seven historical verdicts/target values.
- Independently checked synthetic Codex traces for missing, duplicate, extra and unavailable-predecessor nodes, and compared complete node records to golden.
- ATIF JSON and step/call references were inspected; full external schema validation was not run. Cross-cohort environment-image/Harbor-lock equivalence is not claimed. Historical controls do not certify each differing cell digest.
- No source mutation, project-wide tests, lint, formatter, model trial, cloud sandbox or judge invocation.

## MCP depth and names

All seven use OpenCode1.18.25/ZAI coding-plan. Common declared factors: width2, two distractors, concise schemas, no drift, difficulty none. Depth5/low has9 required calls; depth3/high has5. Flash has seeds42/101/2024; GLM-5.3 is a separate seed42 canary.

| Condition | Model | Pass / attempted |
|---|---|---:|
| Depth5 / low similarity | GLM-5.3-Flash | 3/3 |
| Depth5 / low similarity | GLM-5.3 | 1/1 |
| Depth3 / high similarity | GLM-5.3-Flash | 2/3 |
| Depth3 / low, matched model/route | Unobserved here | 0 attempts |
| Depth5 / high, matched model/route | Unobserved here | 0 attempts |

3/3 versus2/3 is descriptive only. Shared-seed comparisons change depth and naming together. Seed2024 depth5/low correctly propagates factor−5, whereas depth3/high substitutes1; access route is HTTP-shell in the former and native batches in the latter. This disproves a blanket inability to use negative intermediates, not establishes name-similarity causality.

### Execution sequences

Numbers abbreviate compute_unit_N. Correct-call counts evaluate each call independently, not the verifier's correct-prefix metric.

| Trial suffix | Model | Seed | Depth / similarity | Reward | Correct calls | Observed execution mode |
|---|---|---:|---|---:|---:|---|
| `Ca9ToPk` | glm-5.3 | 42 | 5 / low | 1 | 9/9 | Native batches [1,2] [3,4] [5,6] [7,8] [9] |
| `PBZKQYS` | glm-5.3-flash | 42 | 5 / low | 1 | 9/9 | Native batches [1,2] [3,4] [5,6] [7,8] [9] |
| `gXLQWSh` | glm-5.3-flash | 2024 | 5 / low | 1 | 9/9 | Sequential HTTP shell calls: 1→2→3→4→5→6→7→8→9 |
| `TkhqHSN` | glm-5.3-flash | 101 | 3 / high | 1 | 5/5 | Shell background pairs [1,2] [4,3], wait barriers, then [5] |
| `sbQkE2R` | glm-5.3-flash | 42 | 3 / high | 1 | 5/5 | Sequential HTTP shell calls: 1→2→3→4→5 |
| `mJU4JQC` | glm-5.3-flash | 101 | 5 / low | 1 | 9/9 | Sequential HTTP shell calls: 1→2→3→4→5→6→7→8→9 |
| `DvpHJGh` | glm-5.3-flash | 2024 | 3 / high | 0 | 3/5 | Native batches [1,2] [3,4] [5]; wrong argument at 3 |

**Parallel versus sequential has two levels.** Native multi-call steps establish batched request intent, not physical overlap inside the server. Ca9ToPk/PBZKQYS/DvpHJGh batch independent siblings then sequence dependent layers. TkhqHSN uses shell `&` followed by `wait` at layer barriers. The other three synchronously invoke `curl` sequentially, even when two node calls share one bash tool call. Server events have ordinals but no start/end timing; actual overlap cannot be quantified. TkhqHSN submits independent units4 then3; server events record3 then4. Both orders satisfy dependencies.

Across seven attempts: **51 successful MCP events;49 independently correct calls;2 wrong expected bindings/results;0 distractor calls;0 missing nodes;0 repeats;0 prerequisite-order violations.** Every tool returns status ok, which does not establish semantic correctness.

### Earliest failure: high-similarity seed2024, DvpHJGh

Source: `research/evidence/runs/zai-wave2-flash-matrix/mcp-funcdag-name_similarity_high__DvpHJGh/`.

| ATIF step / event | Node / tool | Required | Observed | Assessment |
|---|---|---|---|---|
| 2 / 1 | node_0_0 / unit1 | 9+4=13 | 13 | Correct |
| 2 / 2 | node_0_1 / unit2 | 4−9=−5 | −5 | Correct prerequisite available |
| 3 / 3 | node_1_0 / unit3 | base13, **factor−5** →−62 | base13, **factor1** →16 | First executed wrong binding |
| 3 / 4 | node_1_1 / unit4 | val−5, offset13 →16 | 16 | Independently correct |
| 4 / 5 | node_2_0 / unit5 | u−62, v16 →−39 | u16, v16 →23 | Propagates wrong upstream value |
| 5 | write | target_value−39 | target_value23 | Records wrong computation |

Decisive call: `call_423c88a1db1b43d8b1b0c1c4`, step3. Step2 already plans with factor1; step3 acknowledges both observed predecessor values but passes1. Step3 is the earliest externally executed wrong tool action. No missing-parent call, wrong tool name, transport error, refusal or retry appears.

Historical stdout says `dag_conf=False, val_prop=0.4`. **This is not an ordering-error metric.** Retained verifier logic combines tool identity, arguments, outputs and list sequence; it stops at the first mismatch. Two correct prefix calls yield2/5=0.4, although unit4 is independently correct too:3/5=0.6. Never translate0.4 into “60% topological errors.”

Classification: valid attempt; primary tool_use, value-binding subtype. Confidence high about mechanism, low about name-similarity causality. Alternatives: dependency-reference misreading or persistence of an early computed plan. Neither name-similar distractor compute_context_1/2 was called.

## Synthetic Function-DAG depth and verifier rejection

These tasks expose Python source and require JSON traces, **not MCP discovery/selection**. Their rewards cannot be pooled into MCP depth/name rates.

| Adapted Codex trial | Declared depth | Actual target computational depth | Ancestor nodes | Explicit distractors | Historical reward | Independent result |
|---|---:|---:|---:|---:|---:|---|
| PmTen2E easy | 2 | 1 | 1 | 0 | 1 | Valid |
| NoqKuag medium | 4 | 3 | 3 | 2 | 1 | Valid |
| kziNARo hard | 6 | **5** | 9 | 4 | **0** | Valid; verifier false negative |

All use Codex0.153.4/gpt-5.6-terra. Seeds, widths, node counts and distractors vary together: exploratory ladder, not controlled depth. Easy n_2_0 directly consumes in_1. Medium chain is n_1_0→n_2_1→n_4_0. Hard has five computational nodes on its longest target path, despite layer6 naming.

### Hard: valid topological permutation

Source: `runs/funcdag-codex-canary/trials-hard/adapted-syn-funcdag-hard__kziNARo/`.

Observed:

```text
n_1_1=26, n_1_3=3, n_2_1=15, n_3_0=-11,
n_2_2=112, n_2_3=20, n_3_2=20, n_4_3=-220, n_6_0=9
```

Required internal edges:

```text
n_1_3 → n_2_1
n_2_1 + n_1_1 → n_3_0
n_2_3 + n_2_2 → n_3_2
n_3_0 + n_3_2 → n_4_3 → n_6_0
```

All predecessors precede consumers. n_3_0 has no dependency on n_2_2/n_2_3. Golden lists those two nodes before n_3_0. **First list mismatch: position4 (one-based), not an unsatisfied dependency.** All9 node-keyed records, ordered inputs and integer values are identical to golden.

Preserved `adapted-syn-funcdag-hard/instruction.md:35–39` requires each ancestor once “in topological order,” not global layer/name sorting. `tests/verify.py:32–45,87–93` zip-compares lists against GOLDEN. This rejects a valid output relative to the stated task. An undocumented canonical-order rule explains implementation only, not the visible contract.

ATIF step8 (`call_KuTTBbhuRw8xtSii5eac8HT6`) writes the trace. Step9 (`call_RWbIU5E3gTI10QsJVyoFreE6`) loads actual Python functions, checks ancestor coverage, ordered input values and every function result, and prints `validated: 9 nodes; target value: 9`. A trailing jq command is unavailable; Python validation already succeeded. Missing jq does not cause the verifier mismatch. Host-side independent edge/node-record checks also pass.

Codex uses sequential outer exec tools (easy2; medium/hard4 each). Easy has several awaited inner actions, not concurrent execution. Medium/hard function checks are sequential Python. There is no evidence of parallel DAG computations here; output order alone does not show execution timing.

## Repeated synthetic easy Flash

Bundle: `research/evidence/runs/zai-flash-funcdag-easy-r3-20260829/`. Flash passes2/3. mtesCiD, h2hcQHH and failed AfLvCrL all compute n_2_0=3. Step2 batches three independent file reads: batched inspection, **not parallel DAG evaluation**.

AfLvCrL step3 prints a diagnostic integer and then JSON into one redirected file and copies it to the required output. Artifact begins `3
{...}`. Strict decoding reports `Extra data: line 2 column 1 (char 2)`. Rejection is appropriate. Primary implementation/serialization; missing final artifact parsing contributes verification_behavior. Passes separate diagnostic execution and structured writing. No topological failure is supported.

## Synthesis and discriminators

Among13 completed model attempts: **10 accepted successes,1 tool-value-binding failure,1 serialization failure,1 verifier false negative**. This is failure accounting across distinct cohorts, not an aggregate benchmark pass rate.

1. **Offline verifier discriminator:** freeze hard task/output; compare dependency-edge validation with exact-list acceptance and a true parent/child swap negative control. The independent check here accepts the actual trace; historical verdict remains unchanged. No model spend required.
2. **Name similarity, approval required:** seed2024 depth3, same Flash/OpenCode/native route, width2,2 distractors, concise/no-drift schema and identical dependency graph. Change only low versus high distractor-name similarity. Initially1 paired attempt per condition, concurrency1,300s/attempt,max2 attempts; stop on infrastructure/trace-integrity failure. Name confusion predicts wrong distractor choices or repeated high-only degradation; binding noise need not depend on names. **Not executed; an explicit paid cost ceiling is required before admission.**
3. **Depth, separately approved:** low names/model/route/seed fixed, depth3 versus5 with nested graph construction;1 attempt/cell,concurrency1,300s each,stop on invalid evidence. Do not also change names. Approved monetary ceiling is an admission prerequisite.
4. **Serialization:** strict-parse archived AfLvCrL output; diagnosis needs no model retry. Keep diagnostic output outside JSON artifact streams.

Native batching versus shell successes does not identify a parallelism effect. The failed call preserves prerequisite timing. Retained MCP verifier also insists on exact event order; a valid sibling reorder could be rejected in another trial. That is a verifier-design risk, **not** the cause of DvpHJGh's wrong value and not an observed MCP false negative here.

Human review remains pending. No source run, task, verifier, reward or policy changed.

## Provenance and structured sidecars

Bounded analysis brief used for prompt digest:

> Analyze tool-use and DAG evaluation performance across depth and name similarity; trace topological failures and parallel versus sequential calls; write Stage 5 FuncDAG composition analysis.

```json
{
  "schema_version": 1,
  "analysis_id": "598bdf31-c47d-4f22-8da9-cea8c9977960",
  "analysis_provenance": {
    "agent": "FuncDAGAnalyst/native OMP task",
    "agent_version": null,
    "model": "devin/gpt-6-astra",
    "prompt_digest": "sha256:980763c176ac8a0ae2eb9cde353436e613419d6c02e401fd1cbac769b434e104",
    "prompt_digest_scope": "bounded brief reproduced below",
    "rubric_digest": "sha256:a9a2f6a7763621408c6964f4bdeace731375b496f051e4386b53b879cc0e32b5",
    "created_at": "2026-09-10T21:57:49.766533+00:00",
    "cost_usd": null
  },
  "review_status": "needs_human_review",
  "canonical_results": 42,
  "analyzed_model_attempts": 13,
  "excluded_infrastructure_or_setup": 16,
  "controls": 13,
  "incomplete_baseline_directories": 3
}
```

Unknown agent version and cost_usd:null are deliberate; no invented zero/version. Historical task checksums differ from retained reference-file digests. Evidence paths inside each record resolve under source_path.

### Per-attempt Stage5 records

```json
[
  {
    "source_trial_id": "2fb983b0-eb31-4f96-8d37-dbf5950923c2",
    "source_path": "research/evidence/runs/zai-wave2-glm53-funcdag-canary/mcp-funcdag-depth_5-seed42__Ca9ToPk",
    "source_digests": {
      "result": "sha256:844c440b30ae032b13c0c3348325d9f5bf4a046393234a6350c89b1cc369594e",
      "trajectory": "sha256:15ff55ab35e3a368256e15d2854db96c021d6d159c8dbc542a92358a643ffd30",
      "events": "sha256:6290137aa5a9c9129102b4eac6d2a0434dac051f2186256239d38edbfeb4adf3",
      "task_checksum_recorded": "b618f2b339a295299700808169c3f3cbc6152d3f6b3b417ac783ad4bae208a0f",
      "matched_instruction": "sha256:99f608907af15b5fe45a1dcc4b36e11d383a2152d135f147139de2bf1e28525d",
      "reference_truth": "sha256:80a9eb6fc985ca2c4afcb5a9014fb8793f9248061bf15928bc9cb0762b159cb9"
    },
    "validity": "valid_agent_attempt",
    "primary_category": null,
    "summary": "All required tool calls, bound arguments and outputs match reference; no missing or distractor nodes.",
    "evidence": [
      {
        "path": "artifacts/app/output/benchmark-events.jsonl",
        "supports": "Complete success event sequence checked against dependency edges and reference calls."
      }
    ],
    "alternative_explanations": [],
    "proposed_discriminator": null,
    "confidence": "high",
    "deterministic_facts": {
      "trial_name": "mcp-funcdag-depth_5-seed42__Ca9ToPk",
      "source_trial_id": "2fb983b0-eb31-4f96-8d37-dbf5950923c2",
      "task": "evallab/mcp-funcdag-depth_5-seed42",
      "factors": {
        "depth": 5,
        "difficulty": "none",
        "distractor_count": 2,
        "name_similarity": "low",
        "schema_drift": false,
        "schema_token_volume": "concise",
        "seed": 42,
        "width": 2
      },
      "reward": 1.0,
      "tool_calls": 9,
      "correct_independent_calls": 9,
      "wrong_argument_or_value_ordinals": [],
      "topological_violations": [],
      "missing_nodes": [],
      "distractor_calls": 0,
      "atif_tool_calls": 10,
      "atif_multi_call_steps": [
        2,
        3,
        4,
        5
      ],
      "total_prompt_tokens": 68823,
      "total_completion_tokens": 395
    }
  },
  {
    "source_trial_id": "068bb919-9cb8-4292-bffd-bf73adc44b01",
    "source_path": "research/evidence/runs/zai-wave2-funcdag-depth5-s42/mcp-funcdag-depth_5-seed42__PBZKQYS",
    "source_digests": {
      "result": "sha256:c27e95e89ae80b8e6fd41914b90301cb95a818593fa97f6eb15d7ef5ccf38472",
      "trajectory": "sha256:c9b6a1259905e9091034ac361cb71624807d2f160f202e3b944265fa29dcb5d5",
      "events": "sha256:6290137aa5a9c9129102b4eac6d2a0434dac051f2186256239d38edbfeb4adf3",
      "task_checksum_recorded": "b618f2b339a295299700808169c3f3cbc6152d3f6b3b417ac783ad4bae208a0f",
      "matched_instruction": "sha256:99f608907af15b5fe45a1dcc4b36e11d383a2152d135f147139de2bf1e28525d",
      "reference_truth": "sha256:80a9eb6fc985ca2c4afcb5a9014fb8793f9248061bf15928bc9cb0762b159cb9"
    },
    "validity": "valid_agent_attempt",
    "primary_category": null,
    "summary": "All required tool calls, bound arguments and outputs match reference; no missing or distractor nodes.",
    "evidence": [
      {
        "path": "artifacts/app/output/benchmark-events.jsonl",
        "supports": "Complete success event sequence checked against dependency edges and reference calls."
      }
    ],
    "alternative_explanations": [],
    "proposed_discriminator": null,
    "confidence": "high",
    "deterministic_facts": {
      "trial_name": "mcp-funcdag-depth_5-seed42__PBZKQYS",
      "source_trial_id": "068bb919-9cb8-4292-bffd-bf73adc44b01",
      "task": "evallab/mcp-funcdag-depth_5-seed42",
      "factors": {
        "depth": 5,
        "difficulty": "none",
        "distractor_count": 2,
        "name_similarity": "low",
        "schema_drift": false,
        "schema_token_volume": "concise",
        "seed": 42,
        "width": 2
      },
      "reward": 1.0,
      "tool_calls": 9,
      "correct_independent_calls": 9,
      "wrong_argument_or_value_ordinals": [],
      "topological_violations": [],
      "missing_nodes": [],
      "distractor_calls": 0,
      "atif_tool_calls": 10,
      "atif_multi_call_steps": [
        2,
        3,
        4,
        5
      ],
      "total_prompt_tokens": 68971,
      "total_completion_tokens": 477
    }
  },
  {
    "source_trial_id": "27694a47-ee5b-4c99-9171-64b4af607121",
    "source_path": "research/evidence/runs/zai-wave2-flash-matrix/mcp-funcdag-depth_5-seed2024__gXLQWSh",
    "source_digests": {
      "result": "sha256:114944e5f2e2709e013074718e6f8a551f789ebce2cba7adec7c428992f1baec",
      "trajectory": "sha256:fb2146e22ce7ed4b5c1888a4b1f61bedb76748afc625f3eac9f66daca3594d5b",
      "events": "sha256:5cb063730a67ea47a2b707af2d421af5f7cbf3d0bdabdb9723603610e4fb4c6a",
      "task_checksum_recorded": "7f73f63aebc6a58a1445762e1179a34097a1f49d8e5fe178d53a6b5c8ad2f76a",
      "matched_instruction": "sha256:f54e1bf56bf8f0b3566ace7959d7079e1869f22dcaaed6ed82c62dfe9007876a",
      "reference_truth": "sha256:8942cf56d50092c7f30f1b41dc6fff1af2f6d6d68f4aa2bfc73a044bcbcdf859"
    },
    "validity": "valid_agent_attempt",
    "primary_category": null,
    "summary": "All required tool calls, bound arguments and outputs match reference; no missing or distractor nodes.",
    "evidence": [
      {
        "path": "artifacts/app/output/benchmark-events.jsonl",
        "supports": "Complete success event sequence checked against dependency edges and reference calls."
      }
    ],
    "alternative_explanations": [],
    "proposed_discriminator": null,
    "confidence": "high",
    "deterministic_facts": {
      "trial_name": "mcp-funcdag-depth_5-seed2024__gXLQWSh",
      "source_trial_id": "27694a47-ee5b-4c99-9171-64b4af607121",
      "task": "evallab/mcp-funcdag-depth_5-seed2024",
      "factors": {
        "depth": 5,
        "difficulty": "none",
        "distractor_count": 2,
        "name_similarity": "low",
        "schema_drift": false,
        "schema_token_volume": "concise",
        "seed": 2024,
        "width": 2
      },
      "reward": 1.0,
      "tool_calls": 9,
      "correct_independent_calls": 9,
      "wrong_argument_or_value_ordinals": [],
      "topological_violations": [],
      "missing_nodes": [],
      "distractor_calls": 0,
      "atif_tool_calls": 9,
      "atif_multi_call_steps": [],
      "total_prompt_tokens": 109943,
      "total_completion_tokens": 1477
    }
  },
  {
    "source_trial_id": "6fe8a323-0251-477f-aabd-9e5d34d9a008",
    "source_path": "research/evidence/runs/zai-wave2-flash-matrix/mcp-funcdag-name_similarity_high__TkhqHSN",
    "source_digests": {
      "result": "sha256:8eda659f09e0b50f24e33ea03a473eb0b0c6c24732a3ef53d6079d06791fb213",
      "trajectory": "sha256:186a149cfb4e97c894731cb3e4979acd2d52e0bec2b9efc28be0667d519a6807",
      "events": "sha256:97533d18a651109d33379da1cf3f0cf3be26e470e84672bac6edf32dfd220755",
      "task_checksum_recorded": "773b595487f74fead36c848c3e00b3c60ffc6be1e32adc66a90a8c79b975cfdf",
      "matched_instruction": "sha256:572f7e60edcdbf8fbe34a19076c1f88442484ee78802f5f22e459b578331cf97",
      "reference_truth": "sha256:dcc41a8eac009eb84706edb376c5ce2d3679e3419cd2dfebef303139124def4d"
    },
    "validity": "valid_agent_attempt",
    "primary_category": null,
    "summary": "All required tool calls, bound arguments and outputs match reference; no missing or distractor nodes.",
    "evidence": [
      {
        "path": "artifacts/app/output/benchmark-events.jsonl",
        "supports": "Complete success event sequence checked against dependency edges and reference calls."
      }
    ],
    "alternative_explanations": [],
    "proposed_discriminator": null,
    "confidence": "high",
    "deterministic_facts": {
      "trial_name": "mcp-funcdag-name_similarity_high__TkhqHSN",
      "source_trial_id": "6fe8a323-0251-477f-aabd-9e5d34d9a008",
      "task": "evallab/mcp-funcdag-name_similarity_high-seed101",
      "factors": {
        "depth": 3,
        "difficulty": "none",
        "distractor_count": 2,
        "name_similarity": "high",
        "schema_drift": false,
        "schema_token_volume": "concise",
        "seed": 101,
        "width": 2
      },
      "reward": 1.0,
      "tool_calls": 5,
      "correct_independent_calls": 5,
      "wrong_argument_or_value_ordinals": [],
      "topological_violations": [],
      "missing_nodes": [],
      "distractor_calls": 0,
      "atif_tool_calls": 6,
      "atif_multi_call_steps": [],
      "total_prompt_tokens": 69235,
      "total_completion_tokens": 934
    }
  },
  {
    "source_trial_id": "66790181-86d2-42b2-9307-cdfca40e40aa",
    "source_path": "research/evidence/runs/zai-wave2-flash-matrix/mcp-funcdag-name_similarity_high__sbQkE2R",
    "source_digests": {
      "result": "sha256:69a3ba0f3fea8410a80e180e3194dad0fabe5e015f7bcc1540faee84a41ea1b6",
      "trajectory": "sha256:89a38cc2721e519b0d746f23f7ac394459447c7955f8f0d69f1538d29a5af0f3",
      "events": "sha256:0bbec02a2aded7fe09bc7225e8b5d4c7d6a24414cc6b636b19ac095d6cce7702",
      "task_checksum_recorded": "8ef9c66edc29366bdc4d512fd2188df9337a9fe95cb4f0697dc44575fc76c98d",
      "matched_instruction": "sha256:b549170340f5b928d73cb8919516402779e5ef2449c4b38a0c5c8f5b10ab95f3",
      "reference_truth": "sha256:452c4aa69ba81665762c7541e2aff7b0348fef6a978c3f93a15731db4914f39f"
    },
    "validity": "valid_agent_attempt",
    "primary_category": null,
    "summary": "All required tool calls, bound arguments and outputs match reference; no missing or distractor nodes.",
    "evidence": [
      {
        "path": "artifacts/app/output/benchmark-events.jsonl",
        "supports": "Complete success event sequence checked against dependency edges and reference calls."
      }
    ],
    "alternative_explanations": [],
    "proposed_discriminator": null,
    "confidence": "high",
    "deterministic_facts": {
      "trial_name": "mcp-funcdag-name_similarity_high__sbQkE2R",
      "source_trial_id": "66790181-86d2-42b2-9307-cdfca40e40aa",
      "task": "evallab/mcp-funcdag-name_similarity_high-seed42",
      "factors": {
        "depth": 3,
        "difficulty": "none",
        "distractor_count": 2,
        "name_similarity": "high",
        "schema_drift": false,
        "schema_token_volume": "concise",
        "seed": 42,
        "width": 2
      },
      "reward": 1.0,
      "tool_calls": 5,
      "correct_independent_calls": 5,
      "wrong_argument_or_value_ordinals": [],
      "topological_violations": [],
      "missing_nodes": [],
      "distractor_calls": 0,
      "atif_tool_calls": 6,
      "atif_multi_call_steps": [],
      "total_prompt_tokens": 69455,
      "total_completion_tokens": 1198
    }
  },
  {
    "source_trial_id": "cd810b29-b752-4c6c-bf15-01ceaf0e4d53",
    "source_path": "research/evidence/runs/zai-wave2-flash-matrix/mcp-funcdag-depth_5-seed101__mJU4JQC",
    "source_digests": {
      "result": "sha256:2b8ef497bd65021777ac766c4aa36e371608cea953241120626dc69be7ec8a65",
      "trajectory": "sha256:d3a1af561372d37abe4972ea48506b7e397ac5fae8af5dd2b3fb3e1a006b08c4",
      "events": "sha256:84e6d263b6d8f708613c5c2718c046e70635b16e50ddae5f199c8d8fb7534eb0",
      "task_checksum_recorded": "f2bc66b14cad67ca506befccbcc839d0cf35526b78c4b78aeeaded75497846df",
      "matched_instruction": "sha256:753c9256617a77d383fc4c2ce881abdd05a0c9a325a0f4fc0c893f58897f6093",
      "reference_truth": "sha256:7e4608b188e6507a6aefad29741dcb4a1194d5f6d0ecc54ac2f23129c669b1ec"
    },
    "validity": "valid_agent_attempt",
    "primary_category": null,
    "summary": "All required tool calls, bound arguments and outputs match reference; no missing or distractor nodes.",
    "evidence": [
      {
        "path": "artifacts/app/output/benchmark-events.jsonl",
        "supports": "Complete success event sequence checked against dependency edges and reference calls."
      }
    ],
    "alternative_explanations": [],
    "proposed_discriminator": null,
    "confidence": "high",
    "deterministic_facts": {
      "trial_name": "mcp-funcdag-depth_5-seed101__mJU4JQC",
      "source_trial_id": "cd810b29-b752-4c6c-bf15-01ceaf0e4d53",
      "task": "evallab/mcp-funcdag-depth_5-seed101",
      "factors": {
        "depth": 5,
        "difficulty": "none",
        "distractor_count": 2,
        "name_similarity": "low",
        "schema_drift": false,
        "schema_token_volume": "concise",
        "seed": 101,
        "width": 2
      },
      "reward": 1.0,
      "tool_calls": 9,
      "correct_independent_calls": 9,
      "wrong_argument_or_value_ordinals": [],
      "topological_violations": [],
      "missing_nodes": [],
      "distractor_calls": 0,
      "atif_tool_calls": 9,
      "atif_multi_call_steps": [],
      "total_prompt_tokens": 116583,
      "total_completion_tokens": 1829
    }
  },
  {
    "source_trial_id": "b8b93e08-a182-40ff-a22d-7980820805b9",
    "source_path": "research/evidence/runs/zai-wave2-flash-matrix/mcp-funcdag-name_similarity_high__DvpHJGh",
    "source_digests": {
      "result": "sha256:f133e1e9d4ef67f55b0450440a47785710fa8a99a533889f5bed418995e66b27",
      "trajectory": "sha256:94875c7d5ebeb90f28e95f03821d984a6899628612dec85782cdf1f75c3648bb",
      "events": "sha256:3542db167ef5e91953f74335b932b5cef08ed22e1bd718aa02c3019f373d283f",
      "task_checksum_recorded": "3b61174aa37da0d5fd6167a351f0584b286f543f6de70e5fac36187949b71bfb",
      "matched_instruction": "sha256:9345178658e016e5943b355179668911cd9960a66c9de34f66505d3668d2c883",
      "reference_truth": "sha256:1ce80f4c94f3cbdaeccde1cb58e107b37a75b549647085d5ac19ab1519752719"
    },
    "validity": "valid_agent_attempt",
    "primary_category": "tool_use",
    "summary": "Correct tools and prerequisite ordering; wrong bound argument factor=1 instead of -5 at first dependent layer.",
    "evidence": [
      {
        "path": "agent/trajectory.json",
        "step_id": 3,
        "tool_call_id": "call_423c88a1db1b43d8b1b0c1c4",
        "supports": "First externally executed wrong binding; factor must be prior node_0_1 output -5."
      }
    ],
    "alternative_explanations": [
      "Misreading the dependency reference or carrying a precomputed plan forward; name-similarity causality is not established."
    ],
    "proposed_discriminator": "Same depth3 seed2024 graph/model/native route with name_similarity low versus high; no other factor changed.",
    "confidence": "high",
    "deterministic_facts": {
      "trial_name": "mcp-funcdag-name_similarity_high__DvpHJGh",
      "source_trial_id": "b8b93e08-a182-40ff-a22d-7980820805b9",
      "task": "evallab/mcp-funcdag-name_similarity_high-seed2024",
      "factors": {
        "depth": 3,
        "difficulty": "none",
        "distractor_count": 2,
        "name_similarity": "high",
        "schema_drift": false,
        "schema_token_volume": "concise",
        "seed": 2024,
        "width": 2
      },
      "reward": 0.0,
      "tool_calls": 5,
      "correct_independent_calls": 3,
      "wrong_argument_or_value_ordinals": [
        3,
        5
      ],
      "topological_violations": [],
      "missing_nodes": [],
      "distractor_calls": 0,
      "atif_tool_calls": 6,
      "atif_multi_call_steps": [
        2,
        3
      ],
      "total_prompt_tokens": 43138,
      "total_completion_tokens": 412
    }
  },
  {
    "source_trial_id": "d50dc421-93cc-48a8-9a1e-cff895838cec",
    "source_path": "runs/funcdag-codex-canary/trials-hard/adapted-syn-funcdag-hard__kziNARo",
    "source_digests": {
      "result": "sha256:49aa214c01fa98b98fba39c2ec357cadf51fd476ddd88aba64063c500eaa623e",
      "trajectory": "sha256:fcb9ae38d7893f42c8af9e03d2de236cab6b5ebc06dc92eba9e9454c952145a8",
      "task_checksum_recorded": "95a54cb43477381b694f9c4cfd1e79831af4e9446b809a95ef071b809e38d322",
      "instruction": "sha256:7837fe16b8f067a9e1b3ea034afeec719937ad767975a57edd3f3b34f77a6af7",
      "verifier": "sha256:d11cc47ef497475631cf242683fa6737ffb0b5d85923096fd14292ccf48f64be",
      "output": "sha256:eaf1b70d0fe1457cb525e5de3b3ff27aa174f52bced983f896d20ac2095dbc4f"
    },
    "validity": "valid_agent_attempt_verifier_false_negative",
    "primary_category": "verifier_false_negative",
    "summary": "Valid alternative topological order rejected by exact list comparison.",
    "evidence": [
      {
        "path": "artifacts/app/output/result.json",
        "supports": "All required nodes once, zero predecessor violations, identical node records to golden."
      },
      {
        "path": "verifier/result.json",
        "supports": "Reward zero only for hard ordering permutation; successful verdict for easy/medium."
      }
    ],
    "alternative_explanations": [
      "An undocumented canonical-order requirement would explain the verdict but is absent from the preserved instruction."
    ],
    "proposed_discriminator": "Offline regrade archived hard output using dependency-edge validation instead of total list equality.",
    "confidence": "high",
    "deterministic_facts": {
      "trial": "adapted-syn-funcdag-hard__kziNARo",
      "declared_depth": 6,
      "effective_target_depth": 5,
      "ancestry_nodes": 9,
      "observed_nodes": 9,
      "missing": [],
      "extra": [],
      "topological_violations": [],
      "node_records_equal": true,
      "exact_golden_equal": false
    }
  },
  {
    "source_trial_id": "86eb12d5-08eb-4fdf-9343-7a4e327872d4",
    "source_path": "runs/funcdag-codex-canary/trials-medium/adapted-syn-funcdag-medium__NoqKuag",
    "source_digests": {
      "result": "sha256:fefb1d2a061137df3c746b01d7e1662452826e39bee84c601145f8d8fda25142",
      "trajectory": "sha256:0688c4b4dc1c2fc7315eb3681ed053dd39060fa55058a308bd45898709f99501",
      "task_checksum_recorded": "215d97ffdc68f8448fcac3c19b929b3698c34dace233edd22dd7e85ba3d184d2",
      "instruction": "sha256:a16171a9ac79a7fb23b0df214688cdccaa9b2639c3d972118e27d646a92affdb",
      "verifier": "sha256:5066daf370a6dde0e319a450aa6315cba6592e6adc598d8798ea236e8ba1c05d",
      "output": "sha256:6cf23d652f5768226e4777b4244698af4959cc061d6d6852365322cc3016e160"
    },
    "validity": "valid_agent_attempt",
    "primary_category": null,
    "summary": "Exact trace accepted; no ordering or arithmetic errors.",
    "evidence": [
      {
        "path": "artifacts/app/output/result.json",
        "supports": "All required nodes once, zero predecessor violations, identical node records to golden."
      },
      {
        "path": "verifier/result.json",
        "supports": "Reward zero only for hard ordering permutation; successful verdict for easy/medium."
      }
    ],
    "alternative_explanations": [],
    "proposed_discriminator": null,
    "confidence": "high",
    "deterministic_facts": {
      "trial": "adapted-syn-funcdag-medium__NoqKuag",
      "declared_depth": 4,
      "effective_target_depth": 3,
      "ancestry_nodes": 3,
      "observed_nodes": 3,
      "missing": [],
      "extra": [],
      "topological_violations": [],
      "node_records_equal": true,
      "exact_golden_equal": true
    }
  },
  {
    "source_trial_id": "72d21d01-8cf1-4c86-afc9-399d5398b854",
    "source_path": "runs/funcdag-codex-canary/trials/adapted-task__PmTen2E",
    "source_digests": {
      "result": "sha256:1e39239556412ed93a14bbbdcd98942697112a4086beee367fbc1ef7fdede571",
      "trajectory": "sha256:07154b27db3145add1aff02bbedd0c14978b035d00e151942d88dd2533fbe2f5",
      "task_checksum_recorded": "b8100361065a0ec0c0b23368873dd880f1cbbbbaf5e7c0e593c3360d723d0644",
      "instruction": "sha256:264c98f3508cef3bf894bab59445aaab95b6c541f27dc39d8e95034930c0acbb",
      "verifier": "sha256:be8ae2d55d47e0321650287f12a48f0c69c8756336e7a94ae48a1bd15765531f",
      "output": "sha256:2b3f9d1362daf50faf9110274b80e7523affbdf2684c88196e9c4999f4c46c9f"
    },
    "validity": "valid_agent_attempt",
    "primary_category": null,
    "summary": "Exact trace accepted; no ordering or arithmetic errors.",
    "evidence": [
      {
        "path": "artifacts/app/output/result.json",
        "supports": "All required nodes once, zero predecessor violations, identical node records to golden."
      },
      {
        "path": "verifier/result.json",
        "supports": "Reward zero only for hard ordering permutation; successful verdict for easy/medium."
      }
    ],
    "alternative_explanations": [],
    "proposed_discriminator": null,
    "confidence": "high",
    "deterministic_facts": {
      "trial": "adapted-task__PmTen2E",
      "declared_depth": 2,
      "effective_target_depth": 1,
      "ancestry_nodes": 1,
      "observed_nodes": 1,
      "missing": [],
      "extra": [],
      "topological_violations": [],
      "node_records_equal": true,
      "exact_golden_equal": true
    }
  },
  {
    "source_trial_id": "b08a8e96-90d6-4419-b200-d5d53500f5a5",
    "source_path": "research/evidence/runs/zai-flash-funcdag-easy-r3-20260829/evallab-zai-syn-funcdag-easy__AfLvCrL",
    "source_digests": {
      "result": "sha256:44b03840b8b3aa77afe82bdac9085fbda165b7f97fae63fe430e22eec102f195",
      "trajectory": "sha256:1d9a92b8c41148ed257a4a00cc1d240ae913faaf92b559443bb8f8d18d0849c0",
      "output": "sha256:22ee14dfd8df2499e9ba5845410f1cb6e93285632437956c8cac618be1017331",
      "task_checksum_recorded": "cae348401d1f4c0f7b1f8b75c1d799d2f2e8ab74150e30e74ed06b8a65c5f5ce"
    },
    "validity": "valid_agent_attempt",
    "primary_category": "implementation",
    "summary": "An extra printed integer precedes the JSON object; correct computed value but invalid artifact.",
    "evidence": [
      {
        "path": "agent/trajectory.json",
        "step_id": 3,
        "supports": "Execution writes diagnostic integer plus JSON to the same redirected file."
      }
    ],
    "alternative_explanations": [],
    "proposed_discriminator": "Offline parse archived file as one JSON document; keep diagnostics separate from artifact output.",
    "confidence": "high"
  },
  {
    "source_trial_id": "86ae0d4a-2e98-446d-ac38-96c8678d2943",
    "source_path": "research/evidence/runs/zai-flash-funcdag-easy-r3-20260829/evallab-zai-syn-funcdag-easy__mtesCiD",
    "source_digests": {
      "result": "sha256:2e84cd114aef19c6ea3881a43337e9539fd1bc068a86ebf88b6ca230d523dc96",
      "trajectory": "sha256:612aa141c2693434ed1646719c1321b85041c75323ee954a6c40556ae91a753b",
      "output": "sha256:2b3f9d1362daf50faf9110274b80e7523affbdf2684c88196e9c4999f4c46c9f",
      "task_checksum_recorded": "cae348401d1f4c0f7b1f8b75c1d799d2f2e8ab74150e30e74ed06b8a65c5f5ce"
    },
    "validity": "valid_agent_attempt",
    "primary_category": null,
    "summary": "One-node target trace and value accepted.",
    "evidence": [
      {
        "path": "verifier/result.json",
        "supports": "Trace target n_2_0=3 accepted."
      }
    ],
    "alternative_explanations": [],
    "proposed_discriminator": null,
    "confidence": "high"
  },
  {
    "source_trial_id": "72f691a4-7677-4319-b3f2-0a4bd224c95a",
    "source_path": "research/evidence/runs/zai-flash-funcdag-easy-r3-20260829/evallab-zai-syn-funcdag-easy__h2hcQHH",
    "source_digests": {
      "result": "sha256:a3e7606246e94eab00cbc26bb996b335e788cb676930782a7bc83cc474955f63",
      "trajectory": "sha256:b214ea139ca569973d4eb63380c924b5c054e32a443112887326ed87e7e9d463",
      "output": "sha256:2b3f9d1362daf50faf9110274b80e7523affbdf2684c88196e9c4999f4c46c9f",
      "task_checksum_recorded": "cae348401d1f4c0f7b1f8b75c1d799d2f2e8ab74150e30e74ed06b8a65c5f5ce"
    },
    "validity": "valid_agent_attempt",
    "primary_category": null,
    "summary": "One-node target trace and value accepted.",
    "evidence": [
      {
        "path": "verifier/result.json",
        "supports": "Trace target n_2_0=3 accepted."
      }
    ],
    "alternative_explanations": [],
    "proposed_discriminator": null,
    "confidence": "high"
  }
]
```

### Complete canonical result inventory

Repository-relative paths. Null reward means absent, not zero. Controls are historical, not newly run.

```json
[
  {
    "source_trial_id": "bb573ff5-d65b-49be-979a-ba183dfbcdda",
    "path": "research/evidence/runs/syn-funcdag-easy-registry-nop-33133765977/syn-funcdag-easy__peGyXfC",
    "agent": "nop",
    "reward": 0.0,
    "exception_type": null,
    "result_sha256": "sha256:20aa049c4e8b6ba51d99d1167ca5b1d99677233e2258c34420078289f2b11c88",
    "disposition": "control_not_model_capability"
  },
  {
    "source_trial_id": "8ff8d4e4-8b0c-4098-81cb-1ed7f469ac98",
    "path": "research/evidence/runs/syn-funcdag-easy-registry-oracle-33133765977/syn-funcdag-easy__KVthvbs",
    "agent": "oracle",
    "reward": 1.0,
    "exception_type": null,
    "result_sha256": "sha256:3cd5889794c977b2076aa3bf8371ccdd33a91e2db49a0cb81719397f1bc7f0bd",
    "disposition": "control_not_model_capability"
  },
  {
    "source_trial_id": "b08a8e96-90d6-4419-b200-d5d53500f5a5",
    "path": "research/evidence/runs/zai-flash-funcdag-easy-r3-20260829/evallab-zai-syn-funcdag-easy__AfLvCrL",
    "agent": "opencode",
    "reward": 0.0,
    "exception_type": null,
    "result_sha256": "sha256:44b03840b8b3aa77afe82bdac9085fbda165b7f97fae63fe430e22eec102f195",
    "disposition": "analyzed"
  },
  {
    "source_trial_id": "72f691a4-7677-4319-b3f2-0a4bd224c95a",
    "path": "research/evidence/runs/zai-flash-funcdag-easy-r3-20260829/evallab-zai-syn-funcdag-easy__h2hcQHH",
    "agent": "opencode",
    "reward": 1.0,
    "exception_type": null,
    "result_sha256": "sha256:a3e7606246e94eab00cbc26bb996b335e788cb676930782a7bc83cc474955f63",
    "disposition": "analyzed"
  },
  {
    "source_trial_id": "86ae0d4a-2e98-446d-ac38-96c8678d2943",
    "path": "research/evidence/runs/zai-flash-funcdag-easy-r3-20260829/evallab-zai-syn-funcdag-easy__mtesCiD",
    "agent": "opencode",
    "reward": 1.0,
    "exception_type": null,
    "result_sha256": "sha256:2e84cd114aef19c6ea3881a43337e9539fd1bc068a86ebf88b6ca230d523dc96",
    "disposition": "analyzed"
  },
  {
    "source_trial_id": "cd810b29-b752-4c6c-bf15-01ceaf0e4d53",
    "path": "research/evidence/runs/zai-wave2-flash-matrix/mcp-funcdag-depth_5-seed101__mJU4JQC",
    "agent": "opencode",
    "reward": 1.0,
    "exception_type": null,
    "result_sha256": "sha256:2b8ef497bd65021777ac766c4aa36e371608cea953241120626dc69be7ec8a65",
    "disposition": "analyzed"
  },
  {
    "source_trial_id": "27694a47-ee5b-4c99-9171-64b4af607121",
    "path": "research/evidence/runs/zai-wave2-flash-matrix/mcp-funcdag-depth_5-seed2024__gXLQWSh",
    "agent": "opencode",
    "reward": 1.0,
    "exception_type": null,
    "result_sha256": "sha256:114944e5f2e2709e013074718e6f8a551f789ebce2cba7adec7c428992f1baec",
    "disposition": "analyzed"
  },
  {
    "source_trial_id": "b8b93e08-a182-40ff-a22d-7980820805b9",
    "path": "research/evidence/runs/zai-wave2-flash-matrix/mcp-funcdag-name_similarity_high__DvpHJGh",
    "agent": "opencode",
    "reward": 0.0,
    "exception_type": null,
    "result_sha256": "sha256:f133e1e9d4ef67f55b0450440a47785710fa8a99a533889f5bed418995e66b27",
    "disposition": "analyzed"
  },
  {
    "source_trial_id": "6fe8a323-0251-477f-aabd-9e5d34d9a008",
    "path": "research/evidence/runs/zai-wave2-flash-matrix/mcp-funcdag-name_similarity_high__TkhqHSN",
    "agent": "opencode",
    "reward": 1.0,
    "exception_type": null,
    "result_sha256": "sha256:8eda659f09e0b50f24e33ea03a473eb0b0c6c24732a3ef53d6079d06791fb213",
    "disposition": "analyzed"
  },
  {
    "source_trial_id": "66790181-86d2-42b2-9307-cdfca40e40aa",
    "path": "research/evidence/runs/zai-wave2-flash-matrix/mcp-funcdag-name_similarity_high__sbQkE2R",
    "agent": "opencode",
    "reward": 1.0,
    "exception_type": null,
    "result_sha256": "sha256:69a3ba0f3fea8410a80e180e3194dad0fabe5e015f7bcc1540faee84a41ea1b6",
    "disposition": "analyzed"
  },
  {
    "source_trial_id": "068bb919-9cb8-4292-bffd-bf73adc44b01",
    "path": "research/evidence/runs/zai-wave2-funcdag-depth5-s42/mcp-funcdag-depth_5-seed42__PBZKQYS",
    "agent": "opencode",
    "reward": 1.0,
    "exception_type": null,
    "result_sha256": "sha256:c27e95e89ae80b8e6fd41914b90301cb95a818593fa97f6eb15d7ef5ccf38472",
    "disposition": "analyzed"
  },
  {
    "source_trial_id": "2fb983b0-eb31-4f96-8d37-dbf5950923c2",
    "path": "research/evidence/runs/zai-wave2-glm53-funcdag-canary/mcp-funcdag-depth_5-seed42__Ca9ToPk",
    "agent": "opencode",
    "reward": 1.0,
    "exception_type": null,
    "result_sha256": "sha256:844c440b30ae032b13c0c3348325d9f5bf4a046393234a6350c89b1cc369594e",
    "disposition": "analyzed"
  },
  {
    "source_trial_id": "f847e897-0ec5-4de6-8772-b8696dba716a",
    "path": "runs/baseline-funcdag-easy-zai-opencode-k3-r2/baseline-funcdag-easy-zai-openco__CzXSt2x",
    "agent": "opencode",
    "reward": 0.0,
    "exception_type": "NonZeroAgentExitCodeError",
    "result_sha256": "sha256:a03cb884fa96e39e814a1672d1fe16038a235afdae07d3b214fde2e3fa61e264",
    "disposition": "excluded_infrastructure_or_setup_failure"
  },
  {
    "source_trial_id": "5288d58f-3760-4517-97d0-e7b9681833cf",
    "path": "runs/baseline-funcdag-easy-zai-opencode-k3-r2/baseline-funcdag-easy-zai-openco__iXwg8xq",
    "agent": "opencode",
    "reward": 0.0,
    "exception_type": "NonZeroAgentExitCodeError",
    "result_sha256": "sha256:b8d32f5621833285d0f53258acfa83fccb35234947d371d553035087f8547f40",
    "disposition": "excluded_infrastructure_or_setup_failure"
  },
  {
    "source_trial_id": "8d43f47b-06c8-44d2-9dd1-f2da8b909315",
    "path": "runs/baseline-funcdag-easy-zai-opencode-k3-r2/baseline-funcdag-easy-zai-openco__w2qNCfc",
    "agent": "opencode",
    "reward": 0.0,
    "exception_type": "NonZeroAgentExitCodeError",
    "result_sha256": "sha256:8ab6bc9a60fb8c916af55b81a5e8df93564b1ec61830dd68f6377f6035adc5fb",
    "disposition": "excluded_infrastructure_or_setup_failure"
  },
  {
    "source_trial_id": "1f52357c-0c7e-4b50-a2fd-ad08b73f4f3f",
    "path": "runs/funcdag-codex-canary/syn-funcdag-easy__Az2rApj",
    "agent": "codex",
    "reward": null,
    "exception_type": "ValueError",
    "result_sha256": "sha256:73d4c76b8a9ea70cf74fe304594afee26acc389f52c8f3a90a6eb33e6d5d0852",
    "disposition": "excluded_infrastructure_or_setup_failure"
  },
  {
    "source_trial_id": "8c658358-475d-418f-9c7f-e13658d02af7",
    "path": "runs/funcdag-codex-canary/syn-funcdag-easy__F4mA4VR",
    "agent": "codex",
    "reward": null,
    "exception_type": "ValueError",
    "result_sha256": "sha256:23fb132d719c5b810abc2fb82602e0591289387032e27edfdd9f9321f77a8982",
    "disposition": "excluded_infrastructure_or_setup_failure"
  },
  {
    "source_trial_id": "d50dc421-93cc-48a8-9a1e-cff895838cec",
    "path": "runs/funcdag-codex-canary/trials-hard/adapted-syn-funcdag-hard__kziNARo",
    "agent": "codex",
    "reward": 0.0,
    "exception_type": null,
    "result_sha256": "sha256:49aa214c01fa98b98fba39c2ec357cadf51fd476ddd88aba64063c500eaa623e",
    "disposition": "analyzed"
  },
  {
    "source_trial_id": "86eb12d5-08eb-4fdf-9343-7a4e327872d4",
    "path": "runs/funcdag-codex-canary/trials-medium/adapted-syn-funcdag-medium__NoqKuag",
    "agent": "codex",
    "reward": 1.0,
    "exception_type": null,
    "result_sha256": "sha256:fefb1d2a061137df3c746b01d7e1662452826e39bee84c601145f8d8fda25142",
    "disposition": "analyzed"
  },
  {
    "source_trial_id": "72d21d01-8cf1-4c86-afc9-399d5398b854",
    "path": "runs/funcdag-codex-canary/trials/adapted-task__PmTen2E",
    "agent": "codex",
    "reward": 1.0,
    "exception_type": null,
    "result_sha256": "sha256:1e39239556412ed93a14bbbdcd98942697112a4086beee367fbc1ef7fdede571",
    "disposition": "analyzed"
  },
  {
    "source_trial_id": "b3a9cff8-e2c5-4c41-9813-00e7b8a2124e",
    "path": "runs/oracle-probe-escape-quarantine-2/oracle-probe-escape-quarantine-2__JkPFm45",
    "agent": "oracle",
    "reward": 1.0,
    "exception_type": null,
    "result_sha256": "sha256:ebbeb2706c427c3335fe5aed6a9522e11780603afce56ed8ed1bf5199d69ec25",
    "disposition": "control_not_model_capability"
  },
  {
    "source_trial_id": "bfbeebe5-8857-47b3-8844-a7b8223fbc41",
    "path": "runs/oracle-probe-funcdag-capability-fix/oracle-probe-funcdag-capability__75mKJvw",
    "agent": "oracle",
    "reward": 1.0,
    "exception_type": null,
    "result_sha256": "sha256:bb0c8a676b2974b1e9641796dfff53076cbd6c42b51f1ce036a99f2b6291f3f7",
    "disposition": "control_not_model_capability"
  },
  {
    "source_trial_id": "2e590219-6f79-4e2a-bfa2-ca2e975bf7fd",
    "path": "runs/oracle-probe-funcdag-easy-quarantine-break/oracle-probe-funcdag-easy-quaran__hgUHWWA",
    "agent": "oracle",
    "reward": 1.0,
    "exception_type": null,
    "result_sha256": "sha256:30446cf646df9d278b8c403754f6b706381bdfd0e487bd65284e755bc09ce890",
    "disposition": "control_not_model_capability"
  },
  {
    "source_trial_id": "e4233e8a-7207-4794-b8a3-e19740452b66",
    "path": "runs/oracle-probe-k1h-unblock/oracle-probe-k1h-unblock__XefAZqU",
    "agent": "oracle",
    "reward": 1.0,
    "exception_type": null,
    "result_sha256": "sha256:609dcb921645c625c7d514fad90458611a02283aa7f25c47c5bbebcfd4163274",
    "disposition": "control_not_model_capability"
  },
  {
    "source_trial_id": "ba39af52-828e-4ada-bafa-4af96a038d64",
    "path": "runs/oracle-probe-k1i-unblock/oracle-probe-k1i-unblock__2fX2mn9",
    "agent": "oracle",
    "reward": 1.0,
    "exception_type": null,
    "result_sha256": "sha256:9595ece3ae6f9866f4f94172fb017b31156cf4384995ad189201d8aadff09bd6",
    "disposition": "control_not_model_capability"
  },
  {
    "source_trial_id": "c3f07806-d7e4-4a65-98c6-a7fc1254748f",
    "path": "runs/oracle-probe-k1j-unblock/oracle-probe-k1j-unblock__sCHnFiC",
    "agent": "oracle",
    "reward": 1.0,
    "exception_type": null,
    "result_sha256": "sha256:67526c0ec094cece138377dca1c9ccdeeba443c694ffaa1309caf39b6a2bde55",
    "disposition": "control_not_model_capability"
  },
  {
    "source_trial_id": "1e339d33-4ef4-41db-9ecd-294988cd294a",
    "path": "runs/probe-oracle-syn-funcdag-01/probe-oracle-syn-funcdag-01__GR9q6EE",
    "agent": "oracle",
    "reward": 1.0,
    "exception_type": null,
    "result_sha256": "sha256:e119dabcf9152e97865098cd159fdcf1b83e7bb57a18039eb27bca39a526889d",
    "disposition": "control_not_model_capability"
  },
  {
    "source_trial_id": "f1393fdc-e8e8-4af5-a82e-0f03fe760d2e",
    "path": "runs/probe-oracle-syn-funcdag-02/probe-oracle-syn-funcdag-02__tQ76C7g",
    "agent": "oracle",
    "reward": 1.0,
    "exception_type": null,
    "result_sha256": "sha256:3b47b6c5cadad6d80420b88d67cfd22a9eb38ec68be6f7249417fa8257ff78d0",
    "disposition": "control_not_model_capability"
  },
  {
    "source_trial_id": "bf527b73-7b3d-4692-8e9a-bf038f8a1614",
    "path": "runs/r1-nop-funcdag-easy-20260908/r1-nop-funcdag-easy-20260908__QtZzXzT",
    "agent": "nop",
    "reward": 0.0,
    "exception_type": null,
    "result_sha256": "sha256:5096681bd11de66b1ad073c471aa4ce27dc682aa543d4c9a1cd2268a6238e8d7",
    "disposition": "control_not_model_capability"
  },
  {
    "source_trial_id": "2eb1413e-eda3-490d-9a87-cf9263994d01",
    "path": "runs/r1-oracle-funcdag-easy-20260908/r1-oracle-funcdag-easy-20260908__ZgcSFX8",
    "agent": "oracle",
    "reward": 1.0,
    "exception_type": null,
    "result_sha256": "sha256:c03d1c29d688552864fa61af0c6b6532d29318497ecd0d9733861a83dee03c33",
    "disposition": "control_not_model_capability"
  },
  {
    "source_trial_id": "b628eb5a-d439-4306-bc58-d421023d8601",
    "path": "runs/r2-oracle-funcdag-easy-20260908/r2-oracle-funcdag-easy-20260908__kNQsLwG",
    "agent": "oracle",
    "reward": 1.0,
    "exception_type": null,
    "result_sha256": "sha256:88318f1b09edfcacf3e481b6f4e3524ba2648528503ce7c61110565eb2f47c60",
    "disposition": "control_not_model_capability"
  },
  {
    "source_trial_id": "fddcbb98-4e41-401e-a621-554339e6e7e3",
    "path": "runs/screening-metered-funcdag-easy-zai-opencode-k1/screening-metered-funcdag-easy-z__Ccz3g5Q",
    "agent": "opencode",
    "reward": 0.0,
    "exception_type": "NonZeroAgentExitCodeError",
    "result_sha256": "sha256:5577215bb6129e5b6de1ea19c766be2a59b9aeb9810d7c071f42fcf41a329aa8",
    "disposition": "excluded_infrastructure_or_setup_failure"
  },
  {
    "source_trial_id": "5c6a7227-ae25-4f11-bd23-478621b7ff53",
    "path": "runs/screening-metered-funcdag-easy-zai-opencode-k1b/screening-metered-funcdag-easy-z__XxTfTWT",
    "agent": "opencode",
    "reward": null,
    "exception_type": "RuntimeError",
    "result_sha256": "sha256:d8b690982f3e32a3e1d0fbf42f867e165ab8aa5350e82dcbdd6cb24296bca0fd",
    "disposition": "excluded_infrastructure_or_setup_failure"
  },
  {
    "source_trial_id": "c8bb27ff-e795-4e6f-8713-91a8017b13ac",
    "path": "runs/screening-metered-funcdag-easy-zai-opencode-k1c/screening-metered-funcdag-easy-z__vcsBBnX",
    "agent": "opencode",
    "reward": 0.0,
    "exception_type": "NonZeroAgentExitCodeError",
    "result_sha256": "sha256:ae65fec68ea00193530e2eb3c2c2bf94aabd32fb4b2de5d8e6095f2eda5f9432",
    "disposition": "excluded_infrastructure_or_setup_failure"
  },
  {
    "source_trial_id": "f00633d7-70f3-4558-a6c0-47036d1b99ac",
    "path": "runs/screening-metered-funcdag-easy-zai-opencode-k1d/screening-metered-funcdag-easy-z__nzxG3E8",
    "agent": "opencode",
    "reward": 0.0,
    "exception_type": "NonZeroAgentExitCodeError",
    "result_sha256": "sha256:8708ba52204628795423a624eb92a11f632b5f5df3925811d03bfb7b762b0bce",
    "disposition": "excluded_infrastructure_or_setup_failure"
  },
  {
    "source_trial_id": "0e5ec8fe-038e-489c-8d6c-e069c2c24947",
    "path": "runs/screening-metered-funcdag-easy-zai-opencode-k1e/screening-metered-funcdag-easy-z__JQaTDUs",
    "agent": "opencode",
    "reward": null,
    "exception_type": "CancelledError",
    "result_sha256": "sha256:e55a49822e6ac9f84c927841fc9dccf60483b31a36d9593bc28e1ce296b3881a",
    "disposition": "excluded_infrastructure_or_setup_failure"
  },
  {
    "source_trial_id": "3bec938b-737d-4780-a988-f22377f09291",
    "path": "runs/screening-metered-funcdag-easy-zai-opencode-k1f/screening-metered-funcdag-easy-z__XVnK4V2",
    "agent": "opencode",
    "reward": 0.0,
    "exception_type": "NonZeroAgentExitCodeError",
    "result_sha256": "sha256:0e1e8ff87bc5bcd2c890f249d1e2ba7ecd889fa852f99c8250eabcbd4a933bde",
    "disposition": "excluded_infrastructure_or_setup_failure"
  },
  {
    "source_trial_id": "530637f5-2d57-4027-821a-34a33b249b17",
    "path": "runs/screening-metered-funcdag-easy-zai-opencode-k1g/screening-metered-funcdag-easy-z__LqrhMcn",
    "agent": "opencode",
    "reward": 0.0,
    "exception_type": "NonZeroAgentExitCodeError",
    "result_sha256": "sha256:67997ba0c091acfbf25fe86d969bdf2aba9c9c7bffe378ac7ccc784b297649cd",
    "disposition": "excluded_infrastructure_or_setup_failure"
  },
  {
    "source_trial_id": "e8ec6590-7652-4119-a0ff-8f8b396e7846",
    "path": "runs/screening-metered-funcdag-easy-zai-opencode-k1h/screening-metered-funcdag-easy-z__LnZMrtW",
    "agent": "opencode",
    "reward": 0.0,
    "exception_type": "NonZeroAgentExitCodeError",
    "result_sha256": "sha256:1d82471c9160db85f967012a84008e42d4856cc24b4042012dd400b2f1dbe6d0",
    "disposition": "excluded_infrastructure_or_setup_failure"
  },
  {
    "source_trial_id": "9803827e-0f84-478e-bb79-d3818af82e20",
    "path": "runs/screening-metered-funcdag-easy-zai-opencode-k1i/screening-metered-funcdag-easy-z__n7fbki7",
    "agent": "opencode",
    "reward": null,
    "exception_type": "NonZeroAgentExitCodeError",
    "result_sha256": "sha256:28841a3b51bc4d8699a3b2fe629d5dbd50a2cfe8d2705c0525ab0ff2f802650d",
    "disposition": "excluded_infrastructure_or_setup_failure"
  },
  {
    "source_trial_id": "45415821-eb27-402f-8151-31b88799dbee",
    "path": "runs/screening-metered-funcdag-easy-zai-opencode-k1j/screening-metered-funcdag-easy-z__V9ospfw",
    "agent": "opencode",
    "reward": null,
    "exception_type": "CancelledError",
    "result_sha256": "sha256:ad5bcd1c9331e5281b9b468c8c3ac50f3081ee90c72fb78fe04aaa9cdebe1e97",
    "disposition": "excluded_infrastructure_or_setup_failure"
  },
  {
    "source_trial_id": "39cc2927-83d0-4827-8970-2c09929a52bb",
    "path": "runs/screening-metered-funcdag-easy-zai-opencode-k1n/screening-metered-funcdag-easy-z__A8SMCZE",
    "agent": "opencode",
    "reward": null,
    "exception_type": "CancelledError",
    "result_sha256": "sha256:293f3073faa7571950be4c5d7f7a240d6ab57f1ad92d80bb60a3d71a40cfd0ea",
    "disposition": "excluded_infrastructure_or_setup_failure"
  }
]
```

## Minimal offline reproduction

Run from the repository root with standard-library Python; read-only, no model calls. The path/digest inventory above supplies the exact inputs for broader extraction. This reproduces the decisive hard-trace and serialization findings:

```python
import json
from pathlib import Path
base = Path('runs/funcdag-codex-canary')
task = base / 'adapted-syn-funcdag-hard'
trial = base / 'trials-hard/adapted-syn-funcdag-hard__kziNARo'
nodes = json.loads((task / 'environment/dag_spec.json').read_text())['nodes']
observed = json.loads((trial / 'artifacts/app/output/result.json').read_text())
golden = json.loads((task / 'tests/golden.json').read_text())
seen = set()
for entry in observed['dependency_trace']:
    node = entry['node']
    assert node not in seen
    assert all(s not in nodes or s in seen for s in nodes[node]['inputs'])
    seen.add(node)
assert {x['node']: x for x in observed['dependency_trace']} == {
    x['node']: x for x in golden['dependency_trace']
}
assert observed != golden
print('Valid topological permutation; historical exact-list comparison differs')
p = Path('research/evidence/runs/zai-flash-funcdag-easy-r3-20260829')
p /= 'evallab-zai-syn-funcdag-easy__AfLvCrL/artifacts/app/output/result.json'
try:
    json.loads(p.read_text())
except json.JSONDecodeError as error:
    print(error)
else:
    raise AssertionError('Expected malformed multi-document artifact')
```

Observed host-side results: `Extra data: line 2 column 1 (char 2)`; exact-list hard comparison differs while every node record and predecessor condition agrees. Embedded JSON sidecars parse;42 unique result IDs,13 analyzed records,51 MCP events and49 independently correct calls were checked by assertions before writing this report.
