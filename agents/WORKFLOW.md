# Agent workflow

`agents/OWNERS.md` routes semantic responsibility; `agents/STRUCTURE.md` owns placement; `agents/CHECKS.md` defines exact-head delivery. Linear is the current queue. Global OMP rules own persistent-chat routing and dispatcher behavior; this file does not grant peer-contact authority.

## Worktree and scope

`AGENTS.md` owns the ordered worktree and delivery commands. Keep a persistent repository chat out of bare `/tmp`.
Use each worktree's isolated environment and owned runtime/job roots.
Do not copy an environment from another checkout or drain a shared queue as a smoke.
Harbor jobs belong in the owned worktree's ignored run directory.
The primary sync timer only fast-forwards a clean `main`; it never resets, stashes, cleans, or switches branches.

## Intake and parallel work

A direct Peter request authorizes scoped free work without a second claim or go. Linear holds current work. Backlog proposals and historical boards do not authorize work or peer contact.

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

## Delivery

The author follows the root change path through the checks in `agents/CHECKS.md`.
No independent reviewer or integration steward is a routine prerequisite.
The delivery manual, `docs/ship.md`, owns guarded merge, recovery, and retirement.
Record final evidence in the existing work item. Open PRs remain author-owned work.
Code merge, runtime adoption, and scientific acceptance are distinct outcomes.

Merged source code and deployed runtime environments remain distinct. Runtime adoption preserves all user and owner state. A merge does not authorize deployment, shared-service disruption, model calls, or paid queue drains.

Dispatcher wake/presence follows current configuration (`dispatch.wake` is presently false); do not run `dispatch --once`, enable wakes, or rewire routing merely to answer status.

## Retirement

`docs/GENERATED-CACHE-POLICY.md` owns worktree retirement and evidence retention.
Use native removal without force after delivery. Preserve unknown, dirty, active, or evidence-bearing trees.
Do not publish unrelated local branches merely to remove their worktrees.

## Optional bulk-change sequencing

For repeated edits, establish the contract and pilot the transformation where its risk warrants it (for example, one unit by hand before scripting a sweep). Incremental localization is an option when later steps depend on earlier correctness, not a compulsory check after each hunk. Required green applies to the published merge head, not every unpublished development commit.

Split independently reviewable contracts: keep generated payloads and the code under review apart, and keep schema/code with their required matching fixtures when integrity depends on the same change. Parallel workers skip shared validation; one integration owner verifies the integrated batch at the barrier.

Scripts become permanent only with a recurring consumer or as the requested deliverable; otherwise preserve the result and remove the probe. Record material forks, observed checks, pivots, and blockers in the existing task/handoff/result manifest. Scope input reads and use native workers for genuinely independent authorized work, not for a payload alone.
