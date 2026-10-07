# AgentEnv test bench: model-free controls for a MiMo general task

**Question.** Can one MiMo-V2.6 "general" task be proven solvable and
correctly graded without spending anything, and is Scale's AgentEnv a useful
place to build new variants of it?

**Authority and scope.** Peter, 2026-10-06 (chat): $0 only. Local Docker and
Hugging Face downloads; no model, judge, cloud sandbox or GPU. Linear HAR-190.
Results are task/grader validity evidence, never model-capability evidence.

**Task.** `mimo-v2.6-rl/s3k_1591_hr_people_en_t1_rl_008` from
`FineEnvs/MiMo-V2.6-RL-harbor-general@10b732c5079c` (HAR-81 **train** split).
The agent must resubmit Marcus Doyle's returned merit case `MAC-FY26-0274` in
the BlueSky approval system with the current row version and a stable
idempotency key, touch nothing else, and report the facts. The world is four
SQLite-backed MCP systems (Anaplan, BlueSky, DePaul audit register, Workday).
Upstream grading: three deterministic `rule` checks on BlueSky's database plus
one paid LLM judgment of the final report. The dataset ships no `solution/`.

Tooling: `tools/agentenv-bench/` (usage and limits in its README).

## Results (2026-10-07)

### Plain Python, upstream rules executed verbatim

`direct.py`, 24 control runs, 0 verdict mismatches. Same outcome on the
upstream task and both generated variants.

| Control | Mistake | Upstream rules | Bench (rules + isolation) | With report proxy |
|---|---|---|---|---|
| oracle | none | **PASS** 3/3 | PASS | PASS |
| nop | does nothing | FAIL 1/3 | FAIL | FAIL |
| wrong_case | resubmits a different case | FAIL 0/3 | FAIL | FAIL |
| touch_other_case | also resubmits another case | FAIL 2/3 | FAIL | FAIL |
| stale_version | stale row version, gives up | FAIL 1/3 | FAIL | FAIL |
| wrong_decision | tries VP approval instead of resubmit | FAIL 1/3 | FAIL | FAIL |
| touch_other_system | correct resubmit + edits an unrelated Workday employee | **PASS 3/3** | FAIL | FAIL |
| vp_claim | correct resubmit, report claims VP/Dean approval | **PASS 3/3** | PASS | FAIL |

Generated variants (same instruction template, facts swapped, parameters
recomputed from the pristine database): `MAC-FY26-0173` (Luis Ramirez) and
`MAC-FY26-0284` (Tiana Phillips). These are all the other cases the
instruction applies to (operationally returned, eligible, accepted by a
dry-run submit). No negative variant exists: every returned case is eligible.
The generator reproduces the upstream constants exactly (row version 8, 59
other cases, 78 other events, both fingerprints) before emitting anything.

### Same controls inside AgentEnv 0.9.1275, local Docker

`aenv/run.py --variant all`, 11 control runs, 449.8 s, all acceptance checks
true (`control_scores`, `rbac`, `race`, `model_free`, `local_only`). Every
`base` verdict equals the plain-Python one. Graded by AgentEnv's
`env_outcome_verifier` on the real post-run SQLite files exported through the
environment data plane.

| Variant | Control | Expected | Verdict | Trigger fires |
|---|---|---|---|---|
| race | oracle | pass | PASS (row version 8 → 10) | 1 |
| race | naive | fail | FAIL (stops at `ROW_VERSION_CONFLICT`) | 1 |
| race | nop | fail | FAIL | 0 |

`race` is a new task built with a gateway trigger: when the solver's first
accepted `dry_run` submit for the target case returns, a world-only tool bumps
that case's row version (another editor), holding the solver's response until
it is done. A correct solver re-reads and resubmits. The world-only tool is
disabled for the `solver` role: absent from its tool list, a direct call
refused (checked in every run). The trigger is about 10 lines of JSON in the
task DAG; no server code changed.

No model was used: every scripted-agent trace records zero model calls, no
loaded model-client modules and no network attempts outside the gateway.

## Findings

1. **The task is solvable and its rules are mostly sound.** First reference
   solution for a MiMo general task: the upstream rules accept it and reject
   five of seven wrong behaviours.
2. **Grader blind spot: other systems.** Upstream rules only inspect BlueSky.
   A run that does the right thing and also edits an unrelated employee in
   Workday gets full rule credit. The bench's isolation check (unchanged
   non-BlueSky database bytes) catches it; it is labeled as not upstream.
3. **Report claims rest entirely on the paid judge.** A run with the correct
   state change that falsely reports VP/Dean approval passes every rule.
   Upstream, only the LLM item can catch it.
4. **Instruction asks for information no tool provides.** "Use the current
   concurrency version", but no BlueSky read tool returns `row_version`, and
   conflict errors omit it. A solver can only find it by probing with
   `dry_run` submissions. A real agent may fail here for reasons unrelated to
   the skill the task means to test.
5. **AgentEnv works for this, with caveats.** Port effort: four MiMo tool
   modules wrapped generically as MCP environments, composed behind the
   gateway, scripted A2A agent, about 670 lines. Issues hit:
   - The agent container can reach every environment's data plane (full
     databases) and the gateway `/trajectory`; RBAC hides tools only. Fine for
     scripted controls; unsafe for an untrusted or model-backed agent without
     network isolation.
   - `agentenv-framework-protocol` 0.1.290 imports `regex` without declaring it.
   - Stock A2A deployment requires LiteLLM settings even for a model-free agent
     (given an unreachable dummy; no request made).
   - `/trajectory` returns JSONL, not JSON.
   - No converter to or from Harbor's task format; this bench does not add one.

## What this does not show

- Anything about model capability; no model ran.
- That the upstream LLM report item is sound; it was not called. The report
  check here is a lexical proxy.
- That the generated or race variants are Harbor tasks. They exist as bench
  parameter sets and AgentEnv task DAGs; Harbor packaging is a separate step.

## Next steps (not started)

- Package `race` and the two data variants as Harbor task variants
  (`evallab tasks derive`, with lineage) carrying the oracle as `solution/`
  and the isolation check in the verifier.
- Repeat on more of the 189 database-changing general tasks; the controls and
  grader are task-specific today.
- One model-backed trial only after network isolation of the data plane and
  an explicit spend approval.

## Evidence

Kept in the primary checkout's runtime store (ignored, not committed; the
world files are upstream MiMo bytes):

| Run | Path | sha256 |
|---|---|---|
| plain Python | `runs/agentenv-bench/direct-final/receipt.json` | `f861bb036f90241282fa6dd130ea66c26179e3b1a29eac05f97d6bf605c62608` |
| plain Python table | `runs/agentenv-bench/direct-final/summary.txt` | `1f8899d41febc4f74e2c3e8e0e965f5c12d9820ccd1c17bec7be86e806aaa582` |
| AgentEnv | `runs/agentenv-bench/aenv-final/receipt.json` | `d7895097027f96d36c202ba3d77441c1078137f32bbbff1d633116e1a511dc90` |

Receipts carry input hashes (task files, world files, verifier metadata),
per-control grading rows, post-run database hashes, AgentEnv task instance
ids, trigger state and tool versions. Rerun with the commands in
`tools/agentenv-bench/README.md`.
