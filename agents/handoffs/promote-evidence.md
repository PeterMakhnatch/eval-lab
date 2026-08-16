Status: review-wanted
Last: opened PR #58 promoting the three scored 2026-08-15 Codex canary jobs into research/evidence/runs/ as redacted bundles (666,650 B, 120 files, per-file parent SHA-256 in PROMOTION.json), choosing redaction option (b) because ATIF system/user steps carry the verbatim Codex vendor system prompt, and repointing PROGRAM.json references.jobs plus STATUS.md at the committed paths — validator OK
Next: integrator review of PR #58 at its current head, above all the redaction decision (option b) and the R1/R2/R3 rules; if the reviewer disagrees with promoting agent output text at all, the fallback is dropping `agent/codex.txt` and keeping only `trajectory.redacted.json`; do not merge from this role
Blockers: none

# PROMOTE-EVIDENCE handoff

Agent/model: Claude Opus 4.5 (Anthropic), Oh My Pi harness, subscription-only.
Worktree `.worktrees/promote-evidence` on `role/promote-evidence`, branched from
`origin/main` `ad67126`. No paid model call, no Harbor run, no Docker build, no
cloud sandbox, no deploy, no publication, no API-key environment variable. No
`evallab` command that writes was run. Nothing under `policy/`, `src/`, `tests/`,
`docs/`, or `library/` touched. The primary checkout `~/Developer/eval-lab` was
read only: `runs/` was read, never written, and no `git` state-changing command
was run there.

Addresses **F-05** from `docs/checkpoints/2026-08-16-m009-integration-flight.md`
(no trajectory evidence for any real agent this repo may run) and the ledger gap
that checkpoint records at line 192 (`PROGRAM.json` `references.jobs` is `[]`
while `STATUS.md` cites runtime `runs/`).

Leased paths, all written: `research/evidence/`,
`research/experiments/PROGRAM.json`, `research/experiments/STATUS.md`,
`agents/handoffs/promote-evidence.md`. No new repository-root entry, so
`agents/STRUCTURE.md` needed no edit (`research/evidence/` is line 79 there).

## 1. Survey of the source corpus

`~/Developer/eval-lab/runs/` holds 11 `canary-*-codex-*` job directories,
4,505,504 B of file content (`du -sh` reports 6.3 MB with block overhead).
Eight contain `agent/trajectory.json`; **23** trajectory files exist across those
eight (not 24 — the batch contract's figure is one high;
`canary-transaction-reconciliation-codex-20260816` has 2, not 3).

| job | trajectories | bytes |
| --- | ---: | ---: |
| `canary-event-summary-codex-20260814` | 0 | 64,721 |
| `canary-event-summary-codex-20260815` | 3 | 307,696 |
| `canary-event-summary-codex-20260816` | 3 | 298,004 |
| `canary-terminal-bench-html-js-filter-codex-20260814` | 0 | 64,050 |
| `canary-terminal-bench-html-js-filter-codex-20260814-r2` | 3 | 241,330 |
| `canary-terminal-bench-html-js-filter-codex-20260815` | 3 | 1,386,486 |
| `canary-terminal-bench-html-js-filter-codex-20260816` | 3 | 1,395,447 |
| `canary-transaction-reconciliation-codex-20260814` | 0 | 39,379 |
| `canary-transaction-reconciliation-codex-20260814-r2` | 3 | 221,947 |
| `canary-transaction-reconciliation-codex-20260815` | 3 | 273,211 |
| `canary-transaction-reconciliation-codex-20260816` | 2 | 213,233 |

A trial directory is `config.json`, `lock.json`, `result.json`, `trial.log`,
`artifacts/`, `verifier/`, and `agent/` = `codex.txt` + `trajectory.json` +
`sessions/<Y>/<M>/<D>/rollout-*.jsonl`. `trajectory.json` is `ATIF-v1.7`:
`schema_version`, `session_id`, `agent{name,version,model_name,extra}`,
`steps[]`, `final_metrics{...tokens, cost, steps}`. A step is `step_id`,
`timestamp`, `source`, `message`, and optionally `model_name`, `tool_calls[]`
(`tool_call_id`, `function_name`, `arguments`), `observation`, `metrics`,
`llm_call_count`, `extra`.

Existing promoted shape, matched exactly: `research/evidence/runs/<harbor job
name>/` with job-level `config.json`, `job.log`, `lab-metadata.json`,
`lock.json`, `result.json`, and one `<task>__<trial>/` subtree per trial. The two
existing bundles (`event-summary-oracle-evidence`, `event-summary-nop-evidence`)
total 31,716 B; the oracle bundle's `agent/` holds a 0-byte `oracle.txt`, which is
exactly F-05.

**No `evallab` promotion path exists.** `evallab --help` lists 29 command groups
and none is `promote`. `src/evallab/gc.py` only compresses and prunes
*unpromoted* jobs, and treats `research/evidence` as a protected layout
(`gc.py:202-208`); `retain` there means "do not prune", not "copy in".
`docs/analysis-loop.md:253` forbids *automatic* promotion, so promotion is a
human-reviewed PR by design. I therefore added the mechanism inside my lease:
`research/evidence/promote_codex_bundle.py`, deterministic and re-runnable, with
`--verify` and a `--force` guard because `agents/STRUCTURE.md` calls these
bundles immutable. Re-running with `--force` reproduced the bundles byte for
byte.

## 2. Redaction decision: **option (b), promote a redacted form**

Option (a) is impossible. Quoting the fields I inspected in
`runs/canary-event-summary-codex-20260815/event-summary__5E3btLv/agent/trajectory.json`:

- `steps[0]` — `source: "system"`, `message` 3,858 chars, begins
  `"<skills_instructions>\n## Skills\nA skill is a set of instructions provided
  through a `SKILL.md` source..."`.
- `steps[1]` — `source: "system"`, 2,264 chars, begins ``"You are `/root`, the
  primary agent in a team of agents collaborating to fulfill the user's
  goals."``
- `steps[2]` — `source: "system"`, 271 chars, `<multi_agent_mode>`.
- `steps[3]` — `source: "user"`, 2,135 chars, `<recommended_plugins>` listing
  uninstalled plugins.
- `steps[6]` — `source: "system"`, 1,014 chars, `<plugins_instructions>`.
- `steps[4]` — `source: "user"`, 710 chars, the task instruction.

That is the Codex vendor system prompt verbatim. Across the nine trajectories of
the three selected jobs the step census is **31 `system`, 18 `user`, 67 `agent`**,
and the five vendor-prompt markers appear in **zero** `agent`-source steps.
`agent/sessions/**/rollout-*.jsonl` is worse: it carries the same prompts plus
`payload.encrypted_content` (a 2,596-byte base64url reasoning blob in
`.../terminal-bench-html-js-filter__5rgjEEt/.../rollout-2026-08-15T06-42-37-*.jsonl`).
`AGENTS.md:22-23` bars all of it.

Option (c) was not necessary, because the reduced form the assignment sketches
(rewards, metrics, step counts, tool names, digests) is achievable without a
policy decision, and no credential was found.

**Credential scan.** 69 `agent/` files across the eight jobs, six patterns
(`sk-…`, `gh[pousr]_…`, JWT, `AKIA…`, PEM header, and `api_key|access_token|
refresh_token|id_token|client_secret|password|bearer` assignments). One hit, and
it is a **false positive**, confirmed by the repository's own gate. The match
begins at offset 1,438 inside the 2,596-byte `/payload/encrypted_content` value
above and runs 1,156 characters; the eight characters preceding it are `eG5AneP7`,
so `sk-` is spelled by the tail of one base64url run and the head of the next,
not by a key.

`tests/test_repository_contract.py:27` requires `(?<![A-Za-z0-9_])` before `sk-`
and does **not** match this content, because the preceding character is `7`. My
own scan omitted that lookbehind, which is the only reason it fired. Two facts
follow. The repository gate would not have flagged the rollout file even if R2
had promoted it, so R2's justification is prompt and reasoning content, not
credentials. And the literal run is deliberately not reproduced in this file:
quoting it after a backtick strips the preceding token character, turns it into a
standalone `sk-` plus 30+ characters, and made
`test_repository_has_no_high_confidence_secrets` fail on this very handoff —
which is the gate working, and is why the description replaced the quotation.

No real credential exists in the corpus. The final promoted tree scans clean on
all six of my patterns and on the repository's four, and the file holding the run
is omitted under R2 regardless.

**Rules applied** (full text in the script's module docstring, machine-readable
in each `PROMOTION.json`):

- **R1** `agent/trajectory.json` → `agent/trajectory.redacted.json`. Every
  `system`/`user` step's `message` becomes `null` plus `message_sha256` and
  `message_chars`. `agent`-source `message`, `tool_calls` and `observation` stay
  verbatim — they are agent output and environment response, not prompts, which
  keeps the `arguments.input` classification in
  `analysis/html-js-filter-codex-20260815-brief.md` reproducible.
- **R2** `agent/sessions/**` omitted, SHA-256 recorded. 9 files, 827,328 B
  dropped.
- **R3** `verifier/*` only, never `agent/`. R3a: JSON string values over 1,024 B
  become digest markers — the largest legitimate CTRF string is an 85-byte test
  name, so this removes exactly `results.tests[].trace`. R3b: text files over
  4,096 B are promoted as a whole-file digest marker with no body.

R3's motive is separate from prompts. `terminal-bench-html-js-filter`'s verifier
loads its attack-vector corpus from an archive baked into the verifier image
(`tests/test_outputs.py:98,337`) and pytest echoes the whole failed batch on
failure: `verifier/test-stdout.txt` is 77-80 KB of which one line is 64 KB, and
CTRF `trace` is ~68 KB. `PROGRAM.json` `EXP-N1` blocks copying that hidden
input. I first tried line-level and signature-level filtering
(`<iframe srcdoc="&lt;`, from `test_outputs.py:317`); it left residue, because
the rendered corpus also spans ~260 short lines that no per-line predicate
catches safely. Whole-payload replacement is safe by construction. Cost: the
per-trial failed-batch counts (`assert 12 == 0`, `11`, `15` for D3GZpFU,
5rgjEEt, kzGxL7Q — observed by me in the unredacted originals) are no longer
quotable from the promoted text; rewards, statuses, test names and timings are.

**Residue audit of the 120 promoted files.** Zero vendor-prompt markers, zero
secret-shaped strings, all JSON parses. `<iframe srcdoc`, `onerror=alert`,
`javascript:alert` and `&lt;script&gt;` survive **only** under `agent/` (6, 6, 4
and 2 files) and are the agent's own self-test payloads: in
`terminal-bench-html-js-filter__kzGxL7Q/agent/trajectory.redacted.json` the
occurrence is inside `steps[10]`, `source: "agent"`, in a payload the agent wrote
to exercise its own filter. Zero occurrences under `verifier/`.

`agent/codex.txt` (the raw `codex exec --json` tee) is promoted **verbatim**: its
event types are only `thread.started`, `turn.started`, `item.started`,
`item.completed`, `turn.completed`, and it contains none of the five
vendor-prompt markers, no reasoning text and no encrypted payload. It is the
pre-ATIF primary source and the only unconverted record of the session.

`job.log` and `trial.log` are verbatim. They embed the task instruction as the
`codex exec` argument, and I verified it is this repository's own committed
content, not vendor material: extracting the quoted argument and diffing against
`library/tasks/<task>/instruction.md` gives byte equality for `event-summary`
(710/710) and, for the other two, differences that are *only* shell quote
escaping (`'"'"'`) and a stripped `<!-- harbor-canary GUID … -->` comment.

## 3. Selection rule and the bundle

**Rule:** exactly the jobs `STATUS.md` names as the scored 2026-08-15 set — one
job per `policy/canary-suite.yaml` member, `k=3`, `exception_info` `null` on
every trial, and named by `PROGRAM.json` `EXP-S01-canary-codex-k3`'s
`decision_rule` as "the scored set". Three of eight eligible jobs. Excluded:
the three 2026-08-14 jobs (0 trajectories; 9/9 `ValueError`, outside the
capability denominator), the two `-r2` jobs (`NonZeroAgentExitCodeError`), and
the three 2026-08-16 jobs (not cited by `STATUS.md`, no retained baseline, and
one is incomplete at 2 trials).

| bundle | trials | reward | promoted | on disk |
| --- | ---: | --- | ---: | ---: |
| `canary-event-summary-codex-20260815` | 3 | 3/3 `1.0` | 44 files, 109,647 B | 131,119 B |
| `canary-transaction-reconciliation-codex-20260815` | 3 | 3/3 `1.0` | 35 files, 103,667 B | 121,772 B |
| `canary-terminal-bench-html-js-filter-codex-20260815` | 3 | 0/3 `1.0` | 38 files, 393,916 B | 413,759 B |

**Total added: 666,650 bytes** (651 KiB) across 120 files, from 1,967,393 B of
source — a 66% reduction, and 9 files omitted outright. `research/evidence/`
goes from 34,564 B to 717,680 B, the difference being the three bundles plus the
13,446 B promotion script and the rewritten `README.md` (496 B → 3,516 B).

**Parent digests.** Each bundle's `PROMOTION.json` records, per source file:
`source_path`, `promoted_path`, `action`, `rule`, `source_bytes`,
`source_sha256`, `promoted_bytes`, `promoted_sha256` — 126 file entries in
total, including the 9 omitted ones. Each manifest also records
`source_job_result_sha256`, and those three digests are **identical** to the ones
already published in `research/experiments/baselines/codex-canary-20260815.md`:

| bundle | job `result.json` SHA-256 | matches baseline |
| --- | --- | --- |
| event-summary | `d471db9c534aa7d5a12661b7832555778099d0d25604feb92ef837da3695863d` | yes |
| transaction-reconciliation | `cf134cbb67126fdd1646141102fb03ba9cd7f207cfead5c897dfd00bb5b6a198` | yes |
| html-js-filter | `1b860cfe0e674675171a43727ffe776329f73f09c16a55982bbd9c025bf87b2c` | yes |

## 4. Ledger gap closed

`references.jobs` now cites committed paths on the seven experiments whose
`evidence_provenance.basis` names the retained primary cell, and each of those
`basis` strings was rewritten to say so:

| experiment | `references.jobs` |
| --- | --- |
| `EXP-S01-canary-codex-k3` | all three bundles |
| `EXP-S02-txn-recon-k` | txn bundle |
| `EXP-S03-preamble-ab` | event-summary bundle |
| `EXP-S04-claude-vs-codex` | event-summary bundle |
| `EXP-N1-html-js-official-tests` | html-js bundle |
| `EXP-N2-event-summary-sol-vs-terra` | event-summary bundle |
| `EXP-N3-claude-code-event-summary` | event-summary bundle |

`EXP-S05-curated-nominees` and `EXP-S06-query-optimize-register` keep
`jobs: []` on purpose: both are `inherited_unresolved` with no scored Codex job
in the primary store. `updated_at` moved to `2026-08-16`. The diff is
+31/−15 lines; no reformatting.

`STATUS.md`: the three RECENT bullets and the `gpt-5.6-sol` review citation now
point at `research/evidence/runs/…`, and the provenance-boundary paragraph was
rewritten — the old claim "Runtime `runs/` … are not versioned local references"
was made false by this PR for these three jobs and true for everything else, so
it now says exactly that. The 2026-08-14 and `-r2` lines still cite runtime
paths, correctly, because those jobs are not promoted.

## Validation, scoped

```text
$ uv run python research/experiments/validate_program.py
…/research/experiments/PROGRAM.json OK

$ uv run pytest -q research/experiments/tests/test_validate_program.py tests/test_program_contract.py
.....................                                                    [100%]

$ uv run pytest -q tests/test_repository_contract.py tests/test_gc.py tests/test_registry.py
..............................................                           [100%]

$ uv run ruff check research/evidence/promote_codex_bundle.py
All checks passed!

$ uv run python research/evidence/promote_codex_bundle.py --verify
canary-event-summary-codex-20260815: 47 source files recorded
canary-terminal-bench-html-js-filter-codex-20260815: 41 source files recorded
canary-transaction-reconciliation-codex-20260815: 38 source files recorded
verified 117 promoted files across 3 bundles, 0 failures
```

The validator result is load-bearing, not decorative: `validate_program.py:134`
requires every reference path to exist inside the repository. That is *why*
`references.jobs` was `[]` — a runtime path could never validate. Negative
control, run against a temporary copy so nothing in the repo changed:

```text
runtime runs/ path -> ['experiments[0].references.jobs[0] does not exist: runs/canary-event-summary-codex-20260815/']
committed ledger   -> OK (no errors)
```

Not run, deliberately: the full suite, `scripts/premerge.sh`, any formatter, any
`evallab` command, `docker compose`, `evallab tick`.

## Capability labels

- Codex canary night 2026-08-15 citable from a fresh clone — **pending in PR**.
- Redaction rules R1/R2/R3 and the digest manifest — **pending in PR**
  (mechanism exercised on real data; `--verify` and `--force` determinism both
  proven live in this worktree).
- `PROGRAM.json` `references.jobs` non-empty and validator-clean — **pending in
  PR**.
- F-05's underlying gap (no real-agent trajectory in the repo) — closed for
  three jobs by this PR, **pending in PR**. F-05 also covers empty ATIF/Parquet
  catalog tables and `trajectory_document_id` NULLs, which this PR does not
  touch.
- 2026-08-16 canary jobs, the `-r2` jobs, and the 2026-08-14 waves — **not
  promoted**, still runtime-only on this workstation.
- The failed-batch counts 12/11/15 — observed by me in the unredacted
  originals; **not** citable from the promoted bundle after R3.

## For the reviewer

1. The redaction decision is the load-bearing judgement. If you want zero agent
   text in the repository, drop `agent/codex.txt` (107,523 B across the three
   bundles) and keep only `trajectory.redacted.json`; the manifests already carry
   its digests, so nothing else changes.
2. `agents/STRUCTURE.md:79` describes `research/evidence/` as "reviewed,
   immutable control **bundles**". These are agent bundles, not controls. Only
   the description drifts, not the layout, and `STRUCTURE.md` is outside my
   lease — widen the wording if you agree.
3. `research/experiments/PROGRAM.schema.md` documents `references` but does not
   constrain `jobs` to a prefix; nothing there needed changing, and it is outside
   my lease.
4. F-01 and F-04 untouched, as instructed. F-02, F-03, F-08, F-09, F-11, F-12
   (OperatorFixes) and F-06 (WorkbenchSurface) untouched.
5. `RealTrajectoryAnalysis` confirmed over IRC that it writes only
   `research/analysis/`, `docs/checkpoints/2026-08-16-real-trajectory-analysis.md`
   and `agents/handoffs/real-traj-analysis.md`, and promotes nothing into
   `research/evidence/`. No lease collision.
