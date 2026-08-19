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

- 2026-08-19 [orchestrator, M032 GYM-RUN cycle 3]: wave 1 sized by preflight at
  **zero codex trials**, and the campaign card `research/cards/campaign-gym-v0.md` was
  written by the integrator (not a worker) as the cycle-3 record because the finding is
  a determination, not a build. Two independent blockers, both measured: (1) the
  registry is empty (`registry list` -> "No task records found in library/registry/";
  only `.gitkeep` present), and `registry.py:370` refuses unregistered tasks, so zero
  specs are submittable and there are zero task families for the free oracle control
  arm; (2) codex preflight reads `used_percent 92.0 / remaining 8.0 / credits_balance 0
  / hard stop True`, resets 2026-08-20T18:32:49Z, snapshot 65h27m stale. Per the doc's
  own constraint no ceiling was worked around and no credentials were provisioned. The
  card validates against the mandatory caveat checks and reports no rates by design.
  The binding constraint on the whole campaign is Peter decision #2 (register the
  curated-nominee slice, or reject) — escalated as-is, not as a new decision.

- 2026-08-19 [orchestrator, gym campaign night 1]: **context-supply dispatch is now
  quota-blocked for the SECOND consecutive night, which trips its own escalation rule.**
  `docs/prompts/context-supply-program.md` says two consecutive blocked nights on the
  same item escalate to Peter's morning read via STATUS open-decisions. Tonight
  M025 cycle 2 (llm-as-a-verifier intake) and M026 cycle 1 (EX-MT) were not re-dispatched
  because the same provider that killed them last night killed all three gym agents
  tonight: `GymRunFreeze`, `GymRunExpS03` and `GymDataHarborIndex` were each killed by
  `resource_exhausted` at ~10 minutes having created no worktree at all, after a probe
  call to `google-antigravity/gemini-3.7-flash:high` returned clean. The three gym cycles
  were then completed by the integrator directly (#128, #129, #130). The escalation is
  therefore a capacity decision, not a mission decision: **subagent dispatch on the
  Antigravity lane cannot currently sustain a multi-cycle night.** Recorded here rather
  than added to the gym doc's Peter-decision list, which the dispatch prompt fenced to
  its three named items.

- 2026-08-19 [orchestrator, model lanes + delegation capacity]: **the Cursor lane is
  live on main** (#132): Harbor's native `cursor-cli` adapter, a new
  `subscription-cli-session` auth mode with `CliSessionProbe` (cursor keeps its
  credential in an opaque store, so a file probe would report "available" on a stale
  session), four pinned profiles, default `cursor-grok-4.6-high` per Peter. Verified:
  `available_credentials() -> ['codex_auth', 'cursor_session']`. **gemini-cli is dead**
  for this account (`IneligibleTierError: no longer supported for Gemini Code Assist
  for individuals`), so the Gemini route is through Cursor. Antigravity has no
  installable CLI found: `@google/antigravity-cli` is 404 on npm and
  `~/.gemini/antigravity-cli/bin/` holds only `agentapi` and `webm_encoder` — Harbor
  supports `antigravity-cli`/`antigravity-sdk` as agents but the transport is
  unidentified; that slice died with its agent and is unstarted.
- 2026-08-19 [orchestrator]: **free control battery ran on all four `library/tasks/`
  packages** — oracle=1 and nop=0 for `event-summary`, `query-optimize`,
  `transaction-reconciliation`, `terminal-bench-html-js-filter` (job dirs
  `runs/gymv0-{oracle,nop}-*`). That is the `control_evidence` a `TaskRegistryRecord`
  requires, so registration is now blocked only on **`evallab registry promote` not
  existing** — the command was never implemented (`registry` has only `list`/`audit`;
  nothing writes records). Two dispatch attempts at implementing it died on provider
  limits; it is the highest-value unstarted slice.
- 2026-08-19 [orchestrator]: **subagent delegation is currently non-viable on BOTH
  lanes, at any fan-out.** Tonight: 4 concurrent cursor-lane agents all killed by
  `resource_exhausted` at 8-10 min having created no worktree; then a SINGLE
  gemini-lane agent killed identically at 10m04s. Interactive single-shot probes on
  both lanes succeed every time. So this is neither concurrency nor exhausted credits
  in the ordinary sense — sustained agent sessions specifically fail. Every deliverable
  tonight was therefore hand-executed. This is now the binding constraint on lab
  throughput and it needs Peter's read: it is a provider/harness capacity question, not
  a mission question.

- 2026-08-19 [orchestrator, delegation root cause]: **the `task`-tool subagent path is
  broken against these providers; `omp -p` headless processes are fine.** Isolated by
  experiment, not inferred: `omp -p --no-tools` works; `omp -p` WITH tools doing a real
  tool call works in **3.4s**; a `task` subagent with a 5k-token brief hangs and dies at
  ~10min; a `task` subagent with a **12-word** brief hangs identically; removing the
  `:high` thinking suffix changes nothing. The dead agents' transcripts contain only the
  user message and **zero assistant turns** — they never received a first response, and
  OMP reported the exhausted retry budget as `resource_exhausted`. So it was never
  credits (Peter confirmed plenty available), never concurrency, never brief size. It
  also explains why M015-M019 produced 29 real cycles: those were dispatched as
  hub-supervised `omp -p` processes. Standing rule for this repo: **dispatch workers as
  `hub` supervised `omp -p` processes, not via the task tool.** Three workers launched
  that way tonight all delivered merged PRs (#133, #134, #135).
- 2026-08-19 [orchestrator, policy correction]: my earlier advice to add `cursor-cli` to
  `auto_run.agents` in `policy/standing-approvals.yaml` was **wrong and was not applied**.
  The file's own header says adding a billable agent there "grants nothing; it only
  misstates what this lab does", and `queue.py` confirms it: `if spec.billable:` refuses
  before any standing rule is consulted unless a recorded authorisation exists. `auto_run`
  is the list of agents permitted to run with **no human in the loop**, and by design may
  only ever hold the free controls `oracle`/`nop`. Billable lanes are admitted per-spec
  via `uv run evallab approve <spec-id> --actor <name>`. The one genuinely open policy
  knob is `refuse_billable_at_used_percent` (currently `null`), which is Peter's spend
  decision.
