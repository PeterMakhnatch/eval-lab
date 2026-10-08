# Upstream sources for the cyber task ledger

All ID lists below are vendored from `raimondasl/agentleak` at pinned commit
`949763fe0d6ca59ba387fcc904fe752856af1b02`, which reproduces the investigator's
local copies byte for byte (verified with `cmp` against
`/private/tmp/mimo-clean/MimoCyberMusicTerminal/` on 2026-10-08).

License: MIT, Copyright (c) 2026 Raimondas Lencevicius — see
`LICENSE.agentleak` (attribution as the license requires). Upstream pins
recorded in `summary.json`:

| Source | Pin |
|---|---|
| MiMo source rows | `XiaomiMiMo/MiMo-V2.6-RL-oss@639865fd3374018d6cb29b9fb82dd531406fcf5f` |
| CyberGym tasks | `sunblaze-ucb/cybergym@bde190ded494e52bc684b66073b436c9d992c7c6` |
| OSS-Fuzz renumber map | `n132/ARVO@bc2a373c6b32fb3d9e7f86c516b1844885dcec51` (`arvo/oss_fuzz_mappings.csv`) |
| ARVO metadata | `n132/ARVO-Meta@7e1a64f52520a5d63c766d5546a32fd748f23e21` |
| SEC-bench | `SEC-bench/SEC-bench@11422e774857272b8f5460c699dca7a64046308b` |

Harbor snapshot decided here (read-only, never edited):

| Snapshot | Revision |
|---|---|
| `FineEnvs/MiMo-V2.6-RL-harbor-cyber` | `763882ade5fc018892f1aa3c559f997138eb92cc` |

## Files (`out/` in agentleak, renamed verbatim except where noted)

| File | sha256 (first 16) | Role in `build.py` |
|---|---|---|
| `overlap.csv` | `c5c0374287872a08` | 278 overlapping tasks → `exclude_train` |
| `mimo_duplicates.csv` | `1f6c8678f494f2bf` | 164 old/new-ID pairs → keep one, discard other |
| `mimo_suspect_specs.csv` | `abac52a38417a137` | 26 bogus specs → `discard` |
| `related_bugs.csv` | `b17728d68493d616` | 19 related tasks (6 net of overlap) → keep with flag |
| `mimo_secbench.csv` | `1c37299f386182d7` | 24 SEC-bench tasks → `exclude_train` iff crash evidence corroborates, else keep with flag |
| `mimo_cyber_clean_ids.txt` | `60761cbfaeb6e0dd` | cross-check only: keep set must equal clean minus corroborated SEC-bench overlaps |
| `image_build_steps.csv` | `f9a58e3613869eb0` | `fix_binary` flags (assert none kept); per-task image digests |
| `summary.json` | `dd5fdb1309dddf94` | upstream pins and counts (above); not read by the build |

To re-vendor at a new agentleak commit: download the same `out/` paths at
that commit, update this file's hashes, and re-run `build.py` — any shape
change fails closed via the `EXPECTED_*` counts in `src/evallab/cyber_ledger.py`.
