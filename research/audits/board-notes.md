# Board Notes (Audits)

- 2026-08-18 [M016]: `lessons_digest_section()` is absent on `origin/main` in `src/evallab/lessons.py` (M019's lease). In accordance with M016 instructions, M016 did not touch `lessons.py` and deferred lessons digest integration until LOOP-LESSONS exports this public function.
- 2026-08-18 [M015-audit]: `agents/handoffs/preflight.md` claimed documentation in `docs/operations.md`, but `docs/operations.md` contains no section or mention for `evallab preflight`. Recommend Platform lane / LOOP-SURFACE add preflight CLI instructions to `docs/operations.md`.
- 2026-08-18 [M015-audit]: `src/evallab/status_generator.py` targets `research/experiments/STATUS.md` by default, whereas night loop expectations target `docs/STATUS.md`. Also, no CLI entrypoint exposes `status_generator` directly. Recommend LOOP-SURFACE wire CLI and align target path.
- 2026-08-18 [M015-audit]: Finding: Disconnected Operator Surfaces (No Direct CLI Subcommand Pattern). Across multiple audited modules, implementations were delivered with comprehensive unit tests but lack top-level CLI entrypoints or production callers in `src/`: (1) `evallab.storm` has 11 tests but 0 imports/callers across `src/` and no CLI command; handoff claimed digest/status generation, leaving the engine entirely unwired in production. (2) `evallab.parquet_compaction` runs via `python -m evallab.parquet_compaction compact` (as documented in handoff), but is not exposed under the root `evallab` CLI. (3) `evallab.status_generator` has 9 tests but no CLI subcommand (`evallab status` does not invoke it) and 0 callers in `src/`/`scripts/`; its documented output `docs/STATUS.md` was never generated on main. Recommend Platform/LOOP-SURFACE wire standard CLI subcommands and connect production callers.
- 2026-08-18 [M015-audit]: Correction & Systemic Finding — Wired But Never Run (Nightly Step Registry & Scheduler Gap). (1) Correction on prior note: `evallab.storm` and `evallab.status_generator` are NOT unwired. `storm.py` is imported by `digest.py` (line 29) and `status_generator.py` (line 22) for alarm detection and markdown formatting; `status_generator.py` is imported by `automation.py` (line 34) and called at line 774 as a registered nightly automation step (`NightlyCycle`). (2) Systemic Finding ("wired but never run"): While step functions are correctly wired in `automation.py` (landed in PR #103/#106), `launchctl list | grep evallab` confirms no scheduled job is currently loaded in launchctl (despite plist presence in `~/Library/LaunchAgents/com.petermakhnatch.evallab.nightly.plist`). Because the automated pipeline has never executed unattended on this host, outputs like `docs/STATUS.md` were never generated on main. Recommend Platform/Operator load the launchctl plist or verify scheduler execution in the environment.
- 2026-08-18 [M015-audit]: Finding: `evallab.provenance` operates via module entrypoint `python -m evallab.provenance {classify,report}` but lacks a root `evallab provenance` subcommand in `src/evallab/cli.py`.
- 2026-08-18 [M015-audit]: Finding: Postgres Backup Restore Path. `src/evallab/backups.py` provides atomic dump generation and SHA-256 integrity manifest generation (`create_postgres_backup`), but provides no programmatic restore helper or CLI command (`evallab db restore`). Live custom-format restore was verified into a throwaway database using `pg_restore` (restoring 69 jobs, 83 trials, 257 rewards without error), but operators currently rely on direct manual `docker compose` invocation. Recommend adding a tested `restore_postgres_backup` / `evallab db restore` helper.

- 2026-08-18 [M024 TIDY-SQUASH]: pre-existing test-isolation defect, unowned.
  `tests/test_tidy.py::test_tidy_fixture_findings` fails when run in isolation and
  passes inside the full suite, on `main` as well as on `role/m024-tidy`
  (`assert 'z3_hot_partition' in {...}` — the retention fixture depends on state some
  earlier test leaves behind). Not fixed here: it sits in the file M024 rewrote, and
  silently editing an unrelated retention fixture during a deletion-safety mission is
  how real bugs get laundered. Needs its own small mission.
- 2026-08-18 [M021 CLI-REGISTRY]: `repomap.py` was edited outside any mission lease,
  deliberately. Converting `cli.py` to a `set_defaults(func=...)` registry removes the
  `args.command == "x"` chain that `repomap.parse_cli_commands` pattern-matches to
  attribute commands to modules. The authoring agent had kept a 106-line `if False:`
  block of dead comparisons to keep the map's output stable; that makes the map lie
  about reachability, which is the one signal this lab uses to catch built-but-dead
  code. `repomap.py` now reads the registry (`_registry_owners`) and excludes signature
  annotations from scoring (`_body_names`). Two mutation-verified tests added in
  `tests/test_repomap.py`.
- 2026-08-18 [M021 CLI-REGISTRY]: FOLLOW-UP for whoever owns `repomap.py` next. The
  Command->Module column is a name-frequency heuristic and is wrong on `main` in places
  (`verdict` -> `__version__`, which is not a module). 84 commands are attributed both
  before and after the conversion, none lost, 11 shifted; three tie-break rules were
  measured (recursion 25 shifts, first-reference 20, body frequency 11 — kept the last).
  An exact answer needs real import-graph attribution, which is a mission, not a patch.

- 2026-08-18 [integrator, found by using M024's own tool]: content-equivalence merged
  detection has a **false-negative** mode, which is the safe direction but limits the
  reclaim. `git merge-tree` compares a branch against `main` **as it is now**. Once main
  moves past the branch in any shared file — including the generated `docs/INDEX.md` and
  `docs/repo-map.md` that every mission regenerates — merging the stale branch back would
  conflict, so it classifies `unmerged` and is never swept. Measured immediately after
  tonight's five merges: `tidy` flagged only `role/m020-queue` (the last one merged) and
  left the four earlier merged worktrees as "active", holding 1.8 GB. All five PRs are
  MERGED per `gh pr list --state merged --head role/<branch>`. FOLLOW-UP: add recorded PR
  merge state as a third signal alongside ancestry and content equivalence — `tidy`
  already has a "no open PR" notion in its merged-branches sweep, so the data source
  exists. Do not fix this by loosening the content predicate; the current failure
  direction refuses to delete, which is correct.

- 2026-08-18 [integrator, verifying the board's own #1 item]: **the board was wrong that
  green-lighting real runs is "purely a spend decision".** Verified by reading the code:
  `analyst.py:150` `ModelAnalyzer.analyze()` raises `ModelProviderRefusedError`
  unconditionally — passing `--model` only selects the class at `analyst.py:404`, the call
  is unimplemented; `analysis_worker.py:657` `_no_adapter` raises; `authoring.py:642`
  `default_novel_designer` is a deterministic stub. No provider SDK is installed
  (`openai`, `litellm`, `dspy`, `sentence-transformers` all absent). Execution against
  real agents *does* work — 33 `codex` trials in the catalog beside 57 `oracle` + 2 `nop`.
  Corrected on the board and in `docs/platform-architecture.md` §12, which also had
  `queue.py` as unbuilt (leases landed, M020) and `craft.py` as unbuilt (shipped, M023).
- 2026-08-18 [integrator]: embedder swap is smaller than assumed and has one real trap.
  `lance.py` already has the seam — `Embedder` Protocol (`lance.py:43`), every builder
  takes `embedder: Embedder`, and only **two** sites construct one (`build()` at :574,
  `search()` at :620). `lancedb` 0.37.1 already ships an embedding registry including
  `gemini-text`, `huggingface`, `gte-text`, so a real embedder needs **no new
  dependency**. The trap: nothing records *which* embedder built a table, so a table
  built with the 256-dim `HashingEmbedder` and searched with a different model returns
  meaningless distances with no error. Any swap must persist embedder identity + dim and
  refuse a mismatched search.

- 2026-08-19 [orchestrator, context-supply program]: **reporting handshake requested,
  not hand-applied.** The program's reporting section asks for a weekly rollup line in
  the digest (corpus files and versions, packs built with hashes, evidence lines
  appended, experiments pre-registered/run) and a morning STATUS refresh of the form
  `context-supply: HARVEST 4/6 intake, STANDARDS EX-MT landed@v1, PACK budget cycle in
  review`. Both `digests/<date>.md` and `docs/STATUS.md` are **generated** surfaces
  (`digest.DigestRenderer`, `status_generator`), and PACK's own lease says the
  digest/STATUS surface is reached "via board-note to the SURFACE owner". Hand-editing a
  generated file to make a report appear is precisely the defect M016 fixed last night,
  so it was not done. FOR THE SURFACE OWNER: add a `context-supply` section fed from
  (a) `research/inbox/QUEUE.md` state column for intake progress, (b)
  `library/curated/standards/**` front-matter for file@version, (c) pack build hashes
  once PACK cycle 3 lands citations. Until that exists, the rollup lives in
  `agents/missions/ACTIVE.md` under the program registration, which the orchestrator owns.
- 2026-08-19 [orchestrator, context-supply program]: tonight's rollup, for the record —
  HARVEST intake 1/6 landed (queue item 1, Meta-Task appendices F.1/F.2/F.3 + B + D, five
  notes, verbatim under CC BY 4.0) with `tests/test_inbox_conformance.py` added and
  mutation-verified; cycle 2 (llm-as-a-verifier) dispatched. STANDARDS EX-MT dispatched,
  no corpus file exists yet so no file@version to report. PACK: not started, blocked on
  the first two STANDARDS files. VERIFIER: blocked on HARVEST queue item 2. Zero packs
  built, zero evidence lines appended, zero experiments pre-registered — all four are
  genuinely zero rather than unmeasured.
