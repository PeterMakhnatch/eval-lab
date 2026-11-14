# Definition of Green

Green is a property of the exact pull-request head on GitHub, not a local claim.
Every reported GitHub Actions check must finish successfully. Pending, skipped,
cancelled, missing, neutral, and failed checks are not green.

## CI contract

The repository supports Python 3.12 and newer. Python 3.12 is the development and
lint floor. Pull-request CI runs the test suite on 3.12 only; the `main`/`integrate/**`
push run, `merge_group` and `workflow_dispatch` also run it on Python 3.14. No merge
queue is enabled, so a 3.14-only regression is found by the push run on the merged
revision, not before merge. Fix it forward from that run.

Pull requests split the suite into six shards; push, merge-group and manual runs use
two. Each shard imports and runs only its own test files, so the shards together run
every test exactly once, except `tests/test_ci_coverage.py`, which runs in every shard.
More shards shorten the wait for a PR author but add about 55 s of setup per shard to
the shared pool of 20 hosted jobs. Nobody waits on a push run, so it keeps two.

`quality` and `typecheck` run on every PR target branch, including stacked topic
branches, and on `merge_group: checks_requested`. PR edits also rerun them so
retargeting does not reuse evidence for the old base. Push runs remain limited to
`main` and `integrate/**`; `workflow_dispatch` is diagnostic, not a substitute for
a PR or merge-group run.

Configure these exact, unique check names as required. Bind both workflow
gates to **GitHub Actions**:

| Required check | Direct prerequisites |
|---|---|
| `quality-required` | `lint` and every `test` matrix row: `test (3.12, 1/6)`, `test (3.12, 2/6)`, `test (3.12, 3/6)`, `test (3.12, 4/6)`, `test (3.12, 5/6)`, `test (3.12, 6/6)` on pull requests; `test (3.12, 1/2)`, `test (3.12, 2/2)`, `test (3.14, 1/2)`, `test (3.14, 2/2)` on push, merge-group and manual runs |
| `typecheck-required` | `ty` |

Both gate jobs use `always()` and accept only the literal prerequisite result
`success`. They do not check out code or receive token permissions. A failed,
cancelled, skipped, empty, or unknown prerequisite result fails the gate; a missing
workflow/check cannot satisfy the required-check rule. Do not add conditional
skips or `continue-on-error` to core jobs or matrix members, remove a supported
Python version, or conditionally skip either gate. GitHub itself accepts skipped
or neutral check conclusions; the gate's unconditional execution and explicit
success comparisons are therefore essential. Review changes to these workflows
as changes to the merge boundary, not just plumbing.

`ci.yml` and `typecheck.yml` set `cancel-in-progress: false`.
A cancelled prerequisite fails the gate. Auxiliary workflows may cancel superseded PR runs.

All required checks originate from GitHub Actions CI runs. There is no mandatory
independent review gate or review attestation status (`independent-review`).
Past `independent-review` commit statuses in GitHub history are historical artifacts,
not active CI requirements, and must never be reset or faked. Zero reviewer approvals
are required for merge. The author owns diagnosing and fixing all CI failures, as well as
any known concrete bugs, before merging.

Auxiliary benchmark, certification, performance, and platform workflows are
additional evidence, not substitutes for these core gates. Every CI check that
actually reports on the PR must still succeed under the merge rule below.
Do not require path-filtered workflows globally without first making their
trigger and dependency handling fail closed, including merge-group support.

| Gate | Command | Version / interpreter |
|---|---|---|
| Locked install | `uv sync --locked --no-group observability --group benchmarks --group inspect --extra laminar` once per test job | uv 0.9.24; Python 3.12 and 3.14; `observability` stays opt-in |
| Lint | `uv run ruff check .` | locked Ruff; Python 3.12 |
| Governance | `uv run python -m evallab.governance check` | Required governance documents, live handoff headers, tracked root freeze, sealed paths matched by root `.ignore` |
| Registry audit | `uv run evallab registry audit --json` | locked; clean-checkout task/inventory audit |
| Lessons freshness | `uv run python -m evallab.lessons` | locked; statistical lessons lineage |
| Tests | `uv run pytest` (CI: `--shard ${{ matrix.shard }}`) | locked pytest; six-shard matrix on Python 3.12 for pull requests (`test (3.12, 1/6)` to `test (3.12, 6/6)`); two-shard matrix on Python 3.12 and 3.14 (`test (3.12, 1/2)`, `test (3.12, 2/2)`, `test (3.14, 1/2)`, `test (3.14, 2/2)`) on push, merge-group and manual runs. `tests/test_test_hygiene.py` proves that each supported shard count is a complete partition. |
| Native Harbor verification | Each shard installs the union of base and native dependencies once, before either suite. After the base suite, it checks `harbor.job_diff` and syncs `tools/mimoagent-harbor` with Python 3.12. It runs the native file inventory with `-n 2 --dist load --maxschedchunk 2`. Item-level scheduling in chunks of 2 splits the isolated native worker cases across 2 processes; the base suite keeps its existing scheduler. Its JUnit census rejects Harbor-named skips. The inventory coverage gate remains until full native discovery is safe. | Python 3.12 on pull requests; Python 3.12 and 3.14 on push, merge-group and manual runs; every shard |
| Types | `uvx ty@0.0.71 check src/ --output-format=concise` | Python 3.12; zero-diagnostic gate (runs before tests in premerge) |

The full suite writes `full-suite.xml`; its census reports skips without rejecting
Harbor-named reasons. The native phase writes `harbor-native.xml`; its census rejects
Harbor-named skips. Both phases use the locked Harbor 0.24.0 wheel.
The RewardKit tests use local fixtures; CI does not fetch a separate Harbor source checkout.
The native inventory includes `tests/test_integrity_reward.py` without an exemption.
Each pytest session log ends with a `Module durations` table: the 15 test modules
with the largest setup, call and teardown total. Use it to find slow modules and to
balance shards; the per-test list shows only call phases of 1 s or more.

Each test job initializes its fresh PostgreSQL service directly from `sql/schema.sql`.
`psql -X --single-transaction --set ON_ERROR_STOP=1` ignores user startup files,
applies the complete schema atomically, and fails the job on any SQL error.
This setup does not import the application CLI. CLI behavior remains covered by tests.
The test job retains inspect dependencies during the native phase.
The default `dev` group already includes the pinned Laminar dependency;
the explicit `--extra laminar` records the native requirement without a second root sync.

Full native discovery remains blocked by environment-dependent ATIF validation in
`src/evallab/evidence/atif.py` and the native worker fixture contract. Preserve that
behavioral coverage and stored evidence when resolving this prerequisite. Do not
replace the native inventory until the complete native suite passes.

The ty job fails on any diagnostic. Keep the local premerge baseline and the
GitHub `typecheck` workflow at zero; never restore a positive baseline.

Lint, test and typecheck jobs cache uv artifacts keyed by OS, architecture,
Python, uv version, dependency profile, lock digest and defining workflow.
Separate writer keys prevent the smaller lint install from occupying an
immutable test-cache entry; workflow hashes also bind dependency groups and the
pinned typechecker. Lint and typecheck may seed a miss from the matching test
cache, but save only to their own keys. Both fallback digests include the worker
lockfile, matching the test cache writer. No project `.venv` is cached. Locked
sync, all checks and every test shard still run on a cache hit. Cache-transfer
segments abort after two minutes; existing job caps are unchanged. On
cancellation, inspect setup and checker/test durations separately before
blaming a test hang.

Run focused checks or explicit premerge before pushing.

### Size limits

`uv run python -m evallab.governance check` enforces Python growth limits against the PR merge base.
It limits new files and growth per root and large module. Stage new files before the check.
It also checks total budgets in `agents/budgets.toml` on the tested candidate tree.
An over-budget metric blocks only PRs that add net size to it. Push events report total excess without failing.
The failure prints the exact fix. Delete obsolete code first; do not split files only to evade a limit.
For necessary PR growth, add the printed line to `agents/growth-ledger.jsonl` with a concrete reason.
For necessary total growth, raise only the affected budget row with a new concrete `reason` and `raised_by`.
Raises remain visible in `git log agents/budgets.toml`. Shrinking PRs need no budget edit.


### Local line-change gate

Before merging a line-behavior change, run the paired full-line DEV gate:

```bash
git fetch origin
uv run python scripts/line-gate.py --paths --output /absolute/new/path/paths
uv run python scripts/line-gate.py --pr <PR_NUMBER> --output /absolute/new/path/gate
```

The first gate command lists explicit decision-sensitive paths and changed files.
It is discovery only, not a gate pass. The second command writes `gate.json` and
`gate.md`. Require exit 0 and `passed: true` for the exact PR head and current
`origin/main`. Keep both receipts with the merge record. A changed head or base
requires another run. Exit 1 blocks; exit 2 is an error, including absent data.
CI without the private replay set says
`line gate: replay data unavailable, run locally`. This is not a pass.

The gate blocks any per-corpus DEV cheat-keep increase. It also preserves the
existing honest-drop allowance in [the line contract](../docs/line.md).
An exception requires Peter's exact PR, head and corpus-delta approval in the
existing Peter-owned standing policy on `origin/main`. Candidate policy edits
and command-line flags cannot authorize an exception.

`evallab ship` selects the registered `line` gate for decision-sensitive paths
and runs `make line-gate` automatically before merge. Missing private data blocks
delivery. The gate also runs on its registration PR. Documentation-only and
unrelated research changes do not select it. See `agents/ship-gates.toml` for
the path patterns and [the line contract](../docs/line.md#sensitive-decision-paths)
for historical coverage.


### Local checkpoints versus full CI

Run **`make prepush` before every push**. This is a local checkpoint, **not CI green**.

1. **Static checkpoint (`make check`)**:
   Runs the locked static gates: `ruff`, `governance`, `registry audit`, `lessons`,
   and `ty@0.0.71`. It runs no tests or runtime smoke.
2. **Prepush checkpoint (`make prepush`)**:
   Reuses `scripts/premerge.sh --focused`. It runs the cheap static gates and
   typecheck. It leaves the expensive lessons lineage check to full static/CI.
   On a clean tracked tree, it runs `evallab ship check-push` against committed HEAD.
   It skips this comparison for uncommitted tracked edits, which could cause a false same-patch refusal.
   Commit fixes before later checkpoints. Remote command failures warn so offline local checks can finish;
   policy refusals still block. Rerun `uv run evallab ship check-push` before pushing after a warning.
   For a real conflict or needed base commit, use
   `EVALLAB_PREPUSH_NEEDS_BASE=<sha> EVALLAB_PREPUSH_REASON='<reason>' make prepush TESTS='<selectors>'`.
   Both variables are required; they pass `--needs-base <sha> --reason '<reason>'`, not a blanket skip.
   It selects tests against the merge base with `origin/main`, including committed branch
   changes, local edits, untracked files, and both sides of renames.
   It selects direct module consumers and tests that reference changed paths,
   filenames, or parent directories. This includes deleted and non-Python files.
   Asset references in runtime loaders also select their direct test consumers.
   It does not expand every Markdown edit to all documentation consumer tests.
   A bare collect-only run checks the full test tree for import and collection errors.
   Workflow/native-list, governance, fixture conformance, harness
   fixture analysis, and front-matter parser contracts run without branch changes.
   Pytest uses at most 2 workers. Typical changes should finish within
   2 minutes; broad changes can take longer. No automatic full-suite fallback runs.
   This does not prove transitive or dynamic dependencies, runtime smoke, or CI.
   For an explicit selection, use `make prepush TESTS='tests/test_foo.py [pytest args]'`
   or `scripts/premerge.sh --focused TEST_FILE [PYTEST_ARGS...]`.
   During development, `make loop` retains the conservative devloop planner.
   Devloop `--json` includes `lane` and per-consumer `reasons`.
   With `--run --json`, stdout remains one JSON document, child output goes to
   stderr, and the command returns the child's exit status.
   Inspect the prepush selection with
   `uv run evallab registry devloop --since origin/main --checkpoint --json`.
3. **Explicit full local reproduction (`make premerge` or `scripts/premerge.sh [--full]`)**:
   Pins Python 3.12, checks uv 0.9.24, and installs the locked static CI profile.
   It runs static gates and typecheck before the complete pytest suite.
   It then runs the Docker-free runtime smoke test
   (`uv run --no-sync python -m evallab.smoke --docker-free`).
   It does not reproduce the separate hosted native phase. That phase remains
   necessary until the complete suite supports native dependencies.
4. **Full hosted CI gate on GitHub Actions**:
   Pull requests run the 3.12 six-shard matrix; the push run on the merged revision
   runs two shards on 3.12 and additionally proves Python 3.14. Green is a
   property of the exact pull-request head on GitHub Actions; local runs reproduce
   the commands on Python 3.12, but do not substitute for hosted gates or branch
   enforcement.

All workflows group PR runs by pull-request number and other events by
`github.run_id`, so each push retains its own CI evidence. A shared push group is
insufficient even with cancellation disabled: GitHub can replace an older pending
run in that group. Auxiliary workflows set
`cancel-in-progress: ${{ github.event_name == 'pull_request' }}` and cancel
superseded PR revisions promptly; `ci.yml` and `typecheck.yml` set
`cancel-in-progress: false`, because a cancelled prerequisite fails their required
gate (HAR-232).

Heavy certification suites retain automatic PR path triggers (including workbench
edits); no weekly schedule is added as implied authorization.

## Deterministic-test rule

Tests must inject every external-state probe or seam. They must never depend on a
developer's Keychain, `~/.codex`, Docker daemon, network, wall clock, database, or
other host state. Use explicit temporary homes, fixed dates/times, stub credential
sets, fake runtimes, and injected I/O collaborators. A test that passes because a
developer happens to be authenticated is a failing test design.
Tests that read or write behavior labels must use a temporary derived root, even
for read-only queries that can create lock files. Use `EVALLAB_DERIVED_ROOT` or an
explicit per-call `derived_root`; linked worktrees otherwise share the primary
checkout's derived store. Keep real canary inputs read-only and verify persisted
labels in the isolated store, not by restoring shared data after the test.

Hermetic-test guards in `tests/test_test_hygiene.py` reject unscoped import-time host changes,
database probes, and slow server polling. `_HOST_STATE_EXCEPTIONS` there is the exact-operation allowlist.
Do not add whole-file exemptions.

An autouse `subprocess.Popen` guard rejects `codex`, `claude`, `agy`,
`cursor-agent`, `opencode`, `omp`, `security`, and `gemini` before they launch
outside the current test's `tmp_path` and `_isolate_temp_root` directories.
Use injected subprocess runners or fake executables inside either directory.
The guard checks the selected executable, including PATH lookup and explicit
overrides, and resolves symlinks; a link to a host executable is not a fake.

Event filenames are not a chronology guarantee: same-millisecond ULIDs may sort
out of order. Check semantic reason codes, or order by recorded timestamps when
the latest event matters; inject reversed IDs and a fixed clock for that boundary.

## Merge rule

Before any role (author DRI or integrator) merges a PR:

1. Fetch the current PR head and confirm the intended diff.
2. Inspect `gh pr checks <number>` and require every reported CI check to be complete
   and successful for that head. Explicitly confirm `quality-required` and
   `typecheck-required` are present. The retired `independent-review` commit status
   may still appear on older heads; it is not a CI check or a merge requirement.
   Do not rewrite its history or substitute auxiliary green checks for missing core gates.
3. Ensure there are no unresolved CI failures or known concrete bugs on the branch.
   Authors own fixing concrete issues on their changes; removing the review gate does
   not permit knowingly merging broken code.
4. Inspect the corresponding workflow runs for the current head and base branch and
   successful `lint`, every `test` matrix row, and `ty`. PR workflows normally
   check out GitHub's test merge commit; record both PR head and tested merge SHA
   where available. Retargeting the PR to another base branch requires fresh combined
   evidence, and GitHub reruns both gates for it. New commits on the same base branch
   do not; see [Keep CI cycles low](#keep-ci-cycles-low).
5. Do not substitute local green, an old run, a manual dispatch, mergeability, or
   unavailable branch protection for these checks.
6. Run `evallab ship <PR>` from the detached delivery worktree.
   Ship enforces exact-head checks and path gates, then proves the merged revision and retires the topic.

A squash-merged branch is never rebased, reused, or pushed again. After your PR
merges, delete the branch and start any follow-up from a fresh branch off
`origin/main`. An add/add conflict in your own files after a squash merge means
you are rebasing a spent branch — abort.

If an integrator must make a local merge, the corresponding PR must already be
fully green. Run `scripts/premerge.sh` on the merge result before pushing, and put
both `Premerge: scripts/premerge.sh (pass)` and the green PR number/head SHA in the
merge commit body.

### Keep CI cycles low

In the last 60 merged PRs before the guards, 31 extra CI runs followed avoidable updates:
25 followed base-only updates and 6 followed title/body edits. There were 0 first-cycle failures.
Every run uses the shared pool of 20 hosted jobs.

- Create the PR once with `evallab ship open --title-file <file> --body-file <file>`.
  Add later evidence with `evallab ship comment <PR> --body-file <file>`. Do not edit the title or body.
- Run `evallab ship <PR> --preflight-only` from the detached delivery worktree immediately after creation.
- Do not update the branch because `main` moved. Ship accepts current same-base CI without a fresh head.
  Update only for a real conflict or a needed commit. Prepush checks the committed patch before another push.
- Workflow exception: if the PR changes `.github/workflows/**` and main changed the same file after the branch point,
  a token without `workflow` scope can refuse the merge even with green CI.
  Merge `origin/main` into the topic once, run `make prepush`, push, and wait for fresh CI.
  Alternatively, Peter can run `gh auth refresh -s workflow`. This does not authorize a base-only update elsewhere.
- Push 1 head per fix. Read its CI result before another push. Use final `evallab ship <PR>` after all checks pass.

## GitHub enforcement versus process

Recommended protection for `main` and any branch accepting integrated work:
require a PR, require `quality-required` and `typecheck-required` from GitHub
Actions, require conversation resolution,
block force pushes/deletion, and apply the rules to administrators with no bypass.
Zero reviewer approvals are required. Use an active ruleset or explicit
non-overlapping branch protections; merely running checks on arbitrary stack
bases does not protect those bases.
Do not require the branch to be up to date while the merge queue is disabled: that
setting forces one full CI cycle on every open PR for each merge to `main`.

Draft pull requests and explicit operator holds (e.g. labels) remain honored pauses
preventing accidental merge.
Keep the native merge queue disabled unless explicitly authorized with live combined-check
validation. The `merge_group` triggers prepare the CI side only; they do not authorize or
enable a queue.
### Integration-owner verification

After publishing the workflow change, open or update a PR against a non-`main`,
non-`integrate/**` stack base and retarget it without changing its head. Confirm
fresh PR runs contain `lint`, `test (3.12, 1/6)` to `test (3.12, 6/6)`, `ty`, and both
gates (`main` push runs contain `test (3.12, 1/2)`, `test (3.12, 2/2)`,
`test (3.14, 1/2)` and `test (3.14, 2/2)`).
Wait for successful runs before selecting the new check names in settings.
Reopen or synchronize older PRs whose workflow definitions predate this change;
do not dispatch an unrelated workflow and call them green.

Run `uv run pytest tests/test_quality_gates.py` for isolated gate regressions.
On a disposable PR that will not be merged, force a prerequisite to fail and
then to skip; its aggregate must execute and fail in both cases. Cancel a
prerequisite run and confirm no successful required gate is accepted. With the
protection active, a missing gate must leave merging blocked. Inspect the actual
merge box/rule evaluation, not just `gh pr checks`' exit code. If a merge queue is
enabled, separately confirm these checks run on its merge-group SHA.

Official semantics:
- [Required checks and skipped jobs](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks)
- [Branch protections and expected check source](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
- [Merge queue configuration](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue)

