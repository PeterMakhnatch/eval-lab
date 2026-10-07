# Agent workflow

`agents/OWNERS.md` routes semantic responsibility; `agents/STRUCTURE.md` owns placement; `agents/CHECKS.md` defines exact-head delivery. Linear is the current queue. Global OMP rules own persistent-chat routing and dispatcher behavior; this file does not grant peer-contact authority.

## Worktree and scope

Keep the primary checkout on `main`, preserving user/other-writer state. A sync timer fast-forwards it to `origin/main` only when the tree is clean, HEAD is on `main`, and local `main` is an ancestor of `origin/main`; it never resets, stashes, cleans, or switches branches. Substantive code work uses an owned topic branch and native `git worktree add`; launch the session with `omp --cwd <owned-worktree>`. `.worktrees/` is conventional; temporary scratch may use `/tmp`, but a persistent repository chat must not start at bare `/tmp`.

```bash
cd ~/Developer/eval-lab
/usr/bin/git fetch origin
/usr/bin/git worktree add -b <topic> /private/tmp/eval-lab-<topic> origin/main
cd /private/tmp/eval-lab-<topic>
uv sync --locked
```

Use the worktree's own isolated environment and absolute owned runtime/job roots; do not copy the primary directory to create an isolated checkout (a copied tree can carry an environment pointing at the original interpreter). Do not reset a dirty primary, blindly pull, force-remove a worktree, or smoke-test by draining a shared/paid queue. Harbor jobs belong in that worktree's `runs/`, never in another worker's runtime.

## Intake and parallel work

Pull authorized lane work with `lin next`; a direct Peter request authorizes scoped free work without a second claim or go. Backlog proposals remain proposals. Historical board claim files are lineage and the governance pickup counter, not contact rights or permission to resume a mission.

Set disjoint write scopes and shared interfaces before native task fan-out. One writer per file/worktree, one integration owner per barrier. Pass relevant repository requirements explicitly; task spawning does not inherit AGENTS.md files. Delegates skip checks unless explicitly assigned verification.

If another owner's change overlaps, stop only the contested write and record the dependency in the existing handoff/task; continue independent safe work. Do not contact a persistent peer without Peter's approved route.

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

Use an existing handoff for sustained work that needs one; do not create a new
decision log, script, or handoff for a small change. Material decisions, scoped
Peter instructions, approvals, and evidence belong there or on the existing Linear
issue. An old plan/cap does not authorize new spend or revive a missing worktree.

`python -m evallab.governance check` validates claim fields, claim uniqueness,
linked live handoffs, governance documents, and the tracked root freeze.
`scripts/fleet-status.sh` displays Git state and the actual pickup counter;
it does not grant ownership or authorize deletion.

## Delivery

The author follows CHECKS through PR, CI fixes, protected merge, and bounded merged-revision verification. Do not add a separate reviewer/integrator gate. `lin review` records final delivery evidence; open PRs stay author-owned work. Code merge, runtime adoption, and Research-Harbor scientific acceptance are distinct outcomes.

Guarded merge uses the head-commit guard to prevent racing pushes:

```bash
gh pr merge <PR_NUMBER> --squash --delete-branch --match-head-commit <EXACT_HEAD_SHA>
```

Never pass `--admin` to bypass required checks or branch protections. Native auto-merge is optional: opt in only after all reported CI checks pass, never for a draft or an explicit operator-held PR. A changed head/base requires fresh combined evidence before merging.

The HAR-46 release path is closed (Linear: Done); its standing delivery contract survives in CHECKS. Merged source code and deployed runtime environments remain distinct: adopting merged code into the active runtime preserves all user and owner state, and merging to `main` is not permission to deploy or disrupt shared-runtime environments. Do not trigger live model execution, billable inferences, or batch queue drains merely to smoke-test adoption.

Dispatcher wake/presence follows current configuration (`dispatch.wake` is presently false); do not run `dispatch --once`, enable wakes, or rewire routing merely to answer status.

## Retirement

Follow `docs/GENERATED-CACHE-POLICY.md`: prove inactive ownership/processes, merged/current refs, clean state, recoverable commits, and survival of unique ignored evidence. Preserve local refs or a verified archive when publication is not authorized; do not push all unpushed branches by default. Use native no-force removal after the owned PR is complete, and keep unknown/dirty/in-use trees intact.

For routine estate reduction, prefer conservative hygiene sweeps (such as `evallab tidy`)
over ad-hoc removals: preserve every commit (unpushed branches are pushed before their worktree
is removed), hold trees carrying ignored `runs/` job evidence for the evidence lifecycle instead
of deleting them, and record dispositions.

## Optional bulk-change sequencing

For repeated edits, establish the contract and pilot the transformation where its risk warrants it (for example, one unit by hand before scripting a sweep). Incremental localization is an option when later steps depend on earlier correctness, not a compulsory check after each hunk. Required green applies to the published merge head, not every unpublished development commit.

Split independently reviewable contracts: keep generated payloads and the code under review apart, and keep schema/code with their required matching fixtures when integrity depends on the same change. Parallel workers skip shared validation; one integration owner verifies the integrated batch at the barrier.

Scripts become permanent only with a recurring consumer or as the requested deliverable; otherwise preserve the result and remove the probe. Record material forks, observed checks, pivots, and blockers in the existing task/handoff/result manifest. Scope input reads and use native workers for genuinely independent authorized work, not for a payload alone.
