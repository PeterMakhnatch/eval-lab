# Definition of Green

Green is a property of the exact pull-request head on GitHub, not a local claim.
Every reported GitHub Actions check must finish successfully. Pending, skipped,
cancelled, missing, neutral, and failed checks are not green.

## CI contract

The repository supports Python 3.12 and newer. Python 3.12 is the development and
lint floor; CI also exercises Python 3.14.

`quality` and `typecheck` run on every PR target branch, including stacked topic
branches, and on `merge_group: checks_requested`. PR edits also rerun them so
retargeting does not reuse evidence for the old base. Push runs remain limited to
`main` and `integrate/**`; `workflow_dispatch` is diagnostic, not a substitute for
a PR or merge-group run.

Configure these exact, unique check names as required. Bind the two workflow
gates to **GitHub Actions**; the review status is issued by the integration owner:

| Required check | Direct prerequisites |
|---|---|
| `quality-required` | `lint`, `test (3.12)`, `test (3.14)` via the `test` matrix |
| `typecheck-required` | `ty` |
| `independent-review` | Integration-owner attestation of an actual independent review of this exact head |

Both gate jobs use `always()` and accept only the literal prerequisite result
`success`. They do not check out code or receive token permissions. A failed,
cancelled, skipped, empty, or unknown prerequisite result fails the gate; a missing
workflow/check cannot satisfy the required-check rule. Do not add conditional
skips or `continue-on-error` to core jobs or matrix members, remove a supported
Python version, or conditionally skip either gate. GitHub itself accepts skipped
or neutral check conclusions; the gate's unconditional execution and explicit
success comparisons are therefore essential. Review changes to these workflows
as changes to the merge boundary, not just plumbing.

`independent-review` is a commit status, not an Actions job or a fabricated GitHub
approval. The integration owner records it only after resolving a different
agent's or eligible human's review, with the PR review receipt as its target URL.
No CI workflow may automatically issue it. New commits have no such status and
remain blocked until reviewed again. See `agents/WORKFLOW.md`.

Auxiliary benchmark, certification, performance, and platform workflows are
additional evidence, not substitutes for these core gates. Every check that
actually reports on the PR must still succeed under the merge rule below.
Do not require path-filtered workflows globally without first making their
trigger and dependency handling fail closed, including merge-group support.

| Gate | Command | Version / interpreter |
|---|---|---|
| Locked install | `uv sync --locked` | uv 0.9.24; Python 3.12 and 3.14 |
| Lint | `uv run ruff check .` | locked Ruff; Python 3.12 |
| Doc index freshness | `uv run python -m evallab.docindex check` | locked; Python 3.12 |
| Repository map freshness | `uv run python -m evallab.repomap check` | locked; Python 3.12 |
| Governance | `uv run python -m evallab.governance check` | Required governance documents, live handoff headers, tracked root freeze |
| Registry audit | `uv run evallab registry audit --json` | locked; clean-checkout task/inventory audit |
| Lessons freshness | `uv run python -m evallab.lessons` | locked; statistical lessons lineage |
| Tests | `uv run pytest` | locked pytest; Python 3.12 and 3.14 |
| Types | `uvx ty@0.0.71 check src/ --output-format=concise` | Python 3.12; zero-diagnostic gate |

The ty job fails on any diagnostic. Keep the local premerge baseline and the
GitHub `typecheck` workflow at zero; never restore a positive baseline.

Run `make premerge` before pushing. Before final review and doc freshness checks,
run explicit `make docs` to regenerate `docs/INDEX.md` and `docs/repo-map.md`.
`scripts/premerge.sh` pins Python 3.12, checks uv 0.9.24, performs the locked
install (including the `benchmarks` dependency group so the live
fastmcp/cryptography contract tests run locally instead of skipping via
`pytest.importorskip`), runs every gate above, and applies the same ty 0.0.71
ratchet. It reproduces the commands in `quality` and `typecheck` on Python 3.12;
the GitHub matrix additionally proves Python 3.14. It does not reproduce GitHub's
event routing or branch enforcement.

During active local development loops, prefer focused checks for touched modules
rather than running the entire project-wide test suite on every small edit.
## Deterministic-test rule

Tests must inject every external-state probe or seam. They must never depend on a
developer's Keychain, `~/.codex`, Docker daemon, network, wall clock, database, or
other host state. Use explicit temporary homes, fixed dates/times, stub credential
sets, fake runtimes, and injected I/O collaborators. A test that passes because a
developer happens to be authenticated is a failing test design.

## Merge rule

Before any role, human, or integrator merges a PR:

1. Fetch the current PR head and confirm the intended diff.
2. Run `gh pr checks <number>` and require every reported check to be complete and
   successful for that head. Explicitly confirm `quality-required` and
   `typecheck-required` are present: an all-green list of auxiliary checks is not
   evidence that missing core workflows ran.
3. Inspect the corresponding workflow runs for the current head/base pair and
   successful `lint`, both Python `test` jobs, and `ty`. PR workflows normally
   check out GitHub's test merge commit; record both PR head and tested merge SHA
   where available. A base change requires fresh combined evidence.
4. Do not substitute local green, an old run, a manual dispatch, mergeability, or
   unavailable branch protection for these checks.
5. Apply the independent-review and auto-merge rules in `agents/WORKFLOW.md`.

A squash-merged branch is never rebased, reused, or pushed again. After your PR
merges, delete the branch and start any follow-up from a fresh branch off
`origin/main`. An add/add conflict in your own files after a squash merge means
you are rebasing a spent branch — abort.

If an integrator must make a local merge, the corresponding PR must already be
fully green. Run `scripts/premerge.sh` on the merge result before pushing, and put
both `Premerge: scripts/premerge.sh (pass)` and the green PR number/head SHA in the
merge commit body.

## GitHub enforcement versus process

These documents and workflow files do not enable repository settings. The
2026-09-14 delivery inspection found a public repository, unprotected `main`,
and repository auto-merge disabled; that is a dated observation, not a permanent
plan limitation. The integration owner must inspect the live settings and record
changes rather than infer enforcement from this document.

Recommended protection for `main` and any branch accepting integrated work:
require a PR, require `quality-required` and `typecheck-required` from GitHub
Actions plus the `independent-review` commit status, require the branch to be up
to date, require conversation resolution, block force pushes/deletion, and apply
the rules to administrators with no bypass. When there is a real independent
GitHub reviewer, also require their native approval and dismiss stale approvals.
In the shared-principal workflow, the commit status enforces a fresh review
attestation, not independently authenticated reviewer identity; do not describe
an agent comment as a native GitHub approval.
Use an active ruleset or explicit non-overlapping branch protections; merely
running checks on arbitrary stack bases does not protect those bases.

Keep the native merge queue disabled. Its combined merge-group SHA is different
from the reviewed PR head, so the required `independent-review` status would be
missing. Queue activation requires a separately approved genuine review and
attestation procedure for each group SHA, plus live combined-check validation.
Never make CI synthesize review success to fill that gap. The `merge_group`
triggers prepare the CI side only; they do not authorize or enable a queue.

### Integration-owner verification

After publishing the workflow change, open or update a PR against a non-`main`,
non-`integrate/**` stack base and retarget it without changing its head. Confirm
fresh PR runs contain `lint`, `test (3.12)`, `test (3.14)`, `ty`, and both gates.
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

## 2026-08-14 incident lessons

- `pyproject.toml` advertised Python 3.11 while the lock supported only 3.12+;
  lint and the 3.11 test job could never install.
- A canary test used the executor's real credential probe. It passed on an
  authenticated workstation and deferred all dispatch on a clean CI runner.
- At the time, private-repository branch protection was unavailable on the
  repository's plan. Several locally integrated PRs therefore reached main with
  red GitHub checks. This historical limitation is not evidence of today's
  protection settings.

Deterministic seams, a local CI-parity command, explicit check inspection, and an
auditable merge record remain process obligations. They supplement enforced
GitHub rules; they do not provide equivalent enforcement when rules are absent.
