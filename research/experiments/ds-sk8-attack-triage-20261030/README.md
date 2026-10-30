# ds-sk8-attack-triage-20261030 — static triage of proposed grader attacks

Question: given the **text** of a proposed grader attack (never executed), does TQ-2's cumulative
hardening recipe stop it, and at which layer? Validated against TQ-2's 12-row matrix, then applied
to DA-3b's 210 parseable seeker proposals.

**No script is executed, no grader is run, no model is called.** The rule is a deterministic
lexical classifier (`classifier.py`); `run_triage.py` reads the published receipts and writes the
two result files.

| file | holds |
|---|---|
| `classifier.py` | the frozen rule (R1 → L1, R2 → SURVIVES, R3 → L3, R4 → out of family) |
| `run_triage.py` | validation on TQ-2's matrix, application to DA-3b, DT-5b cross-tab |
| `receipts/rule-table.json` | rule signatures, per-row validation, agreement and misses |
| `receipts/seeker-predictions.json` | ids, DA-3b classes, predicted outcome, cross-tab (no payload text) |
| `attestation.json` | zero model calls, nothing executed, inputs read |

Re-run (paths are the squad's read-only receipts; the classifier itself needs nothing else):

```bash
python run_triage.py \
  --tq2 ~/Developer/eval-lab/.worktrees/ds-tq-2-20261029/research/experiments/ds-tq2-hook-hardening-20261030 \
  --raw  ~/Developer/research-context/bridge/2026-10-29-ds-squad/raw/SK-8 \
  --out  <any writable dir>
```

Layer semantics come from TQ-2's `hardened-test-sh-template.sh`: L1 restores the auto-load/config
paths **inside the work tree** from the base commit; L2 isolates the interpreter; L3 requires a
JUnit report with at least the expected tests and zero failures/errors/skips; L4 grades a clean
tree. Anything outside the work tree (an interpreter site directory) is out of L1's reach.

Result: 11/12 agreement on TQ-2's rows (8/8 on the attacks that work at L0), and on the 210
proposals 48 predicted stopped at L1, 8 at L3, 0 report forgeries, 154 out of family. The single
miss is TQ-2's row C, whose payload is identical to C_pp: whether it works depends on the grading
environment's `PYTHONPATH`, which the text does not carry.
