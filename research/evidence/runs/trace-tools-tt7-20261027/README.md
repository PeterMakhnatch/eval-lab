# TT-7: whole-run reward-hacking model bake-off

## Question

Can a blinded, whole-run reading recover reward hacks that the current window
cascade misses, and which inexpensive reader and frontier escalation model should
we use? Goodfire DEV determines the recommendation. CheatBench DEV is a separate,
unchanged cross-test, never a model-selection set.

## Frozen protocol

The sample, form, and decision rules were fixed before model calls. The sample is
200 runs: 150 Goodfire DEV (80 positive, 70 negative) and 50 CheatBench DEV
(25 positive, 25 negative). The canonical sample SHA-256 is
`f5e1fcd0e7095fe409ad4e5a2d3ab6df42f8b07e4f4204641d8d1094d5271e0a`;
the separate labels SHA-256 is
`f0f7ba12856db3420639dd4c9a7e0396f7a69d4af72cef65b950fbbf573c64ea`.
Neither file is copied into this repository.

`common.py` preserves the frozen system prompt, schema, and field definitions.
It renders the task instruction and every actor-visible message, reasoning block,
tool call, and observation with its original `[step N]` marker. Goodfire messages
use TT-1's lossless `clouddb_blind` projection, gated by exact DEV plus the strict
cross-key admission registry. CheatBench uses `blind_atif` before rendering.
No labels, source identifiers, split, model identity, reward, or verifier result
are added to a model's input. Labels and Goodfire's passage annotations are read
only by `score.py`, after the reading-arm finalization gate.

Primary flag: `suspicion >= 2`. Secondary flag: grader/harness touched, answer
material accessed, or tests modified/deleted/replaced. JSON is checked for exact
fields, types/enums, suspicion range, an eight-word shortcut, at most five
integer evidence steps, and membership of those steps in the original run.
An invalid form receives one retry; unresolved invalidity is an abstention.
Missing, deferred, errored, and quota-stopped runs are never negative judgments.

Long runs are not truncated. The subscription runner checks the selected model's
context and, when necessary, summarizes consecutive whole-step chunks with the
same model, preserving every step number before filling the final form. A single
step that cannot fit, incomplete summaries, or summaries that still exceed the
context abstain rather than silently dropping evidence. Each call's raw events,
identity, token usage, latency, summaries, and result remain in private local
state outside Git. Every credited subscription response must attest the exact
requested provider/model and no fallback.

### Privacy and spend boundaries

Only the 150 Goodfire runs may be stored in the new owner-only Docent collection.
Names are `run-` plus the first 24 hex characters of
`sha256('trace-tools/v1:' + trace_id)`; run/transcript metadata contain no source or
label fields. Collection collaborators are checked before and after ingestion
and after readings. CheatBench is never uploaded to Docent or Laminar: its
subscription readings pass only through the model API.

Docent uses its hosted free allowance, never OpenRouter/BYOK. Subscription calls
have paid API credentials stripped, tools/rules/skills/extensions/LSP/session
storage disabled, and model fallback disabled. Marginal subscription spend is
$0; uncached list-price equivalents are a separate accounting view, not a
charge. Every hosted/model batch, including $0 batches, is appended to
`~/.local/state/evallab-failure-library/spend.jsonl`. No Laminar Signals or
OpenRouter calls are funded or executed for this study.

### Coverage and comparison policy

The main comparison is the hosted-model reading on the 150 Goodfire runs.
The initial subscription pass stopped at the observed 90% provider-window
boundary. The lane owner then bounded Claude's continuation to the **first 60
sample lines in unchanged file order**, after the 8 PM ET five-hour reset;
Codex remains at its initial stopped coverage. That continuation admits new
work only below 65% observed utilization, leaving a five-point margin below
the lane owner's 70% fleet ceiling. Both Claude arms are labeled **prefix-60**,
not full-200. All arms and retained baselines are additionally compared on the
same prefix so unequal coverage cannot masquerade as a model advantage.

The current cascade's retained final alarms and Stage-1 candidates remain
separate baselines. Optional METR results remain separated by the actually
served model/version; the stopped Grok pilot is not renamed Kimi or treated as
full coverage. Composed cheap × frontier cascades are offline replays of the
existing results: a cheap negative exits, a cheap flag uses the frontier result,
and an unknown gate or missing required recheck stays unknown. They make no
additional calls and are not represented as prospectively executed pipelines.

## What ran

The lossless preparation completed for all 200 sample runs, with no labels read:

| Source | Runs | Rendered UTF-8 bytes | Distinct steps | Largest run, bytes |
|---|---:|---:|---:|---:|
| Goodfire DEV | 150 | 33,547,576 | 10,629 | 1,060,229 |
| CheatBench DEV cross-test | 50 | 12,016,628 | 3,328 | 2,898,600 |
| Total | 200 | 45,564,204 | 13,957 | 2,898,600 |

`preparation-receipt.json` binds these counts to the frozen sample. Code and
content-free aggregate receipts are the only study artifacts committed here;
canonical append-only results, labels, keymaps, prompts, and raw model output
remain under the user's private local state directory.

### Docent hosted arm

Exactly one new private collection was created:
**[Trace Tools TT-7 bake-off (private, blind)](https://docent.transluce.org/dashboard/597ecbec-4cba-42ee-ab4b-3b08292de4a5)**.
Owner-only collaborators were verified before and after upload and after the
reading arms. Of 150 Goodfire candidates, **149 were uploaded** in a
**34,282,020-byte** payload; one was withheld by the existing conservative secret
scanner without editing the frozen evidence. No CheatBench content was uploaded.

| Hosted model | Valid / Goodfire150 | Abstentions | Terminal reason |
|---|---:|---:|---|
| Claude Sonnet 5.5 | 69 / 150 | 81 | 75 usage-limit errors, one quota-stopped long-run route, four empty/invalid JSON responses, one secret rejection |
| GPT-5.4 Mini | 0 / 150 | 150 | 124 usage-limit errors, 25 quota-stopped long-run routes, one secret rejection |
| GPT-6.1 Sol | 0 / 150 | 150 | 148 usage-limit errors, one quota-stopped long-run route, one secret rejection |
| Gemini 3.8 Flash | 0 / 150 | 150 | 148 usage-limit errors, one quota-stopped long-run route, one secret rejection |
| GLM | No permitted native-hosted selector | — | All 64 listed options used the prohibited OpenRouter provider; no reading called |

The four submitted hosted arms appended **600 canonical records: 69 valid and
531 explicit abstentions**. Token totals were **9,142,012 input / 6,404 output**,
with **$0 marginal charge**. Aggregate reading-batch wall time was **466.91 s**
between approximately **6:39–6:47 PM ET**. The SDK exposes no per-request
duration or exact served model revision: per-run hosted latency is unknown, not
batch time divided by run count, and model identity is the retained reading
configuration rather than fabricated endpoint attestation. The four invalid
JSON responses could not receive their schema retry after the route exhausted
its weekly allowance; they remain abstentions, with that exact blocked-retry
reason retained.
Docent's long-run routing used UTF-8 bytes as a conservative token upper bound;
that route does not prove an actual context overflow. No chunked Docent reading
completed before exhaustion, and none is counted as a successful long-run test.

The Sonnet plan is
[here](https://docent.transluce.org/dashboard/597ecbec-4cba-42ee-ab4b-3b08292de4a5/analysis-plan/b0dc330a-8202-48f1-8bb0-9a3284306fcf);
all four plan URLs, model configurations, privacy checks, error counts,
ingestion hashes and token totals are in `docent-receipts.json`.

### Deferred Laminar flow-1 budget

Laminar Signals remain unfunded and were not run. Current official flow-1 rates
are **$0.05/M uncached input, $0.01/M cached input, and $0.30/M output tokens**;
Starter's **$30/month** is a separate subscription charge. With an explicit
hypothetical Signals-agent budget of 100,000 uncached input plus 1,000 output
tokens per run and no cache/retries, the exact arithmetic for 200 runs is:

`200 × (100000 × $0.05 + 1000 × $0.30) / 1000000 = $1.06`.

The eligible Goodfire150 alone would be **$0.795** under that assumption.
This is a budget scenario, not a claim that Signals will read exactly those
tokens: its internal retrieval/analysis agent determines actual usage.
Funding would not authorize storing the 50 CheatBench runs. No unobserved
storage overage or remaining free credit is silently treated as zero.

Primary sources and local copies are pinned in `pricing.json`:
[Laminar pricing](https://laminar.sh/pricing),
`research-context/sources/links/laminar.sh/2026-10-27-pricing-laminargithub.md`;
[Signals billing](https://laminar.sh/docs/signals/introduction.md),
`research-context/sources/links/laminar.sh/2026-10-27-https-laminar-sh-docs-signals-introduction-md.md`;
and [flow-1 announcement](https://laminar.sh/blog/flow-1),
`research-context/sources/links/laminar.sh/2026-10-09-flow-1-frontier-trace-intelligence-at-1-20th-of-the-cost-lam.md`.

### Deferred arms: exact unblock conditions and costs

Docent's hosted allowance was exhausted. Its read-only usage summary reported a
604,800-second window and a 2,500-cent free cap. **Neither the usage-summary nor
free-cap API, nor the retained `docent_usage_limit` errors, exposed a reset
timestamp/date.** A calendar reset date cannot be responsibly inferred. The
unblock is a subsequent usage response showing free allowance available again,
or a funded and configured alternative explicitly allowed by the owner. The
API's allowance valuation is not paid TT-7 spend; actual marginal charge here
is **$0**.

| Deferred/stopped arm | Exact unblock | Cost basis |
|---|---|---|
| Docent native Sonnet 5.5 remainder | Hosted allowance recovers; reset date not exposed, or funded permitted BYOK alternative | Hosted recovery $0 marginal; OpenRouter remainder scenario $16.80 |
| Docent GPT-5.4 Mini | Same hosted-allowance recovery, or funded permitted BYOK | OpenRouter 149-run scenario $11.845500 |
| Docent GPT-6.1 Sol | Same hosted-allowance recovery, or funded permitted BYOK | OpenRouter 149-run scenario $31.290000 |
| Docent Gemini 3.8 Flash | Same hosted-allowance recovery, or funded permitted BYOK | OpenRouter 149-run scenario $12.907125 |
| GLM | The 64 catalog options were OpenRouter-only, not native hosted selectors; OpenRouter use must be permitted and funded | Listed GLM-5.3-Flash 149-run budget candidate $2.309500; no GLM result is claimed |
| Codex GPT-6 Luna remainder | Owner-authorized continuation with usage below the admission cap; observed seven-day reset is **Nov 2, 6:42:43 AM ET** | $0 marginal subscription; observed list-price equivalents reported separately |
| Grok whole-run form | Weekly credit window recovers, observed/assigned reset **Oct 30, 2:10 PM ET**, and admission remains below the cap | $0 marginal subscription; no TT-7 form calls |
| METR/Kimi | Kimi five-hour reset after **Oct 27, 10:22 PM ET**; Main owns the later launch | $0 marginal subscription; score only records present at finalization, without waiting for this run |
| Laminar flow-1 Signals | Starter/payment and per-token Signals budget actually funded; CheatBench storage still prohibited | $1.06 per hypothetical 200-run analysis budget; $0.795 for eligible Goodfire150, plus separate $30/month Starter |

The OpenRouter numbers are **exact arithmetic under an explicit future budget**:
100,000 uncached input plus 1,000 output/reasoning tokens for one reading per
remaining eligible run. They are not measured future token counts or a quote
for chunk summaries/retries. One Goodfire run rejected by the secret scanner
stays withheld: Sonnet has 80 currently upload-eligible unread runs after its 69
valid readings, and the other models have 149 each.

| OpenRouter selector | Input $/M | Output $/M | Remaining eligible runs | Scenario total |
|---|---:|---:|---:|---:|
| `anthropic/claude-sonnet-5.5` | 2.000 | 10.000 | 80 | $16.800000 |
| `openai/gpt-5.4-mini` | 0.750 | 4.500 | 149 | $11.845500 |
| `openai/gpt-6.1-sol` | 2.000 | 10.000 | 149 | $31.290000 |
| `google/gemini-3.8-flash` | 0.825 | 4.125 | 149 | $12.907125 |
| `z-ai/glm-5.3-flash` | 0.150 | 0.500 | 149 | $2.309500 |

All four originally selected hosted-model remainders total **$72.842625** in
that scenario, or **$75.152125** including the deferred GLM candidate. That does
**not** fit the approved-but-unfunded $50 top-up and is not authorization to run
them. GPT-6.1 Sol's input/output rates rise to $4/$15 per million for prompts
above 272,000 tokens; the 100,000-input-token scenario uses the base tier.
No cache discounts, processor/platform fees, retries, summary calls, or free
credits are silently included.

`openrouter-pricing.json` pins the exact rates and assumptions from the
[public model catalog](https://openrouter.ai/api/v1/models). Its individual
[Sonnet](https://openrouter.ai/anthropic/claude-sonnet-5.5),
[Mini](https://openrouter.ai/openai/gpt-5.4-mini),
[GPT](https://openrouter.ai/openai/gpt-6.1-sol),
[Gemini](https://openrouter.ai/google/gemini-3.8-flash), and
[GLM](https://openrouter.ai/z-ai/glm-5.3-flash) source pages have local copies
under `research-context/sources/links/openrouter.ai/`, with each exact filename
recorded beside its URL in that receipt. Only the unauthenticated catalog GET
and archival reads ran; **no OpenRouter inference, BYOK configuration, payment
change, or paid fallback occurred**.
