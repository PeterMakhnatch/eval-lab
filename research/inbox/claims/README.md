# Claims — the pickup counter (quiet by default)

To claim a board item, write `<pane>-<lane>-<n>.md` here. Three fields:

```
item: <lane> #<n> <short title>
role: <verb phrase for THIS turn: verify X / cover Y / challenge Z>
why-me: <pane/session + model; one-line reason>
```

Claim files name their own role. Before claiming, read the lane's prior
outputs (the board links them); pick the complement, not the duplicate:
verifier after prover, second cluster after first, skeptic after advocate.
If no complement exists, pass — "already covered by <who>" is first-class
data. Roles are hats per turn, never titles: the same pane may mine one
round and verify the next.

For durable work using a live handoff, add the optional field
`handoff: agents/handoffs/<id>.md`. The handoff format and closure/archive rules
live in `agents/WORKFLOW.md`. A claim without a handoff remains valid.
`python -m evallab.governance check` rejects missing fields, multiple open claims
for the same pane, and absent or unclaimed live handoffs.

Rules: one open claim file per pane (finish or delete before claiming again);
passes need no file; the keeper polls this dir on rhythm and replies with a
single grant page each. Delete your file when done (DONE goes to the keeper
once, with output path + sha). No claim pages ever.
