# ov-deepswe-autoreview-20261031 — full n=100 paired DeepSWE DEV test

Research-only experiment for `W3-DEEPSWE-AUTOREVIEW`: a paired two-arm evaluation across
the exact 100 length-matched `deep_swe` DEV runs (50 `enacted_yes` / 50 `clean_no`),
comparing:

- **Arm 1 (Old DT-15 Truncated Masked View):** `run_atlas.render_step` (4,000-char call /
  2,500-char observation caps, source redaction) with agent narration (`reasoning_content`
  and `message`) replaced by `(tool calls only)`.
- **Arm 2 (AutoReview Monitor):** `evallab.autoreview_monitor` over `TraceAnatomy`
  (budgeted compact view, protected-first mutation ledger, reverted-edit ledger,
  action index, claim-vs-evidence backcheck, and targeted Stage-2 retrieval for runs in
  the uncertain band), evaluated under both fixed rubric and ControlArena honest-quantile
  cross-fit.

Both arms run on `google-antigravity/gemini-3.8-flash` (`--config worker.yml --no-skills`,
max 4 concurrent, foreground, 100% attested, `reasoning_content` 100% stripped).

## Boundaries

- DEV ONLY. Admitted Goodfire DEV traces (`goodfire-reward-hack-data`, `split='dev'`).
  The 95 held tasks (`split='held'`) and `research/answer-keys/` are NEVER accessed or scored.
- No local Docker, no local generative LLM inference.

## Repro

```bash
uv run --no-sync python research/experiments/ov-deepswe-autoreview-20261031/ov_deepswe_autoreview.py build
uv run --no-sync python research/experiments/ov-deepswe-autoreview-20261031/ov_deepswe_autoreview.py read-arm1
uv run --no-sync python research/experiments/ov-deepswe-autoreview-20261031/ov_deepswe_autoreview.py read-arm2
uv run --no-sync python research/experiments/ov-deepswe-autoreview-20261031/ov_deepswe_autoreview.py score
```
