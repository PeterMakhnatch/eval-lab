# Initial local control results

Run date: 2026-08-13 America/New_York (trial timestamps are also recorded in
UTC). Producer checkpoint: `cf652523a14a42fe907441c0fd15b28e8ec58084`.

## Hypothesis

With the task, Docker provider, resource limits, artifact declarations, and
separate verifier held fixed, Oracle should create the required summary and earn
`reward=1`; no-op should leave the output absent and earn `reward=0`.

## Configuration and results

| Job | Job UUID | Trial UUID | Adapter | Primary reward | Exception | Runtime |
|---|---|---|---|---:|---|---:|
| `event-summary-oracle-evidence` | `886e92a2-0de4-4384-b7ad-aa8c623e96b1` | `a729171b-81ac-4c83-8195-e437eef2602a` | Oracle 1.0.0 | 1.0 | none | 8.881 s |
| `event-summary-nop-evidence` | `08032ad4-7418-447b-a656-fee3db3e44dc` | `46fd9b0b-8ebe-413a-8b7b-2759fe8d0a4e` | no-op 1.0.0 | 0.0 | none | 8.040 s |

Both trials used Harbor 0.21.0, Docker 29.4.1, one attempt, concurrency one,
the separate verifier, and task checksum
`2a7d47fa52ff2f00f8876da4f8b52a783e05d930a7480581a9ab9bf0c7b0537e`.
No model was configured; token use and cost are null.

## Evidence inspected

- Oracle's artifact manifest records the input and output as `ok`; its verifier
  passed schema, correctness, input preservation, and output hygiene.
- No-op's input artifact is `ok`, its output artifact is `failed` because the
  file is absent, and its verifier still completed normally. Input preservation
  earned `1`; the other dimensions and primary reward earned `0`.
- Both copied input artifacts have SHA-256
  `e1342580d8b19cae1c5d789466f39168d9cd53705c50831c9b2814825e6590c0`,
  matching each other and the trusted task fixture.
- PostgreSQL ingestion round-tripped the pair into two jobs, two trials, eight
  named rewards, six artifact-manifest entries, and 32 file inventory rows.

## What this establishes

The task is solvable by its reference solution, its unsolved starting state does
not pass, declared artifacts cross into the separate verifier, multi-dimensional
rewards persist, and the database can index the resulting Harbor bundles.

It does not establish capability for any evaluated model. The task's `public`
network baseline is a documented Docker Desktop/macOS limitation; use an
environment provider that enforces `no-network` when network isolation is part
of a model experiment.
