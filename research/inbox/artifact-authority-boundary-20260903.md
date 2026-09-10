---
source_type: internal
---

# Track F second wave — shared artifact-authority boundary (2026-09-03)

Replaces the repeated `OFFLINE-VALIDATES-ONLY` self-attestation pattern across A (training export), B (deficit miner), D (trainer bundle), and the result-manifest consumer. No signatures: authority comes from repo-jailed bytes, not from anyone's claim. Single shared module, one owner; no overlapping branch work until the integration owner lands it. Revision 2: bytes-verification substrate is the existing evidence archive family (per wH:p9), not an invented receipt store.

## 1. The distinction, as types

Two and only two authority levels exist:

```python
AuthorityLevel = Literal["structural-self-consistent", "bytes-verified"]
```

- **`structural-self-consistent`**: the artifact's digests parse (`Digest` pattern), recompute over its content, and its internal references are well-formed. Nothing was read from disk/CAS. This proves math, not existence. Sufficient only for: fixtures, planner/bundle plan arithmetic, and dry-run validation.
- **`bytes-verified`**: every bound artifact was read from an authenticated immutable source — the existing `EvidenceArchive`/`EvidenceLocator` family (`evidence_store.py`: `reopen_evidence_archive:580`, `materialize_evidence:619`, `load_blob:697`; reopen revalidates record, archive, content digest, count, and bytes at `:461-571`) — or, for plain repo files, the repo-jailed no-follow walk (`_read_repo_regular_file` pattern, `campaigns.py:132-137`). `sha256` is computed over the actual anchored bytes, and **exact ref/digest parity** holds: the digest equals the declared `Digest`, and the ref is the canonical constant form — a CAS locator, an archive+record anchor, or a repo-relative POSIX path — never a caller-provided arbitrary path. Invented receipt types are prohibited: bytes-verification is a projection over authenticated archive entries, preserving archive+record+content anchors end to end.

An artifact claiming bytes-level authority without the bytes having been read by the verifying call is exactly the PR #359 defect; this boundary makes that state unrepresentable.

## 2. Exact interface (new module `src/evallab/artifact_authority.py`)

```python
class ArtifactRef(ContractModel):
    ref: str          # canonical form: repo-relative POSIX path, cas://sha256/<hex>,
                      # or archive+record anchor; validated, never trusted
    digest: Digest    # expected content digest

class ArchiveAnchor(ContractModel):
    archive_uri: str      # EvidenceLocator form from evidence_store
    record_kind: str      # e.g. "source-receipt", "campaign-job"
    record_id: str

class ArtifactAuthority(ContractModel):
    artifact: ArtifactRef
    anchor: ArchiveAnchor | None   # set when verified through the evidence archive
    level: AuthorityLevel
    verifier_implementation_digest: Digest   # sha256 over the verifying module's canonical identity
    # semantic digest covers ALL content fields; envelope timestamps are excluded from it:
    authority_digest: Digest

class AuthorityRefusal(ContractModel):
    reason: Literal[
        "authority_level_insufficient", "ref_digest_parity_failed",
        "ref_not_canonical", "source_unreadable", "verifier_implementation_mismatch",
        "receipt_digest_mismatch", "receipt_contradiction",
    ]
    detail: str

AuthorityResult = ArtifactAuthority | AuthorityRefusal

def verify_artifact(
    artifact: ArtifactRef,
    *,
    minimum_level: AuthorityLevel,
    verifier_implementation_digest: Digest,
    admissibility: TrialAdmissibilityV1 | None = None,   # required when minimum_level == "bytes-verified" on trial evidence
) -> AuthorityResult
```

Rules (all fail-closed, typed refusals, never exceptions across the boundary):

1. `verify_artifact` with `minimum_level="structural-self-consistent"` MAY verify structure only and returns the `structural` level; it never reads bytes.
2. `minimum_level="bytes-verified"` reads bytes through the authenticated archive path (`reopen_evidence_archive`/`materialize_evidence`/`load_blob`) or the jailed walk, checks ref/digest parity over the anchored bytes, and — for trial-derived evidence — additionally requires the Gate Zero source receipt (`track-f-gate-zero-receipt-contract-20260903.md`): a `TrialAdmissibilityV1`-verified record referenced by digest whose bound source digests match the computed bytes digests. Missing/unknown admissibility → `authority_level_insufficient`; wrong bytes → `receipt_digest_mismatch`; contradicting receipts → `receipt_contradiction`.
3. **Consumer obligation**: every consuming module declares its minimum level at the callsite. An artifact verified only `structural` handed to a `bytes-verified` consumer refuses with `authority_level_insufficient`. This one check deletes every prose `OFFLINE-VALIDATES-ONLY` self-attestation: the level is data, not a comment.
4. `verifier_implementation_digest` binds WHO verified: sha256 over `(module name, module version constant, verifying function name)`. Consumers pin the expected verifier implementation digest; mismatch refuses. This is the auditor's "which code asserted this" answer without signatures.
5. **No timestamps in the semantic digest**: `created_at`-style fields live on a non-semantic envelope excluded from `authority_digest` exactly like `_SPEC_DIGEST_EXCLUDES` (`campaigns.py:83-88`). Deterministic re-verification is byte-identical forever.
6. **No path-derived authority**: refs must match the canonical constant form; resolution is repo-jailed (`safe_resolve_subpath` / no-follow walk / authenticated archive anchors). A ref that resolves outside jail, or an absolute/alias spelling, refuses `ref_not_canonical` regardless of digest correctness.
7. **Spine substrate mandate**: bytes verification MUST go through `EvidenceArchive`/`EvidenceLocator` + `reopen_evidence_archive`/`materialize_evidence`/`load_blob` (independently anchored, no-follow) — not a new receipt/verification store. The boundary is an adapter/projection from authenticated archive entries into the facts A/B/D/result expect; archive+record+content anchors are preserved end to end.
8. Rehydration parity: a persisted `ArtifactAuthority` re-loaded must re-derive `authority_digest` and, for `bytes-verified`, re-anchor through `reopen_evidence_archive` on demand; a mutated payload with a recomputed digest is caught by consumer-side parity re-checks (same rule as PR #355 R1 / #357 rehydration).

## 3. Spine reuse (no second convention)

`ContractModel` (`schemas/__init__.py:21-24`), `Digest` (`schemas/__init__.py:160`), `canonical_json`/`compute_sha256` (`benchmark_program_contracts.py:20-31`), jailed file read + `EvidenceArchive`/`EvidenceLocator` + `read_record`/`restore_evidence`/`load_blob` (`evidence_store.py`), `TrialAdmissibilityV1` (`schemas/__init__.py:501`) and its verifier (`trial_admissibility.py:551`). The Gate Zero receipt (`track-f-gate-zero-receipt-contract-20260903.md`) remains the only new CAS record kind (`source-receipt`); it is itself an archive record, not a parallel store.

## 4. Owner and callsite migration plan

**Shared module owner: Repo Custodian (wH:p0)** — one PR, base `integrate/spine-batch1`, adding `src/evallab/artifact_authority.py` + tests only (no consumer changes in that PR).

Per-consumer migration (each in its owning PR, after the shared PR lands):

| Callsite | Owner | Current self-attestation | Migrates to |
|---|---|---|---|
| A — training export (`TrainingExampleManifest` mint) | wK:p9 (PR #359 branch) | caller-asserted lineage/CAS strings | `bytes-verified` via source receipt + archive anchors for every real row; `structural` rows only for fixture exports, typed by fixture flag |
| B — deficit miner evidence IDs | wK:p5 (PR #356 branch) | digest parity checked ad hoc | `bytes-verified` for real corpus, `structural` permitted for fixture artifacts with fixture flag; evidence IDs become `ArtifactRef`s |
| D — trainer bundle dataset/handoff bindings | wH:p1 (PR #357 branch) | self-digested handoff; `source_authority_status=copied_digest_refs_only` | plan/handoff math stays `structural`; dataset manifest binding becomes `bytes-verified`; expected-result parity re-expressed as level checks |
| result — external trainer result manifest consumer | wS:p5 (second-wave PR) | none yet (future surface) | result manifest accepted only `bytes-verified` against the frozen held-out evaluation refs; the only ingestion path for trainer outputs |

Sequence: shared PR → A and B (swap at their next repair commit) → D → result consumer. No branch starts before the shared PR lands (per second-wave freeze). wH:p1 has correctly recorded the shared PR as an unlanded dependency; D keeps `source_authority_status=copied_digest_refs_only` until migration.

## 5. Non-goals

No signatures, no KMS, no timestamps-as-authority, no new CAS kinds beyond `source-receipt`, no change to `TrialAdmissibilityV1` itself, no consumer behavior change beyond replacing ad-hoc checks with `verify_artifact` calls and level declarations.