# Checkpoint — 2026-08-16 — closing the trajectory hop over promoted Codex evidence (F-05)

Hands-on verification by `TrajectoryProof`, worktree `.worktrees/trajectory-proof`
on branch `role/trajectory-proof`, from `origin/main` = `972866d`.

Predecessor: `docs/checkpoints/2026-08-16-m009-integration-flight.md`. That flight
proved seven of eight hops and declared the trajectory hop **`blocked`** because
every agent this repository may run for free (`oracle`, `nop`) writes
`agent/oracle.txt`, not ATIF. Its stage-5 analysis therefore rested on a fresh
`oracle` run with no trajectory, and its citations pointed at `verifier/` output.
PR #58 has since promoted nine real (redacted) Codex ATIF trajectories. This
checkpoint re-runs the hop against that committed evidence.

## The answer, first

**`task -> run -> evidence -> ingest -> facts -> trajectory -> analysis -> operator surface`
is `proven live` over real agent behaviour, with one hop split in two:**

```
task -> run -> evidence -> ingest -> facts -> TRAJECTORY -> analysis sidecar -> operator surface
 [PL]   [PL]     [PL]       [PL]     [PL]      [PL]           [PL/blocked]         [PL]
                                                ^^^^                ^^^^
                                       M009 called this      analysis *machinery* is
                                       `blocked`.            proven live; analysis
                                       It is not any more.   *authorship by a model*
                                                             stays `blocked`.
```

**Citations into a real trajectory resolve end to end.** All six citations of a
hand-authored stage-5 analysis over trial `terminal-bench-html-js-filter__5rgjEEt`
resolve — in the sidecar validator, in the catalog + Parquet join, and on the run
explorer. A deliberately broken control analysis is refused by the validator
(exit 1, three named errors) and marked `unavailable` with a reason for every
citation on the explorer. Resolution is a real check, not a vacuous pass.

**What still does not work is authorship, not resolution.** The finding text was
written by hand. No CLI stages an analysis request (F-01), and M006's calibration
gate is correctly closed, so nothing in this repository can *produce* a stage-5
finding for free. The machinery that carries a finding to a step and a tool call
is now proven; the thing that writes findings is not.

**Redaction cost is real but narrow.** A citation into a redacted system step
resolves identically to a verbatim agent step on every surface — the step
envelope survives redaction. What it yields is empty: 0 readable characters where
4876 bytes were withheld. 42.2% of promoted steps are redacted; 0 tool calls and
0 observations are. Analyses about **what the agent did** are fully supported.
Analyses about **what the agent was told** are not supported from the promoted
corpus at all, and the surfaces do not say so — the explorer renders a redacted
step and a verbatim step identically.

---

## Subject under test

| Field | Value |
|---|---|
| Job | `canary-terminal-bench-html-js-filter-codex-20260815` |
| Job path | `research/evidence/runs/canary-terminal-bench-html-js-filter-codex-20260815` |
| Harbor `jobs.id` | `03c50e09-d16f-4058-93b9-893bb9cae9da` |
| `experiment_id` (= `lab-metadata.experiment.spec_id`) | `01M021T5QYSJKEQV0AVH1WDBJC` |
| Trial | `terminal-bench-html-js-filter__5rgjEEt` |
| Harbor `trials.id` | `1e40baab-3f5b-4030-89a0-439c25638328` |
| Task | `terminal-bench/html-js-filter`, `task_checksum 80fd6f91…c148c` |
| Agent / model | `codex 0.147.0` / `gpt-5.6-terra` |
| Steps / tool calls | **21 / 15** (`evallab trajectories`) |
| Primary reward | **0.0** (`verifier/reward.txt` = `0`; `verifier_result.rewards.reward = 0.0`) |
| Verifier detail | CTRF `tests=2 passed=1 failed=1`; `test_filter_blocks_xss` failed |
| `exception_info` | `null` — no harness exception, so `valid_agent_attempt` is admissible |
| ATIF | `agent/trajectory.json`, `ATIF-v1.7`, `session_id 01a00428-7bb4-7b30-a87c-c6e43aa9f6a2` |
| ATIF digest | `sha256:f112fc244724a9b9ac6e1d7658bceff259670a14564f1f5c948d1cfd2d3075a2` |

Chosen as the richest promoted trial: 21 steps and 15 real `exec` tool calls, the
most of the nine. It is also one of the three reward-0.0 trials, so there is an
actual outcome to classify rather than a control that simply passed.

### Redaction status and what a citation may point at

The ATIF is **redacted**, declared in-band under `evallab_redaction`:

```json
{"rule": "R1",
 "removed": "verbatim message text of every system-source and user-source step",
 "reason": "AGENTS.md forbids committing unredacted model prompts",
 "steps_redacted": 5,
 "recover": "message_sha256 identifies the original text; the unredacted parent digest is in PROMOTION.json"}
```

Measured against the file (`derived/proof/14-redaction-cost.txt`), the in-band
declaration is accurate. What that means for a citation:

| Element | State in the promoted bundle | A citation can point at it and… |
|---|---|---|
| Agent-source `message` | verbatim | …read the agent's own words |
| `tool_calls[]` (id, function, arguments) | verbatim | …read exactly what the agent ran |
| `observation.results[].content` | verbatim | …read what the agent saw back |
| `metrics`, `timestamp`, `step_id`, `source` | verbatim | …quote them |
| System/user-source `message` | `<<evallab-redacted: N bytes, sha256:…>>` | …**resolve** the step, but read **nothing**. Only `N` and the digest survive. |

So a citation into this corpus can support any claim about **agent behaviour**
and cannot support any claim about **instruction content**. Every substantive
citation in the analysis below is agent-source; citation `[4]` is a deliberate
probe into a redacted system step, labelled as such in its own `supports` text.

---

## Databases

- **Throwaway:** `evallab_trajproof` on the existing `eval-lab-postgres-1`
  container, created for this mission. URL
  `postgresql://evallab:local-development-only@localhost:54329/evallab_trajproof`.
  Every `--database-url` below points at it. `evallab analyze ingest-sidecar`
  echoes `catalog: localhost:54329/evallab_trajproof`, so the target is on the
  record rather than assumed (this is M009's F-11 complaint, already fixed for
  `analyze`, still open for `doctor`).
- **Shared `evallab` catalog: read-only, untouched.** Verified before and after.

```
$ docker exec eval-lab-postgres-1 psql -U evallab -d evallab \
    -c "SELECT count(*) AS trajectory_documents FROM trajectory_documents;"
 trajectory_documents
----------------------
                   23
(1 row)
EXIT=0        # identical before the mission and after it
```

**Operational hazard found while setting this up.** From a linked worktree,
`evallab ingest` and `evallab trajectories --export` default their Parquet root
to the **primary checkout** (`src/evallab/paths.py:37-61`,
`shared_checkout_root`), by design, so worktrees share one derived store with the
shared catalog. With `~/Developer/eval-lab` under a read-only lease that default
is a violation waiting to happen. Both invocations below therefore pass an
explicit `--derived-dir` / `--output-dir` inside the worktree.
`find ~/Developer/eval-lab/derived -newermt "2026-08-16 17:00"` returns nothing,
confirming the primary's derived store was not written.

---

## Hop-by-hop

Full transcripts are under the worktree's gitignored `derived/proof/`
(`01-plan.txt` … `20b-agreement-detail.txt`, plus the six read-only probe
scripts they came from); every exit status is reproduced inline below.

### Hop 1–2 — task -> run — `proven live`

Not re-executed by this mission, and not free to re-execute. Proven on
2026-08-15 by the canary run itself and recorded in the promoted bundle:
`lab-metadata.json` carries `exit_code: 0`, `harbor 0.21.0`,
`docker 29.4.1`, `uv 0.9.24`, `timed_out: false`, the full `harbor run` argv,
`policy_rule: canary`, and `spec_id 01M021T5QYSJKEQV0AVH1WDBJC`.
`result.json` reports `n_completed_trials: 3, n_errored_trials: 0`.

### Hop 3 — run -> immutable evidence — `proven live`

`PROMOTION.json` records `promoted_by: scripts/promote_codex_bundle.py`,
`source_job_result_sha256`, four redaction rules (R1, R2, R3a, R3b), per-file
digests for all 41 source files, and `promoted_files: 38 / omitted_files: 3`.

Immutability held under analysis. `run_trial_analysis` digests the whole trial
tree before and after and raises `analysis modified the immutable source trial`
on any difference; it did not raise. Independently:

```
$ git status --porcelain research/evidence/
              # empty
EXIT=0
```

### Hop 4 — evidence -> ingest — `proven live`

```
$ uv run evallab ingest \
    research/evidence/runs/canary-terminal-bench-html-js-filter-codex-20260815 \
    --database-url $TPDB --derived-dir derived/proof/parquet
ingested 1 job(s)
artifact_facts: 6 row(s)
jobs: 1 row(s)
observations: 35 row(s)
reward_facts: 3 row(s)
steps: 54 row(s)
tool_calls: 35 row(s)
tool_usage: 3 row(s)
trajectories: 3 row(s)
trial_facts: 3 row(s)
EXIT=0
```

### Hop 5 — ingest -> facts — `proven live`

```
$ uv run evallab trajectories \
    research/evidence/runs/canary-terminal-bench-html-js-filter-codex-20260815 \
    --export --database-url $TPDB --output-dir derived/proof/parquet
| job | trial | status | documents | steps | tools |
| canary-terminal-bench-html-js-filter-codex-20260815 | terminal-bench-html-js-filter__5rgjEEt | valid | 1 | 21 | 15 |
| canary-terminal-bench-html-js-filter-codex-20260815 | terminal-bench-html-js-filter__D3GZpFU | valid | 1 | 18 | 12 |
| canary-terminal-bench-html-js-filter-codex-20260815 | terminal-bench-html-js-filter__kzGxL7Q | valid | 1 | 15 |  8 |
… 25 Parquet files written (1 job-level + 8 tables × 3 trial partitions) under
   derived/proof/parquet/job_id=03c50e09-…/trial_id=<trial uuid>/
totals: steps 54, tool_calls 35, observations 35, trajectories 3
EXIT=0
```

Partition for the subject trial:
`derived/proof/parquet/job_id=03c50e09-d16f-4058-93b9-893bb9cae9da/trial_id=1e40baab-3f5b-4030-89a0-439c25638328/`
— `trajectories/steps/tool_calls/observations/trial_facts/reward_facts/artifact_facts/tool_usage`.parquet,
`steps.parquet` = 21 rows, `tool_calls.parquet` = 15 rows.

### Hop 6 — facts -> trajectory — `proven live` (M009 called this `blocked`)

```
$ docker exec … -d evallab_trajproof -c \
    "SELECT id, trial_id, validation_status, step_count, llm_call_count
       FROM trajectory_documents ORDER BY id;"
 2bc07646…fb3b46 | 1e40baab-3f5b-4030-89a0-439c25638328 | valid | 21 | 16
 9e30c54f…c77c24 | e94ad89c-f584-4797-b58d-e0f8dc0017f0 | valid | 18 | 13
 b8a7ad95…052d23f | 03a98d62-9a24-4c7e-852e-b60168bfc335 | valid | 15 |  9
(3 rows)
EXIT=0
```

`validator = internal-atif-v1`, `schema_version = ATIF-v1.7`, `validation_error`
null for all three, `parquet_path` populated.

**The contrast with M009 is exact.** M009 measured `trajectory_documents` empty,
zero Parquet trajectory rows, and `trajectory_document_id` NULL for all six of
its trials. Here:

```
$ docker exec … -d evallab_trajproof -x -c \
    "SELECT * FROM experiment_trial_analysis_path WHERE analysis_id IS NOT NULL;"
-[ RECORD 1 ]--------------+-----------------------------------------------------
experiment_id              | 01M021T5QYSJKEQV0AVH1WDBJC
job_id                     | 03c50e09-d16f-4058-93b9-893bb9cae9da
trial_id                   | 1e40baab-3f5b-4030-89a0-439c25638328
trajectory_document_id     | 2bc076460b8ebd69529224d55f735713753c9156e27f796b094b9f7e97fb3b46
analysis_id                | 1687ce14-e4b0-43cf-ac4e-a735c8d14a50
analysis_validation_status | valid
EXIT=0
```

Every column in the documented association path
(`experiments.id -> jobs.experiment_id -> trials.job_id ->
trajectory_documents.trial_id -> analysis_invocations.source_trial_id`) is
non-NULL for the first time.

### Hop 7 — trajectory -> analysis sidecar — split verdict

**7a. The stage-5 machinery over a real trajectory: `proven live`.**
**7b. Authorship of an analysis by a model: `blocked`.**

Plan first, no model call:

```
$ uv run evallab analyze plan \
    research/evidence/runs/canary-terminal-bench-html-js-filter-codex-20260815/terminal-bench-html-js-filter__5rgjEEt \
    --output-dir derived/analyses
{ "agent": "codex", "agent_version": "local",
  "destination_root": "derived/analyses",
  "estimated_model_calls": 1, "maximum_model_calls": 2,
  "experiment_id": "01M021T5QYSJKEQV0AVH1WDBJC",
  "job_id": "03c50e09-d16f-4058-93b9-893bb9cae9da",
  "source_trial_id": "1e40baab-3f5b-4030-89a0-439c25638328",
  "queue_policy_rule": "researcher-followups",
  "prompt_digest": "sha256:13a175e4…995277c",
  "rubric_digest": "sha256:010d4ed2…6837f62",
  "output_schema_digest": "sha256:7c0c4977…b2274e8" }
EXIT=0
```

The saved response is hand-authored and committed at
`research/analysis/stub-codex-html-js-filter-analysis.json`
(`sha256:c7d00390f0a5b08a6dc8b1c1b33873e07ed0b2d6a81bc66fded3046722a3dbca`). It
carries six citations: four verbatim agent-source, one deliberate redacted-step
probe, one non-ATIF verifier file.

```
$ uv run evallab analyze stub <trial> \
    --response research/analysis/stub-codex-html-js-filter-analysis.json \
    --output-dir derived/analyses
analysis: …/derived/analyses/1687ce14-e4b0-43cf-ac4e-a735c8d14a50/analysis.json
validation: valid
indexed: no (the catalog is a derived index, written on request)
next: uv run evallab analyze ingest-sidecar …/analysis.json
EXIT=0

$ uv run evallab analyze ingest-sidecar \
    derived/analyses/1687ce14-e4b0-43cf-ac4e-a735c8d14a50/analysis.json \
    --database-url $TPDB
indexed analysis: 1687ce14-e4b0-43cf-ac4e-a735c8d14a50
indexed reviews: 0
catalog: localhost:54329/evallab_trajproof
EXIT=0

$ uv run evallab analyze review \
    derived/analyses/1687ce14-e4b0-43cf-ac4e-a735c8d14a50/analysis.json \
    --disposition accepted --rationale "…" --reviewer "TrajectoryProof (agent)" \
    --index --database-url $TPDB
review: …/reviews/e1ed512d-7dd3-4de6-954f-03ec015d2b1c.json
disposition: accepted
indexed review: e1ed512d-7dd3-4de6-954f-03ec015d2b1c -> analysis_reviews
catalog: localhost:54329/evallab_trajproof
EXIT=0
```

Sidecar identity and provenance, verbatim from
`derived/analyses/1687ce14-e4b0-43cf-ac4e-a735c8d14a50/analysis.json`
(`sha256:f3ed0a08643ece0e247126de7b2768721d9ba7a54a3e8a1c2c73d52236996c5b`):

| Field | Value | M009's value |
|---|---|---|
| `analysis_id` | `1687ce14-e4b0-43cf-ac4e-a735c8d14a50` | — |
| `experiment_id` | `01M021T5QYSJKEQV0AVH1WDBJC` | present |
| `source_trial_id` | `1e40baab-3f5b-4030-89a0-439c25638328` | present |
| **`source_digests.trajectory`** | **`sha256:f112fc24…3075a2`** | **`null`** |
| `source_digests.files` | 4 entries incl. `agent/trajectory.json` | no ATIF entry |
| `validation_status` | `valid` | `valid` |
| `raw_response_digest` | `sha256:c7d00390…a3dbca` | present |
| `analysis_provenance` | `agent: stub`, `model: saved-response`, `input_tokens: 0`, `output_tokens: 0`, `cost_usd: 0.0` | identical |

`source_digests.trajectory` moving from `null` to a real digest is the single
field that closes F-05 at the sidecar layer.

**Why 7b is still `blocked`, and it is not a defect of this hop.** The finding
prose is mine, not a model's. The only path from a real trajectory to a
model-authored finding is the M006 analysis worker, and per this mission's
constraints its calibration gate stays closed. It is also unreachable: see F-01
below. Both refusals are correct behaviour and are recorded, not routed around.

### Hop 8 — analysis -> operator surface — `proven live`

Catalog side. Every analysis table is populated and linked:

```
$ docker exec … -d evallab_trajproof -c "SELECT citation_index, source_path, step_id, tool_call_id FROM analysis_evidence_citations WHERE analysis_id='1687ce14-…' ORDER BY citation_index;"
 0 | agent/trajectory.json       |  6 | call_TTpI6vDDn6HQL38EAugdVfz7
 1 | agent/trajectory.json       |  7 | call_GT33BBGyNkYakzpgLfteokYC
 2 | agent/trajectory.json       | 20 | call_3OjTUrQcQ3vdLWujMPYQ7Yt6
 3 | agent/trajectory.json       | 21 |
 4 | agent/trajectory.json       |  1 |
 5 | verifier/ctrf.redacted.json |    |
(6 rows)
EXIT=0

analysis_findings: valid_agent_attempt | verification_behavior | earliest_failure_step_id 7 | medium
analysis_invocations: 1687ce14-… | valid | stub | saved-response | cost_usd 0
analysis_reviews:    accepted | TrajectoryProof (agent)
```

**Postgres has no `steps` or `tool_calls` table** — `\dt` on the catalog lists
twelve tables and neither is among them. Step-level facts live only in Parquet.
So "does a citation resolve in the data layer" is a two-store question:
Postgres names the document and its `parquet_path`, DuckDB resolves the step and
call inside it. Executed, `derived/proof/19-citation-join.txt`:

```
$ uv run python derived/proof/citation_join.py
6 indexed citation(s) for analysis 1687ce14-e4b0-43cf-ac4e-a735c8d14a50

[0] agent/trajectory.json step=6:  RESOLVED (source=agent tool_calls=1 obs=1); call call_TTpI6vDDn6HQL38EAugdVfz7: RESOLVED (function=exec)
[1] agent/trajectory.json step=7:  RESOLVED (source=agent tool_calls=1 obs=1); call call_GT33BBGyNkYakzpgLfteokYC: RESOLVED (function=exec)
[2] agent/trajectory.json step=20: RESOLVED (source=agent tool_calls=1 obs=1); call call_3OjTUrQcQ3vdLWujMPYQ7Yt6: RESOLVED (function=exec)
[3] agent/trajectory.json step=21: RESOLVED (source=agent tool_calls=0 obs=0)
[4] agent/trajectory.json step=1:  RESOLVED (source=system tool_calls=0 obs=0)
[5] verifier/ctrf.redacted.json: no step_id -> not an ATIF citation; resolves as a file only
EXIT=0
```

Explorer side, built exactly as `dashboard/explorer.py` builds it
(`build_index([root/"runs", root/"research/evidence/runs"], root/"derived/analyses",
root/"library/registry")`), `derived/proof/07-explorer.txt`:

```
$ uv run python derived/proof/explorer_probe.py 1687ce14-e4b0-43cf-ac4e-a735c8d14a50
jobs indexed: 5     trials indexed: 11     analyses indexed: 1
index notes:
  - jobs root unavailable: …/.worktrees/trajectory-proof/runs

analysis 1687ce14-e4b0-43cf-ac4e-a735c8d14a50
  trial_key: 'canary-terminal-bench-html-js-filter-codex-20260815/terminal-bench-html-js-filter__5rgjEEt'
  status: valid (observed)      validity: valid_agent_attempt (draft)
  citations:
    [agent/trajectory.json step=6  call=call_TTpI6vDDn6HQL38EAugdVfz7] -> resolved (derived; file, step, and call verified)
    [agent/trajectory.json step=7  call=call_GT33BBGyNkYakzpgLfteokYC] -> resolved (derived; file, step, and call verified)
    [agent/trajectory.json step=20 call=call_3OjTUrQcQ3vdLWujMPYQ7Yt6] -> resolved (derived; file, step, and call verified)
    [agent/trajectory.json step=21 call=None] -> resolved (derived; file, step, and call verified)
    [agent/trajectory.json step=1  call=None] -> resolved (derived; file, step, and call verified)
    [verifier/ctrf.redacted.json step=None call=None] -> resolved (derived; file verified)
  trial trajectory step_count: 21     tool_calls: 15
  step sources rendered by the explorer: {'system': 3, 'user': 2, 'agent': 16}
EXIT=0
```

**6 of 6 resolved.** M009 saw `Trajectory — unavailable: missing: trajectory.json`
on this same surface. `index.notes` carries one honest degradation — the
worktree has no `runs/` directory — and no parse error for the review sidecar,
confirming #57's positive `analysis.json` discovery holds with a review present.

`evallab status` also surfaces the analysis, unprompted
(`derived/proof/18-status.txt`):

```
Analysis [observed]
  [observed] 1687ce14-e4b0-43cf-ac4e-a735c8d14a50 — Codex wrote an in-place HTML sanitizer …
EXIT=0
```

### Negative control — resolution is a real check

Same trial, a response citing a step that does not exist, a real tool-call id at
the wrong step, and a missing file (`derived/proof/negative-control-response.json`):

```
$ uv run evallab analyze stub <trial> \
    --response derived/proof/negative-control-response.json \
    --output-dir derived/negative-analyses
analysis: …/derived/negative-analyses/75ecae96-82b5-42ad-b4b2-36b4952569d5/analysis.json
validation: invalid
EXIT=1

sidecar validation_errors:
  "evidence[0] missing step 99 in agent/trajectory.json"
  "evidence[1] missing tool call call_GT33BBGyNkYakzpgLfteokYC at step 6"
  "evidence[2] missing file: agent/does-not-exist.json"
```

And on the explorer (`derived/proof/09-explorer-negative.txt`):

```
analysis 75ecae96-…  status=invalid (observed)
  [agent/trajectory.json step=99 call=None] -> INVALID (unavailable): cited step 99 not in trajectory
  [agent/trajectory.json step=6 call=call_GT33BBGyNkYakzpgLfteokYC] -> INVALID (unavailable): cited tool call 'call_GT33BBGyNkYakzpgLfteokYC' not found in step 6
  [agent/does-not-exist.json step=None call=None] -> INVALID (unavailable): cited file does not exist: 'agent/does-not-exist.json'
EXIT=0
```

Note citation `[1]`: the tool-call id is genuine but belongs to step 7. Both
surfaces catch the step/call mismatch, not merely a bad id. The positive result
above is therefore load-bearing.

---

## What redaction cost

Measured with `derived/proof/redaction_cost.py`
(`derived/proof/14-redaction-cost.txt`). The two elements the mission asked for,
resolved to their terminal destination in canonical ATIF:

| | Citation `[0]`: step 6, agent-source, verbatim | Citation `[4]`: step 1, system-source, redacted |
|---|---|---|
| Sidecar validator | valid | valid |
| Catalog + Parquet join | RESOLVED (`source=agent`) | RESOLVED (`source=system`) |
| Explorer | `resolved` — *file, step, and call verified* | `resolved` — *file, step, and call verified* |
| `evallab trajectories` step count | counted | counted |
| Readable `message` | **283 chars, verbatim** | **0 chars; 4876 bytes withheld** |
| Recoverable identity | full text | `sha256:6866d85e…0f7d858`, `message_chars: 4872` |
| `tool_calls` | 1, verbatim, `exec`, 373 chars of arguments | 0 |
| Linked observation | 197 chars, verbatim | none |

**Do the surfaces behave differently? No — and that is the finding.** Every
surface treats a redacted step exactly like a verbatim one, because every
surface consumes only the step *envelope* (`step_id`, `source`, counts, metrics),
never message text. `steps.parquet` has 17 columns and none of them is a
message; the Postgres catalog has no step table at all; the explorer renders
`{'step_id': 1, 'source': 'system', 'n_tool_calls': 0}`, which is all it renders
for any step. Redaction is therefore **invisible above the raw file** and becomes
visible only at the moment a human opens `agent/trajectory.json`, which is the
citation's actual destination.

A citation into a redacted step **resolves but does not evidence.** It is a
pointer to a hole with a known size and digest. Nothing on any surface warns the
reader of that; a redacted step and a verbatim step look identical until opened.

Corpus-wide shape of the promoted Codex evidence:

| Trial | steps | redacted | sources | withheld bytes | tool calls |
|---|---:|---:|---|---:|---:|
| `event-summary__5E3btLv` | 11 | 6 | system,user | 10256 | 4 |
| `event-summary__EKfePmM` | 11 | 5 | system,user | 10256 | 5 |
| `event-summary__h2D9f6f` | 11 | 5 | system,user | 10256 | 5 |
| `terminal-bench-html-js-filter__5rgjEEt` | 21 | 5 | system,user | 10394 | 15 |
| `terminal-bench-html-js-filter__D3GZpFU` | 18 | 5 | system,user | 10394 | 12 |
| `terminal-bench-html-js-filter__kzGxL7Q` | 15 | 6 | system,user | 10394 | 8 |
| `transaction-reconciliation__W5o8QpH` | 10 | 6 | system,user | 10214 | 3 |
| `transaction-reconciliation__ba8ovxZ` | 10 | 6 | system,user | 10214 | 3 |
| `transaction-reconciliation__frxRezo` | 9 | 5 | system,user | 10214 | 3 |
| **total** | **116** | **49 (42.2%)** | system,user only | **92592** | **58** |

`evallab_redaction.steps_redacted` matches the measured count in all nine files.
Every redacted step is system- or user-source; **no** agent message, tool call,
or observation is redacted anywhere in the corpus.

**Verdict on the promoted corpus's analytical usefulness: `proven live` for
agent-behaviour analysis, `blocked` for instruction-content analysis.** Any
finding of the form "the agent did X, here is the tool call" is fully citable.
Any finding of the form "the task told the agent Y" cannot be evidenced from
this corpus — the discriminator has to reach the unredacted parent job, whose
digest is in `PROMOTION.json`. The analysis under test states this limitation as
its own first alternative explanation, which is the honest way for an analysis of
this corpus to be written.

### Correction to the mission brief

The brief states the nine promoted trials carry **126 steps**. The observed total
is **116**. `evallab trajectories` over the whole repository, summed:

```
$ uv run evallab trajectories | awk -F'|' 'NF>6 && $5 ~ /^ *[0-9]+ *$/ {s+=$6; t+=$7; n++} END {print "trials="n, "steps="s, "tools="t}'
trials=11 steps=116 tools=58
EXIT=0
```

11 trials because the two zero-trajectory `oracle`/`nop` controls are listed with
0 steps. The **58 tool calls** figure is correct. The nine Codex trials account
for all 116 steps and all 58 tool calls.

---

## `evallab trace` — succeeds now, and one earlier claim corrected

```
$ uv run evallab trace --dry-run \
    research/evidence/runs/canary-terminal-bench-html-js-filter-codex-20260815/terminal-bench-html-js-filter__5rgjEEt
traced 1  skipped 0  failed 0
  ok      terminal-bench-html-js-filter__5rgjEEt  spans=34 root=codex kinds=AGENT,LLM,TOOL
EXIT=0
```

**`trace --dry-run` succeeds on real committed ATIF: 34 spans, one root span
named `codex` of kind `AGENT`, kinds `AGENT,LLM,TOOL`.** Conversion:
`proven live`. Nothing was POSTed — `--dry-run` returns before
`ship_resource_spans` (`tracing.py:297`). Phoenix receipt is **UNVERIFIED by this
mission**, deliberately: the mission forbids POSTing, so M009's `blocked` label
on the receipt itself is neither confirmed nor refuted here.

M009's refusal reproduces exactly on the trajectory-less control, so the earlier
observation is real — but its **exit code came from the shipping path, not from
the absence of a trajectory**:

```
$ uv run evallab trace research/evidence/runs/event-summary-oracle-evidence
traced 0  skipped 1  failed 0
  skipped event-summary__FZg7pvq  control agent (oracle/nop); pass include_controls to trace
EXIT=1

$ uv run evallab trace --include-controls research/evidence/runs/event-summary-oracle-evidence
traced 0  skipped 1  failed 0
  skipped event-summary__FZg7pvq  no ATIF trajectory at …/agent/trajectory.json (oracle/nop controls write agent/oracle.txt instead)
EXIT=1

$ uv run evallab trace --dry-run --include-controls \
    research/evidence/runs/event-summary-oracle-evidence/event-summary__FZg7pvq
traced 0  skipped 1  failed 0
  skipped event-summary__FZg7pvq  no ATIF trajectory at …/agent/trajectory.json (oracle/nop controls write agent/oracle.txt instead)
EXIT=0
```

Correction of record: `cli.py:1102-1106` returns 1 only when a conversion
*failed*, or when nothing shipped **and** `--dry-run` was not passed. A
trajectory-less trial is `skipped`, never `failed`. So the M009 checkpoint's
`EXIT=1` transcripts are accurate for the commands it ran — both omit
`--dry-run` — but `trace --dry-run` never exited 1 for a missing trajectory,
then or now. Neither invocation POSTs anything: with zero conversions,
`ship_resource_spans` is unreachable.

### Verified: converted spans carry no `spec_id` / `job_id` / `trial_id`

**CONFIRMED as stated.** Every attribute key in the converted OTLP payload for
the subject trial, `derived/proof/13-span-attrs.txt`:

```
resource attributes (4): service.name=evallab, telemetry.sdk.language=python,
                         telemetry.sdk.name=harbor-atif2otel, telemetry.sdk.version=0.1.0

span attribute keys (14): agent.name, agent.version, atif.schema_version,
  input.value, llm.cost.total, llm.model_name, llm.token_count.completion,
  llm.token_count.prompt, llm.token_count.prompt_details.cache_read,
  llm.token_count.total, openinference.span.kind, output.value,
  session.id, tool.name

keys containing any of ('spec_id','job_id','trial_id','experiment','evallab'): NONE
```

The conversion is done by the external `harbor-atif2otel` 0.1.0 SDK from the ATIF
document alone; the document has no knowledge of Harbor's job or trial UUIDs or
of the queue's `spec_id`, so no such attribute can exist.

**The conclusion drawn from it is PARTIALLY REFUTED.** A join into the research
graph does exist, via one attribute the earlier claim did not consider:

```
root span:  session.id = 01a00428-7bb4-7b30-a87c-c6e43aa9f6a2
catalog:    SELECT trial_id FROM trajectory_documents WHERE session_id = '01a00428-7bb4-7b30-a87c-c6e43aa9f6a2';
            -> 1e40baab-3f5b-4030-89a0-439c25638328
            -> trials.job_id -> 03c50e09-… -> jobs.experiment_id -> 01M021T5QYSJKEQV0AVH1WDBJC
```

`trajectory_documents.session_id` is populated for **23 of 23** rows in the
shared catalog with **23 distinct** values, so the bridge is currently 1:1 —
but it is **unenforced**: `\d trajectory_documents` shows indexes on `id`,
`validation_status`, and `trial_id`, and no unique constraint on `session_id`.

Precise restatement: *converted spans carry no `spec_id`, `job_id`, or `trial_id`
attribute, so they cannot be joined on those keys; they can be joined to the
research graph through `session.id` = `trajectory_documents.session_id`, a
1:1-in-practice but unenforced bridge that no code, query, or document in this
repository currently uses.* Making that join first-class — an
`atif.session_id`-keyed query in `research/analysis/queries.sql`, or a unique
index — is a Platform/Research decision, not this mission's.

Also worth recording: `input.value` on the root span is
`<<evallab-redacted: 2135 bytes, sha256:58835126…>>`. **Redaction markers
propagate verbatim into the OTel payload**, so shipping this corpus to Phoenix
would not leak prompt text — and equally, Phoenix would show the marker, not the
prompt.

---

## Known defects that obstructed this mission

Both are recorded and routed around, per instruction. Neither is fixed here.

### F-01 — no CLI stages an analysis request

```
$ uv run evallab analyze worker-status
{"counts": {}, "requests": [], "provenance": "observed"}
EXIT=0

$ uv run evallab analyze worker-plan          # lists all 9 Codex trials as eligible
[ … {"job": "canary-terminal-bench-html-js-filter-codex-20260815",
      "trial": "terminal-bench-html-js-filter__5rgjEEt",
      "request_id": "3f6fd86fc7c07134",
      "current_state": null, "eligibility": "eligible"} … ]
EXIT=0

$ uv run evallab analyze worker-run-one 3f6fd86fc7c07134
error: [Errno 2] No such file or directory: '…/derived/analyses/worker/requests/3f6fd86fc7c07134/request.json'
EXIT=2
```

`worker-plan` computes a `request_id` and calls the trial `eligible`, then
`worker-run-one` cannot find a request, because nothing wrote one.
`AnalysisWorker.stage()` (`analysis_worker.py:677`) has exactly two callers:
`analysis_worker.py:887` inside `run_cycle`, and `cli.py:1454` inside
`_nightly_analysis_stager` — reachable only by running the whole `nightly`
cycle. **How it obstructed me:** the worker path — the only route to a
model-authored finding, and the route that would have exercised M006's
calibration gate — is unreachable without running an unattended nightly cycle,
which is out of scope for a role worktree. Routed around by using
`evallab analyze stub`, which needs no staged request. Consequence for the
result: hop 7b stays `blocked` and the gate refusal is recorded as designed
behaviour rather than observed, because I could not reach the gate.

### F-04 — the explorer silently loses runs whose jobs root is nested

Reproduced as an A/B, `derived/proof/17-f04.txt`:

```
$ uv run python derived/proof/f04_probe.py
flat root (what dashboard/explorer.py passes): research/evidence/runs
  jobs=5 trials=11 analyses=1
  analysis 1687ce14-…: trial_key='canary-terminal-bench-html-js-filter-codex-20260815/terminal-bench-html-js-filter__5rgjEEt' unresolved_citations=0/6
  notes: ()

nested root (one level up): research/evidence
  jobs=1 trials=5 analyses=1
  job names: ['runs']
  analysis 1687ce14-…: trial_key=None unresolved_citations=6/6
  notes: ()
EXIT=0
```

One level too high and the walk (`explorer.py:589-593`, a single `iterdir` for
jobs then one for trials) reports one job literally named `runs`, mistakes the
five job directories for trials because each has a `result.json`, leaves the
analysis `trial_key=None`, and marks **6 of 6 previously-resolved citations
unresolved** — with `index.notes` **empty**. Silent, exactly as F-04 says.
**How it obstructed me:** it did not, because `research/evidence/runs` is
already the flat root `dashboard/explorer.py` hardcodes, so the promoted bundles
sit at the depth the explorer expects. Had I ingested from a nested layout, the
positive result in hop 8 would have inverted with no warning. This is the single
cheapest way to make the proof above look false, so it is worth naming here even
though it did not bite.

---

## Verdict table

| Hop | Verdict | Evidence |
|---|---|---|
| task -> run | `proven live` | `lab-metadata.json` `exit_code: 0`, harbor 0.21.0, `spec_id 01M021T5QYSJKEQV0AVH1WDBJC`; 2026-08-15 canary run, not re-executed here |
| run -> immutable evidence | `proven live` | `PROMOTION.json` digests all 41 source files (38 promoted, 3 omitted), 4 redaction rules; `git status --porcelain research/evidence/` empty |
| evidence -> ingest | `proven live` | `evallab ingest` EXIT=0, 1 job / 3 trials |
| ingest -> facts | `proven live` | `evallab trajectories --export` EXIT=0, 25 Parquet files, steps 54 / tool_calls 35 |
| facts -> trajectory | `proven live` | `trajectory_documents` 3 valid rows, `step_count 21`; `experiment_trial_analysis_path.trajectory_document_id = 2bc07646…` (M009: NULL) |
| trajectory -> analysis, *machinery* | `proven live` | sidecar `1687ce14-…` `validation_status: valid`, `source_digests.trajectory: sha256:f112fc24…` (M009: `null`), 5 ATIF citations accepted |
| trajectory -> analysis, *authorship by a model* | `blocked` | F-01 leaves the worker unreachable; M006 calibration gate deliberately left closed; finding prose hand-authored |
| analysis -> operator surface | `proven live` | 6/6 citations `resolved` on the explorer, 6/6 RESOLVED through catalog+Parquet, `evallab status` lists the analysis; negative control 3/3 `unavailable` with reasons |
| analysis -> draft-label comparison | `proven live` | `evallab analyze agreement` EXIT=0, matched 1/1 valid sidecar to its label, both digested, disagreement reported (`verification_behavior` vs draft `implementation`) |
| ATIF -> OTel conversion | `proven live` | `evallab trace --dry-run` EXIT=0, spans=34, root kind AGENT |
| OTel -> Phoenix receipt | UNVERIFIED this mission | POST forbidden by the mission; not attempted |
| promoted corpus for agent-behaviour analysis | `proven live` | 58/58 tool calls and all agent messages verbatim; 0 redacted |
| promoted corpus for instruction-content analysis | `blocked` | 49/116 steps redacted, all system/user; 92592 bytes withheld; only digest + length survive |

**F-05 is closed for the trajectory hop.** The chain
`task -> run -> evidence -> ingest -> facts -> trajectory -> analysis -> operator surface`
is `proven live` over real agent behaviour. What remains is not the hop: no
component of this lab can *author* a stage-5 finding for free, and the corpus
cannot evidence claims about instruction text. Both are stated limits, not
breaks.

---

## Artifacts kept, and provenance

Committed under this mission's lease:

- `research/analysis/stub-codex-html-js-filter-analysis.json` — the hand-authored
  saved response, `sha256:c7d00390f0a5b08a6dc8b1c1b33873e07ed0b2d6a81bc66fded3046722a3dbca`.
  Provenance and reproduction command are recorded in
  `research/analysis/README.md`. It is a **stub**, not a model output:
  `agent: stub`, `model: saved-response`, `cost_usd: 0.0`. Its findings are not
  reviewed research ground truth.
- this checkpoint.

Deliberately **not** committed, per `research/analysis/README.md` ("Every
generated table, comparison, and analysis sidecar lives under ignored
`derived/`"): the sidecar `1687ce14-…/analysis.json`, its review
`e1ed512d-…json`, the negative-control sidecar `75ecae96-…/analysis.json`, the 25
Parquet partition files, the agreement report
`derived/analysis/failure-taxonomy-agreement.json`, and the 21 transcripts plus
six probe scripts under `derived/proof/`.
The sidecar is reproducible from the two committed inputs —
`analysis_id` and `created_at` are fresh per invocation, everything else is a
function of the committed bytes:

```bash
uv run evallab analyze stub \
  research/evidence/runs/canary-terminal-bench-html-js-filter-codex-20260815/terminal-bench-html-js-filter__5rgjEEt \
  --response research/analysis/stub-codex-html-js-filter-analysis.json \
  --output-dir derived/analyses
```

**Nothing was promoted into `research/evidence/`.** Nothing was written to the
primary checkout. The throwaway database `evallab_trajproof` is disposable; the
shared `evallab` catalog reports **23** `trajectory_documents`, unchanged.

## The nine taxonomy labels — and one more surface that works

No taxonomy label is cited as ground truth anywhere above. All nine shipped with
the promoted bundles carry `review_status: draft_pending_research_review`
(verified in `research/calibration/trajectory-labels/*.json`); they are drafts.
The sidecar's own `validity` and `primary_category` likewise render with
provenance `draft` on the explorer, which is correct — a stub's classification is
a draft too.

They do, however, give the comparison surface something real to chew on, so it
was worth exercising over a genuine trajectory:

```
$ uv run evallab analyze agreement derived/analyses \
    --labels research/calibration/trajectory-labels
report: …/derived/analysis/failure-taxonomy-agreement.json
agreement: 0/1 (0.000)
label coverage: 0.029
EXIT=0
```

```json
{"analysis_id": "1687ce14-e4b0-43cf-ac4e-a735c8d14a50",
 "trial_name": "terminal-bench-html-js-filter__5rgjEEt",
 "analysis_validation_status": "valid",
 "exact_match": false,
 "predicted_category": "verification_behavior",
 "expected_category": "implementation",
 "label_path": "research/calibration/trajectory-labels/terminal-bench-html-js-filter__5rgjEEt.json",
 "label_sha256": "sha256:a774058242b7501cd89fade95a59adcffee6c926c9f30804d426cdf5f838c00a",
 "sidecar_sha256": "sha256:f3ed0a08643ece0e247126de7b2768721d9ba7a54a3e8a1c2c73d52236996c5b"}
```

`analyze agreement` matched sidecar to label by trial name, digested both, and
reported the disagreement without touching either — `n_labels: 34`,
`n_matched_valid: 1`, `n_invalid_analyses: 0`, `unmatched_analysis_ids: []`.
**Verdict: `proven live`.** M009 could not reach this surface with a
trajectory-backed sidecar at all.

The disagreement itself is not a finding about the agent. My stub says
`verification_behavior` (the agent's self-authored payload set was its only
acceptance signal); the draft label says `implementation` and argues in its
`review_note` that `implementation` is "the narrowest category the retained
evidence supports". Both read the same two verbatim agent-source elements —
step 21 and `verifier/ctrf.redacted.json` — and the draft label notes that
redaction rule R3 excised the rendered XSS corpus, so neither can name the
bypassing vector. That is a Research-lane judgement on a draft label versus a
stub, and it is not settled here. What is settled is that the comparison ran,
resolved both sides to digests, and disagreed visibly rather than silently.

