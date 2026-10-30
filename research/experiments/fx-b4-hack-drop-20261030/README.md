# fx-b4 hack-drop filter

Frozen tripwire caller for R2 use #2: drop a submission before it can enter a
training export. The rules are `evallab.tripwires` at `RULES_VERSION = 3`
(`evaluate_content_rules` stop-eligible keys, `tamper_signature_hit`, and
`scan_trace` only when the bytes already contain agent steps). This directory
does not retune them.

`notes.json` is not scored. On the executed exploit events it carries
`exploit_found`.

`score_cohort.py` reads the production journal read-only, checks each exploit
bundle against `candidate_digest`, recovers each reference patch from the
content-addressed store by the oracle sha256, and writes a receipt. The pytest
file parametrizes the ids in `located_manifest.json` and skips, with a reason,
when that machine does not have the file.
