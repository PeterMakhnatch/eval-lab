# Calibration evidence packs

The judge input the sealed calibration keys were decided against. Each family
directory is a byte-for-byte copy of the incident's evidence directory from the
read-only judged-output task source, the files the postmortem author had under
`/app/evidence`, plus a `manifest.json` that binds the copy to its origin:

```json
{
  "family": "checkout-pool-exhaustion",
  "source": {
    "repository": "https://github.com/PeterMakhnatch/harbor-practice",
    "revision": "a3bedf451187952f79f30c817cdcf5738ee6c24e",
    "path": "datasets/judged-output/checkout-pool-exhaustion/environment/evidence",
    "mount": "/app/evidence"
  },
  "files": [{"name": "alerts.log", "sha256": "sha256:…", "bytes": 914}, …]
}
```

`evallab.calibrate.load_evidence_pack(repo_root, family)` verifies every listed
file against its sha256, refuses missing or unlisted files, and returns the pack
with a digest over the source binding and every file's bytes. That digest is what
`JudgePredictionBundle.evidence_digest` and `JudgeCalibrationRecord.evidence_digest`
carry: a measurement names the exact evidence its judge saw, and the daily digest
counts a record as calibration only when it is evidence-bound.

## What lives here and what does not

- Evidence files and the manifest only. Never an answer key, a reference fact
  sheet, a rubric, or anything under a task's `tests/`; the sealed keys and the
  rubric stay in `research/calibration/` and `evallab.calibrate.RUBRICS`.
- The `research/calibration/` tree stays free of these copies (its README
  forbids vendoring evidence next to the keys); the judge input path assembles
  rubric, evidence and document at run time.

## Refreshing

Only from the same pinned source, and only by rewriting the manifest with the new
revision and digests together with the files. A changed pack changes every
evidence digest, so records measured before the change remain bound to the old
bytes and are not comparable to new ones.
