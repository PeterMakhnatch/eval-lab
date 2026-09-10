---
source_type: internal
---

# M4 Eval Runner result — 2026-09-04

- Status: DONE, independently reviewed, merged.
- Output: `research/inbox/held-out-freeze-design-20260904.md`; SHA-256 `02fb6137202c228f1e3ba0862b8af473a918b11aa90893cced4d1b8dd4da2ca2`.
- Source head: `48b686f9525790c73183e4b68d46e297aad82208`.
- PR: #371, merged as `a696777d223693a5ee5e26337b8c9b470f11e835`.
- Integration spine: `95f0569e`.
- Verification: 5/5 M4 module tests passed at merge; prior adjacent suite 82 passed; Ruff and diff checks clean.

Frozen-eval status:
- Local-control identities are frozen and non-submittable/non-claimable.
- Suite `sha256:4eb30a17d9bef97514d434b2b7fea3da422ed0f989d4e216e3edbf85cb648f55`; task set `sha256:c053e981289c32301e6ed434d14d57227c77057b30a7373ac76253365f33e18b`; pair set `sha256:e2a7b89b4c57881fe7380adcb9037832450ef9555665adcd789cad34f053b948`; projection `sha256:c2682bc869ef80e4975e5c6a3e92339b7b494b93aad411eece264d44384cc8b6`.
- Scientific held-out identities are not frozen; F2 freeze counts remain typed-unavailable.

Blockers:
1. F3 `ownership_domain` must land on existing `TrainingSplit`.
2. F4 stopping rule, preregistered exclusions, and hardware class must land on `SftSignalFreezeV1`; owed reasons belong in `SftSignalRefusalCode`.
3. M2/Program Lead must provide cluster-disjoint scientific identities.
4. Evaluation execution additionally requires a valid candidate trainer result and separate Peter run approval.

Next executable action: land and locally verify F3/F4 plus the three permanent projection-digest assertions; then construct the scientific pre-outcome freeze from M2 identities. No billable run is authorized.
