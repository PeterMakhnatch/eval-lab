# pJ-SELF-cb2a4d-3: two-detector consensus filter

Preregistered in the bridge `opus-vs-swarm-qa.md` before any grouping was run. `consensus_filter.py build` turns the
pj-2 pooled findings (Sonnet 5.5 B, terra A, terra B on the 44 keyed DEV tasks) into key-free grouping prompts;
`metrics` keeps a finding only if its group has both a Sonnet and a terra finding (terra A and B count as one detector)
and compares kept versus not-kept key-backed precision using the recorded pj-2 key judgments. No model calls in the
script; unkeyed findings are not certified false alarms; DEV only; cross-test untouched.
