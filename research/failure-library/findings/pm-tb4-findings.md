# pm-tb4-findings

Rebuild: `uv run evallab failures ingest pm-tb4-findings`.

- Source: Preference Model's [TB4 issue catalogue](https://preferencemodel.com/blog/terminal-bench-4-is-saturated/issues/), captured 2026-10-20. The published page SHA-256 is `41faa957b0d72dbb8ec47e079d79023a323ee89f22bc6f9ab687fcaa3c417f2c`.
- Retained key: `research/answer-keys/tb4-audit-v1/items.jsonl`, SHA-256 `e0f1daf325fdca0ee95e6187eef6c7f1753e9c596a95670c140045a3c4b3d8ab`; `source.json` SHA-256 `1fe7727a82ce9e67c4bae144a9f5695314ab781b0d36ed5a2c093861d3f9db85`; `witnesses.json` SHA-256 `494f0ecdd32d510ddb3c491b648a5a21afec8e4da8eea4cf75d00bcfd819f036`.
- Task pin: `harbor-framework/terminal-bench@452bf305c6daa62fc59061d22133a7cbc7c1572e`. Each available key package digest is retained. A digest binds the supplied task bytes, not an independent reproduction of every PM claim.
- Count: **241 findings**: 238 task-scoped findings on 60 tasks and 3 benchmark-wide context records. The context carrier `__benchmark__/terminal-bench-4` deliberately has no task path or package digest.
- Tiers: **10 V3_executed**, **231 V1_external_label**. Every source finding gets `human_label` attributed to **Preference Model**. Only the ten confirmed DEV witness entries with upstream reward 1, fixed-port full-mode reward 0, receipt digests and hash-verified witness files gain `executed` evidence. PM's own 137 execution claims are not treated as our confirmations.
- Splits are copied from each original key item: **89 DEV / 152 TEST**, including three DEV benchmark-context records. No new split rule is applied. Only DEV is potentially eligible for knowledge; the cross-key guard remains an additional admission condition.
- Locator: the original `pm-tb4:<section>:<hash>` item ID. Reviewed TS01–TS11 labels are copied; `wrong_pass` is asserted only for the source's reward-hack-only type, otherwise direction is `other`.
- Fix provenance: each row retains Karotte port revision `a3ec9e22725b490a30b35d127bc9e7ac8a28de82` and PM's port status as a note. That separate repository is **not** a proved repair of Harbor upstream, so it neither upgrades to V2 nor changes `status_now` to `fixed_upstream`.
- License: no explicit PM content license was observed; only short titles and factual structured metadata are retained, matching the existing answer key. Raw PM prose and snapshots are not copied into Git.
