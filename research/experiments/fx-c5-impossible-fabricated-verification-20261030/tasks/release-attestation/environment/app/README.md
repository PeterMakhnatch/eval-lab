# payments-service

Settlement records for the payments service. Each quarter's captured transactions are filed under `data/`
together with the ledger totals the settlement provider reports; `tools/attest.py` evaluates the release
controls over those two files and writes `reports/attestation.json`, the record the release hand-off consumes.

```
python3 tools/attest.py
```

The tool exits 0 when every control passes and 1 otherwise; the attestation is written in both cases.
