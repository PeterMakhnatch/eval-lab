# HAR-185: built/installed project copies

## Decision and measured result

**No repair was applied. No further spend is authorized.** This investigation establishes a real second-copy class in two known-positive task images, but does **not** estimate its true population prevalence.

The preselected HAR-177 sample is 100 distinct usable-ledger tasks, seed `177100`, from 1,146 usable rows. Its SHA-256 is `5edd504232b5999deeb9a7f264844b5a149268c0bae16b3a10dc8a2f35363060`. The same IDs, runs, ledger digests, and image digests were reused; the two deliberately chosen positive controls are separate from the random sample and do not increase its numerator or denominator.

| Final random-sample result | Tasks |
|---|---:|
| Confirmed newer/different copy | 0 |
| Verified-clean comparison | 0 |
| Unverifiable comparison | 100 |
| Total | 100 |

**Confirmed detection rate: 0/100 = 0%; Wilson 95% interval: 0–3.6995%.** This is a lower-bound *detection* rate under a bounded scanner. The interval is not an upper bound on true leak prevalence. Unknown comparisons are not verified clean negatives.

The inventory flags 75 rows with candidate own-project artifacts, 18 with no candidates observed, and 7 unscanned/unresolved rows (3 remote timeouts and 4 unresolved project identities). These are not an independently validated 75% installed-source-copy rate: metadata-only distributions, editable finder hooks, dependency adapters, and name/path heuristics can generate candidate flags. All 100 rows lacked a matching base-versus-copy fix-region hash pair. Public version comparisons were 25 equal, 1 older, and 74 unavailable; equal release labels cannot rule out differing source. Fifty-three scans hit a bounded-index/archive limit. Even the 18 no-candidate rows are not certified complete clean images.

`built_copy.csv` contains one row per sampled task. `comparison_status=unverifiable` is explicit on every row. `needs_repair=false` means no confirmed trigger, **not** clearance for admission or a health-tag change.

### Torchaudio false positive

Task `format-code-task-002446` has installed metadata `0.8.0a0+674a71d` and base packaging version `0.8.0a0`. Its base commit is `674a71d1a239fd6bf48d5797053590f593efff08`: the local suffix is exactly the base revision. PEP 440 sorts a local label above the unqualified version, but that ordering is not evidence of newer code. The final comparison uses public releases, records `same`, and removes the provisional positive. The append-only spend log preserves the original provisional count and its correction.

## Independently confirmed copies

The controls establish **2 distinct tasks, 4 physical copy roots, and 6 differing file pairs**. These are not four independent task examples. Every independently confirmed copy root is listed; multiple changed files within one root are grouped rather than inflated into extra examples.

| Task | Copy root/kind | Confirmed differing image paths |
|---|---|---|
| `format-code-task-001269` (`responses`) | `/testbed/build/lib/responses` — build/lib | `/testbed/build/lib/responses/__init__.py` |
| `format-code-task-001269` (`responses`) | `/usr/local/lib/python3.10/site-packages/responses` — installed source | `/usr/local/lib/python3.10/site-packages/responses/__init__.py` |
| `format-code-task-002308` (`pre_commit`) | `/testbed/build/lib/pre_commit` — build/lib | `/testbed/build/lib/pre_commit/commands/hook_impl.py`; `/testbed/build/lib/pre_commit/commands/run.py` |
| `format-code-task-002308` (`pre_commit`) | `/usr/local/lib/python3.9/site-packages/pre_commit` — installed source | `/usr/local/lib/python3.9/site-packages/pre_commit/commands/hook_impl.py`; `/usr/local/lib/python3.9/site-packages/pre_commit/commands/run.py` |

`validation2.csv` preserves the full base/copy paths, SHA-256 pairs, digests, and all comparisons. For responses, base `responses/__init__.py` hashes to `bff21668c0c1da959a17632ad980f394cc281e0cb5d7bc4586c3aafc23e94b41`; both copies hash to `86fd7c50847de4956854d0a90f10e67ee2f4827324380c85aa0edfd2484370ff`. For pre_commit, base `commands/hook_impl.py` hashes to `3ce2ef04355b4489f04ca0de582443bd8fa864128805721d75690a01187d5f15`; both copies hash to `931d9720fb67e152ab4c0275f0aaf0c5370c89e687eb98a58640f968daea23d5`.

The source/copy version labels are equal in both controls (responses `0.20.0`, pre_commit `2.14.1`). Version-only screening would miss these real differences. The responses image digest is `sha256:e4feae817a2d6660ebb8ea03f3487af0acaa2431fd2cf3763738395d67a76709`; pre_commit is `sha256:4d725f62dd434c67283850e6e9b3cfa0ddf6e08c78396e083a082e5cf88ec8d6`.

## Method and limits

`scan.py` stream-reads pinned OCI layers through `mirror.gcr.io`, with anonymous Docker Hub fallback. Overlay replacements and whiteouts are applied before comparison. It selects site/dist-packages, build/lib, egg/dist metadata, wheels/sdists, pip cache bodies, .tox/.nox, editable hooks, and vendored candidates. Archive parsing uses a bounded temporary spool, removed afterward. Persistent evidence contains paths, parsed packaging identifiers/versions, and hashes only—no image source or diff contents.

Fix-region candidates are inferred from task test-patch paths/imports and paired by package-relative suffix, not basename alone. Test-only patch layouts, namespace/src layouts, dynamic packaging metadata, editable installs, and ambiguous ownership prevent many comparisons. Presence flags remain candidates where ownership or independent source bytes are not established. The final scanner also rejects dynamic Python Name assignments and marks truncated equal comparisons unverifiable; the already collected inventory is not presented as a validated ownership census.

This measures **raw image-baked artifacts**, not post-healthcheck exposure or whether an artifact is a correct answer. In these task packages setup uses `git clean -fdx`; build/lib is not excluded and may disappear before an agent runs. Installed site-packages can survive. A runtime/post-setup scan is necessary before concluding an exposed route needs a repair. Removing future Git history alone does not remove an independent installed source copy.

## Proposed repair and validation cost — approval required

Propose a narrowly scoped `purge-own-project-built-copies@1` repair kind, **not implemented or enabled**:

1. Bind an explicit path allowlist to the image digest, project identity, and confirmed base/copy hash evidence. Verify which copies survive normal setup. Do not treat third-party vendor dependencies, adapters, or editable metadata alone as answer copies.
2. Remove only confirmed stale/different own-project build/install/cache artifacts and matching own-distribution metadata. Do not blanket-delete site-packages, pip caches, .venv, or vendor trees.
3. If execution requires an installed/compiled form, rebuild/reinstall the project's own distribution offline from the base working tree with existing dependencies. Do not reuse the removed own-project wheel or permit a network fallback. A failed rebuild is a failed validation, not permission to keep the copy.
4. In fresh processes verify import origins, re-scan after setup/reinstallation, and establish that each surviving own-project fix-region copy is base-equivalent. Reject incomplete or ambiguous post-repair comparisons.
5. Gate on NOP reward 0 and trusted-oracle reward 1 for both task images, while preserving legitimate dependency/import behavior. A different hash is not a substitute for an oracle control.

**Preferred validation cost: $0 external spend with local Docker**, two images × NOP/oracle = four sandbox controls, plus before/after metadata scans. No model call is needed. Each task config permits 1,200 seconds healthcheck and 2,100 seconds verifier time: four sequential controls have a configured setup+verifier ceiling of 3 h 40 min, excluding image provisioning (up to another 30 min per run) and re-scan time. This is a ceiling, not an observed estimate; controls have not been run or claimed to pass. Oracle inputs must come from the trusted control path, not from a copied installed answer.

If only the four metadata scans move to Modal, the scanner's fixed 0.25 CPU/512 MiB, 660-second input limit, 60-second startup allowance, and $0.06 reserve give a resource-cost ceiling of **$0.0726288**, using the documented function CPU/memory prices. This is a future proposal requiring a fresh explicit per-call approval; it does not cover cloud sandbox controls and is not permission to spend.

## Spend and exercised checks

One authorized Modal app (`ap-YHSzQYIWyDZHX5hdHVVoxm`) scanned the 100 rows after a five-image pilot. Posted app-specific charges: CPU `$0.08798764`, memory `$0.02955938`, egress `$0.01969402`, **total $0.13724104**, under the explicitly authorized $0.40 slice cap. Local attempts/control scans cost $0; the superseded three-row artifact was removed. No Daytona, model, queue, or repair operation was performed. `spend.log` includes cap, pilot, termination, billing reconciliation, and the false-positive disposition.

Executed: `uv run ruff check research/experiments/har185-built-copy/scan.py tests/test_har185_built_copy.py` (passed) and `uv run pytest tests/test_har185_built_copy.py -q` (**46 passed**). Synthetic coverage includes overlay/whiteout handling, exact suffix pairing, cached-wheel contents, dependency ownership, local-version ordering, unverifiable equal-version metadata, and truncated comparisons. Both known-positive images were separately revalidated by hash comparisons. No full suite or repair validation was run.
