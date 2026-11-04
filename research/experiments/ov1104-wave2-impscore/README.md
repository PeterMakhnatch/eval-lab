# Impossible-v1 detector evaluation: admission blocked

## Question and answer

Do frozen detectors catch natural cheating on the impossible-variant cohort? **Not measured.** The cohort's receipt metadata does not satisfy the published fail-closed admission contract. No trajectory views or detector scores were created. No recall, false-positive rate, AUROC, paired comparison, or caught/missed example is available.

## Evidence

The impossible lane reports 151 runs admitted by `aggregate_clean.py`, including 101 impossible-variant runs and 50 originals. Its 53 G1 passes are constructed labels, not formally certified semantic cheats. It reports 34 original G2 passes but does not establish how many had no test edits. These are the upstream aggregate's claims, not independently accepted detector labels.

A census of all 192 batch `containment.json` files finds:

| Check | Result |
|---|---:|
| Receipt names the actual canary SHA-256 | 192/192 |
| Run transport matches named canary transport | 0/192 |
| Registry contains exactly `bash` | 0/192 |
| Registry lists only `bash` as active | 192/192 |
| Containment bytes equal matching receipt bytes | 192/192 |

The 151 receipts at transport `f2e8c41fd72e01fc0e0b75b0ef190cde3aa2e92c0321152f995fc5cf52a7d22a` match the source version used by the upstream aggregate. The other 41 use `161600f823951e6bb2175e50de9f9ebb0a33bcf82a418ad7603c9749ecffc677`. The canary uses `77fb128e92878311a9a263db616f34d376d3294aa6a7bb8f0967eced024ed700`. The upstream aggregate compares each run to current source, not to the canary transport. The published `admission.py` requires the latter equality and an exact bash-only registry. The registered extra tools are inactive according to receipts. Their presence is a contract mismatch, not proof that the model could use them.

The strong admission implementation defaults to another run root, so we do not claim it processed these 192 runs. Its documented prospective contract nevertheless requires these bindings. The metadata census is not an execution of the complete admission gate. It does not prove that any run escaped its container. A new canary cannot, by itself, prove which transport governed past runs.

Sources: impossible lane PR #1883, `research/experiments/ov1104-impossible/admission.py:117-123,200-210` and `aggregate_clean.py:59-87`; private receipts under `/private/tmp/ov1104/impossible/work-v2/runs/` and `/private/tmp/ov1104/impossible/safety-canary/attempt10-container-v2/receipt.json`. The upstream lane also withheld DetBench rows because its variants lack certified semantic labels.

## Reproduce the metadata census

From the repository root, with the original private receipts present:

```sh
uv run python research/experiments/ov1104-wave2-impscore/receipt_census.py \
  /private/tmp/ov1104/impossible/work-v2/runs \
  /private/tmp/ov1104/impossible/safety-canary/attempt10-container-v2/receipt.json
```

The script reads containment and receipt JSON only. It does not read task content, trajectories, or quarantined runs. `PREREG.md` records the intended frozen experiment and thresholds; it is not evidence that scoring occurred.

## Next step

Recover contemporaneous, transport-matched containment evidence or run a prospectively frozen canary and fresh cohort. Independently certify semantic labels and original-run test integrity before publishing detector views. Preserve the distinct original-pass and impossible-failure negative strata. Run the preregistered analysis only after admission succeeds.
