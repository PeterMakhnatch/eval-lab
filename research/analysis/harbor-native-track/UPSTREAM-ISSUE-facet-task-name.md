---
title: "FACET-Terminal task.toml name='FACET-Terminal' violates Harbor 0.21.0 org/name rule"
target_repo: "StoKou/FACET-Terminal"
target_schema: "Harbor 0.21.0+ TaskConfig / PackageInfo"
status: "filed-artifact"
date: "2026-09-06"
---

# Upstream Issue: `name = "FACET-Terminal"` rejected by Harbor 0.21.0 `org/name` rule

## Summary
In the released FACET-Terminal archive (`FACET-Terminal-Tasks-6k`, SHA256 `7355ea41…`), every task's `task.toml` defines a single static name:

```toml
[task]
name = "FACET-Terminal"
```

When attempting to load these tasks via Harbor 0.21.0 (`harbor run -p <task_dir>` or `harbor run -p derived/harbor-packs/facet/tasks`), Harbor rejects every task directory. The run fails with:
`"Either datasets or tasks must be provided"` because `TaskModel.is_valid_dir` returns `False` for all task directories.

## Root Cause
Harbor 0.21.0 enforces that task package names must match `ORG_NAME_PATTERN` (`^[a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+$`):
1. A valid task name requires an organization component and a task slug component separated by a slash (e.g., `facet/task_000001` or `facet-terminal/task_000001`).
2. Single-component names without a slash fail validation in `PackageInfo.model_validate`.
3. Furthermore, giving all 6,020 tasks the identical name `"FACET-Terminal"` collides task identities across trial results, traces, and metrics.

## Proposed Upstream Fix
Update the release generation pipeline (specifically `facet/facet_terminal/harbor-template/task.toml`) to emit namespaced, unique task names matching the directory slug:
```toml
[task]
name = "facet-terminal/{{ task_id }}"
```
For example: `name = "facet-terminal/task_000001"`.
