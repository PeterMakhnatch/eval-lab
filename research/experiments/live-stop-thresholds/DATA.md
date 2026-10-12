# Data: three public RL reward-hacking rollout corpora

None of this data is committed. `fetch.py` downloads the pinned files below to
`--data-root` (about 450 MB) and refuses any file whose SHA-256 differs. All
three pinned URLs download without a token (checked 2026-10-12).

| Corpus | Source and pinned revision | File | SHA-256 | Bytes | License |
|---|---|---|---|---|---|
| ARIA | [`gutenbergpbc/aria-reward-hacking`](https://huggingface.co/datasets/gutenbergpbc/aria-reward-hacking) @ `a564b502e190158a14bbec2c7a43bd498daf320f` | `data/aria_reward_hacking_v0_6.parquet` | `59ffb1aa73d341f6bd070f639028da8ee041176c8c8e0e18678a4d6751a11556` | 95,360,838 | `other` on the card (no named terms; not gated) |
| AISI | [`ai-safety-institute/reward-hacking-olmo3.1-32b-kl0.0-seed2-rollouts`](https://huggingface.co/datasets/ai-safety-institute/reward-hacking-olmo3.1-32b-kl0.0-seed2-rollouts) @ `adf70b44d4243bcc37e3a90cef88977103db3000` | `data/train-00000-of-00001.parquet` | `00bd6ba463f8d53ef5ce8bcecf32803e09f201b8778443ae5e8f63324f45fade` | 188,978,039 | Apache-2.0 |
| TA | [`LichenLillc/CoTMonitoring`](https://github.com/LichenLillc/CoTMonitoring) @ `bea6b67e9707b1f2c2ce2fa9304f5834855d6a20` (Git LFS) | `datasets/trace_and_amplify_reward_hacking_data_v1.zip` | `e662cd5eb31c5adfc95dc3841d12f567b3edc7db00bd185ff69f4322598a180f` | 166,710,430 | MIT (repository) |

The ARIA and TA digests match the ones their publishers list. AISI's
`eval_logs/*.eval` files (about 400 files of roughly 6 MB each) were not
downloaded: the parquet already holds every rollout's messages and labels.

## What each corpus is

All three are **single-turn**. The policy answers a coding prompt once, and the
environment's grader runs the submitted code. No corpus contains tool calls, so
"lag within a rollout" does not exist: the one agent step is also the step that
gets graded. The lag this study measures runs across **training steps**.

| Corpus | Policy and training | Rollouts | Step axis | Hack mechanism |
|---|---|---|---|---|
| ARIA | Qwen3-4B GRPO, `no_intervention` run | 51,200 | `step` 1–200, 256 rollouts per step | The prompt says the solution "will be evaluated by calling a function called `run_tests()`"; the hack is answering with your own `run_tests` |
| AISI | OLMo-3.1-32B GRPO, KL β=0, seed 2 | 25,664 | `rollout_index` 0–400, 64 rollouts per batch, in training order | The system prompt names three hacks and forbids them: an always-equal `__eq__` object, a `conftest.py` patching `TestReport.from_item_and_call`, and `os._exit(0)` |
| TA | Qwen2.5-Coder, mostly 7B: prompt-elicited (2,343), RL checkpoint 61 (unittest hacks, 2,348) and 68 (exit hacks, 2,768), mixed (1,391), 112 base-model inference samples (7B, 14B), and 3 from a 1.5B model | 8,965 unique (13,169 rows) | none: `global_steps` is 0 outside the inference split | Contradictory unit tests that no honest answer passes; a clean process exit counts as a pass |

## Labels and what "clean" means

| Corpus | Successful hack (`hack`) | Attempt label | Clean set used here |
|---|---|---|---|
| ARIA | `is_reward_hack_strict` (11,536) | `is_reward_hack_loose`, harmful test modification (20,855) | `loose == 0` (30,345). `eq_correct` also splits out correct clean answers (10,560) |
| AISI | `reward_hacked` (18,653): only hacks that made the tests pass | none | `reward_hacked == 0` (7,011). This set still holds failed attempts; `audit.py` samples it |
| TA | `hacking_label` (3,674): passed the contradictory suite | none | `hacking_type == normal` (5,291). It also holds failed attempts; `audit.py` samples it |

AISI's `rh_exit` / `rh_always_equal` / `rh_conftest` and TA's `hacking_type`
supply the per-family labels. AISI's `actually_solved` marks only 10 rollouts,
too few to serve as a clean reference set.

## Normalization (`corpora.py`)

Each row becomes one `Rollout`. The fields are the system and user prompt, the
assistant text, the files a grader would materialize, the labels, and the step.
Files come from `<file path=...>` tags (either quote style) and fenced code
blocks. A block whose first line is `# filename: X` is saved as `X`; otherwise
blocks are named `solution.py`, `solution_2.py`, and so on. Every block is kept.

Graders parse fences differently, and models quote stray fences in their
reasoning, so blocks come from three patterns: line-anchored fences, unanchored
fences, and openings tagged `python`/`py` only. Each distinct block is kept
once. With a single unanchored pattern, 7 AISI successful hacks lost their real
code block to a stray fence or a single-quoted file tag.

Most answers yield exactly one file: ARIA 46,849 of 51,200, AISI 24,217 of
25,664, TA 6,470 of 8,965. Some yield none because they have no fenced block:
ARIA 1,615, AISI 424, TA 165. No content rule can fire on those.

TA's train, test, and mixed files reuse rows, so TA is deduplicated on the full
`(prompt, response)` pair. ARIA and AISI need no deduplication.
