# SB-1 strict clean seeker cohort

**44 qualified, distinct task names: 36 MiMo and 8 TB4. The >=100 target is not
met.** These are **static-plan-qualified**, not a claim of measured runtime
success or permission to execute. This census made zero model calls, executed
zero graders, and reserved $0.

| Dataset | Candidate identities | Qualified |
|---|---:|---:|
| `breaker-bench-v1` | 214 | 0 |
| `mimo-defect-map-v1` | 118 | 36 |
| `tb2-to-tb21-v1` | 52 | 0 |
| `tb4-audit-v1` | 66 | 8 |
| **Total** | **450** | **44** |

The ledger is `research/answer-keys/splits/assignments.csv`, SHA-256
`7f4703f623952545f75a89d7ec223280b043fb8b8ca45a6660f3b2beda5f880f`.
Only **81** of the 450 identities pass its affirmative-DEV/cross-key gate.
Even making all 37 remaining ledger-positive candidates runnable would give
81, not 100. TEST/heldout/held assignments were not changed or bypassed.

## Rebuild ($0, local source reads only)

From a repository checkout with the referenced local packages available:

```bash
EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run python research/experiments/sb1-clean-seeker-set-20261027/build_cohort.py
```

The builder also unconditionally sets `EVALLAB_DISABLE_LOCAL_DOCKER=1`. Every
candidate-runner call has `plan=True`, `backend="modal"`,
`allow_local_docker=False`, and a 600-second watchdog. It passes no approval,
spend ledger, provider token, or model route. Its inert marker submission is
hashed for planning only and is never executed. The port route plans the
**original package with `grader="port", port_task=<port>`**, exactly the
comparison target used by `verify_differential_replay`; package repairs plan
the repaired package with `grader="upstream"`.

The default lab root is `~/Developer/eval-lab`; the default CloudDB is
`~/.local/state/evallab-prod-live/cloud-trace-db.sqlite`. `--lab-root` and
`--cloud-db` can select other local roots. Absolute source locators inside
manifests remain authoritative; the builder does not rewrite or download them.
CloudDB is opened with SQLite `mode=ro`, `query_only`, and a read transaction;
only paired-digest metadata is queried, never trace steps or instructions.
Missing CloudDB is explicitly reported as unavailable, not silently treated as
a successful CloudDB census.

Output JSON is sorted, contains no timestamps, and is not rewritten when bytes
are unchanged. Two consecutive builds produced identical hashes for **both**
artifacts. Inputs include Git HEAD, builder/source-file digests, the assignment
ledger, the CloudDB query snapshot, and local package bytes/modes. Changing any
of those inputs can legitimately change a later rebuild. The committed Git
HEAD is the source revision observed at generation, not a self-referential
claim that the generated files already existed in that commit.

## Candidate universe and ordered gates

Sources are the 79 gym pair rows; MiMo's 567-original inventory and 57 repair
edges; the executed-clean manifest (81 packages, of which unchanged controls
are not invented repair pairs); the retained TS3 task identities; the
TB4 `CorpusRegistry` task metadata; both external answer-key pair indices;
and CloudDB's 110 paired-digest task records (69 MiMo, 15 TB4, 26 TB2).
`source_coverage` records exact queried roots/counts. MiMo's known task-store
variant roots and inventory-resolved original roots are checked, including the
retained native-executor worktree roots used by the seeker worker. External
campaign discovery is bounded to retained campaign `packages/` roots, not an
unqualified scan or reconstruction of task bodies.

One census row represents one `(dataset_id, bare task name)`. Repeated models,
repair versions, CloudDB variants, and gym aliases supply alternatives, not
additional tasks. An inventory original without any repair source is not a
new pair. For a task with alternatives, the best available pair is selected
by how far it advances through these gates; all unsuccessful alternatives
remain in its census row. Each candidate is counted exactly once.

1. **a — ledger:** use the existing worker helpers to require an affirmative
   DEV/train assignment and zero non-DEV assignments under **any** key,
   including namespaced and `__` bare-name aliases. Unlisted means excluded.
2. **b — original:** require an existing directory and readable strict source
   tree; reject unsupported links/special files.
3. **c — repaired:** require an existing repaired package or the port adapter's
   required local input files, a readable digest, source-pinned package-digest
   matches where supplied, and a grader-source digest different from original.
4. **d — plan:** both candidate-runner source-only plans must be runnable,
   unexecuted, unreserved, and free of errors/refusals. Port setup recipes are
   interpreted by the existing adapter, never executed by this builder.
5. **e — tier:** inspect the TOML/Dockerfile and planned resources; exclude
   explicit Docker-only/socket contracts, GPUs, and memory above 24,576 MiB.
   An amd64 `FROM` alone is not a blocker. Earlier failures retain their earlier
   reason code even if a later resource restriction would also fail.

The digest algorithms are named in both artifacts. Package digests use
`evallab.registry.task_directory_digest`; strict exact-tree digests additionally
bind modes/empty directories. Grader-source digests cover `tests/` (or
`verifier/`), or the entire port source. A successful port plan additionally
records its actual adapter grader digest. Static-plan digests hash canonical,
sorted-key compact JSON returned by the existing planner. No task body,
assertion, solution, prompt, or answer-key label is retained. `assignment_rows`
contains only the five identity/split columns needed to demonstrate DEV
admission, not the ledger's expected-verdict/exposure columns.

Each qualified row includes `original_reservation_usd` and
`repaired_reservation_usd`, plus the native pricing basis. Package estimates
come from the plan's `modal_reservation`. A source-only port plan deliberately
has no native reservation until materialization; its estimate therefore uses
the existing native `_reservation` formula on the plan's exact adapter resource
declarations, marked `adapter_resources_native_formula`. These are conservative
per-grader-run estimates, **not** executed charges or observed invoices.

## Binding gates and required unlocks

| First failing gate | Count |
|---|---:|
| `a_cross_key_non_dev` | 327 |
| `a_no_affirmative_dev` | 42 |
| `b_original_missing` | 29 |
| `c_repaired_missing` | 2 |
| `d_original_plan_refused` | 1 |
| `d_repaired_plan_refused` | 5 |
| Qualified | 44 |

The **binding overall gate is the ledger** (369 exclusions). The largest
remaining runnable gate is unresolved original native packages (29 eligible
Breaker identities). Unlocking these requires source-bound original **and**
hardened native packages in retained local custody, not interpreting a
Benchflow sandbox-version label as a materialized grader.

The two eligible MiMo repair gaps are:

- `format-code-task-000030`: an original resolves, but no repaired package
  locator/digest resolves in the enumerated pair sources.
- `format-code-task-002486`: the required child
  `sha256:628a68c0f4cf0b6e1070d180332ce3adbbbc01986c7a1125a19079eb1aa146d9`
  is not materialized at the checked native/task-store variant roots. Its
  custody owner must provide that exact child, not a new guessed repair.

Five TB4 repairs need currently unsupported offline setup-adapter inputs or
operations: `distributed-dedup`, `html-js-filter`,
`layout-config-recreation2`, `vba-userform-port`, and `vpp-loss-divergence`.
`jax-speedrun-gpu` already fails the original static plan and is not counted.
Refusal categories and full plan hashes are retained without copying exception
text that could embed source expressions.

Reaching 100 therefore requires **additional genuinely eligible task identities
outside the current 81 ledger-positive pairs**, with source-bound original and
repaired packages and appropriately authorized DEV assignments. Restoring
custody and adapter support alone is insufficient. No heldout relabeling,
unlisted-task padding, repair synthesis, or paid qualification was performed.

## Files and checks

- `cohort.json`: `evallab.sb1_clean_seeker_set/v1`, qualified task inputs for the
  error-first driver.
- `census.json`: counts, provenance, first exclusion reasons, source coverage,
  and all 450 accounted identities.
- `build_cohort.py`: deterministic source-only discovery, gating, and generation.
- `tests/test_sb1_clean_seeker_set.py`: isolated unit tests for ledger-positive
  admission, cross-key/alias seals, source-only call flags, gate ordering,
  digest-only evidence, tier boundaries, and deterministic census accounting.

```bash
EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run pytest tests/test_sb1_clean_seeker_set.py -q
EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run ruff check .
EVALLAB_DISABLE_LOCAL_DOCKER=1 uvx ty@0.0.71 check src/ --output-format=concise
```
