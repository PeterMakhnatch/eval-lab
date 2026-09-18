# Platform Builder — expose CLI staged-control bootstrap

## Dedicated writer branch

Create and use a new isolated worktree; do not edit canonical integration or dirty root:

- Base repository: `/private/tmp/eval-lab-staged-spine-integration`
- Exact base: `770488cfb1c5318c6b1f39d386c2e251d8864487`
- New worktree: `/private/tmp/eval-lab-cli-stage-controls`
- New branch: `fix/cli-stage-controls`

One writer only. Do not run models, controls, paid services, or integrate.

## Defect

The production Python API has the strict registered two-phase bootstrap:

```python
promote_task(..., state="registered", actor=..., certification_path=..., stage_controls=True)
```

But `src/evallab/cli.py::_registry_promote_command` and the `registry promote` parser expose no `--stage-controls` flag. A user therefore cannot perform phase 1 entirely through the CLI. This is a real control-plane gap documented in `/tmp/platform-builder-registry-test-adaptation-map.md` lines 177–183.

## Required behavior

1. Add an explicit boolean `--stage-controls` option to `registry promote` and pass it exactly to `promote_task(stage_controls=...)`.
2. Fail before filesystem/registry mutation for invalid CLI combinations:
   - `--stage-controls` without `--state registered`;
   - missing/blank `--actor`;
   - missing certification packet (the m049-v2/bound certification requirement must remain authoritative);
   - any other combination that cannot satisfy the existing API staging contract.
3. A valid CLI phase-1 call must persist the exact bound registered revision with:
   - `state == "registered"`;
   - `state_reason == "control_evidence_pending"`;
   - `allowed_uses == ["canary"]`;
   - `control_evidence is None`;
   - actor/approval/certification and immutable runtime identity bound.
4. CLI output must state that this is a control-pending staged registered revision, not imply measurement admission or completed registration.
5. Existing non-staged promote/finalization behavior remains unchanged. Phase 2 is the same `registry promote` command without `--stage-controls`, with the same actor/immutable revision after durable strict oracle/nop controls exist.
6. Do not add a dispatch shim, synthetic authority, or pre-registration evidence shortcut. Do not weaken Python API checks.

## Tests

Use public CLI behavior, not source-text assertions:

- parser accepts `--stage-controls`;
- invalid combinations return nonzero and leave no registry record;
- valid m049-v2 CLI stage persists exact pending registered fields and CLI wording;
- measurement spec remains unavailable while pending; only the existing oracle/nop baseline bootstrap exception applies;
- a strict-control fixture can be finalized through the CLI without the flag, preserving the staged runtime identity;
- existing CLI promote/register/external-lineage/refusal tests remain green.

Prefer reusing current packet and strict-control bootstrap helpers. Do not hand-author a shadow trial-analysis schema.

## Verification and handoff

Run only focused CLI/registry/campaign tests covering the changed contract, touched Ruff/format, and `git diff --check`. Commit cleanly and page `wH:p9` with exact base/head, changed paths/stat, test counts, and no-model/no-integration confirmation. Do not land it into the canonical spine.
