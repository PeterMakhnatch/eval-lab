# Eval cards

Eval cards are provenance-bearing summaries drafted from completed experiment specs. Generate one
with `evallab report card <completed-spec.json>`. The command finds the corresponding completed
Harbor job, refuses incomplete evidence or overwrites, and fills [TEMPLATE.md](TEMPLATE.md) with the
config and lock digests, task-clustered numbers and interval, elicitation tuple, contamination note,
and observed threats.

Generated cards are drafts. A human must resolve the contamination section and review every claim
before a card is treated as a research conclusion.

