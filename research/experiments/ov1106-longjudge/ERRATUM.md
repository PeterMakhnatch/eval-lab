# Post-score token accounting correction

The corrected study records **22,820,295 judge tokens**, not 22,832,008.
No score, threshold, sample, prompt, model route, raw receipt, AUROC, interval, or flag count changes.
All new model scoring ends before this correction.

## Defect and proof

The original report matches a call log to a final receipt by run, window, and prompt hash.
A failed call and its later accepted retry share that logical window.
The failed call consumes the accepted receipt's accounting slot.
The report then adds the accepted call again as an unmatched call.

The affected held-out window has these recorded attempts:

| Attempt | ET time | Recorded tokens | Final receipt |
|---|---|---:|---|
| Unserved failure | November 4, 5:14:47 PM ET | Unknown | No |
| Accepted Terra response | November 4, 5:15:22 PM ET | 11,713 | Yes |

The original held-out union reports 3,942,862 tokens.
The actual call log and final receipts each sum to 3,931,149 tokens.
The original union therefore counts 11,713 tokens twice.

`evaluate.py` now matches the recorded attempt timestamp, served route, and error state as well as the logical window.
The independent standard-library checker makes the same identity correction.
Two regression cases cover distinct and equal timestamps.
They also preserve a paid unmatched attempt, so the fix does not discard extra usage.

The actual-ledger smoke confirms the corrected total against both independent raw sums.
The focused suite passes all 12 cases.
The corrected independent recomputation passes 36,993 checks.
Three negative controls reject false nonzero usage, explicit null usage, and a missing populated counter.

## Frozen source and scientific results

Preregistration remains `f0080d90a2ec2de2c093b4426623dee4629bd5a5`, retained by tag `ov1106-longjudge-prereg`.
Its manifests and calibration remain unchanged.
All classifier and gateway files remain unchanged.
Only the reporting file `evaluate.py` changes after scoring among the 14 files listed in the freeze.

The corrected checkout intentionally differs from the original scoring freeze's reporting-file hash.
Use the preregistration tag to inspect the exact source admitted for historical scoring.
Use this corrected evaluator to reproduce the final aggregate from existing receipts.
Do not start new model calls: the quota stop remains in force.

The original output has SHA256 `1a10ad127da123ac91c7274fa4c96b4304a33996aee19d50c28c260e122b54d8`.
The corrected output has SHA256 `d0bf21db69e3462fbb3811030c04fcfa8d502c4a76b8f0d1c13148b35169c30d`.
A deep comparison confirms that every non-token cohort field is identical.

The independent checker also needs one format adaptation.
An empty accounted-token `Counter` omits its zero keys for unattempted HackBench.
The checker now expands absent keys to zero without accepting explicit nulls or false usage.
The first failure, both checker versions, and all controls remain preserved.

## Receipts

- Corrected aggregates: `metrics.json`.
- Integrity and control results: `verification.json`.
- Private original output and original source: `~/.local/state/eval-lab/ov1106-longjudge/reporting-erratum/`.
- Actual-ledger smoke: `reporting-erratum/actual-ledger-smoke.json` under that private study root.
- Final independent checker and output: `verification/final/verify.py` and `verification/final/clean.stdout.txt` under that private study root.

Recorded tokens remain a lower bound. Provider errors and internal retries can omit usage.
The correction does not assign zero actual usage to those unknown calls.
