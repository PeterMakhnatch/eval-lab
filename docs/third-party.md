---
status: living
audience:
  - builder
  - operator
---

# Third-party tools and support

Hand-maintained inventory. Owners are checked against main at `9125e8f98` on November 11, 2026.
CORE means required infrastructure, not complete support. OPTIONAL means replaceable or explicitly selected. DROP means scheduled removal, not removed data.
Each CORE boundary needs a pin, 1 owner, a contract test, a health check, a failure description, and a recovery path.
**Unpinned** and **missing** identify open support gaps. An observed version is not a pin.
Commands run from the repository root. Set `PRIMARY_ROOT` to the nightly plist's `--data-root`, not its runtime snapshot.
On this host, `PRIMARY_ROOT=/Users/petermakhnatch/Developer/eval-lab`; nightly state is `~/.local/state/evallab-nightly`.
`uv.lock` pins below include distribution hashes. A source revision lives in the named owner's constants unless stated otherwise.
Run **`uv run evallab doctor`** for every CORE boundary below. It exits 1 when any CORE check fails.
Credential cells contain names only. `none` means no provider credential; `external` means the external tool owns authentication.
For all locked Python packages, recovery starts with `uv sync --locked` in an isolated checkout. Never remove evidence to repair a dependency.
Doctor reads local versions, retained bytes, stores, and installed jobs. It makes no model call, download, or service change.
GitHub protection visibility uses free, read-only GitHub metadata. An offline host reports that boundary unavailable.
The headless scheduler gate remains separate output of the same command, selected with `--headless`; its execution policy does not change.

| Tool / status / lab use | Owner on main | Pin and location | Credential names | Exact health / contract command | Failure and recovery |
|---|---|---|---|---|---|
| Harbor — CORE; task execution and certification | `src/evallab/harbor_common.py` | 0.24.0 in base `pyproject.toml`, `uv.lock`, and `runner.HARBOR_CLI_PIN` | none | `uv run evallab doctor`; contract: `tests/test_harbor_contract.py` | SDK drift breaks task, result, and verifier fields. Run `uv sync --locked`; remove old isolated CLI overrides. Do not use a 0.21 runtime. |
| Docker Engine / Compose / OCI — CORE; isolated environments | `src/evallab/harbor_host_isolation.py` | Engine 29.8.0, Compose 5.5.1 in `automation.CORE_TOOL_PINS`; task images retain Dockerfile digests | external registry login | `uv run evallab doctor`; contracts: `tests/test_harbor_docker.py`, `tests/test_host_isolation_compose.py` | An unreachable or non-Linux daemon blocks the shared isolation preflight. Older Docker Desktop can lack `nftables fib inet`. Restore the supported runtime; never disable containment checks. |
| PostgreSQL — CORE; live catalog | `src/evallab/database.py` | `postgres:18.4-alpine3.24` in `compose.yaml`; image digest and applied schema revision are MISSING | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `DATABASE_URL` | `uv run evallab doctor`; contracts: `tests/test_pipeline.py`, `tests/test_backups.py` | Stale dumps, missing projections, or an untracked applied schema fail health. Use the isolated restore procedure below. The database owner must reconcile schema provenance; never invent a migration revision. |
| psycopg — CORE; catalog client | `src/evallab/database.py` | 3.3.6 in `uv.lock` | `DATABASE_URL` | `uv run evallab doctor`; contract: `tests/test_pipeline.py` | Connection or schema errors mean unavailable data, not zero rows. Restore connectivity and the locked client; preserve raw jobs. |
| DuckDB — CORE; local analysis | `src/evallab/storage/attach.py` | 1.5.6 in `uv.lock`; `<2` in `pyproject.toml` | none | `uv run evallab doctor`; contract: `tests/test_attach.py` | A newer writer can create an incompatible store. Keep the cap; rebuild derived stores from retained evidence. The current owner uses in-memory DuckDB over Parquet, not a required persistent database. |
| PyArrow / Parquet — CORE; evidence projections | `src/evallab/evidence/parquet_io.py` | PyArrow 26.0.0 in `uv.lock`; readable Parquet footer versions reported by doctor | none | `uv run evallab doctor`; contract: `tests/test_parquet_io.py` | Partial or stale projections are not raw evidence. Restore raw jobs and rebuild projections; publish replacements atomically. |
| SQLite — CORE; domain journals and trace database | `src/evallab/backups.py` | SQLite >=3.49.1 and header formats 1/2 in `automation.py`; Python uses the project requirement | none | `uv run evallab doctor`; contract: `tests/test_backups.py` | Copying a live main file loses WAL changes. Use `backups.sqlite_store_paths()` for read-only discovery and `uv run evallab doctor` for health checks. Never checkpoint or truncate a live WAL for backup. |
| Goodfire reward-hack data — CORE; retained native trace source | `src/evallab/failure_library/runs/io.py` | `bd8fb473568b8ef1d12fc2325a5c8af939c15ad9` in `cached.GOODFIRE_REVISION`; 96-file SHA-256 tree in `io.RETAINED_BUNDLE_SHA256` | `HF_TOKEN` when required | `uv run evallab doctor`; contracts: `tests/test_failure_library_runs.py`, `tests/test_gold_seed.py` | Missing chronology cannot support action claims. Recover hash-matching retained bytes after a 403 or removal. Doctor reads labelled metadata only through `evallab.gold.load_rows`; unresolved traces remain unjudged. |
| Hugging Face Hub — CORE; pinned source access | `src/evallab/failure_library/runs/io.py` | hub 1.33.0 in `uv.lock`; full revisions in `REPOSITORIES`; native tree hashes and committed normalized manifests | `HF_TOKEN` | `uv run evallab doctor`; contract: `tests/test_failure_library_runs.py` | A 403, removed upstream, changed revision, or changed retained bytes blocks acquisition. Use the retained-byte verification procedure below. Native MALT bytes remain excluded; its access receipt is not a dataset. |
| OMP — CORE; approved worker execution | `src/evallab/autoreview_monitor.py` | CLI 18.18.* compatibility line from `automation.CORE_TOOL_PINS`; doctor reports the exact installed patch; each launch selects one explicit route | external OMP auth store | `uv run evallab doctor`; contract: `tests/test_hack_drop.py::test_autoreview_through_stub_omp_binary` | A different major.minor version or a failed no-fallback probe blocks the check. Quota, an ignored overlay, or a fallback response invalidates a worker route. Never enable paid fallback implicitly. |
| keys store / macOS Keychain — CORE; secret custody | `src/evallab/profiles.py` | keys executable SHA-256 in `automation.CORE_TOOL_PINS`; `keys has NAME` interface v1 and `KeychainProbe` | `HF_TOKEN`, `HARBOR_CLAUDE_KEYCHAIN_SERVICE`, `HARBOR_CLAUDE_KEYCHAIN_ACCOUNT` | `uv run evallab doctor`; contracts: `tests/test_core_health.py`, `tests/test_profiles.py` | Code drift or missing credentials blocks the probe. Restore the pinned CLI through its installation owner. Renew credentials through their owner; never print or copy a value. |
| launchd / launchctl — CORE; installed local schedules | `src/evallab/automation.py` | Each installed plist's source revision; missing revisions fail doctor | none; local user ownership | `uv run evallab doctor`; contract: `tests/test_core_health.py` | A loaded job can have stale useful output or a failed last exit. Doctor uses `launchctl print` only. The service owner must reinstall a known merged revision or approve retirement; source delivery changes no installed service. |
| Git — CORE; source identity | `src/evallab/ship/__init__.py` | 2.50.1 in `automation.CORE_TOOL_PINS`; each PR pins its head | external SSH/auth store | `uv run evallab doctor`; contract: `tests/test_ship.py` | Dirty or moving heads block delivery. Commit owned paths and rebase; never reset another writer's work. |
| GitHub / gh / Actions — CORE; protected delivery | `src/evallab/ship/__init__.py` | gh 2.96.0 in `automation.CORE_TOOL_PINS`; full action SHAs in `.github/workflows/ci.yml` | `GH_TOKEN`, `GITHUB_TOKEN`, external gh auth store | `uv run evallab doctor`; contract: `tests/test_ship.py` | Auth or protection visibility can fail. Pending, failed, or missing exact-head checks still block merge. Repair auth or CI, then use `uv run evallab ship <PR>`; never bypass protection. |
| uv / PyPI — CORE; locked installs | `pyproject.toml` | uv 0.9.24 in `automation.CORE_TOOL_PINS` and CI; package hashes in `uv.lock` | none for the public index | `uv run evallab doctor` | Index or resolver failure blocks installation. Use the locked cache or approved index; do not refresh pins as a repair shortcut. |
| Python — CORE; application runtime | `pyproject.toml` | `>=3.12` in `pyproject.toml`; CI tests 3.12 and 3.14; exact patch builds are not pinned | none | `uv run evallab doctor`; contract: `tests/test_core_health.py` | Interpreter drift can change SQLite and serialization. Use a project-compatible interpreter and the locked environment. |
| Pydantic — CORE; typed contracts | `src/evallab/schemas/__init__.py` | 2.14.1 in `uv.lock` | none | `uv run evallab doctor`; contract: `tests/test_profiles.py` | Invalid contracts fail validation. Restore the locked version or migrate data explicitly; never coerce away failures. |
| PyYAML — CORE; task/config parsing | `src/evallab/profiles.py` | 6.0.3 in `uv.lock` | none | `uv run evallab doctor`; contract: `tests/test_profiles.py` | Invalid YAML blocks admission. Repair the owned input and validate it before dispatch. |
| tomlkit — CORE; task metadata edits | `src/evallab/task_health_tags.py` | 0.15.2 in `uv.lock` | none | `uv run evallab doctor`; contract: `tests/test_task_health_tags.py` | Invalid metadata breaks derivation. Preserve the parent package; restore the locked parser and regenerate the variant. |
| Modal — OPTIONAL; approved cloud trials | `src/evallab/harbor_modal.py` | 1.6.2, root `uv.lock`; isolated tools retain 1.5.5 | `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` | read-only: `modal app list`; contract: `uv run pytest -q tests/test_harbor_modal.py` | Deployments hit the 200-app limit on Oct 29. The owner must remove only owned stopped apps after checking receipts; never run global cleanup. |
| Laminar / lmnr — OPTIONAL; native observability; replica writer retired | `src/evallab/laminar.py`; quarantine: `src/evallab/trace_store/laminar_quarantine.py` | lmnr 0.7.64, `uv.lock` | `LMNR_PROJECT_API_KEY` | contract: `uv run pytest -q tests/test_laminar_quarantine.py` | Deletion returns HTTP 500; 108 journal copies remain quarantined. Keep them excluded until the owner proves deletion/readback. Do not release quarantine on an attempted delete. |
| OpenTelemetry — OPTIONAL; opt-in mirror export | `src/evallab/laminar_tracing.py` | SDK/proto/exporter 1.45.1, `uv.lock` | `LMNR_PROJECT_API_KEY` for Laminar | contract: `uv run pytest -q tests/test_laminar_tracing.py` | Export failure must not change native evidence or grading. Keep export opt-in; rebuild derived mirrors from admitted native bytes. |
| Terminal Wrench — OPTIONAL; trace/task source | `src/evallab/failure_library/runs/cached.py` | index `622449769ec6ddf33f770794489b8cd21712a0ad` | `HF_TOKEN` when required | contract: `uv run pytest -q tests/test_failure_library_runs.py` | Task/trace revision mismatch breaks provenance. Recover the exact retained pair. |
| SWE-bench — OPTIONAL; source evidence | `src/evallab/failure_library/sources/swe_family.py` | original `f5351ee8c6663736817027db3ad03fe662cb5bb8`; Verified `c104f840cc67f8b6eec6f759ebc8b2693d585d4a` | `HF_TOKEN` when required | contract: `uv run pytest -q tests/test_failure_library_sources_swe.py` | Labels and task versions differ. Keep source identity and version-specific evidence; recover pinned bytes. |
| OpenAI CDN — OPTIONAL; public source host | `src/evallab/failure_library/sources/swe_family.py` | `OPENAI_SHA` | none | contract: `uv run pytest -q tests/test_failure_library_sources_swe.py` | Removal or changed bytes breaks acquisition. Recover hash-matching retained bytes; keep rights exclusions. |
| OpenAI / Codex — OPTIONAL; approved model route; native adapter scheduled for removal | `src/evallab/harbor_codex.py`; OMP owner above | native CLI 0.162.1, `PINNED_CODEX_VERSION`; OMP selectors are not immutable weights | `CODEX_FORCE_AUTH_JSON`, `OPENAI_API_KEY` for direct SDK only; external OMP auth | version/quota: `omp --version && best-model --all` | Quota or identity drift blocks the route. Wait for reset or choose an approved route; retain served-model receipts. |
| Anthropic / Claude — OPTIONAL; approved subscription route | `src/evallab/profiles.py` | explicit selectors, no immutable model pin | `CLAUDE_CODE_OAUTH_TOKEN`, `HARBOR_CLAUDE_KEYCHAIN_SERVICE`; external OMP auth | contract: `uv run pytest -q tests/test_profiles.py` | Expired OAuth blocks execution. Renew the subscription route; do not substitute a paid API key implicitly. |
| OpenRouter — OPTIONAL; approved paid model routes | `src/evallab/spend_reconcile.py` | explicit model/provider selectors, no immutable backend pin | `OPENROUTER_API_KEY` | presence: `keys has OPENROUTER_API_KEY`; contract: `uv run pytest -q tests/test_spend_reconcile.py` | Attempt and settled charges can disagree. Reconcile by attempt identity; do not count overlapping ledgers twice. |
| DeepSeek — OPTIONAL; explicit paid route | `src/evallab/spend_reconcile.py`; legacy proxy: `containers/deepseek_secret_proxy.py` | selector only; no immutable weights | `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY` | presence: `keys has DEEPSEEK_API_KEY`; contract: `uv run pytest -q tests/test_spend_reconcile.py` | Unlogged paid fallback adds $9.84 in the Nov 4 backfill. Reject implicit fallback; reconcile the existing ledger before an explicitly approved paid run. |
| Z.ai / GLM / OpenCode — OPTIONAL; selected model route; stale native routes scheduled for removal | `src/evallab/harbor_zai_opencode.py` | OpenCode 1.18.25, `PINNED_OPENCODE_VERSION`; model selectors are mutable | `ZAI_CODING_PLAN_API_KEY`, `ZAI_OPENAPI_API_KEY` | contract: `uv run pytest -q tests/test_profiles.py` | Unqualified endpoints or wrong billing routes block admission. Use only an attested, approved route; preserve its usage identity. |
| Muse / Meta — OPTIONAL; OMP workers | `src/evallab/profiles.py` | selector only; no CLI or weight pin | external OMP auth | quota: `best-model --all` | Worker quota limits can stop a batch. Stop before the provider gate; wait for reset or select an approved route. No paid fallback. |
| Cursor — OPTIONAL; selected worker route | `src/evallab/profiles.py` | external CLI/model unpinned | external CLI auth store | contract: `uv run pytest -q tests/test_profiles.py` | An existing config file does not prove an active session. Renew through the CLI; require a successful route receipt. |
| Kimi — OPTIONAL; selected worker route | `src/evallab/profiles.py` | selector only; no immutable weights | external OMP auth | quota: `best-model --all` | Quota and served-route drift invalidate availability. Wait or choose an approved route; preserve receipts. |
| Grok / xAI — OPTIONAL; declared route | `src/evallab/profiles.py` | selector only; no immutable weights | external OMP auth | contract: `uv run pytest -q tests/test_profiles.py` | A declaration is not qualification. Keep the route blocked until approved evidence exists. |
| Herdr — OPTIONAL; operator and cleanup references | `src/evallab/ship/cleanup.py` | host unpinned; audit observes 0.9.5 | none; local socket | version: `herdr --version` | Stale pane references can block safe retirement. Preserve referenced worktrees; the owner resolves references without reading other chat history. |
| Linear / lin / Foreman — OPTIONAL; assignment and notifications | `src/evallab/auto_watch.py`; Foreman is outside this repository | external CLIs unpinned | `FOREMAN_LINEAR_CLIENT_ID`, `FOREMAN_LINEAR_CLIENT_SECRET`, `LINEAR_API_KEY` | local contract: `uv run pytest -q tests/test_auto_watch.py` | Notification failure is not a scientific result. Preserve the local finding and let the authorized owner recover delivery. |
| FastAPI — OPTIONAL; local read-only views | `src/evallab/results_viewer.py` | 0.141.1, `uv.lock` | none for local read-only viewer | contract: `uv run pytest -q tests/test_results_viewer.py` | Missing or stale evidence must render unavailable, not empty success. Repair the source path and preserve raw runs. |
| Uvicorn — OPTIONAL; local ASGI serving | `src/evallab/results_viewer.py` | 0.54.0, `uv.lock` | none for local read-only viewer | version: `uv run uvicorn --version` | A listening process does not prove fresh data. Check the actual viewer and its source age; the service owner restores the pinned release. |
| pip — OPTIONAL; trusted wheel resolution | `pyproject.toml` | 26.1.2, `uv.lock` | none for public index | version: `uv run python -m pip --version` | Resolver drift changes task supply chains. Use the pinned resolver and committed wheel hashes. |
| tiktoken — OPTIONAL; transitive in `uv.lock` | none (`cost_inventory.py` retired) | 0.14.0, `uv.lock` | none | none | Estimates are not measured billed usage. Keep missing usage explicit and reconcile provider receipts separately. |
| Typer — OPTIONAL; Harbor job-diff CLI | `src/evallab/job_diff.py` | 0.27.3, transitive `uv.lock` pin | none | contract: `uv run pytest -q tests/test_job_diff.py` | CLI dependency drift blocks diff output. Restore the locked environment; raw results remain authoritative. |
| pytest / Hypothesis / pytest-xdist / Ruff — OPTIONAL; development checks | `pyproject.toml`; `.github/workflows/ci.yml` | 9.1.1 / 6.170.8 / 3.8.0 / 0.17.2, `uv.lock` | none | `uv run pytest --version && uv run ruff --version` | Missing plugins or flaky host probes invalidate checks. Use isolated deterministic fixtures and the locked CI environment. |
| hatchling — OPTIONAL; build backend | `pyproject.toml` | unbounded build requirement; pin missing | none | local contract: `uv build --offline` | Backend drift can change builds. Pin the backend through the owning dependency lane; do not claim the root lock covers isolated build resolution. |
| LiteLLM — OPTIONAL; native Harbor model client | `src/evallab/harbor_terminus.py` | 1.106.2 in `uv.lock` | selected provider names only | contract: `uv run pytest -q tests/test_terminus2_runtime.py` | Client or served-route drift blocks the selected model. Restore the locked Harbor runtime and attested route; preserve native usage receipts. |
| S3 / R2 / Tigris / boto3 — DROP; remote CAS branches | `src/evallab/production/cloud_trace_db.py` | boto3 1.43.117, `uv.lock` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` | contract only: `uv run pytest -q tests/test_cloud_trace_db.py` | No current remote receipt. Keep local CAS and raw bytes; remove remote branches before dependencies. |
| MiMo agent / mini-SWE-agent / OpenAI SDK — DROP; stale native agents | `src/evallab/harbor_mimoagent.py`; `tools/mimoagent-harbor/pyproject.toml` | MiMo `467f0a19016f0ac4d63b8d17a1f0da9ba07f232c` in isolated project; OpenAI 2.54.0 in root `uv.lock`; mini-SWE lacks a supported root pin | `MIMO_SELFHOSTED_API_KEY`, `LMNR_PROJECT_API_KEY` | contract only: `uv run pytest -q tests/test_mimoagent_worker.py` | Stale native runtimes lack current qualification. Remove callers and isolated projects together; retain native run evidence. |
| Tinker / TRL / Torch / Transformers / SGLang — DROP; training and serving | `tools/modal-mimo-sft/pyproject.toml` | Transformers 5.12.1 in isolated `tools/modal-mimo-sft/`; `tools/tinker-sft/` and `sft_tinker.py` removed | `MIMO_SELFHOSTED_API_KEY`, `HF_TOKEN` | none | Training is outside the target. Retire isolated projects without running GPU jobs or local inference; preserve paid receipts and unique data. |
| Beam / Runpod — DROP; proposed alternate backends | `src/evallab/campaign_execution.py` | no qualified SDK or deployment pin | `BEAM_TOKEN`; no verified Runpod credential | contract only: `uv run pytest -q tests/test_campaign_execution.py` | No qualified active integration. Remove backend declarations, not create a new runtime. |
| GLM / Breaker self-hosting — DROP; unqualified hosted models | `src/evallab/harbor_glm_selfhosted.py` | endpoint/model selectors only; no current deployment pin | `GLM_SELFHOSTED_API_KEY`, `BREAKER_SELFHOSTED_API_KEY` | contract only: `uv run pytest -q tests/test_selfhosted_adapter_profile.py` | No current served-model receipt. Retire profiles and adapters; never infer qualification from credential presence. |

## Native runtime and retained ATIF

Harbor 0.24.0 owns new task, trial-result, and verifier runtime contracts.
`tests/test_harbor_contract.py` checks their consumed fields against retained fixtures.
`src/evallab/evidence/atif.py` owns the stable retained ATIF-v1 contract.
Its validator preserves lab custody and redaction metadata. SDK installation must not change evidence admission.
`tests/test_fixture_conformance.py` checks retained documents and malformed-input refusals with the normal SDK installed.
`src/evallab/execution_contracts.py` owns the verifier import selector. Pure planning does not import the native verifier.

## Backup recovery

The installed nightly plist omits `--backup`. The old code therefore only reports age and never creates a dump.
The fixed refresh uses a 120-second backup guard when the verified dump is missing or older than 36 hours.
`--backup` still requests a dated dump even when the previous dump is fresh. Immutable same-date generations remain reusable.
Health returns FAIL and exit 1 while the verified dump is missing or stale, including on a projection `noop`.
Run `uv run evallab nightly --refresh --backup --data-root "$PRIMARY_ROOT"` for a dated backup (the standalone `python -m evallab.backups` CLI is retired).
Restore into a separate random database with `pg_restore`, compare 8 main-table counts against a read-only count query on the live catalog, and drop only the temporary database. Concurrent ingestion can cause a count mismatch; repeat when idle.
Store recovery receipts outside Git.
A code merge does not update the installed source snapshot. The service owner must reinstall the merged revision through the existing installer.

## SQLite backup and recovery

Use `evallab.backups.sqlite_store_paths()` for read-only discovery of installed journals and the trace database (`python -m evallab.backups` CLI is retired).
Open sources with `mode=ro` and `query_only=ON`, use SQLite's WAL-aware backup API, verify table counts and `PRAGMA integrity_check` on a temporary restore, and publish immutable backups under `$PRIMARY_ROOT/backups/sqlite/`.
Never copy a snapshot over a live database. Restore into a new, owner-controlled path first.
Verify the manifest SHA-256 and table counts before the database owner switches any reader or writer.
Installed jobs do not automatically gain this code after a merge. This lane changes no schedule.

## Retained-byte recovery after a 403 or upstream removal

The existing retained-byte verification owner is `src/evallab/failure_library/runs/io.py` (`retained_source_health`, `verify_retained_bundle`).
The retired Goodfire SQLite cache is not a second download or recovery path.
An existing immutable native directory is verified before any network request.
A filename change, extra file, symlink, missing file, or changed byte fails its tree hash.
Each tree entry binds the relative filename and its SHA-256. Committed normalized manifests bind derived bundle bytes.

1. Preserve the failed access receipt and the retained native directory.
2. Run `uv run evallab doctor` to check revisions and hashes without a download.
3. Point verification at the existing retained directory via `retained_source_health` / `verify_retained_bundle`. Matching bytes verify without contacting Hugging Face.
4. Restore missing files only from an authorized, hash-matching retained copy.
5. Preserve access exclusions when no permitted copy exists. Ask the rights owner to resolve access; never accept a gate by proxy.

Native MALT remains unavailable after its recorded 403. Its retained directory contains metadata, not native dataset rows.
The separately pinned `ktolnos/rh-bench` redistribution has its own normalized manifest.
Neither an access receipt nor redistribution provenance claims native upstream access.

## OMP route recovery

Keep `retry.modelFallback: false`. Reject any saved fallback response or a served model different from the pinned launch route.
The contract replays saved session-v3 events through the worker launch without a live model call.
Doctor tests config resolution with a true control overlay followed by a false overlay through `PI_CONFIG_FILES`.
The worker appends its selected YAML to that same variable. Its no-fallback overlay therefore wins over inherited config.
Do not rely on `--config` alone.
This proves the config surface, not provider availability or a live response.

For new approved work, use the current `best-model` selection order:
GPT-6.1 Sol, Muse, Gemini Argon, DeepSeek, then Devin.
Run `best-model --all` to inspect quota. Resolve `best-model` again for each new launch wave.
Stop at the 90% provider gate. Preserve the failed launch and served-model receipt.
Wait for reset or select the next approved route explicitly; quota headroom does not authorize a paid route.

## Remaining CORE support gaps

PostgreSQL still lacks an image digest and an applied schema ledger.
Python has the project requirement `>=3.12`, not an exact patch-build pin. CI tests 3.12 and 3.14.
Some installed jobs lack a source revision, a useful-output receipt, or a current retained owner.
Missing applied schema provenance or required job facts fail doctor. It does not repair services or turn missing evidence into a pass.
Per-container isolation proof remains mandatory at native launch. The read-only daemon preflight is not containment certification.
