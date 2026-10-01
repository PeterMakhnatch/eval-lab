# Model readers for the HAR-119 hand labels (Traces/model-readers)

Same question set asked twice — once as a Scout `llm_scanner`, once as a
Docent reading with a JSON output schema — over blind agent runs, scored
out of sample against the frozen HAR-119 hand labels. The questions
distill `../har119/RATER_GUIDE.md`: `stop_reason`, `first_failure` (step
ref with quote), `blame`, `loop_kind` with `loop_span`, and `pass_copied`.

## 1. Docent models: what `client.read(model=...)` can use

`client.get_reading_models()` (docent SDK 0.1.87, 2026-10-01) returns
**1,219 rows, every one with `uses_byok=false`**. The SDK's model option
type documents `uses_byok` as "whether this model would use the user's
own API key" (`_llm_util/providers/preference_types.py`); the MCP
server renders `uses_byok=true` models as "BYOK required". Zero of our
1,219 rows needs BYOK: no provider key is required for any of them.

Cost/quota evidence:

- The SDK exposes **no price, free-flag, quota, or billing signal**
  anywhere (same finding as the HAR-119 NOTES). The only money-adjacent
  signals are `InsufficientCreditsException` (raised when a *provider*
  key runs dry — irrelevant with no provider keys) and a null
  `free_usage_cap_cents_override` on our collaborator record.
- Empirically, hosted readings are free within quota: the HAR-119 blind
  reading ran 578,175 input / 15,361 output tokens on
  `openai/gpt-5.6-luna` (`uses_byok=false`) with zero quota or billing
  messages and $0 charged; HAR-81 (44 runs) and HAR-109 ran the same
  way. Account state: auto-approve ON, no provider keys.
- Transluce's own model guidance (`readings-reference.md`, "Model
  selection"): simple transcript questions → `openai/gpt-5.6-luna`;
  **complex interpretation, reasoning, or judgement →
  `openai/gpt-5.6-sol`**. First-failure/blame/loop judgement is the
  second kind, which is why this card uses flagship-tier models.

The 5 strongest usable models (all `uses_byok=false`, all ≥500k
context — a 12-run HAR-119 reading needs ~50k input tokens per run):

| # | model string | why it is flagship-tier | context |
|---|---|---|---|
| 1 | `anthropic/claude-opus-5-5` | Opus = Anthropic flagship; newest Opus generation on the list (4-5 → 4-8 → 5 → 5-5) | 1,000,000 |
| 2 | `openai/gpt-6.1-sol` | newest GPT major generation (5.x → 6.x → 6.1); `-sol` = the reasoning/judgement tier per Transluce's guidance | 1,050,000 |
| 3 | `google/gemini-3.1-pro-preview` | Gemini Pro flagship, newest Pro preview (3.1 > 3.x > 2.5) | 1,048,576 |
| 4 | `openai/gpt-5.6-sol` | previous-gen strong tier; sibling `gpt-5.6-luna` already ran 578k tokens free (HAR-81/HAR-119) | 1,050,000 |
| 5 | `openrouter/x-ai/grok-4.7` | newest Grok generation on the list (4.3 → 4.7); frontier lab, largest OpenRouter context here | 500,000 |

This card's Docent reading uses **`anthropic/claude-opus-5-5`**
(frozen in `prompts.py`): flagship reasoning tier, 1M context, and a
different family from the old `gpt-5.6-luna` readings, so the
comparison tests a stronger cross-family judge rather than a same-family
upgrade. If it errors, the fallback is `openai/gpt-6.1-sol`.

No strong model needs paid credits: nothing was run that isn't covered
by the free hosted quota, and Docent spend for this card is $0 by
construction (cap: $0).

## 2. Scout LLM scanners: which providers work with our keys

`llm_scanner` takes `model: str | Model | None` (an inspect_ai model).
With no Anthropic/OpenAI/OpenRouter key in the store, the only provider
that resolves is **Z.ai via its OpenAI-compatible endpoint**:

- Model string: `openai-api/zai-openapi/glm-5.3` — the `openai-api`
  provider splits `service/model`, so service `zai-openapi` reads
  `ZAI_OPENAPI_API_KEY` (in the keys store) and `ZAI_OPENAPI_BASE_URL`
  (set on the command line to `https://api.z.ai/api/paas/v4`).
  Verified live 2026-10-01 (tiny "OK" generation; usage reported).
- The `/models` endpoint on both the Standard (`ZAI_OPENAPI_API_KEY`)
  and Coding-Plan (`ZAI_API_KEY`) bases lists the same 11 models;
  **`glm-5.3` is the strongest** (5.3 > 5.2 > 5.1 > 5 > 4.7 > 4.5, and
  full > flash/flashx). Do not use Flash for readers.
- Prices (https://docs.z.ai/guides/overview/pricing, also pinned in
  `containers/zai_openapi_secret_proxy.py`): glm-5.3 **$1.40/1M in,
  $4.40/1M out**; flash $0.15/$0.50 (not used for readers).
- Two environment gotchas, both handled in the commands below:
  `harbor==0.21.0` pins `openai<3` via litellm while
  inspect-scout 0.5.3's `openai-api` provider needs `openai>=3.1`, so
  LLM scans run **without harbor** (harbor is only needed for import
  and the deterministic rules); and the scan model comes from the
  `--model` / `--model-base-url` flags, not code.

Anthropic, once Peter adds `ANTHROPIC_API_KEY`: the exact one-line
change is the `--model` flag —

```
--model anthropic/claude-opus-5-5
```

(no `--model-base-url`; the key resolves from `ANTHROPIC_API_KEY`).
Nothing else in the scanner changes.

## 3. The readers

Single source of truth: `prompts.py` (`SCOUT_QUESTION`, `DOCENT_PROMPT`,
`DOCENT_SCHEMA`, both model strings). Frozen by `freeze.py` into
`PROMPTS.sha256` + `FROZEN_AT` **before** either reader touched the 12
HAR-119 runs; iteration happened only on HAR-81/HAR-104 runs.

- **Scout** (`scout_llm/trial_reader.py`): one structured `llm_scanner`
  (`trial_reader`, one typed answer per transcript) with `[Mn]` cites
  and ≤25-word exact quotes per claim. The model never sees step ids;
  `scout_llm/build_predictions.py` maps `[Mn]` → `head#N` through the
  staged trajectory walk (same order as `scout/scanners.py`
  `_ref_index_map`, cross-checked against it) and grades every quote
  (`exact` / `window_match` / `quote_elsewhere` / `quote_missing`, in
  `raw_*`). `pass_copied` is gated on reward ≥ 1 downstream
  (null for fails, like the raters).
- **Docent** (`docent_strong/reading.py`): one template reading with the
  JSON output schema over the same blind metadata subset as HAR-81/HAR-119
  (trial/task/reward/exception/episodes). Evidence fields carry block
  citations; the first citation block of each evidence field maps to a
  trajectory step through the upload's own block map (same method as
  `har119/tools/docent_block_map.py`; every cited block must resolve).

Predictions: `predictions/scout_llm.jsonl`, `predictions/docent_strong.jsonl`
(same shape as `har119/predictions/*.jsonl`) plus per-trial raw files.
Scores: `score_model_readers.py` (har119 match logic copied, har119/
never edited) → `scores/scores.json`.

## 4. How to run

Work dir is gitignored `derived/trace-lab/model-readers/` in this
worktree. Import first (project env, harbor needed here):

```
uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 \
  python research/explorations/trace-lab/scout/import_evallab.py \
  <trial dirs...> --db derived/trace-lab/model-readers/scout-har119/data \
  --staging derived/trace-lab/model-readers/staging-har119
```

Scan (no harbor — see §2):

```
keys run -- env ZAI_OPENAPI_BASE_URL=https://api.z.ai/api/paas/v4 \
uv run --no-project --python 3.12 --with inspect-scout==0.5.3 --with openai \
scout scan research/explorations/trace-lab/model-readers/scout_llm/trial_reader.py \
-T derived/trace-lab/model-readers/scout-har119/data \
--scans derived/trace-lab/model-readers/scout-har119/scans --display plain \
--model openai-api/zai-openapi/glm-5.3 \
--model-base-url https://api.z.ai/api/paas/v4
```

Note: `--worklist <ids.json>` (bare id array) did not filter — the scan's
`_scan.json` listed all 12 transcripts — so the final run was a single
12-transcript scan, not two batches of 6.

Predictions:

```
uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 \
  --with pyarrow --with pandas \
python research/explorations/trace-lab/model-readers/scout_llm/build_predictions.py \
--scan derived/trace-lab/model-readers/scout-har119/scans/scan_id=<id> \
--staging derived/trace-lab/model-readers/staging-har119 \
--trials <trial>:<trial_dir> ... --model openai-api/zai-openapi/glm-5.3 \
--out research/explorations/trace-lab/model-readers/predictions/scout_llm.jsonl \
--raw-dir research/explorations/trace-lab/model-readers/predictions/scout_llm_raw
```

Docent (collection stays private; collaborators re-verified before and
after — owner-only):

```
keys run -- uv run --no-project --python 3.12 --with docent==0.1.87 \
python research/explorations/trace-lab/model-readers/docent_strong/reading.py \
--collection db32cc8f-e610-4373-a8dd-89b697a87a60 \
--plan-name model-readers-strong-trial-reading \
--model anthropic/claude-opus-5-5 \
--block-map research/explorations/trace-lab/har119/predictions/docent_block_map.json \
--out research/explorations/trace-lab/model-readers/predictions/docent_strong.jsonl \
--raw-dir research/explorations/trace-lab/model-readers/predictions/docent_strong_raw
```

Score:

```
uv run python research/explorations/trace-lab/model-readers/score_model_readers.py
```

## 5. Cost

Ledger: `SPEND.md` (every model call, tokens × published price).

- Docent: **$0.00** — 900,566 in / 26,745 out on `anthropic/claude-opus-5-5`
  plus 134,659 / 3,772 on two draft HAR-104 validations, all
  `uses_byok=false` hosted free quota, zero billing signals. Cap ($0) holds.
- ZAI: **$2.31 — $0.31 over the $2.00 cap.** Final scan: 489,045 in
  ($0.68) + 236,838 out ($1.04) = $1.73; validation v1+v2 $0.58; smokes
  $0.001. Two causes: the ledger first summed only each scan's first row
  (missing half the validation calls), and GLM-5.3's thinking is heavy —
  228k of the final's 237k output tokens are reasoning at $4.40/1M. No
  further ZAI calls: the final scan returned 12/12 with 0 errors, so no
  retries were needed. With cached-input credit the total is $2.24 —
  still over. Reported honestly; everything after the overrun finding is $0.

## 6. Scores

Out of sample on the 12 HAR-110 v2 runs (`scores/scores.json`;
`_vs_agreed` = only the run×field cells where both raters agree):

| comparison | stop_reason | first_failure | blame | loop_present | loop_kind | loop_onset | pass_copied |
|---|---|---|---|---|---|---|---|
| rater_a_vs_rater_b | 12/12 | 9/12 | 11/12 | 11/12 | 11/12 | 6/6 | 2/2 |
| evallab_vs_agreed | 12/12 | 4/9 | — | 7/11 | — | 2/4 | 2/2 |
| scout_vs_agreed | 12/12 | 2/9 | 11/11 | 7/11 | — | 1/6 | — |
| docent_vs_agreed | — | 3/9 | 11/11 | — | — | — | — |
| loop_rule_vs_agreed | — | — | — | 7/11 | 7/11 | 3/5 | — |
| scout_llm_vs_agreed | 3/12 | 7/9 | 11/11 | 7/11 | 7/11 | 6/6 | 2/2 |
| docent_strong_vs_agreed | 10/12 | 6/9 | 11/11 | 9/11 | 9/11 | 6/6 | 2/2 |

Did the strong models beat the old readings?

- **first_failure: yes.** `scout_llm` 7/9 and `docent_strong` 6/9, against
  4/9 (Eval Lab), 2/9 (old Scout), 3/9 (old Docent). Misses: both readers
  put X2dMzMw at step 12 vs the agreed 8 (a completion-claim loop whose
  first bad turn is genuinely ambiguous); 002391 at 46/71 vs agreed 7.
- **loop_kind: yes — and it is new.** Neither shipped tool expresses
  claim-vs-repetition. `docent_strong` 9/11 beats the Part-1 loop rule
  (7/11); `scout_llm` 7/11 ties it.
- **blame: tie at 11/11** — uninformative, as before: both raters say
  `model` on every failure in this sample, so a constant answer scores
  the same. Nothing here tests harness/task/infra calls.
- **loop_onset 6/6 both** (old best 3/5); **loop_present 9/11 Docent**
  (old detectors all 7/11); **pass_copied 2/2 both** (n = 2, ties Eval Lab).
- **stop_reason: Scout regresses (3/12).** `scout_llm` systematically
  answers `request_ceiling` where the raters say `token_ceiling`: episodes
  + exception do not tell it *which* budget bound. The run facts need the
  binding ceiling (input-tokens vs requests), which the deterministic rules
  already compute — keep the rules for stop, use the model for the rest.
  `docent_strong` gets 10/12 (same confusion on 2 runs).
- **Loop over-fire persists.** Both strong readers flag `repetition` on
  the same loop-free runs the old detectors flag (000495-plain,
  000587-plain, and 001161-plain/seed): long error-recovery stretches read
  as loops to every judge so far, strong or not.
