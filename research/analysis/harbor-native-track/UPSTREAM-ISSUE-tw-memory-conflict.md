---
title: "TerminalWorld task.toml conflicting memory and memory_mb schema values"
target_repo: "EuniAI/TerminalWorld"
target_schema: "Harbor 0.21.0+ TaskConfig / EnvironmentConfig"
status: "filed-artifact"
date: "2026-09-06"
---

# Upstream Issue: Conflicting `memory` and `memory_mb` values in task.toml

## Summary
In the released TerminalWorld task pack (`EuniAI/TerminalWorld`), multiple task definitions (at least 12 of 20 sampled tasks, including `tw_101703`, `tw_103367`, `tw_104903`, `tw_105786`, `tw_11696`, etc.) declare conflicting resource allocation values in `[environment]`:

```toml
[environment]
build_timeout_sec = 600.0
cpus = 1
memory = "2G"
storage = "10G"
memory_mb = 4096
storage_mb = 10240
```

## Root Cause
Harbor 0.21.0+ enforces strict schema validation in `EnvironmentConfig._migrate_legacy_resource_fields`:
1. `memory` is deprecated in favor of `memory_mb`.
2. When both `memory` and `memory_mb` are present, Harbor parses `memory = "2G"` as `2048 MB` and compares it to `memory_mb = 4096`.
3. Because `2048 != 4096`, Harbor raises `ValueError: Conflicting 'memory' and 'memory_mb' values: memory='2G' (2048 MB) != memory_mb=4096`.
4. This causes Harbor's task discovery to fail with "Either datasets or tasks must be provided" or drop the affected tasks as invalid.

## Proposed Upstream Fix
Remove the legacy string fields `memory = "..."` and `storage = "..."` from the released `task.toml` files, retaining only the modern numeric fields:
```toml
[environment]
build_timeout_sec = 600.0
cpus = 1
memory_mb = 4096
storage_mb = 10240
```
Alternatively, ensure that if both fields are retained for backwards compatibility with legacy runners, the parsed values must match exactly (e.g., `memory = "4G"` if `memory_mb = 4096`).
