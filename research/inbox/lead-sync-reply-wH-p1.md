---
source_type: internal
---

# Lead sync reply — wH:p1

1. **Current focus:** Track D/H contract and publication hardening is complete: Track H source `3b281c79` merged as `d1721ecb`, integration `6ede71a0`; no active mutation.
2. **Top blocker:** Track C supersession `f63172b3` is p7-blocked because it deletes Track H’s authority/quarantine/pair/digest contract; Tutor (`wK:p4`) owns the coordinated C/H repair, and merged Track H must remain untouched until its replacement passes exact-head review.
3. **Unowned generalist task:** Harden `src/evallab/immutable_directory.py::atomic_no_replace_rename` and its direct `src/evallab/queue.py` caller to use the component-wise nofollow retained-parent-fd boundary already used by `staged_immutable_directory`; add before-rename parent/root swap and destination-winner regressions while preserving queue `staged_evidence_tampered` and no-replace behavior. Clean cutover, no compatibility path.
