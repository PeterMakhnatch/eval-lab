# Fixmine: retained upstream-diff candidates

## Evidence boundary

This is a candidate queue, **not a count of verified broken tasks**. A changed test, answer or reference solution does not independently establish parent breakage or a successful repair. Two models agreeing supplies labels, never verification. No task oracle, exploit or re-break campaign was run in this slice.

All raw candidates stay in staging. The publisher writes candidate manifests only; it never creates canonical Findings, assigns a verification tier, marks tasks fixed, or emits upstream_fix/executed evidence from model agreement. Free-form rationale and raw diffs remain private; committed metadata contains counts, digests, neutral labels and references, not reusable recipes.

Collection is the CLI default. New model work requires explicit `--classify --limit N` with positive N; offline `--rebuild` cannot fetch Git or call a model. There is no automatic paid fanout or perpetual funding approval.

## Retained data

**9 repositories; 2,875 raw candidate task-version changes; 32 paired classifications; 0 canonical defect findings.** Candidate split counts: `{"DEV": 1677, "cross_test": 668, "heldout": 530}`. These are untiered candidate records; only DEV may be considered for defensive knowledge with uncertainty intact.

| Repository | Pinned history head | Candidate changes | Paired labels | Yes/yes labels | Candidate splits |
|---|---|---:|---:|---:|---|
| [ShishirPatil/gorilla](fixmine-shishirpatil-gorilla.md) | `6ea57973c7a6097fd7c5915698c54c17c5b1b6c8` | 473 | 0 | 0 | `{"DEV": 358, "heldout": 115}` |
| [harbor-framework/terminal-bench-2](fixmine-harbor-framework-terminal-bench-2.md) | `2fd12b88aafdd04a52c298e3940bcb189f9766d6` | 300 | 0 | 0 | `{"DEV": 25, "cross_test": 270, "heldout": 5}` |
| [harbor-framework/terminal-bench-science](fixmine-harbor-framework-terminal-bench-science.md) | `48f90831c07105d62fad0cb2aa1ee44a8125443e` | 176 | 0 | 0 | `{"DEV": 102, "heldout": 74}` |
| [laude-institute/terminal-bench](fixmine-laude-institute-terminal-bench.md) | `d28711d0da2675d0bb1d56de45ae5df6082438a3` | 666 | 32 | 0 | `{"DEV": 266, "cross_test": 312, "heldout": 88}` |
| [openai/mle-bench](fixmine-openai-mle-bench.md) | `507f92e1138bb6e40dac5c6ee7a6758e6424bf97` | 0 | 0 | 0 | `{}` |
| [sierra-research/tau-bench](fixmine-sierra-research-tau-bench.md) | `59a200c6d575d595120f1cb70fea53cef0632f6b` | 51 | 0 | 0 | `{"DEV": 34, "heldout": 17}` |
| [sierra-research/tau2-bench](fixmine-sierra-research-tau2-bench.md) | `ebf990be93b594a08ed23cb8b7a9773cb9a03678` | 180 | 0 | 0 | `{"DEV": 131, "cross_test": 11, "heldout": 38}` |
| [web-arena-x/webarena](fixmine-web-arena-x-webarena.md) | `dce04686a56253aefba7b18a4fa0937cf1dc987b` | 871 | 0 | 0 | `{"DEV": 632, "cross_test": 75, "heldout": 164}` |
| [xlang-ai/OSWorld](fixmine-xlang-ai-osworld.md) | `044bc33901cb893808e3f91ebc9a17ed8e54b44c` | 158 | 0 | 0 | `{"DEV": 129, "heldout": 29}` |

The source-exposed exploratory canonical projection was archived privately and removed, not credited as evidence. Other unlabelled candidates remain unlabelled rather than being fabricated as findings.

## Observations and unavailable audit

The retained neutral batch was the first 32 Terminal-Bench candidates by candidate id. Actual model votes: `{"deepseek/deepseek-flash": {"no": 30, "yes": 2}, "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free": {"no": 32}}`. Consensus disagreement flags: **2**. This bounded route/provenance observation is not coverage of the other 2,843 candidates.

**The requested random 30-positive third-family audit is unavailable, not passed.** The retained yes/yes population is zero; no semantic accuracy or positive-audit agreement rate is claimed. Further calls were stopped by the integration owner. Raw observations, rejected outputs, provider errors and served-model/session hashes remain in private state.

## Provenance and safe use

Each source manifest pins its Git history head and content-addressed candidate/label bytes; each private candidate pins parent and proposed revision, native task locator, changed files, diff digest, message and license. Source notes include deterministic split and cache-rebuild rules. Git source bytes and model transcripts are not committed. A proposed defect needs independent, task-version-specific evidence and benign controls before canonical promotion. A source label or reproducer path alone does not provide this.

## HF history feasibility

- `princeton-nlp/SWE-bench_Verified`: history returned 7 commits and 3 files, including one Parquet/LFS object, pinned to `c104f840cc67f8b6eec6f759ebc8b2693d585d4a`. Snapshot decoding and stable instance-id alignment are required; metadata alone establishes no broken task.
- `cais/hle`: inaccessible (401 anonymous, 403 with the stored token), not empty history. No gated dataset content was downloaded.
- Immutable metadata is under `raw/hf/<dataset-slug>/<revision>/`; reports are under `fixmine/hf/`.

## Cost and limitations

Settled **catalog-token estimate: $0.105646800**. An interrupted DeepSeek stream reported `aborted` without usable billing usage; its **$0.80 reservation remains**, giving **$0.905646800 conservative exposure**. Provider invoices are not observed. A GLM request rejected before generation was reconciled to estimated $0 and its $0.40 hold released. All reservation/settlement events remain in the append-only lane ledger.

No calls remain running or queued. Paid OpenRouter/Z.ai resources, stale free aliases and upstream free-model rate limits are preserved as errors, not successful observations. The live provider-catalog snapshot is content-addressed under `raw/model-catalog-openrouter/9f4bba732947b093559e139fa910e669f87d18afbf91893202a747faa9b70f3e/`.

MLE-bench yielded no qualifying modified test/answer/solution files in this scan; its post-initial changes touched preparation paths and README. This does not establish a healthy benchmark. Legacy `laude-institute/terminal-bench` redirects to Terminal-Bench-1; historical pins are not claims about Terminal-Bench 4 HEAD.

Formatting, type checking and 19 offline tests passed before the final candidate-only publication and bounded-CLI changes. The integration owner owns final checks and documentation-index regeneration for the handed-back code.
