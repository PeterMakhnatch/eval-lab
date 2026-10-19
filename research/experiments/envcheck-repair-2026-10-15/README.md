---
status: living
audience: [builder, analyst, runner]
---

# EnvCheck test repair — 2026-10-15

## Assignment and acceptance

This is the test-repair stage of the existing loop: executed finder flags →
instruction-scoped test additions → regrade previous passing submissions →
independent verification. The superseding Research steer prioritizes **001809,
001985 and 002486**, followed by confirmed finder flags. The earlier 002402 /
002391 seeds remain in the evidence packet. PR #867 was already merged
(`5e2bb943e`); this work builds on it, not a replacement framework.

A local repair requires reference **3/3 pass**, nop **3/3 fail**, and every
expected previous passing submission to fail. All checks must be scored.
Equivalent or out-of-scope survivors yield `partial`, never `repaired`.
Oracle/nop results alone do not certify assertion semantics or task admission.
Independent native verification is a separate, currently blocked stage.

## Measured outcomes

[results.json](results.json) binds each row to its actual receipt and package
digest. “Current protocol” means regraded after cumulative replay and intake
binding were corrected. Earlier receipts are retained, not relabeled as fresh
protocol executions.

| Task | Evidence class | Reference passes | Nop failures | Past checks failed / graded | Assertion scope |
|---|---|---:|---:|---:|---|
| 000657 | Current-protocol local repair | 3 | 3 | 4 / 4 | Retry chaining and nonretryable errors |
| 001809 | Current-protocol local repair | 3 | 3 | 3 / 3 | Body-plus-attribute SQS size boundary |
| 002486 | Current-protocol local repair | 3 | 3 | 1 / 1 | Explicit browser-install opt-in |
| 002976 | Current-protocol local repair | 3 | 3 | 3 / 3 | Button ordering ties with differing URLs |
| 000371 | Retained earlier local pass | 3 | 3 | 2 / 2 | Ordinal validation when real data is attached |
| 000931 | Retained earlier local pass | 3 | 3 | 1 / 1 | Nested Avro schema evolution / complex union defaults |
| 001686 | Retained earlier local pass | 3 | 3 | 1 / 1 | Torch tile integer repeats |
| 000847 | Partial, current protocol | 3 | 3 | 5 / 8 | Configuration loading; three test-fixture omissions survive |
| 001750 | Rejected first draft | 3 | 3 | 0 / 2 | Revised numerical-entry diagnostic remains ungraded |

000371's earlier receipt includes an obsolete extra docs-bearing comparison;
the required minimal submission also failed. The three retained-pass rows have
not been rerun under the newest protocol. None of the rows is a new task
admission or independent native certification.

### Priority positives

- **001809:** current variant `d10b4570c231…` tests public `send_message` and
  `receive_message` with a real in-memory queue. A binary attribute contributes
  15 bytes: the exact-limit message is delivered, and one extra byte is rejected.
  It does not require a reference-specific helper signature. The minimal
  subset and both real HAR-126 stock/GEPA partial fixes fail.
- **002486:** current variant `7a6e1896d418…` calls public `build_install_plan`
  with Firefox selected and explicit opt-in False/True. The real HAR-168 a2
  submission fails. Its reference implementation was recovered from the
  retained known-passing HAR-161 source-module hunk; the exact source, hashes
  and scope are preserved. No new history search was used.
- **001985:** revised tests exercise the real commit transform and diff parser
  on synthetic before/after C++ inputs. The unchanged fingerprint guard rejected
  earlier long literal byte fixtures; the revision generates ordinary diff
  input with `difflib` and passes static preflight. Its runtime grade was never
  launched. **No repaired claim.**

### Other blocked work

- **000471:** the model's initial “optional argument is harmless” decline was
  wrong: the real regex caller omits that argument. The revised test uses real
  metadata listing and only an external paginator fixture. Harmless annotation
  omissions remain declared survivors.
- **001750:** the first test's backend substitution did not guard the imported
  numerical entry point, so unrelated legacy FFT TypeErrors could satisfy it.
  The revision isolates device rejection before numerical execution, using
  only CPU tensors and mode selection; no GPU execution.
- **001804:** revised test runs real initialization inside an existing notebook
  event loop and observes an unavailable provider's error. No mocked
  initialize/request echo or incidental logging assertion.
- **002205:** prepared scalar scaled-Period round-trip and arithmetic tests.
- **000081:** reviewed public CLI failure-detail/continuation/summary and
  supplied-argv verbosity tests. A preflight enum-spelling error is retained;
  the corrected proposal passes without changing its assertions.
- **000354:** reviewed real-database historical cleanup and cardinality tests.
  They do not require the reference's new migration names or field class.
- **000560:** the climate timestamp defect is source-reviewed, but the actual
  parser/factory/payload fixture snapshot is still unavailable. No runnable
  proposal was fabricated.

All seven complete revised proposals pass the real offline schema, path,
fingerprint and report-binding preflight. That is **not** runtime verification.

## External execution blocker

The independent 002486 worker waited **2,700 seconds** for the required host
Docker FIFO and timed out before launching its first reference control.
Read-only diagnosis found the live unrelated holder
`qa-task/mvcc-lsm-compaction` (PID 83358), held for more than 4,200 seconds.
001809's queued native attempt was cancelled on the parent's instruction.
**Neither worker created a native job or obtained a score.**

The parent also cancelled only its own queued repair/source-extraction waits.
No lock bypass, slot increase, holder interruption or shared-runtime edit was
made. `envcheck` had no outgoing peer grant; the dependency was published in
the established Research bridge rather than bypassing routing.

The native packets retain exact per-case matrices, solution scripts, input
hashes, queue evidence and semantic reviews. Both workers verified test-only
package changes. Actual installed Harbor was **0.21.0** and resolved these
priority variants to a shared verifier environment. All nine SQS and seven
browser native grades remain unscored.

### Older seed failure is distinct

002402 / 002391 retain their Oct 13 partial results. All twelve Oct 15 native
controls failed in infrastructure with `/tests/test.sh` absent. Observed
configuration resolution inherited the agent image when
`verifier.environment` was absent; the separate-verifier path skipped test
upload. This supports the recorded missing-test-image diagnosis, not a new
semantic verdict. Neither the package nor shared Harbor runtime was changed,
and raw sandbox scores were not substituted for these native receipts.

## Replay and provenance corrections

Finder removals are **cumulative**, not independent full-reference-minus-one
mutants. The adapter now reconstructs the actual sequence, checks retained
block counts, and deduplicates exact submissions. This supersedes the earlier
002976/import-only interpretation. 000657, 000847 and 002976 were regraded after
the correction.

The implementation also binds task/package/reference identities, validates
parent/variant consistency and supported patch paths, requires an explicit
shortcut quorum, filters real diffs before prompt truncation, and preserves
lineage-update errors. Reports containing surviving line-level mutations are
refused rather than silently omitting those additional passes.

`--proposal` grades a reviewed retained JSON proposal without a model call or
quota fabrication. MCP tool activity remains rejected; a recovered/reviewed
proposal is not relabeled as a successful text-only transport. Original
failures, capped historical regrade quota receipts and usage records are kept;
the capped records are not treated as live observations or double-counted as
new calls. See the canonical [task-quality guide](../../../docs/task-quality.md)
for the command contract.

## Intake coverage

The frozen later finder snapshot contains **31** high/medium rows: 13
independently labeled confirmed-weak, eight out-of-scope, three neutral and
seven untriaged. It replaces the earlier README's inconsistent inventory
counts. This packet covers the confirmed group, the priority positives and the
older seeds; it does not claim the seven untriaged rows resolved:
000125, 000769, 001205, 001267, 002033, 002290, 002545.

Do not manufacture assertions for neutral, out-of-scope or untriaged flags.
The machine index retains their finder-stage status separately.

## Custody, spend and resumption

Immutable supporting evidence is retained outside Git at:

`~/Developer/eval-lab-results/2026-10-15/envcheck-repair/milestone-3/`

This is a custody packet, **not a fabricated Harbor job**. It preserves original
attempts, native failures/blockers, proposals, model/usage/quota evidence,
reference provenance, input snapshots, owned variant lineage and hashes.
Content-addressed materialized variants remain in the canonical task store.
The current worktree is retained for the blocked assignment; new attempts must
use fresh output directories, never overwrite the frozen receipts.

When capacity is available, use the existing `evallab docker-lock run` and
`evallab hack ref-repair --proposal` paths for prepared revisions, then the
preserved canonical native matrices for independent verification. 000560 first
needs the actual source/fixture snapshot. There is no new paid authorization
requirement for these already-approved local controls.

Paid spend is **$0**, recorded by the existing census ledger in
[spend.jsonl](spend.jsonl). The lane authorization remains **$2**, within the
shared $20 plan; the ledger command's generic $13 slice display is not an
approval increase. No GPU, local model inference or paid API run was used.

The integrated checkpoint at `815f454bf` passed static gates, ty zero and 159
focused tests. The later mixed-report guard was observed failing before,
passing after, and rejecting the public CLI input before any model/Docker call.
Required exact-head GitHub CI remains the code-delivery gate; experimental
acceptance remains separately blocked as described above.

## Released-v7 contract follow-up: PB-1 (2026-10-19)

The separate v7.1 repair fixtures live in
[`envcheck-v71-pb1-20261019/fixtures`](../envcheck-v71-pb1-20261019/fixtures/):

- [`002915.py`](../envcheck-v71-pb1-20261019/fixtures/002915.py) preserves equality
  of separately constructed, structurally equal unmeasured Cirq circuits when
  measurement-key equality is required. It also checks qubit-identity options
  and unequal gate structure through the real Mitiq comparator.
- [`002451.py`](../envcheck-v71-pb1-20261019/fixtures/002451.py) preserves Satpy's
  missing-unit composite workflow and unsupported-conversion errors unless
  conversion is explicitly disabled. It reuses the released-v7 external
  `pyninjotiff` I/O fixture while exercising real writer/conversion logic and
  asserting the numeric image data that reaches that boundary.

Embed these files through `RepairTest` and `derive_repair_variant`; the native
repository destinations are `mitiq/tests/test_v71_empty_measurement_keys.py`
and `satpy/tests/writer_tests/test_v71_ninjotiff_missing_units.py`. The derived
grader appends to the exact released-v7 patch and wraps, rather than replaces,
its original command. Instructions, reference solutions and environments are
unchanged.

The PB-1 acceptance gate is reference repeat3, nop repeat3, every admitted old
and new wrong submission, and distinct fresh native replays of both new
released-v7 wrong programs. No independently admitted correct alternate was
supplied for either task; reference controls do not certify arbitrary correct
submissions. Qualification and release are separate: the delta handoff
`bridge/2026-10-19-night/repairs-v7.1.json` in research-context does not itself
reopen scoring or training.

Both packages completed that native qualification gate. The reviewed
[custody capsule](../../evidence/envcheck-v71-pb1-20261019.tar.gz) retains all
native-consumer source bindings, the exact parent and child packages, full
exposed lineage records, admissions, controls and one-off callers. Its index
binds each original path to an archived member and digest. It excludes virtual
environments and container images and does not authorize retiring referenced
runtimes or releasing the held-out tasks.
