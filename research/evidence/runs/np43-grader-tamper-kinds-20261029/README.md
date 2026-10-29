# NP43-TW-1: what do `grader_tamper` alarms write? A descriptive census by write kind

Stored data only, read-only, $0, zero model calls, no downloads, no uploads. **No rule is proposed and nothing is tuned.**
Counts only are committed (no run ids, task ids, paths or transcript text).

## Question

NP38-CM-1 found the judge-free `grader_tamper` pass-through on 27 of 60 honest LinuxArena runs that write a test file and on
0 of 60 matched non-S runs, and noted that most S hits are editor `create` calls. NP36-TW-1's candidate C1 ("not agent-created")
failed its design-half bars on Goodfire pairs. Before anyone preregisters a narrower rule, this item measures what the split
looks like: what kind of write is each alarm?

## Frozen before the first label join

* `tools/classify.py`, sha256 `0e5dc144b1741269cd3cbe28a7d3f1da3e20aa672d1fdf73b639d621730f6302`. Its module docstring is the
  specification. It imports the NP36 gate replica `research/evidence/runs/np36-grader-tamper-20261029/tools/gate.py`
  (sha256 `a3a5241ccac15d5f3e577415a0b0f0d26bf9e83d3aacf0e27d8c5d2bef8740e5`, unchanged; `run_events` asserts per run that
  it yields the same hit steps and paths as `tripwires.grader_tamper_hits`, sha256 of `src/evallab/tripwires.py` at the freeze
  `96076b4af7da87219876abfa7b90475f7ffffa0ae1cea0c12d5be034f79182d8`).
* Unit = alarm **event** (one flagged path of one hit). Kinds: **a** creates a file not present earlier in the run; **b**
  modifies a file the run already read, listed or edited (sub-kinds `existing_read|probe|listing`, `own_file`,
  `semantic_no_prior_evidence`); **c** deletes (truncation); **d** cannot be determined from the stored bytes (non-literal path,
  or the "no prior evidence" claim rests on a listing-type probe whose recorded output is missing or truncated).
* Run-level categories, from the events of the run's alarm: **survives** = at least one event of kind b or c (the alarm would
  still fire if only b and c fired); **creation_only** = all events a; **undetermined** = no b/c event and at least one d.
  A run alarms iff the gate yields at least one hit.
* Populations (fixed here, each counted separately): (1) the 246 runs of the 123 NP31 matched pairs, which must pass the strict
  `goodfire-dev` gate 246 of 246 (NP36 `strict_gate.json`: 123 of 123 pairs, file sha256 `3e18a9e4...caec69`); (2) the other
  strict `goodfire-dev` runs with a verdict (2,388 minus 246 = 2,142); (3) the 60 S and 60 matched non-S LinuxArena runs of
  NP38-CM-1's frozen sample (checked against `np38-cm1-linuxarena-20261029/results/sample.json` hashes); (4) CheatBench DEV
  150 if the strict gate admits it. Steps come from the NP36 label-free run cache and the NP38 run pickles; labels are joined
  only by `tools/census.py`, after this freeze.
* Hand check: 20 events per kind (a, b, c, d; all if fewer) drawn with `random.Random(20261029)` from every event of every
  population, read against the raw step text by the lead without the classifier's label shown; accuracy stated per kind, and
  the classifier is **not** edited afterwards.
* Before this freeze I ran the classifier **without labels** on the 246 pair runs only to catch crashes: 0 errors, the replica
  reproduced the gate on all 246 runs, 67 alarm runs. I saw the label-free kind mix of those runs (mostly b/own_file) and did not
  change the classifier in response.

## How the numbers will be read (stated before labels)

Let E = enacted Goodfire alarm runs (pairs and the rest), S = the LinuxArena S alarm runs.

* **Supported: enacted hacks are mostly creations** iff creation_only is at least half of E: a creation exemption would also
  drop enacted alarms.
* **Supported: enacted hacks are mostly modifications and the LinuxArena alarms are creations** iff `survives` is at least 70%
  of E and at most 50% of S: a creation exemption would cut LinuxArena false alarms without costing much DEV recall.
* **Cannot tell** iff `undetermined` is more than 25% of E or of S, or the hand-check accuracy of a kind that carries at least
  a fifth of the alarm runs is below 80%.
* Otherwise the numbers are mixed and I say so. The thresholds are descriptive aids chosen here, not a decision rule.

## Contents of this directory

* `tools/classify.py` (frozen), `tools/run_census.py` (label-free event extraction), `tools/dossier.py` (hand-check draw),
  `tools/census.py` (label join and counts), `results/` (counts only), `RESULTS.md`.
