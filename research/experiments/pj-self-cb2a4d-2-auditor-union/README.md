# pJ-SELF-cb2a4d-2: does pooling existing audits raise confirmed-key yield?

Preregistered in the bridge `opus-vs-swarm-qa.md` before any pooled finding was judged. `pool_judge.py build` pools the
gate-eligible findings of three existing DEV audits on the 44 keyed tasks (Sonnet 5.5 B from OQ-6 v4, terra A and
terra B from OQ-8), anonymised and shuffled, and writes one judge prompt per task against the ID-OQ-1 enriched key text.
`pool_judge.py metrics` scores sol and terra judge outputs: distinct keys per audit and per union, key-backed precision,
keys per audited task and per 1M tokens, parity halves, two-judge agreement. No new audit sends. Unkeyed findings are not
certified false alarms. Keys come from DEV answer keys only; cross-test untouched.
