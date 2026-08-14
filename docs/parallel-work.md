# Moved

The parallel work protocol now lives at [`agents/WORKFLOW.md`](../agents/WORKFLOW.md),
with the role registry at [`agents/ROLES.md`](../agents/ROLES.md).

Key change from the original version of this file: worktrees live **inside**
the repository at `.worktrees/<role>` (gitignored). Sibling `../helab-*`
directories are retired; creating anything outside the repo root is a
protocol violation.
