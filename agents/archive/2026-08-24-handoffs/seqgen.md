Status: done
Last: merged the deterministic uncertified SEQGEN v0 candidate batch in PR #152 (`45516f8`)
Next: resolve F-SEQGEN-1, then execute and retain one supported `m049-v1` packet per exact package before any admission decision
Blockers: F-SEQGEN-1 and absent supported M049 executable evidence packets

# M053 (G) — SEQGEN v0 sequence-first synthetic task candidates

## Outcome and boundary

`evallab.seqgen` (`seqgen@0.1.0`) deterministically emits four distinct Zone
03 Harbor task packages from seed 7. They are **uncertified candidates**, not
registered tasks: each package and `BATCH.json` explicitly records
`state: uncertified`, an absent M049 packet, and `admission_state: unadmitted`.
The deterministic inventory schema represents absence from the registry as
`registration_state: null` for all four; no candidate packet, registry record,
or admission exists.

The implementation is an independent reimplementation of the sequence-first
idea from the TASTE paper-level description. A pinned restricted source
snapshot was inspected for dependency and license assessment, so this handoff
does not claim an implementation firewall. No TASTE source code, prompt, model
output, or artifact was copied, adapted, or reused. Statements about that
repository remain scoped to the inspected pin. SEQGEN uses stdlib-only,
in-repository code and a deterministic declarative renderer with no generating
model and no prompt.

## Deterministic package contract

- Domain: seeded JSONL order records and seven pure record-pipeline operations.
- Selection: precondition-valid 3–6 operation sequences, each operation changes
  state, followed by canonical JSONL output; greedy coverage selection has a
  deterministic index tie-break.
- Package identities: `seqgen-s7-000` through `seqgen-s7-003`, each with a
  distinct sequence and package digest in `BATCH.json`.
- Identity record: each `generation.json` binds generator transform and source
  digest, validator code digest, explicit null model/prompt identities, master
  and candidate seeds, sequence digest, input digest, output digest,
  instruction digest, and task/verifier/tool digests.
- Provenance: Zone `03-synthetic`, revision/transform `seqgen@0.1.0`,
  package material digest, code/tool/domain/input/output/influence parents, and
  `license: NOASSERTION`. The repository does not grant a package license here;
  admission must not infer one.
- Research influence identity: `BATCH.json`, every `generation.json`, and each
  provenance parent bind the canonical upstream URL, full pinned revision,
  paper-level design plus dependency/license-assessment role,
  `restricted/NOASSERTION` status, zero code/prompt/output/artifact reuse,
  `snapshot_bytes_ingested: false`, and no implementation-firewall claim.
- Isolation: the committed candidates retain the workbench-required separate
  verifier and `network_mode = "no-network"`. The agent environment remains
  public because the task's visible contract includes only the synthetic input
  and bundled tool; no verifier fixture is exposed there.
- Leakage boundary: expected bytes are retained only in the trusted verifier
  fixture and the hidden reward-hack replay. Neither is copied into the agent
  image or instruction; the environment contains only its Dockerfile, input,
  and record-pipeline tool. Instructions describe outcomes rather than shell
  commands.

## M049 admission dependency

The packages contain the complete `m049-v1` control surface: oracle ×3, nop ×2,
at least three invalid controls, one fair alternative that directly implements
the selected sequence without invoking the bundled `rp` oracle, and one
reward-hack replay that embeds the expected bytes but creates a forbidden
extra output artifact. Presence of those files is not executable evidence and
does not certify a package.

Before any of the four candidates may be registered or admitted, the supported
M049 workbench must execute the fixed control plan against that exact package
digest and retain a valid packet that binds task/version/path, package,
candidate, generator, validator, control-plan, result, and replay identities.
The packet must show deterministic oracle output, expected rewards for all
controls, retained please-hack replay evidence, a clean leakage scan, and the
declared isolation evidence. Tamper, replay, circular identity, missing
evidence, or a package digest change requires refusal and a new packet.

Correctness, verifier soundness, verifier completeness, and solvability are
packet axes. **Difficulty calibration and realism review remain separate
axes**; neither may be inferred from sequence length, static inspection, or
control success.

## F-SEQGEN-1 — blocked follow-up

**Finding:** the committed no-network verifier declaration is required by the
M049 static isolation contract, while the observed Harbor 0.21.0 Docker path
on the inspected macOS workstation rejected that declaration before local
control execution. The locally runnable `inherit` variant does not satisfy the
no-network gate. These observations are machine/configuration scoped and do
not establish behavior on a supported Linux executor.

**Reproduction procedure:** on the affected macOS/Harbor 0.21.0 path, retain
the output from:

```bash
uv run python -m evallab.task_workbench check \
  library/synthetic/seqgen-v0/seqgen-s7-000 \
  --source-uri library/synthetic/seqgen-v0/seqgen-s7-000 \
  --source-ref seqgen@0.1.0 --license NOASSERTION --zone 03-synthetic \
  --run-controls
```

The recorded failure is before the fixed control battery completes, at
Harbor's verifier network-policy support check. Repeat on a supported executor
for all four exact package digests; do not regenerate with `inherit`.

**Evidence required to close:** retain either (a) an `m049-v1` packet produced
by a supported executor that enforces and records verifier no-network
isolation, or (b) an approved workbench/Harbor capability contract that still
fails closed and records equivalent enforced isolation. In both cases, replay
the exact fixed control set for all four package digests and retain the
executor/capability evidence.

**Acceptance:** all four exact candidates have supported, digest-bound
executable packets; no-network isolation is enforced rather than omitted; the
fixed control set and replay evidence are complete; realism and difficulty
remain separately unassessed or separately evidenced; registration remains a
distinct human decision.

Weakening or removing the no-network gate, substituting an `inherit` run, or
treating the earlier partial oracle/nop smoke observations as certification
does not resolve F-SEQGEN-1.
