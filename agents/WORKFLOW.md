# Agent workflow

`AGENTS.md` owns safety boundaries, `agents/OWNERS.md` owns permanent path lanes,
`agents/STRUCTURE.md` owns placement, and `agents/CHECKS.md` owns verification.
The historical backlog and pull protocol is `research/inbox/board.md`; its
`claims/` directory is the pickup counter, preserved for existing owner claims and
archival lineage. For the active release delivery path, Linear issue **HAR-46** is
the explicit release queue (see below). `agents/missions/ACTIVE.md` is navigation,
not a second roster.

## Worktree isolation and repository boundaries

Keep the primary checkout on `main`; do branch work in an isolated worktree under
`.worktrees/` or `/private/tmp/`. Do not reset, switch, clean, or stash another
worker's checkout. External scratch worktrees are permitted; durable deliverables
must land in the repository's declared structure.

On Peter's macOS host, use native `/usr/bin/git` for worktree creation. The local
`git` worktree helper can APFS-copy ignored runtime state, including nested
`.worktrees/` and an environment pointing at the original interpreter. Do not
copy the primary directory to create an isolated checkout.

```bash
cd ~/Developer/eval-lab
/usr/bin/git fetch origin
/usr/bin/git worktree add -b <topic> /private/tmp/eval-lab-<topic> origin/main
cd /private/tmp/eval-lab-<topic>
uv sync --locked
```

Each worktree has its own environment. When intentionally reusing an interpreter
for focused checks, bind `PYTHONPATH` to the target worktree's `src/` and verify
module origins; do not assume an editable installation follows the current directory.
Harbor jobs belong in that worktree's `runs/`, never in another worker's runtime.

## Release operating path (HAR-46)

For repository release management, sitting-PR delivery, and runtime adoption,
Linear issue **HAR-46** is the single explicit release queue. This resolves the
obsolete pickup-board claim for release work while preserving existing historical
owner claims and boundaries in `claims/`.

The release operating loop assigns clear, disjoint responsibilities:
1. **Release owner (RE - Eval Lab)**: Owns release orchestration, integration worktrees
   and branches, PR disposition, CI/devloop repairs, guarded auto-merge opt-in, and
   safe local/runtime adoption.
2. **Code authors**: Original code authors own fixes and repairs within their assigned
   paths and domains.
3. **Independent reviewers**: Native OMP workers in separate worktrees perform independent
   reviews of exact-head PR diffs and produce structured review receipts.
4. **GitHub checks and guarded auto-merge**: GitHub Actions enforces the required gates
   (`quality-required`, `typecheck-required`). Auto-merge is opted-in only after the
   integration owner attests to a completed independent review on that exact head SHA
   via the `independent-review` commit status.
5. **Safe local and runtime adoption**: RE - Eval Lab adopts merged code into the active
   runtime environment preserving all user and owner state.
6. **Acceptance**: Research - Harbor verifies scientific alignment and accepts delivered
   work in Linear.

### Queue dispatch and lane boundaries

RE - Eval Lab operates HAR-46 via explicit CLI commands (`lin get HAR-46`,
`lin start HAR-46`) and direct owner invocation. No automated Linear lane enrollment
or global agent wake is implied (`dispatch.wake=false` in the current configuration); automated dispatcher
wiring or global routing changes require explicit authorization from Peter.

### Runtime source versus primary data separation

Runtime adoption strictly separates executable code from primary research data:
- The primary checkout (`/Users/petermakhnatch/Developer/eval-lab`) contains uncommitted
  user and research state (dirty working tree, raw evidence, local databases, and
  active experiment branches) and MUST remain untouched—never perform a git reset,
  stash, checkout switch, or blind pull in the primary directory.
- Runtime services (e.g. launchctl nightly/tick entrypoints) execute from a reviewed,
  clean runtime checkout or isolated installation rather than mutating the primary working tree.
- No queue-dispatch smoke testing is run against adoption entrypoints; do not trigger
  live model execution, billable inferences, or batch queue drains merely to smoke-test
  adoption. The parent delivery lead provides final verified runtime paths and receipts.

## One integration owner, disjoint paths per worker

Claim work through the existing board protocol or assigned Linear issue. Follow
no-peer-assignment and one-open-claim rules; direct instructions from Peter take
precedence. Native subagents within an assigned task may own disjoint files under
one integration owner. They must not run competing Git mutations, generation, or
full validation against a shared worktree.

A claim signs pane/session identity and model and states the item, turn-specific
role, and reason for taking it. Do not create another mandatory decision log,
script, or handoff for a small change. Durable multi-step work may use the handoff
format below; link it from the existing claim with `handoff: agents/handoffs/<id>.md`.

## The handoff file

An explicitly linked live `agents/handoffs/<id>.md` begins with four lines:

```text
Status: ready | building | blocked | review-wanted | done
Last: <most recent completed step>
Next: <next step>
Blockers: <one line or none>
```

`ready` is pending execution; `building` means work started; `review-wanted` means
implementation awaits review, not that it is merged. `done` is transitional only:
after confirmed closure, normalize the header and archive the file one-to-one in
`agents/archive/<date>-handoffs/`, recording its old and new paths. Do not leave
completed handoffs in the live directory. Preserve archived bytes; update the
archive index rather than rewriting historical evidence.

`python -m evallab.governance check` validates claim fields, claim uniqueness,
linked live handoffs, governance documents, and the tracked root freeze.
`scripts/fleet-status.sh` displays Git state and the actual pickup counter;
it does not grant ownership or authorize deletion.

## Work loop and review

1. Make coherent changes on a named topic branch, confined to the assigned paths.
2. Run focused behavior checks during development. Before pushing, run the exact
   checkpoint in `agents/CHECKS.md`, including explicit `make docs` before freshness
   checks. Generation and installation are intentional writes; check commands are not.
3. Push the topic branch and open a PR against its declared integration target.
   Report the exact head, verification, and any unavailable evidence honestly.
4. The author does not self-approve. Peter or a different reviewer reviews the
   exact head; native GitHub approval requires an eligible principal other than
   the PR author. Merge only after the explicit core gates and every reported
   check succeed for the current head/base pair (`agents/CHECKS.md`). Local green,
   a different head's CI, or a merge to an integration branch is not a merge to
   `main`.
5. Leave the primary checkout and other workers' uncommitted files untouched during
   reconciliation. Never force-push `main` or silently resolve an ownership conflict.
6. Update canonical topic documents in place instead of adding a new dated brief,
   result, or handoff file for each iteration. Dated files are for closure records only.

### Independent review and native auto-merge

An independent native OMP review is an actual review by a worker other than the
author, with findings and disposition tied to the PR head. It is useful evidence,
but it is not a GitHub `APPROVED` review. Multiple agents authenticated as
`PeterMakhnatch` are one GitHub principal, not independent GitHub reviewers.
GitHub does not let a PR author approve their own PR; changing agent/session names,
writing an approval comment, or inventing another identity does not satisfy a
required review. Never fabricate approval events.

Before opting a PR into native auto-merge:

1. Obtain and resolve an independent review of the exact head, including CI
   workflow changes. Record the reviewer, head SHA, findings, and disposition.
   Publish an `independent-review` success status on that exact SHA with the
   review receipt's GitHub URL. This attests to an actual completed review; it
   must never be issued by the author alone or by a workflow merely because
   tests passed. The attestor remains accountable for the review's authenticity.
2. Verify the live target-branch protection enforces the required gates in
   `agents/CHECKS.md`, with no admin bypass. Where required native approvals are
   configured, obtain one from a real eligible independent reviewer.
3. Only then may the integration owner enable repository auto-merge and opt in
   that reviewed PR. Native auto-merge waits for configured requirements, not
   for an agent's written process or for every optional check. If optional checks
   remain outstanding, wait for them before opting in.
4. A changed head/base invalidates the process review receipt. Disable auto-merge,
   rerun/review the new combination, and opt in again only after acceptance.
   Do not rely on GitHub to disable it for every writer: automatic disabling is
   documented for updates by users without write permission.

When all agents share the PR author's GitHub principal, use the required
`independent-review` commit status to enforce a fresh integration-owner
attestation of the separate OMP review. Native approving-review count is zero
in this configuration; the independent review itself is still mandatory.
GitHub enforces the status on the exact commit, but does not authenticate the
identity of the reviewing OMP worker. A new head therefore blocks auto-merge
until a new reviewed-head attestation exists. Do not create another account,
forge a native approval, give an untrusted PR workflow status-write permission,
or automatically turn successful CI into successful review.

After checking the live PR head still matches the reviewed SHA, the integration
owner records the review using the existing GitHub commit-status API:

```bash
gh api --method POST repos/PeterMakhnatch/eval-lab/statuses/REVIEWED_SHA \
  -f state=success -f context=independent-review \
  -f description="Independent review accepted; see exact-head receipt" \
  -f target_url=GITHUB_REVIEW_RECEIPT_URL
```

The **CI steward** (`evallab steward run`, operations in `docs/ci-steward.md`) is
the standing integration owner for `main`: it runs the dual independent review of
each green head, posts the `independent-review` status, carries attestations
forward only across proven pure merges of `main`, squash-merges with GitHub's
SHA guard, deletes merged branches, and sweeps abandoned worktrees. Peter
retains override: the `steward:hold` label, a draft flag, or the steward's
`PAUSE` file removes any PR (or everything) from its authority. A manual
integration owner who reviews and attests per the rules above still satisfies
this section; the steward exists so the pipeline does not stall when no session
is live, not to weaken the evidence bar.

Use GitHub's native auto-merge, not a privileged custom merger, fabricated check,
or `pull_request_target` workflow executing PR code. Workflow tokens remain
read-only and actions remain SHA-pinned.

See GitHub's [native auto-merge semantics](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/automatically-merging-a-pull-request)
and [required review semantics](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches#require-pull-request-reviews-before-merging).

## Integration and sunset
An integration owner serializes shared mutations, final generation, and validation.
A squash-merged branch is spent; start subsequent work from the intended current
base rather than rebasing or pushing the old branch again.

Before archiving drafts or retiring worktrees, apply the preservation gates in
`docs/GENERATED-CACHE-POLICY.md` and record dispositions. Neither zero commits ahead,
age, a `done` label, nor a clean Git status alone permits deletion. Preserve unique
ignored evidence and recovery commits. Use `evallab tidy --dry-run` for its
fail-closed classification; do not reinterpret report-only retention notices as
permission to delete evidence. Do not stop another lane's services or alter its
credentials, environment, or dependency lock.

For routine estate reduction, prefer the standing CI steward hygiene pass
(`evallab steward hygiene --apply`) over ad-hoc removals: it preserves every
commit (unpushed branches are pushed before their worktree is removed), holds
trees carrying ignored `runs/` job evidence for the evidence lifecycle instead
of deleting them, and records every disposition in
`derived/ci-steward/hygiene-last.json`.

