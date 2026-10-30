# QA data-access map audit — 2026-10-29

## Question and answer

Is the external QA data-access map still a reliable recipe against the audited origin/main code and the supplied read-only backup? **Not without corrections.** The admitted store census is reproducible, but command behavior and access guidance have changed.

Audited source revision: `d0608c9da4e7cd8dd9323f98d4e3587db5298904`. Map SHA-256: `8b608e7d523fcab9d62d0b77a764760498349412ac9793b4005e193d909f2762`. Backup size: 4,207,423,488 bytes. The external map has 594 lines and is not edited by this PR.

## Numbers and meaning

| Measure | Count | Meaning |
|---|---:|---|
| Runtime probes executed | 89 | Includes all 14 subcommand help calls and supplemental boundaries; excludes the deferred validator |
| Probes working as documented | 80 | Expected refusal probes count as working |
| Probes with documented-contract differences | 8 | Source-reviewed behavioral/schema differences, not row-count drift |
| Failing probes | 1 | Actual CLI failure relative to the recipe |
| Fenced shell recipes | 84 | Logical commands; continued lines and heredocs stay together |
| Fenced recipes runnable/exercised | 57 | Instantiated safe form, not literal placeholders |
| Fenced recipes not run | 27 | Setup/context or unavailable/unauthorized inputs; reason recorded for each span |
| Inline command mentions | 97 | Separately indexed references, not added to fenced denominator |
| Claim-line units | 362 | All prose/table/comment claim-bearing source lines, conservatively inclusive |
| True / false / unverifiable claim units | 64 / 28 / 270 | Source-reviewed decisions; historical/external claims are not assumed true |
| Proposed corrections | 20 | Full paste-ready replacements below; related repeated locations are grouped |
| Registered qa-data call types | 14 | Includes trace-card; 17 in the map denotes staged corpora, not call types |
| Admitted tasks / runs | 3,474 / 4,544 | Unchanged from the map's backup census |
| Reproducible facet runs / answer-material leads | 1,234 / 29 | Admission-screened facet matches, not verdicts |

## Most consequential defects

- Bare findings exits 2 instead of returning the global census claimed by two recipes.
- task can return prioritized file contents without --file for registry-backed packages; a names-only assumption is unsafe.
- EVALLAB_PROD_STATE does not redirect the default cloud database; --cloud-database is required.
- Raw export-runs now supports --trace-ids-file and --strict; the map incorrectly says otherwise.
- Both findings selectors return a grouped object, while trace-card now provides a compact single-run accessor.

## Proposed corrections (ready to paste)

This table contains schema/command guidance and counts only, no task, trace, source data, seal or label values. C12 is a **current audit-protocol incompatibility**, not a product-code regression. C13/C14/C15/C16 include missing safety/availability guidance as well as literal stale statements.

| ID | Map lines | One-line description | Full replacement text |
|---|---|---|---|
| C01 | 25, 26, 570 | status no longer writes | CloudTraceDatabase(path) still initializes writable by default: use read_only=True. evallab prod status now opens an existing queue strictly read-only; it neither creates nor migrates it and refuses a cloud trace database. Do not substitute it for qa-data. |
| C02 | 65 | database environment override is wrong | The default cloud database is ~/.local/state/evallab-prod-live/cloud-trace-db.sqlite. Override it with --cloud-database PATH. EVALLAB_PROD_STATE changes the auxiliary facet, cluster and trace-tool ledger directory, not this default database path. |
| C03 | 64 | JSON guarantee excludes parser | A successfully parsed call prints one JSON document {ok, data \| error, meta}; exit 0 is success and handler refusals/failures exit 2. Argparse usage errors instead print text on stderr and exit 2; --help prints text and exits 0. |
| C04 | 120, 121, 122 | format-specific path keys and ordering | The receipt contains admitted, exported, refused and manifest_sha256. Per-run entries have trace_id, blind_id and actor_inputs_available; ATIF uses file and includes steps, evidence_steps_original and evidence_steps_blinded, whereas Harbor and Scout use path and have no step map. Facet selections are suspicion-ranked; explicit ids preserve deduplicated request order. A non-admitted explicit id refuses the entire call before publication. |
| C05 | 144 | raw exporter gained strict ids files | evallab prod export-runs accepts --trace-ids-file FILE and --strict. Without --strict it can still exit 0 with refused runs; --strict fails before publication when any requested id is unavailable. It has no facet selector. Prefer qa-data export for admission-first facet selection and strict explicit ids. |
| C06 | 216, 218, 393 | single-run accessor exists | For a compact single-run lookup use qa-data trace-card RUN_ID [--no-ledgers]: it combines stored detector findings, facet leads, original-step citations, trace flags and playbook cards. This is not the full raw facet record; for that record use traces to obtain task_id, runs --task TASK_ID with sufficient --limit/--budget, then select run_id. runs still has no --ids-only. |
| C07 | 516, 549 | bare findings now fails | qa-data findings requires exactly one selector: --run TRACE_ID or --task TASK_ID. With neither or both it exits 2 with give exactly one of --run or --task. It no longer provides a global per-tool census; do not use bare findings to rederive historical aggregate counts. |
| C08 | 517, 524, 526 | both findings modes return grouped object | Both findings --run and findings --task return an object with findings grouped by presented run id, counts, coverage, withheld_strings, dedup_capped, omitted and truncated (a minimal summary is possible when the budget cannot fit counts). Each finding retains tool, detector, run_id, flag, evidence_steps, at and summary. --limit defaults to 20 records across groups, independently of the token budget. Detector text is screened; Scout-METR and Docent detector names are not unconditionally replaced with [withheld]. |
| C09 | 527 | findings has multiple input ledgers | findings reads results-v1.jsonl and laminar-own.jsonl under the selected state's trace-tools directory, plus trace-tools/tt7/docent/arms/*-canonical.json. It deduplicates identical mirrored observations, drops malformed/oversized/unadmitted records and returns admitted per-run coverage; these counts are not raw file line counts. |
| C10 | 448 | history not confirmed-only | qa-data history TASK_ID returns the task's non-refuted own issues (including provisional), similar admitted examples and playbook cards. --with-outcomes adds issue trust_tier and available original/repaired reward differential for scoring only; default output omits these outcome fields. |
| C11 | 455 | package resolution is not CAS-only | file_count includes withheld files. qa-data task can use CAS-backed packages or resolve a registry-pinned local package from approved locators with digest verification and protected-path filtering. --cloud-database redirects only SQLite, not CAS or package roots: a backup alone is not a self-contained task bundle. |
| C12 | 30, 353, 358, 359, 367, 372, 573 | safe-column csv still bypasses registry | Never read assignments.csv directly in QA audit code, even by selecting safe columns after csv.DictReader: parsing still materializes protected columns. Use CorpusRegistry admission APIs and qa-data split; unavailable ids receive the same refusal and must not be probed for TEST membership. Historical split/cross-key censuses need a separately authorized registry-based aggregate, not the direct CSV snippets below. |
| C13 | 114, 149 | export hard cap omitted | qa-data export publishes at most 200 runs per call. --limit, when supplied, must be 1..200; without it a selection exceeding 200 is refused before publication. Narrow the facet filter or provide --limit; --budget only controls the receipt's per-run list, not this hard cap. |
| C14 | 237 | cluster facet intersection can silently undercount | To count a facet over a cluster, intersect its cloud_run_ids with the complete matching run_id set from runs --facet: raise --limit and --budget until data.truncated and meta.truncated are false. --limit 200 is not guaranteed complete; budget clipping can still occur below that limit. Bind cluster ids to their build and distinguish DB from file membership. |
| C15 | 223, 224, 547 | recount does not snapshot auxiliary state with database | recount compares the selected read-only cloud database against the auxiliary facets-v1.jsonl selected by EVALLAB_PROD_STATE (default live state). It locks and hashes that file and keeps the latest screened record per run; --cloud-database does not redirect the JSONL. Report both sources and the JSONL digest. Historical JSONL counts differ between these two paragraphs and must remain explicitly dated rather than described as one current census. |
| C16 | 14, 55 | help is not sole authority and call count discrepancy | qa-data currently registers 14 call types, including trace-card. The help guide still contains stale aggregate-findings and no-single-run-lookup guidance: use subcommand --help for flags and implementation/runtime evidence for behavior. There is no evidence for 17 qa-data call types in this revision; the map's 17 refers to staged corpora, not calls. |
| C17 | 442, 446, 447 | task can return file content without --file | qa-data task may include prioritized verifier and environment file contents automatically for registry-backed packages, even without --file. --file explicitly requests an allowed relative path and prioritizes it within the budget; without it do not assume a names-only response. Protected solution/scoring-key paths remain withheld. Inspect packages[*].contents and its truncation/omission metadata. |
| C18 | 255, 510 | command-localization exclusivity is stale | The registry now marks 2 staged corpora as command-level-localization scorable. Enumerate them with the registry scoring.command_level_localization_scorable query instead of assuming there is only one. This is registry metadata growth; keep each corpus's own unit, gold definition, admission and false-alarm basis separate. |
| C19 | 224, 571 | manual facet intersection misses sealed-text screening | Do not reconstruct the QA-served facet set by taking the last raw JSONL record per run_id and intersecting inventory ids alone. Served facets also screen task ownership and record text for sealed identities; an admitted run's facet can still be withheld. Prefer qa-data runs for served records and qa-data recount for counts, and do not expose raw facet ledgers to QA readers. |
| C20 | 246, 256, 303, 306, 332 | external registry counts have grown (drift) | At the audit snapshot the registry contains 33 staged corpora, with 31 allowed for local Trace Watcher access and 7 held-access entries. Its false_alarm_basis metadata marks 18 yes, 10 upper_bound and 5 no. These are metadata counts, not independently validated scientific verdicts; enumerate the current registry instead of treating the older 17-corpus table as exhaustive. Historical table rows may remain explicitly dated. |

## Legitimate drift, separate from errors

The cloud backup census and facet counts above are unchanged. Auxiliary live files and external registry metadata can grow independently; do not report this as a command defect.

| Map lines | Field | Old | Observed | Classification |
|---|---|---:|---:|---|
| 223, 224 | jsonl_lines | 3118 | 6969 | drift |
| 223, 224 | jsonl_unique_runs | 2703 | 4168 | drift |
| 223, 224 | jsonl_only_runs | 1469 | 2934 | drift |
| 246 | entries | 17 | 33 | drift |
| 260 | hold | 7 | 7 | unchanged |
| 256 | trace_watcher_allowed | 15 | 31 | drift |
| 255, 510 | localization_scorable | 1 | 2 | drift |
| 303, 332 | fa_rate_yes | 9 | 18 | drift |

## Reproduce

Run from this checkout with its locked Python environment. All exports, sealed manifests and scratch caches stay under a **new** child directory of `/private/tmp/ps7j3/`. Replace the map path only with the same map revision: the review line numbers bind to its retained digest. The script never evaluates arbitrary shell, dispatches a model, starts a watcher or opens the production database.

```bash
EVALLAB_DISABLE_LOCAL_DOCKER=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B \
  research/evidence/runs/ps7-data-map-audit-20261029/audit.py \
  --map /Users/petermakhnatch/Developer/research-context/bridge/2026-10-27-run-atlas/data-access-map.md \
  --database /private/tmp/ps2/store.sqlite \
  --scratch /private/tmp/ps7j3/reproduce-new \
  --review research/evidence/runs/ps7-data-map-audit-20261029/review.json \
  --output /private/tmp/ps7j3/reproduced-results.json
```

Source citations live in `review.json`; exact per-probe exit codes, durations, field sets, recipe spans and conservative claim classifications live in `results.json`. Failed bootstrap attempts were implementation errors in the scratch harness, corrected before the recorded audit; they are not map failures.

## Limits and verification

- The map itself and the SQLite backup were not edited; no product code or tests changed.
- All store-reading qa-data probes received --cloud-database; scope used --source-database.
- Auxiliary JSONL/cluster/finding files remain separately timed live read-only inputs, not part of the backup.
- No held-out answer-key rows were read directly. CSV census snippets were not run; registry admission owns access.
- No cross-test adapter selfchecks, synthetic/real gold scoring, production snapshots, unsafe atlas show or live services were run.
- External metadata jq outputs were captured only in-process; evidence retains counts/field names, not corpus identifiers or text.
- Probes instantiate placeholders with admitted ids; export cohorts are bounded to 2, rather than reproducing historical large exports.
- Claims are exhaustive physical source-line units (including code comments and numeric headings), not atomized sentences; compound unknown claims remain unverifiable.
- Historical rater agreement, model quality, task-key counts, no-actor full-corpus projection and remote server state are not independently reestablished.
- No new regression test: this is a documentation investigation, and the observed baseline defects are the result, not changes to product behavior.

The audit is a smoke execution of the real CLI against the backup, not a mocked contract or source-text test. Product code is unchanged, so a fail-main/pass-branch regression test would be misleading; the same bare-findings recipe fails on the audited baseline by design.

Static gates passed: `make docs`, Ruff, docindex, repomap, governance,
lessons freshness and `ty@0.0.71 check src/ --output-format=concise`.
The pinned type checker was available in the host cache, not the scratch cache;
its successful invocation used `uvx --offline` without downloading anything.
Generated indexes were unchanged.

The final run reuses 87 counts-only probes from the same audited code/map/store
and refreshes task, history and cluster scope. Its earlier full run took 87.41 s.
The task-content warning is source-verified conditional behavior: the sampled
CAS-backed task returned zero implicit contents, so this sample does not
demonstrate the registry-backed automatic-content path.
The registry validator was not completed (a retained earlier attempt exceeded
150 seconds); its two recipes are classified not runnable here, not successful.
