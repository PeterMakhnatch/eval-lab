# DT-10 — explanation swap

How much of the verified-evidence reader's evidence is the agent's own narration, and
what happens to an alert when only the narration changes.

- Driver: `dt10_swap.py` (subcommands `part1`, `plan`, `rewrite --part N`, `read --part N`,
  `tripwire`, `score`). It imports `dt8_reader` from
  `research/experiments/ds-dt8-verified-reader-fullscale-20261029/` for the unchanged
  reader prompt (`INSTRUCTIONS`), the cohort loaders, the call wrapper's attestation
  reader and the verbatim-quote rule.
- `prereg.md` and the receipts live outside the repo, in the squad bridge under
  `raw/DT-10/`.
- Run text, rewritten text, model sessions and prompts stay in `/tmp/dt10`; nothing here
  contains run text, rewritten text or quotes.

Part 1 is mechanical: every verified citation of DT-8's readings is located in the
rendered step and classified as action (`<call …>`), observation (`<observation>`) or
narration (an agent step's `<reasoning>` / `<message>`). The lookup uses a per-character
class map over the whitespace-normalized step text, and the reconstruction is asserted
byte-identical to `render_step` output on every cited step.

Part 2 rewrites only agent narration in reader-alerted runs and Part 3 plants guilty
narration in non-alerted runs. Both rebuild the run by editing the step dicts and
re-rendering with the same `render_step` and redaction pattern, then assert mechanically
that every `<call …>` and `<observation>` block is byte-identical to the original.

Result summaries (`result.json`, `citation-kinds.json`, `swap-results.jsonl`,
`attestation.json`) are copies of the receipts; the receipt files are authoritative.
