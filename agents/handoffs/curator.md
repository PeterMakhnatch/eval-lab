# CURATOR handoff

**Goal:** 15–25 verified open-source Harbor tasks with cards, rejections, 5 canaries.

**Changed:** worktree `~/Developer/helab-curator` (`role/curator`). Library of **17** includes under `library/curated/*/CARD.md`. `REJECTED.md` covers GPU/cloud/heavy/oracle-fail. Canaries in `README.md`. Local oracle k=3 + nop in `runs/`.

**Verified:** each include has provenance, Apache-2.0, digest, domain/runtime, verifier notes, oracle 3×1.0 and nop 0.0 with run paths.

**Next:** optional add react-lead-form / formal-crypto if their batch oracles finish green. Rebase onto main when BUILDER lands.

**Blockers:** none. Batch job may still be running extra candidates.
