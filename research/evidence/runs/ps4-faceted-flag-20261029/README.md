# pS-SELF-e2fa4f-4: what `faceted` means in qa-data

On the store copy (4,207,423,488 bytes, read-only) 1,235 admitted runs have a facet row but `runs`, `recount`
and `clusters` serve 1,234: the facet record of `retained-trace:b493cd8d...` (task
`terminal-bench/layout-config-recreation2`) is withheld because the sealed name `layout-config-recreation`
is a substring of its task id. `traces`, `task` and `trace-card` still reported it as faceted and printed its
facet fields. They now ask the same screened read that `runs` uses.

- `payload-diffs.json`: every difference between old and new `data` (meta removed) over 8 real CLI calls. 6 calls
  identical; the differences are the one run: `traces` faceted true -> false; `task layout-config-recreation2`
  runs.faceted 1 -> 0, suspicion_ge_2 1 -> 0 and the run's four facet fields -> null. `trace-card` of that run
  loses its facets block; the card of another run is identical.
- `ab.json`: interleaved CPU s, n=9, old vs new: traces --limit 200 0.468 -> 0.518, traces --task 0.472 -> 0.520,
  task 0.617 -> 0.674 (about +0.05 s, +10%: the price of the screened read).
- `base.json`: CPU/wall of 17 real `qa-data` calls on main before this change (n=3): all 0.18-0.72 s CPU;
  the Atlas CLI is within about 0.4 s of its start-up floor (`split` 0.18 s).
- `prof_retrieve.py`: warm in-process `retrieve_similar_examples`, 0.08-0.28 CPU s per call; of a 10-call profile
  54% is regex search (sealed-text screening 0.46 s, feature-flag cues 0.48 s). Not changed.
