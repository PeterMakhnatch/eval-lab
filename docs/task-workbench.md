---
status: living
audience:
  - builder
---

# Native task package checks

`evallab.task_lint` owns read-only package metadata and security checks.
Workbench scan, plan, control execution, and packet generation are retired.
Registry retains publication and exact-package hash binding.

A clean package check is not a Gold certificate. Certification uses strict G1–G7.
A package-check packet never supplies a missing Gold result.

## Lightweight lint

```bash
uv run evallab tasks lint path/to/task --json
```

This command checks verifier separation, declared artifacts, solution presence,
control expectation headers, and hidden inputs in Docker `COPY`/`ADD` operands.
It reports unsupported syntax instead of silently accepting unreadable instructions.

## Native metadata and security checks

```bash
uv run python -m evallab.task_lint path/to/task
```

The command emits diagnostic JSON. An error returns exit code `1`.
It does not execute a task, start Docker, read credential values, or write a packet.
Use `--credential NAME` to declare an allowed credential name. Repeat the option for each name.

The checks preserve these boundaries:

- Package layout, executable scripts, package names, metadata, timeout limits, and artifact confinement.
- The supported TOML surface and effective verifier network policy.
- Separate verifier execution, without a shared-container acceptance path.
- Compose services, mounts, task-local volumes, internal networks, and credential placeholders.
- Local MCP endpoints and deterministic artifact collection hooks.
- Agent-visible hidden-input screening and pinned Docker images.
- Offline build proofs, lock hashes, trusted wheel manifests, runtime assets, and nested build contexts.
- Runtime network use, static verifier nondeterminism, and missing reward outputs.

The Python API returns diagnostics without candidate identities or control plans:

```python
from pathlib import Path
from evallab.task_lint import check_package

findings = check_package(Path("path/to/task"))
```

Review each diagnostic. Static checks cannot establish semantic verifier correctness.
Use the native certification path for execution evidence and strict G1–G7 acceptance.

## Retained packet and receipt readers

Registry continues to validate historical `certification.json` packets against exact package hashes.
Its `--certification-packet` flag remains available. Existing packet kinds, versions, and hashes remain unchanged.
This binding does not grant Gold certification or publication permission.

Read-only receipt APIs remain in `evallab.task_lint`:

- `load_quality_audit_evidence` reads legacy and owned-format audit receipts.
- `compare_quality_audits` compares retained audit directories or native review/repair records.

The readers preserve raw rewards, unknown outcomes, manifest coverage, archive identity, and provenance limits.
They do not execute controls, create labels, register tasks, or infer semantic improvement from reward changes.
Stored evidence and receipts remain intact. See [task registration](task-registry.md) for the publication boundary.
