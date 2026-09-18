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

Linear issue **HAR-46** owns shared release infrastructure and runtime adoption,
not a mandatory handoff for every PR. Each author's existing issue owns their PR
through delivery. Historical pickup claims in `claims/` retain their lineage and
path boundaries.

Per Peter's explicit direct order, the delivery contract is author → CI/fixes → SHA-guarded
protected merge → bounded merged-revision proof. There is no mandatory independent reviewer,
review attestation, or RE routine merge handoff.

The release operating loop assigns clear, disjoint responsibilities:
1. **Release owner (RE - Eval Lab)**: Owns release orchestration, shared integration
   worktrees and branches, cross-mission coordination, and safe local/runtime adoption.
   RE does not hold routine merge handoffs or act as a gatekeeper for author PR merges.
2. **Code authors**: Original code authors are the delivery DRIs for their PRs. They build,
   shepherd CI to green, fix all CI failures and any concrete bugs (including issues previously
   raised by retired reviewers), execute guarded protected merges to `main`, and verify merged
   behavior on `main`. Authors do not mark delivery complete merely for publishing a PR or opting
   into auto-merge.
3. **GitHub checks and guarded merge**: GitHub Actions enforces the required gates
   (`quality-required`, `typecheck-required`). Merges (manual guarded merge or native auto-merge)
   are executed only after every required and reported check succeeds on that exact head SHA.
4. **Safe local and runtime adoption**: Merged source code and deployed runtime environments
   remain distinct. RE - Eval Lab adopts merged code into the active runtime environment
   preserving all user and owner state. Merging to `main` is not permission to deploy or disrupt
   shared-runtime environments.
5. **Acceptance**: Research - Harbor verifies scientific alignment and accepts delivered
   work in Linear. Genuine external blockers are recorded on the existing Linear card with
   concrete dependencies, not left as idle handoffs.
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
4. Zero reviewer approvals are required. The author executes the merge only after
   the explicit core gates and every reported check succeed for the current head/base
   pair (`agents/CHECKS.md`). Local green, a different head's CI, or a merge to an
   integration branch is not a merge to `main`.
5. Follow through to actual merge and post-merge verification. A PR is not delivered
   when opened or queued for auto-merge; the author confirms the squash-merge has landed
   on `main` and verifies the bounded merged behavior (e.g. checking merge commit CI or postmerge status).
6. Leave the primary checkout and other workers' uncommitted files untouched during
   reconciliation. Never force-push `main` or silently resolve an ownership conflict.
7. Update canonical topic documents in place instead of adding a new dated brief,
   result, or handoff file for each iteration. Dated files are for closure records only.

### Author-driven merge and native auto-merge

Delivery follows an author-driven CI-only model. Zero reviewer approvals are required,
and there is no mandatory independent review gate or review attestation status (`independent-review`).
Past `independent-review` commit statuses in repository history are not active CI gates and must
never be faked or reset. Authors remain responsible for diagnosing and fixing all CI failures,
as well as resolving known concrete bugs on the branch; removing the review gate does not permit
knowingly merging broken code.

Before merging or opting a PR into native auto-merge:

1. Verify that all required and reported GitHub Actions checks (`quality-required`,
   `typecheck-required`, etc.) are successful for the exact PR head SHA.
2. Confirm that there are no unresolved CI failures, test regressions, or known concrete
   bugs.
3. Verify the live target-branch protection enforces the required gates in
   `agents/CHECKS.md`, with no admin bypass.
4. With every required and reported check successful for the current head/base pair, the
   author (or an integrator) executes the merge using the head-commit guard to prevent racing pushes:

   ```bash
   gh pr merge <PR_NUMBER> --squash --delete-branch --match-head-commit <EXACT_HEAD_SHA>
   ```

   Never pass `--admin` to bypass required checks or branch protections.
5. If using GitHub's native auto-merge instead, the author may opt in after the required
   checks are configured and in-progress. Native auto-merge will merge once all requirements succeed.
   Draft pull requests and explicit operator holds (e.g. hold labels) remain honored pauses
   preventing merge.
6. A changed head/base requires fresh combined evidence. If the PR branch is updated or rebased,
   await fresh complete CI runs on the new head before merging.

Use GitHub's native protected merge or native auto-merge, not a privileged custom merger,
fabricated check, or `pull_request_target` workflow executing PR code. Workflow tokens
remain read-only and actions remain SHA-pinned.

See GitHub's [native auto-merge semantics](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/automatically-merging-a-pull-request).
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

For routine estate reduction, prefer conservative hygiene sweeps (such as `evallab tidy`)
over ad-hoc removals: preserve every commit (unpushed branches are pushed before their worktree
is removed), hold trees carrying ignored `runs/` job evidence for the evidence lifecycle instead
of deleting them, and record dispositions.

