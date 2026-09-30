# HAR-117 sync research: keeping the primary checkout on `origin/main`

**Recommendation: a launchd timer (`StartInterval` 300) that runs `git fetch` plus
`git merge --ff-only`, guarded by clean-tree / on-`main` / not-diverged checks, and
skips with a recorded reason otherwise. This confirms Peter's revised design; no
design change is needed.**

- **`git maintenance` cannot move the branch.** Its `prefetch` task fetches objects
  into `refs/prefetch/` and deliberately leaves remote-tracking branches alone, only
  making a later real `fetch` cheaper. It never updates a checked-out branch, so it
  complements but cannot replace the sync step.
- **Hooks cannot pull.** A `post-merge` hook fires only inside the clone where a merge
  already happened; hooks do not propagate across clones and nothing invokes them when
  the remote advances. A pull-based timer is required.
- **`--ff-only` is the safe updater.** It refuses unless local `main` is an ancestor of
  `origin/main`, so it can never discard or rewrite local work. With explicit pre-checks,
  `reset`/`stash`/`checkout` are never needed and are banned.
- **Untracked files block.** An untracked file can collide with an incoming path (in which
  case even `--ff-only` refuses mid-merge), and unknown files mean unknown provenance,
  so any non-ignored untracked file counts as dirty: skip and report the file count.
- **Worktree caveat.** Plain `fetch` only moves remote-tracking refs and is
  worktree-safe, but a forced refspec update to a branch checked out in any worktree is
  refused. So the merge must run *inside* the primary checkout, which is the only place
  `main` is checked out; agents keep using branches in `.worktrees/`.
- **Timer mechanics.** `StartInterval` is the standard periodic-job key, with logs via
  `StandardOutPath`/`StandardErrorPath`. Notifications go through
  `osascript -e 'display notification …'`, rate-limited to state changes or one ping per
  N hours so a persistently dirty tree does not spam.

## Sources

- `git maintenance` / prefetch semantics: https://git-scm.com/docs/git-maintenance
- `git merge --ff-only`: https://git-scm.com/docs/git-merge
- Worktree checkout and ref-update restrictions: https://git-scm.com/docs/git-worktree
- Fetch refspec behaviour: https://git-scm.com/docs/git-fetch
- Hooks run locally, do not propagate: https://git-scm.com/docs/githooks
- Apple, Scheduling Timed Jobs (`StartInterval`): https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/ScheduledJobs.html
- Apple, Creating Launch Daemons and Agents: https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html (`man launchd.plist` for key reference)
- Fetch-vs-pull background: https://www.atlassian.com/git/tutorials/syncing/git-fetch
