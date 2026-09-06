# Agent workflow

`AGENTS.md` owns safety boundaries, `agents/OWNERS.md` owns permanent path lanes,
`agents/STRUCTURE.md` owns placement, and `agents/CHECKS.md` owns verification.
The single backlog and pull protocol is `research/inbox/board.md`; its
`claims/` directory is the pickup counter. `agents/missions/ACTIVE.md` is navigation,
not a second roster. This workflow supersedes `docs/parallel-work.md`.

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

## One integration owner, disjoint paths per worker

Claim work through the existing board protocol. Follow its no-peer-assignment and
one-open-claim rules; direct instructions from Peter take precedence. Native
subagents within an assigned task may own disjoint files under one integration
owner. They must not run competing Git mutations, generation, or full validation
against a shared worktree.

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
   exact head. Merge only after all required GitHub checks succeed for that head.
   Local green, a different head's CI, or a merge to an integration branch is not
   a merge to `main`.
5. Leave the primary checkout and other workers' uncommitted files untouched during
   reconciliation. Never force-push `main` or silently resolve an ownership conflict.

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
