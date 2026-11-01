# ov-tb4-benchguard-opus-20261101 — Interactive Claude Opus 5.5 as BenchGuard Family 1

Item `W4-BENCHGUARD-OPUS-INTERACTIVE`. Extends `ov-tb4-benchguard-3fam-20261031` (#1711) on the 14 TB4 DEV
tasks (45 keys, `oq6_cohort_manifest.json`, `dataset_id == tb4-audit-v1`). DEV only; keys are consumed only by
OQ-6 `scorer.py` from `scorer-only-keys/`.

## What ran

- `interactive_opus55_k8`: 14 audits on `google-antigravity/claude-opus-5-5` (`--thinking high`, `--tools
  read,grep,glob`, `--no-skills`, cwd = task package, max 3 concurrent). Prompt template, system prompt, citation
  verifier, attestation and parser are imported unchanged from the W3 script; only the model changes.
  14/14 sessions attested (0 fallbacks); 261 tool calls, 0 resolved outside the package, into `solution/`, or into
  `answer-keys` (stricter checker: `;`-joined paths, `!` excludes, absolute/`..` glob patterns). 55 findings
  parsed, 52 citation-verified.
- Reused verbatim: `oq6_sonnet_B`, `sonnet_k8`, `deepseek_k8`, `interactive_gemini38_k8`.
- All five arms pooled per task (239 candidates), anonymized by the scorer projection, shuffled with
  `random.Random(case_id)`, and judged twice with the unchanged OQ-6 rubric: `google-antigravity/gemini-3.8-flash`
  (primary, continuity with #1709/#1711) and `google-antigravity/claude-sonnet-5-5` (cross-check). 14/14 and 16/16
  judge calls attested; Sonnet needed a second attempt on D0009 and D0017 (schema-invalid first reply).

## Results (recall k/45, 95% Wilson)

| Arm | Gemini judge NOFLOOR | Gemini CONF0.5 | Sonnet judge NOFLOOR | Sonnet CONF0.5 |
| :-- | :-: | :-: | :-: | :-: |
| `oq6_sonnet_B` | 6 (6.3–26.2%) | 4 | 9 | 5 |
| `sonnet_k8` | 9 | 3 | 11 | 3 |
| `deepseek_k8` | 9 | 9 | 11 | 11 |
| `interactive_gemini38_k8` | 7 | 7 | 10 | 10 |
| `interactive_opus55_k8` | 10 (12.5–36.3%) | 5 | 11 | 6 |
| `union_3fam_top8` (W3 arm, re-judged) | 12 | 11 | 16 | 14 |
| **`union_interactive_3fam_top8` (gated)** | **13 (17.7–43.4%)** | 11 | **15 (21.4–47.9%)** | 15 |
| `union_interactive_3fam_all` | 15 | 12 | 16 | 15 |
| `union_4fam_top8` | 13 | 11 | 15 | 15 |
| `union_4fam_all` | 15 | 12 | 17 | 15 |

Gate (prereg: Gemini judge, NOFLOOR, `union_interactive_3fam_top8` >= 15/45): **FAIL, 13/45**. McNemar vs
`oq6_sonnet_B`: b=0, c=7, p=0.0156; vs `union_3fam_top8`: b=0, c=1, p=1.0. The Sonnet cross-check judge gives 15/45
for the gated arm. Full tables, precision, not_in_view recovery and per-task cells are in `results.json`.

## Reproduce

```bash
EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run --no-sync python \
  research/experiments/ov-tb4-benchguard-opus-20261101/run_opus_interactive.py            # audits + judges
EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run --no-sync python \
  research/experiments/ov-tb4-benchguard-opus-20261101/run_opus_interactive.py --action eval
```

Receipts (sessions, prompts, judge records) live outside the repo under
`research-context/bridge/2026-10-31-overnight/ag-opus/raw/W4-BENCHGUARD-OPUS-INTERACTIVE/receipts/`.
