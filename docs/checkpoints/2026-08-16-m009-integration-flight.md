# M009 — end-to-end integration flight on merged main — 2026-08-16

Pilot: Claude Opus 4.6 (Anthropic), worker `M009Flight`. Tree:
`origin/main` @ `86380b0` (`M007: add task-quality workbench (#49)`), checked
out as worktree `.worktrees/m009-flight` on branch `role/m009-flight`.

Scope of writes: this file, `agents/handoffs/m009-flight.md`, and the
gitignored `runs/`, `queue/`, `derived/` inside this worktree. No committed
path under `src/`, `tests/`, `policy/`, `library/`, `research/`, or
`dashboard/` was modified. No paid model call, no adapter wiring, no gate
opened, no `docker compose` command, no service started or stopped except two
read-only Streamlit servers that were shut down again.

Full verbatim transcript with per-command exit statuses:
`runs/m009/transcript.log` (36 recorded commands, gitignored, local to this
worktree). Every quoted output below is copied from it.

---

## Verdict up front

**The end-to-end path is NOT `proven live` as one chain. It stops at the
trajectory hop, and it stops there structurally, not incidentally.**

```
task -> run -> immutable evidence -> ingest -> facts -> [TRAJECTORY] -> analysis sidecar -> operator surface
 ok     ok         ok                 ok       ok        BREAK           ok (via stub)       ok (partly)
```

Seven of the eight hops are `proven live` on merged main. The trajectory hop
is `blocked` for every agent this repository is currently allowed to run:
`oracle` and `nop` write `agent/oracle.txt`, not an ATIF `agent/trajectory.json`,
so `trajectory_documents` is empty, `derived/.../trajectories.parquet` has zero
rows, `experiment_trial_analysis_path.trajectory_document_id` is `NULL` for all
six trials, and `evallab trace` refuses with exit 1. The chain is bridged only
because `evallab analyze stub` accepts a saved response and cites non-trajectory
files. Analysis therefore rests on `verifier/` output, not on agent behaviour.

The lab can prove **a verifier discriminates**. It cannot yet prove **anything
about an agent's trajectory** without a paid model, which subscription-only
policy forbids. That is the honest state of merged main.

---

## Step 1 — `doctor`

```
$ uv run evallab doctor
ok    harbor         0.21.0
ok    docker         Docker version 29.4.1, build 055a478
ok    uv             uv 0.9.24 (0fda1525e 2026-01-09)
ok    docker-daemon  client=29.4.1 server=29.4.1
ok    postgres       PostgreSQL 18.4 on aarch64-unknown-linux-musl, compiled by gcc (Alpine 15.2.0) 15.2.0, 64-bit
ok    catalog-parquet catalog=69 projected=69 exceptions=0 missing=0 extra=0
ok    task           event-summary
disk  runs=822B  compress-candidates=0  prune-candidates=0  would-reclaim=0B
EXIT=0
```

Zero warnings. Harbor 0.21.0, Postgres 18.4 reachable on the shared
`evallab` database, 69 catalog jobs in perfect parity with Parquet.

Re-run at the end of the flight, with `DATABASE_URL` explicitly unset:
identical, `catalog=69 projected=69`. The shared catalog was not polluted.

**Verdict: `proven live`.**

**But doctor never prints which database it inspected.** Mid-flight, with
`DATABASE_URL` pointing at my throwaway catalog, the same command printed
`catalog=4 projected=4` — same `ok`, same green line, silently a different
database. An operator has no way to tell from doctor's output whether they are
looking at the real catalog. See Failure F-11.

## Step 2 — task selection and digest

Task: `library/tasks/event-summary` (one of four runnable packages under
`library/tasks/`; the one `doctor` validates and `AGENTS.md` advertises in its
"Safe run pattern").

Two independent digest schemes exist over the same task and neither references
the other:

| Source | Field | Value |
|---|---|---|
| M007 workbench (`task_workbench plan`) | `digests.package` | `sha256:972d33bb4c287fe00b6cf39d9a2341b158199f70981afd9dd03ea94b8e25d270` |
| M007 workbench | `digests.verifier` | `sha256:46adc5fd91e233c4fd47296bd55a1e49db4884f859402d6310d36a0c6d4bf995` |
| M007 workbench | `digests.instruction` | `sha256:4bc29dad530dd6db841d77bb342f19b3d2cfe3c16722bb84e4670b042025dd55` |
| M007 workbench | `candidate_record_digest` | `sha256:2ddb46667cef070ab5d62f19ba3f2b90fb93529edfd046267ec804e8b0559f42` |
| M006 analysis sidecar | `source_digests.task` | `sha256:2c16dfb286d74d3ba9069ea19f436d3754887df8cac9613947a20e1b60cebdb3` |

The workbench candidate id is `candidate-661918ad8e06db32139f7b14`.

**Verdict: `proven live`** (a digest is obtainable) **with a caveat**: there is
no single canonical task digest an operator can quote. See Failure F-10.

### The workbench fails the lab's own flagship task

```
$ uv run python -m evallab.task_workbench plan library/tasks/event-summary \
    --source-uri local://library/tasks/event-summary --source-ref 86380b0 --license proprietary
EXIT=1
```

`static_passed: false`, six blocking diagnostics — three classified
`task_defect`, three `harness_defect`:

| classification | code | path |
|---|---|---|
| task_defect | `adversarial_cases_insufficient` — "at least 3 invalid-solution .sh probes are required" | `workbench/adversarial` |
| task_defect | `base_image_unpinned` — "every FROM image must be pinned by @sha256 digest" | `environment/Dockerfile` |
| task_defect | `source_ref_unpinned` | `$source` |
| harness_defect | `unsupported_task_configuration` — `environment.mcp_servers` outside workbench v1.1 surface | `task.toml` |
| harness_defect | `unsupported_task_configuration` — `environment.os` outside workbench v1.1 surface | `task.toml` |
| harness_defect | `unsupported_task_configuration` — `verifier.collect` outside workbench v1.1 surface | `task.toml` |

The workbench merged yesterday (#49) cannot certify the task the repository
runs every day. It says so honestly — the `harness_defect` messages explicitly
state "This is a limitation of the workbench, not necessarily a defect in the
task" — but the practical result is that the certification tool has zero
coverage of the existing task library.

**Verdict for `task_workbench` as an integrated component: `blocked`** on the
four in-repo tasks. Also note the workbench is reachable only as
`python -m evallab.task_workbench`; it is **not** a subcommand of `evallab`, so
`evallab --help` does not mention it at all.

## Step 3 — submit through the policy gate, then dispatch

Isolation chosen before any write (see "Isolation decisions" below):
`jobs_dir` inside this worktree, throwaway database `m009_flight`, absolute
`EVALLAB_DERIVED_ROOT` inside this worktree.

```
$ uv run evallab submit runs/m009/specs/oracle.json
approved: /Users/.../.worktrees/m009-flight/queue/approved/oracle-01M049DVNXHJZCYDST1HHV41PB.json
admitted by standing policy rule local-controls
EXIT=0

$ uv run evallab submit runs/m009/specs/nop.json
approved: /Users/.../.worktrees/m009-flight/queue/approved/nop-01M049DY7T7G2HPFDRSP3E8G8M.json
admitted by standing policy rule local-controls
EXIT=0

$ uv run evallab tick
dispatched 2 experiment(s)
quarantined: no
EXIT=0     (38.17s wall, includes the Docker image build)
```

The policy gate was genuinely exercised: both specs were admitted by the
standing rule `local-controls` in `policy/standing-approvals.yaml`, not
waved through. `queue/done/` holds both; `queue/failed/` and `queue/running/`
are empty. Settlement is clean.

The append-only ledger `queue/events.jsonl` records every transition with its
actor, so the gate decision is auditable rather than asserted (one spec shown,
all four are identical in shape):

```
{"event":"submitted",         "spec_id":"01M049NGB4HV3FJXW0P1M40BE7","to_state":"pending",  "actor":"m009-flight",   "job_name":"m009-flat-event-summary-nop"}
{"event":"policy_admitted",   "spec_id":"01M049NGB4HV3FJXW0P1M40BE7","from_state":"pending","to_state":"approved","actor":"policy-gate","policy_rule":"local-controls"}
{"event":"dispatch_started",  "spec_id":"01M049NGB4HV3FJXW0P1M40BE7","from_state":"approved","to_state":"running","actor":"executor","policy_rule":"local-controls"}
{"event":"dispatch_completed","spec_id":"01M049NGB4HV3FJXW0P1M40BE7","from_state":"running","to_state":"done",   "actor":"executor","policy_rule":"local-controls"}
```

(`schema_version`, `event_id`, and `occurred_at` elided for width; present in
the file.) The submitting actor and the admitting actor are distinct, and the
admitting rule is named at every hop.

Two further free controls were run later to test a hypothesis (Step 6):

```
$ uv run evallab submit runs/m009/specs/oracle-flat.json   -> oracle-01M049MMZP77YCW5YMENDWWD70   EXIT=0
$ uv run evallab tick                                      -> dispatched 1 experiment(s)          EXIT=0
$ uv run evallab submit runs/m009/specs/nop-flat.json      -> nop-01M049NGB4HV3FJXW0P1M40BE7      EXIT=0
$ uv run evallab tick                                      -> dispatched 1 experiment(s)          EXIT=0
```

### Identifiers at the run hop

| spec_id (file stem) | spec_id (in file) | job name | Harbor `jobs.id` | trials |
|---|---|---|---|---|
| `oracle-01M049DVNXHJZCYDST1HHV41PB` | `01M049DVNXHJZCYDST1HHV41PB` | `m009-event-summary-oracle` | `fec0b628-bf49-4a1c-99b1-9fa65643ee19` | 2 |
| `nop-01M049DY7T7G2HPFDRSP3E8G8M` | `01M049DY7T7G2HPFDRSP3E8G8M` | `m009-event-summary-nop` | `c4a2a25d-8549-44c7-8497-4681df0587b2` | 2 |
| `oracle-01M049MMZP77YCW5YMENDWWD70` | `01M049MMZP77YCW5YMENDWWD70` | `m009-flat-event-summary-oracle` | `6f6da5a9-14f8-47d1-99d3-9c2762947306` | 1 |
| `nop-01M049NGB4HV3FJXW0P1M40BE7` | `01M049NGB4HV3FJXW0P1M40BE7` | `m009-flat-event-summary-nop` | `3e8c0d0b-bddf-411b-9322-0db4a4ec19be` | 1 |

`trials.id` UUIDs:

| trial directory | `trials.id` | reward |
|---|---|---|
| `m009-event-summary-oracle/event-summary__JRGWKpY` | `5874c45f-10a9-4fb7-9d0b-a39ab2a8657d` | 1.0 |
| `m009-event-summary-oracle/event-summary__Vbni3GS` | `615f52ad-454c-44bd-a365-7bc2e4d5d238` | 1.0 |
| `m009-event-summary-nop/event-summary__DUypGcB` | `46e76f85-6cf5-48bd-8993-1be223256a07` | 0.0 |
| `m009-event-summary-nop/event-summary__PCPBd7z` | `c9801d95-3e10-4c5b-9fb5-6085b08a5b95` | 0.0 |
| `m009-flat-event-summary-oracle/event-summary__SnQVTgJ` | `754644cd-7aea-42f4-93cd-8f413d89bdcd` | 1.0 |
| `m009-flat-event-summary-nop/event-summary__3D6XU5H` | `7328c898-6c4c-43e8-9a0e-690220b8fd39` | 0.0 |

Discrimination holds: oracle 1.0 / nop 0.0 on all four sub-metrics
(`correctness`, `input_preservation`, `output_hygiene`, `reward`), zero errored
trials, zero retries, zero exceptions.

**The `spec_id` printed by `submit` is not the `spec_id`.** `submit` prints the
queue *filename*, which is `<agent>-<ULID>`; the `spec_id` field inside the
file, and the value `approve`/`reject` expect and that appears as
`experiment_id` in the catalog, is the bare ULID. See Failure F-09.

**Verdict: `proven live`.**

## Step 4 — ingest and project

```
$ uv run evallab ingest runs/m009/jobs/m009-event-summary-oracle runs/m009/jobs/m009-event-summary-nop
ingested 2 job(s)
artifact_facts: 12 row(s)
jobs: 2 row(s)
observations: 0 row(s)
reward_facts: 16 row(s)
steps: 0 row(s)
tool_calls: 0 row(s)
tool_usage: 0 row(s)
trajectories: 0 row(s)
trial_facts: 4 row(s)
EXIT=0
```

```
$ uv run evallab trajectories runs/m009/jobs/m009-event-summary-oracle runs/m009/jobs/m009-event-summary-nop
| job | trial | status | documents | steps | tools |
|---|---|---|---:|---:|---:|
| m009-event-summary-nop | event-summary__DUypGcB | none | 0 | 0 | 0 |
| m009-event-summary-nop | event-summary__PCPBd7z | none | 0 | 0 | 0 |
| m009-event-summary-oracle | event-summary__JRGWKpY | none | 0 | 0 | 0 |
| m009-event-summary-oracle | event-summary__Vbni3GS | none | 0 | 0 | 0 |
EXIT=0
```

`evallab trajectories --export` wrote 27 Parquet files under
`derived/parquet/job_id=<uuid>/trial_id=<uuid>/`, all inside this worktree.
`trajectories`, `steps`, `tool_calls`, `observations`, and `tool_usage` are
zero-row files for every trial. `trial_facts`, `reward_facts`, and
`artifact_facts` are populated.

**Verdict: catalog + Parquet projection `proven live`. Trajectory projection
`blocked`** — see the trajectory break below.

### The trajectory break, root-caused

`src/evallab/atif.py:385-399` looks for `agent/**/*.json` whose name starts
with `trajectory` or whose payload carries `schema_version: ATIF-*`. Measured
contents of the four agent directories:

```
m009-event-summary-oracle/event-summary__JRGWKpY/agent/  ->  oracle.txt
m009-event-summary-oracle/event-summary__Vbni3GS/agent/  ->  oracle.txt
m009-event-summary-nop/event-summary__DUypGcB/agent/     ->  (empty)
m009-event-summary-nop/event-summary__PCPBd7z/agent/     ->  (empty)
```

`evallab trace` states the same thing in the clearest words anywhere in the
repository, and exits non-zero:

```
$ uv run evallab trace runs/m009-flat-event-summary-nop
traced 0  skipped 1  failed 0
  skipped event-summary__3D6XU5H  control agent (oracle/nop); pass include_controls to trace
EXIT=1

$ uv run evallab trace runs/m009-flat-event-summary-nop --include-controls
traced 0  skipped 1  failed 0
  skipped event-summary__3D6XU5H  no ATIF trajectory at /Users/.../event-summary__3D6XU5H/agent/trajectory.json (oracle/nop controls write agent/oracle.txt instead)
EXIT=1
```

Phoenix is running and `evallab status` reports it reachable, but **no span was
ever shipped, because there is nothing to ship.**

**Verdict: trajectory hop `blocked`; Phoenix trace receipt `blocked`** — both
for the same reason, and both unfixable without an agent that is not free.

## Step 5 — stage-5 analysis through the saved-response stub

### The closed calibration gate refuses. Correctly.

```
$ uv run evallab analyze worker-run-one efa8c08c02a88d57
{"state": "deferred", "reason": "policy_requirement_unmet:calibrated_judges_only"}
EXIT=1
```

`analyze worker-status` afterwards: `{"pending": 7, "deferred": 1}`, and the
deferred request carries exactly that reason. Zero model calls; the M006 cycle
report reads `"calls": 0, "completed": 0`. **This is the designed behaviour and
it works.** The gate was not opened, no adapter was wired, and nothing
self-approved.

**Verdict: `proven live` (as a refusal).**

### But there is no way to reach the worker from the CLI

`analyze worker-run-one` on an unstaged request:

```
$ uv run evallab analyze worker-run-one efa8c08c02a88d57
error: [Errno 2] No such file or directory: '/Users/.../derived/analyses/worker/requests/efa8c08c02a88d57/request.json'
EXIT=2
```

A raw `FileNotFoundError` with exit 2 and no guidance — even though
`analyze worker-plan` had just printed `efa8c08c02a88d57` as an `eligible`
request one command earlier. Requests must be *staged* first, and
`AnalysisWorker.stage()` (`src/evallab/analysis_worker.py:677`) has **no CLI
surface**. Its only caller in the repository is `_nightly_analysis_stager`
(`src/evallab/cli.py:1387`), reachable solely by running the whole unattended
`evallab nightly` cycle, which also takes a `pg_dump` backup of the shared
cluster and dispatches queue work — not something a flight should trigger.

I staged through the identical library call that `nightly` makes, and recorded
that I had to leave the CLI to do it:

```
$ uv run python -c "... default_worker(root).stage(default_job_roots(root)) ..."
{"discovered": 8, "staged": 8, "calls": 0, "completed": 0, "adopted": 0, "deferred": {}, "quarantined": {}, "notes": []}
EXIT=0
```

**Verdict: `blocked` through documented commands; `proven live` only via an
undocumented Python entrypoint.** This is the single largest usability defect
found. See Failure F-01.

### The stub path works

```
$ uv run evallab analyze plan runs/m009/jobs/m009-event-summary-nop/event-summary__DUypGcB
{
  "agent": "codex",
  "agent_version": "local",
  "destination_root": "derived/analyses",
  "estimated_model_calls": 1,
  "experiment_id": "01M049DY7T7G2HPFDRSP3E8G8M",
  "job_id": "c4a2a25d-8549-44c7-8497-4681df0587b2",
  "maximum_model_calls": 2,
  "model": "configured-by-queue",
  "output_schema_digest": "sha256:7c0c4977ede3bfb13403f6535a01178497db3e269944917dfd796012eb2274e8",
  "prompt_digest": "sha256:d894d7bd8240e17ecbd97d36cf6e80dcf75ceb1f7576bb0cb0ce235ab4c5b256",
  "queue_policy_rule": "researcher-followups",
  "rubric_digest": "sha256:010d4ed2131a65cef878e9fe9800eaed5525ce605e258c69f5a6592576837f62",
  "source_trial_id": "46e76f85-6cf5-48bd-8993-1be223256a07",
  "source_trial_path": "runs/m009/jobs/m009-event-summary-nop/event-summary__DUypGcB"
}
EXIT=0
```

`analyze plan` is the one place the whole join is printed in a single object:
`experiment_id` -> `job_id` -> `source_trial_id`. Zero model calls.

```
$ uv run evallab analyze stub runs/m009/jobs/m009-event-summary-nop/event-summary__DUypGcB --response runs/m009/nop-saved-response.json
analysis: /Users/.../derived/analyses/a5535560-6f19-4d8e-830d-766d29ae5fa3/analysis.json
validation: valid
EXIT=0

$ uv run evallab analyze stub runs/m009-flat-event-summary-nop/event-summary__3D6XU5H --response runs/m009/nop-saved-response.json --index
analysis: /Users/.../derived/analyses/43d62ce0-0472-4950-b597-fc9b30adce5e/analysis.json
validation: valid
EXIT=0

$ uv run evallab analyze ingest-sidecar derived/analyses/a5535560-.../analysis.json
indexed analysis: a5535560-6f19-4d8e-830d-766d29ae5fa3
EXIT=0

$ uv run evallab analyze review derived/analyses/43d62ce0-.../analysis.json \
    --disposition accepted --rationale "..." --reviewer m009-flight
review: /Users/.../derived/analyses/43d62ce0-0472-4950-b597-fc9b30adce5e/reviews/e17166bc-010c-4d8a-a279-9338d53307e4.json
disposition: accepted
EXIT=0
```

| analysis_id | source_trial_id | validation | provenance model | review |
|---|---|---|---|---|
| `43d62ce0-0472-4950-b597-fc9b30adce5e` | `7328c898-6c4c-43e8-9a0e-690220b8fd39` | valid | `saved-response` | `e17166bc-010c-4d8a-a279-9338d53307e4` accepted |
| `a5535560-6f19-4d8e-830d-766d29ae5fa3` | `46e76f85-6cf5-48bd-8993-1be223256a07` | valid | `saved-response` | none |

Provenance is frozen and honest: `agent: stub`, `model: saved-response`,
`cost_usd: 0.0`, token counts `[redacted]`, `source_digests.trajectory: null`.

**Verdict: `fixture-proven only`.** The sidecar is real and durable, but its
input was a hand-written response file and its citations point at
`verifier/reward.json` and `verifier/test-stdout.txt`, not at agent behaviour.
Calling this a stage-5 analysis of *an agent* would be an overstatement.

**`--index` indexes silently.** It printed nothing about indexing; I had to
query `analysis_invocations` to confirm the row existed.

**`analyze review` does not index the review.** After the review command,
`select count(*) from analysis_reviews` returned `0`. The review only reaches
the catalog when `ingest-sidecar` is run **again** afterwards
(`src/evallab/facts.py:1167` globs `reviews/*.json` during sidecar ingest).
Nothing in the CLI or `docs/analysis-loop.md` says so:

```
$ uv run evallab analyze ingest-sidecar derived/analyses/43d62ce0-.../analysis.json
indexed analysis: 43d62ce0-0472-4950-b597-fc9b30adce5e
EXIT=0
-> analysis_reviews: [(43d62ce0-..., 'accepted', 'm009-flight', 'derived/analyses/.../reviews/e17166bc-....json')]
```

See Failure F-02.

## Step 6 — operator surfaces

### `evallab status` — the best surface in the repository

```
$ uv run evallab status
Eval Lab status
generated_at: 2026-08-16T03:27:14.696064+00:00

Recent [observed]
  [observed] m009-flat-event-summary-nop/event-summary__3D6XU5H — reward=0.0
  [observed] m009-flat-event-summary-oracle/event-summary__SnQVTgJ — reward=1.0
  [observed] m009-event-summary-nop/event-summary__DUypGcB — reward=0.0
  [observed] m009-event-summary-nop/event-summary__PCPBd7z — reward=0.0
  [observed] m009-event-summary-oracle/event-summary__JRGWKpY — reward=1.0
  [observed] m009-event-summary-oracle/event-summary__Vbni3GS — reward=1.0
  [observed] event-summary-nop-evidence/event-summary__edzDz6R — reward=0.0
  [observed] event-summary-oracle-evidence/event-summary__FZg7pvq — reward=1.0

Now [observed]
  [observed] no approved or running work
Next [observed]
  [observed] no waiting work
Tasks [observed]
  [observed] local-lab/event-summary — completed trial
  [observed] library/tasks/event-summary — queued as done
Health [observed]
  [observed] postgres — reachable
  [observed] phoenix — reachable
  [observed] queue — /Users/.../.worktrees/m009-flight/queue
  [observed] parquet — /Users/.../.worktrees/m009-flight/derived/parquet
Analysis [draft]
  [observed] 43d62ce0-... — The nop control performed no work, ...
  [draft]    a5535560-... — The nop control performed no work, ...
EXIT=0
```

All six trials found, including the nested ones. Health names the exact queue
and Parquet paths in use. The reviewed sidecar is labelled `[observed]` and the
unreviewed one `[draft]` — the provenance distinction actually works end to end.

**Verdict: `proven live`.**

### `evallab compare` — refuses to over-claim, correctly

```
$ uv run evallab compare runs/m009/compare-m009.json
not distinguishable / not comparable: only 1 paired task(s); at least 2 are required
json: /Users/.../derived/comparisons/m009-oracle-vs-nop.json
markdown: /Users/.../derived/comparisons/m009-oracle-vs-nop.md
EXIT=0
```

The report still gives per-cohort outcomes (`oracle @1 1.000 (1/1 tasks)`,
`nop @1 0.000 (0/1 tasks)`), the paired delta (`-1.0`, wins/ties/losses
`0/0/1`), complete elicitation tuples for both cohorts, and an explicit
"Interpretation boundary" section — then refuses to print a ranking because one
paired task is below the comparison bar. This is exactly the behaviour the lab
claims to want, working live.

**Verdict: `proven live`.**

### Dashboard — renders, and shows the whole join

Launched read-only on `127.0.0.1:8791`, driven headless, then stopped
(`exit=143`, uptime 4m27s). Nothing left running.

It renders. "Operator status" is six tabs (Recent / Now / Next / Tasks /
Health / Analysis) over `st.dataframe` grids. The **Recent** tab shows all six
M009 trials with `availability`, `label`, `detail` (`reward=0.0` / `reward=1.0`),
`kind`, and the `experiment` / `job` / `trial` identifier columns populated.
The **Leaderboard by cohort** shows four rows keyed by `experiment_id` with
`n total`, `n scored`, `exceptions`, `passes`, `pass@1`, and a Wilson 95% CI
(e.g. `01M049DVNXHJZCYDST1HHV41PB` / `oracle / adhoc` / `100.0%` /
`34.2% – 100.0%`). "Spend vs daily ceiling" reads `$0.00` today against a
`$20.00` ceiling. "ATIF-derived activity" reads `Trials 6, Trajectories 0,
Steps 0, LLM calls 0, Tool calls 0` — an accurate, unflattering summary of what
free controls produce. The **Analysis** tab shows both sidecars with their
experiment and job ids, one `observed` and one `draft`.

**Verdict: `proven live`.** Note the dashboard reads `evallab status`, so
unlike the explorer it sees nested job directories.

### Run explorer — renders, but the joins break here

Launched read-only on `127.0.0.1:8792`, driven headless, then stopped
(`exit=0`, uptime 3m2s).

For a trial it finds, the explorer is genuinely good. Expanded
`m009-flat-event-summary-nop/event-summary__3D6XU5H`:

```
Task: local-lab/event-summary — observed
Agent: nop — observed
Model — unavailable: no model recorded (controls run without one)
Reward: 0.0 — observed
Outcome: reward-failure — derived
Timing: {'started_at': '2026-08-16T03:25:55.811355Z', 'finished_at': '2026-08-16T03:26:03.939500Z'} — observed
Cost — unavailable: no cost recorded (controls and subscription runs bill nothing)
Config: {...'job_id': '3e8c0d0b-bddf-411b-9322-0db4a4ec19be'} — observed
Trajectory — unavailable: missing: trajectory.json
Artifacts (trial-relative, read-only): 6 rows
  events.jsonl / artifacts/app/input/events.jsonl / 458
  manifest.json / artifacts/manifest.json / 495
  checks.json / verifier/checks.json / 348
  ctrf.json / verifier/ctrf.json / 853
  reward.json / verifier/reward.json / 86
  test-stdout.txt / verifier/test-stdout.txt / 319
Next action:
  harbor view /Users/.../runs --jobs
  uv run evallab analyze plan /Users/.../runs/m009-flat-event-summary-nop/event-summary__3D6XU5H
```

Reward/exception separation, artifacts, provenance labels, trajectory
unavailability with a reason, and copyable next actions all work. **Four
things break.**

**(a) A permanent error banner caused by the repo's own commands.** At the top
of every tab:

```
analysis e17166bc-010c-4d8a-a279-9338d53307e4.json: unreadable (ValidationError)
```

`e17166bc-...` is the review I created with `evallab analyze review`.
`_analysis_views` (`src/evallab/explorer.py:499`) does
`analyses_dir.rglob("*.json")` and tries to parse every hit as a
`TrialAnalysisSidecar`, sweeping in `derived/analyses/<id>/reviews/*.json`
written by `analyze review`. **Running two shipped commands in the documented
order permanently corrupts the explorer's banner.** Every future review adds
another line.

**(b) The reviewed disposition is never shown.** The Analyses tab renders
validation status, validity, category, summary, confidence, alternatives,
citations, and provenance — but nothing about reviews. An accepted analysis and
an unreviewed one look identical here. (`evallab status` and the dashboard do
distinguish them.)

**(c) Nested job directories are invisible, silently.** The explorer listed:

```
Jobs:  .executor | m009 | m009-flat-event-summary-nop | m009-flat-event-summary-oracle
       | event-summary-nop-evidence | event-summary-oracle-evidence
Scored trials: 4   (the two flat M009 trials + the two committed evidence trials)
```

My four nested trials are absent. `m009` is listed as a job with zero trials
and `index.notes` is empty — **no warning at all**. Root cause:
`build_index` (`src/evallab/explorer.py:560-568`) iterates exactly two levels,
`<root>/runs/<job>/<trial>`, while `jobs_dir` is a free-form `ExperimentSpec`
field (`src/evallab/schemas.py:27`) that `agents/WORKFLOW.md` positively
encourages varying per worktree.

I proved this rather than assuming it: I submitted an identical oracle spec
differing only in `jobs_dir` (`runs` instead of `runs/m009/jobs`). It appeared
immediately — `m009-flat-event-summary-oracle`, 1 trial. Depth is the cause.

`.executor` — Harbor/executor bookkeeping — is also rendered as a job.

**(d) The consequence: an orphaned analysis.** The Analyses tab shows

```
43d62ce0-0472-4950-b597-fc9b30adce5e -> m009-flat-event-summary-nop/event-summary__3D6XU5H
a5535560-6f19-4d8e-830d-766d29ae5fa3 -> unlinked
```

and for the unlinked one both citations render as
`unresolved: cited trial not found in this index`, despite both cited files
existing on disk and the catalog holding a correct
`experiment -> job -> trial -> analysis` row for it.

**Verdict: explorer `proven live` for flat layouts, `blocked` for nested
`jobs_dir`; review integration `blocked`.**

## Step 7 — does the join actually join?

From `experiment_trial_analysis_path` in the flight catalog, all six trials:

| experiment_id (= spec_id) | job_id | trial_id | trajectory_document_id | analysis_id | status |
|---|---|---|---|---|---|
| `01M049DVNXHJZCYDST1HHV41PB` | `fec0b628-…3ee19` | `5874c45f-…8657d` | **NULL** | NULL | — |
| `01M049DVNXHJZCYDST1HHV41PB` | `fec0b628-…3ee19` | `615f52ad-…5d238` | **NULL** | NULL | — |
| `01M049DY7T7G2HPFDRSP3E8G8M` | `c4a2a25d-…f0587b2` | `46e76f85-…56a07` | **NULL** | `a5535560-…5fa3` | valid |
| `01M049DY7T7G2HPFDRSP3E8G8M` | `c4a2a25d-…f0587b2` | `c9801d95-…8b95` | **NULL** | NULL | — |
| `01M049MMZP77YCW5YMENDWWD70` | `6f6da5a9-…47306` | `754644cd-…9bdcd` | **NULL** | NULL | — |
| `01M049NGB4HV3FJXW0P1M40BE7` | `3e8c0d0b-…4ec19be` | `7328c898-…8fd39` | **NULL** | `43d62ce0-…dce5e` | valid |

Hop by hop, starting from the `spec_id` I submitted:

| Hop | Through the catalog | Through the operator surface |
|---|---|---|
| spec_id -> job | **holds** (`experiment_id` column) | **holds** (`status`, dashboard `experiment` column) |
| job -> trial | **holds** | **holds** |
| trial -> trajectory document | **BREAKS** — NULL for all six | **BREAKS** — `Trajectory — unavailable: missing: trajectory.json` |
| trial -> analysis sidecar | **holds** for both analysed trials | **holds** for the flat trial; **BREAKS** (`unlinked`) for the nested trial |
| analysis -> review | **holds**, but only after a second `ingest-sidecar` | **BREAKS** — explorer shows the review as a parse error, never as a disposition |

Two of five hops are clean in both planes. One breaks in both planes
(trajectory). Two break in the operator plane while holding in the catalog.

**The catalog is more trustworthy than the surface built on top of it.**

---

## Isolation decisions, stated explicitly

1. **Throwaway database `m009_flight`.** The default `DATABASE_URL` points at
   the shared `evallab` catalog, which had 69 jobs / 83 trials and a doctor
   invariant asserting catalog/Parquet parity. Ingesting four throwaway control
   jobs would have broken that parity for everyone. I created a separate
   database on the already-running Postgres (`CREATE DATABASE m009_flight` over
   TCP via `psycopg`; no `docker compose`, no container started or stopped) and
   applied the schema with `evallab db init` (18 tables). Post-flight check:
   shared `evallab` still reports 69 jobs / 83 trials, and doctor still reports
   `catalog=69 projected=69`.

2. **`EVALLAB_DERIVED_ROOT` pinned to an absolute path in this worktree.**
   `derived_root_from_environment` (`src/evallab/paths.py:37-61`) deliberately
   resolves the derived root against the **primary checkout** for linked
   worktrees. Left at its default, `evallab ingest` from this worktree would
   have written Parquet into `~/Developer/eval-lab/derived/parquet` — the
   primary checkout I was told never to write to. Verified afterwards:
   `find ~/Developer/eval-lab/{derived,runs,queue} -newermt "2026-08-15 23:15"`
   returned nothing.

3. **`jobs_dir` inside this worktree.** Confirmed by `evallab status` Health,
   which printed both the queue and Parquet paths under
   `.worktrees/m009-flight/`.

4. **Primary checkout untouched.** `git status --porcelain` in
   `~/Developer/eval-lab` still shows exactly the three pre-existing entries:
   `M digests/DISCOVERIES.md`, `?? docs/prompts/Untitled`,
   `?? docs/repo_overview.html`.

5. **Cleanup.** `m009_flight` was dropped after the flight so the shared
   Postgres server is left exactly as found. Everything cited above is
   reproducible from the immutable Harbor job directories that remain under
   this worktree's gitignored `runs/`: recreate with `CREATE DATABASE
   m009_flight`, `DATABASE_URL=…/m009_flight uv run evallab db init`, then
   re-run the `ingest` / `trajectories --export` / `analyze` commands in
   `runs/m009/transcript.log`. Nothing was promoted to
   `research/evidence/runs/`.

---

## Everything that broke, was missing, was confusing, or needed an undocumented step

| # | What | Severity | Evidence |
|---|---|---|---|
| F-01 | **No CLI can stage an analysis request.** `analyze worker-plan` advertises request ids; `analyze worker-run-one <id>` then dies with a raw `FileNotFoundError`, exit 2. `AnalysisWorker.stage()` is reachable only from the whole `evallab nightly` cycle. | blocking | transcript, `cli.py:1387`, `analysis_worker.py:677` |
| F-02 | **`analyze review` does not index the review.** `analysis_reviews` stays empty until an undocumented second `ingest-sidecar` run. | blocking | `analysis_reviews` count 0 -> 1, `facts.py:1167` |
| F-03 | **`analyze review` output permanently corrupts the explorer.** `rglob("*.json")` parses `reviews/*.json` as sidecars and pins `unreadable (ValidationError)` to every tab. | blocking | explorer banner, `explorer.py:499` |
| F-04 | **The explorer silently loses runs whose `jobs_dir` is nested.** Two-level walk vs a free-form spec field; `index.notes` empty; the resulting analysis renders `unlinked` with unresolvable citations. Proven by a controlled A/B run. | blocking | `explorer.py:560-568`, flat-vs-nested comparison |
| F-05 | **No trajectory, therefore no trajectory analysis, for any agent this repo may run.** `oracle`/`nop` write `agent/oracle.txt`; ATIF, Parquet trajectory tables, `trajectory_document_id`, and Phoenix are all empty as a consequence. | structural | `trace` message, `atif.py:385`, catalog NULLs |
| F-06 | **The M007 workbench cannot certify any of the four in-repo tasks.** `static_passed: false` on `event-summary` with 3 `task_defect` + 3 `harness_defect`. | high | `task_workbench plan`, exit 1 |
| F-07 | **The workbench is not in `evallab`.** Only `python -m evallab.task_workbench`; absent from `evallab --help`. | medium | `evallab --help` |
| F-08 | **The explorer lists `.executor` as a job.** Executor bookkeeping rendered as evaluation output. | low | explorer Jobs list |
| F-09 | **`submit` prints a filename that is not the `spec_id`.** `approve`/`reject` and `experiment_id` want the bare ULID. | medium | `submit` output vs `queue/done/*.json` |
| F-10 | **Two unrelated task digest schemes.** Workbench `package`/`verifier` digests vs sidecar `source_digests.task`; nothing cross-references them, so there is no canonical task fingerprint to quote. | medium | Step 2 table |
| F-11 | **`doctor` never says which database it checked.** The same green `catalog-parquet ok` line printed `catalog=69` and `catalog=4` in the same session. | medium | two doctor runs |
| F-12 | **`analyze stub --index` indexes silently.** No output distinguishes it from the un-indexed form. | low | transcript |
| F-13 | **The default derived root of a linked worktree is the primary checkout.** Documented in a docstring, nowhere an operator reads; the natural command writes outside your worktree. | medium | `paths.py:37-61` |
| F-14 | **`tick` takes no arguments.** Queue and jobs roots come from the installed package location. Correct here, but there is no way to see or override the target before dispatching. | low | `tick --help` |

Things that were **not** broken, recorded so the list is not read as uniformly
negative: the policy gate, queue settlement, Harbor execution and verifier
discrimination, immutability of the job directories, catalog and Parquet
ingest, the closed calibration gate's refusal, `compare`'s refusal to rank
below the comparison bar, provenance labelling (`observed`/`derived`/`draft`/
`unavailable`), the redaction of token counts in sidecar provenance, `status`,
and the dashboard.

---

## The one thing standing between this repository and an unaided operator

Every component works; the *seams between them are not commands*. Three of the
four blocking failures above (F-01, F-02, F-03) are the same defect wearing
different clothes: the analysis lifecycle — stage, run, index, review, re-index
— exists as correct library code with no continuous operator-facing path
through it, so the only way to cross a seam is to read `cli.py`, discover that
`stage()` is called only by `nightly`, and shell into Python; and the one seam
that *is* exposed (`analyze review`) writes a file that the operator surface
then reports as corrupt. An operator following `docs/operations.md` gets as far
as a green `evallab status` and then hits a raw `FileNotFoundError` with no
next step. The fix is not more features: it is to make the analysis lifecycle a
first-class command sequence (`analyze stage`, then `run-one`, then a `review`
that indexes itself), give every refusal a "do this next" line instead of a
traceback, and make the explorer's discovery agree with `status`'s discovery
so that what the catalog joined does not appear unlinked to the human looking
at it. Until then, this lab is operable by the person who wrote it and by
nobody else.

---

## Statement of result

The end-to-end path is **not `proven live`** on merged main as a single chain.

- `task -> run -> immutable evidence -> ingest -> facts` is **`proven live`**:
  four free control jobs, six trials, real Docker execution, real verifier
  discrimination (oracle 1.0 / nop 0.0), clean queue settlement, catalog and
  Parquet projection with zero exceptions.
- `facts -> trajectory` is **`blocked`**, structurally, for every agent
  subscription-only policy permits. Not a bug — a consequence of proving the
  system with controls that have no trajectory to record.
- `trajectory -> analysis sidecar` is **`fixture-proven only`**: the sidecar is
  durable, provenance-frozen, and correctly refuses to be produced by the M006
  worker while the calibration gate is closed, but its input was a saved
  response and its citations are verifier files, not agent behaviour.
- `analysis sidecar -> operator surface` is **`proven live` in the catalog,
  `proven live` in `status`/`compare`/dashboard, and `blocked` in the run
  explorer** for nested `jobs_dir` layouts and for reviews.

The transcript proving all of the above is `runs/m009/transcript.log`,
36 recorded commands: 30 exit 0, 4 exit 1, 2 exit 2. The four exit-1 results
are the workbench's failed certification of `event-summary` (F-06), the two
`evallab trace` refusals (F-05), and the calibration gate's correct deferral.
The two exit-2 results are one argparse usage error of my own (I invoked
`task_workbench plan` without its three required `--source-*` arguments) and
the unstaged-request `FileNotFoundError` (F-01).
