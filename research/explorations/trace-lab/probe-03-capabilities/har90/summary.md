# probe-03 capabilities summary

_Inputs: /Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0036, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0036-b, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0036-c, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0036-d, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0036-e, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0036-f, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0758-a, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0758-b, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0758-c, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0758-d. Invocation: `capabilities.py /Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0036 /Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0036-b /Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0036-c /Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0036-d /Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0036-e /Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0036-f /Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0758-a /Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0758-b /Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0758-c /Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0758-d --out-dir har90 --treatment-key-source har93:/Users/petermakhnatch/Developer/eval-lab/derived/parquet/external/task_catalog/trial_treatment.parquet --wedge-key validation/har99-wedge.hand-key.jsonl`._

## Tag x attribution (outcome-relevant failure)

| tag | attribution | trials |
| --- | --- | --- |
| completion | harness | 1 (`har90-mimo-0036-e`) |
| completion | model | 1 (`har90-mimo-0036-f`) |
| context | harness | 2 (`har90-mimo-0758-b`, `har90-mimo-0758-c`) |
| environment | unclear | 2 (`har90-mimo-0036-c`, `har90-mimo-0758-a`) |
| tool_use | harness | 3 (`har90-mimo-0036-b`, `har90-mimo-0036-d`, `har90-mimo-0758-d`) |
| tool_use | unclear | 1 (`har90-mimo-0036`) |

## Per-trial: first failure vs outcome-relevant failure

| trial | outcome | stop reason | first failure (recovered) | outcome-relevant failure | rule | evidence |
| --- | --- | --- | --- | --- | --- | --- |
| `har90-mimo-0036-b` | unscored | trial_budget_exhausted | head#2 tool_use/unclear (R-TOOL-00, recovered=true) | head#3 tool_use/harness | R-TOOL-01 | head#3,head#32,head#201 |
| | | | | | note: | error_recovery/model secondary: identical run head#32-head#201 |
| `har90-mimo-0036-c (no signal)` | unscored | model_auth_error | -- environment/unclear (R-ENV-01, recovered=false) | -- environment/unclear | R-ENV-01 | -- |
| `har90-mimo-0036-d` | 0.0 (scored) | agent_timeout | head#2 tool_use/harness (R-TOOL-01, recovered=false) | head#2 tool_use/harness | R-TOOL-01 | head#2,head#7,head#530 |
| | | | | | note: | error_recovery/model secondary: identical run head#7-head#530 |
| `har90-mimo-0036-e` | 1.0 (scored) | agent_timeout | head#35 completion/harness (R-COMP-01, recovered=false) | head#35 completion/harness | R-COMP-01 | head#35,head#84 |
| `har90-mimo-0036-f` | 0.0 (scored) | agent_timeout | head#27 tool_use/model (R-TOOL-02, recovered=true) | head#44 completion/model | R-COMP-02 | head#44,head#55,head#172 |
| | | | | | note: | confirmation_with_call loop (inferred): first confirmation head#45, span head#44-head#172; attribution unclear (confirm vs continue ambiguous) |
| | | | | | note: | source_text_assertion: failing verifier test asserts literal source strings (test_outputs.py::test_edge_all_categories_are_derived_from_expanded_item); asserted literal 'is_flagged(f_, \\"pipe\\")' is not in instruction.md; grader audit flag, not R-ENV-02 |
| `har90-mimo-0036` | unscored | trial_budget_exhausted | head#2 tool_use/unclear (R-TOOL-00, recovered=true) | head#17 tool_use/unclear | R-TOOL-00 | head#17,head#121 |
| `har90-mimo-0758-a (no signal)` | unscored | model_auth_error | -- environment/unclear (R-ENV-01, recovered=false) | -- environment/unclear | R-ENV-01 | -- |
| `har90-mimo-0758-b` | 0.0 (scored) | agent_timeout | trajectory.cont-11.json#2 tool_use/harness (R-TOOL-01, recovered=false) | trajectory.cont-11.json#8 context/harness | R-CTX-01 | trajectory.cont-11.json#8 |
| | | | | | note: | pre-livelock failure kept as first_failure: R-TOOL-01 at trajectory.cont-11.json#2 |
| | | | | | note: | old config (max_tokens 8192) summarization livelock: full summary and fallback chat fail, short summary succeeds but context immediately overflows again; loop of summarizations never terminates until timeout |
| `har90-mimo-0758-c` | 0.0 (scored) | agent_timeout | trajectory.cont-31.json#6 tool_use/harness (R-TOOL-01, recovered=false) | trajectory.cont-31.json#8 context/harness | R-CTX-01 | trajectory.cont-31.json#8 |
| | | | | | note: | pre-livelock failure kept as first_failure: R-TOOL-01 at trajectory.cont-31.json#6 |
| | | | | | note: | old config (max_tokens 8192) summarization livelock: full summary and fallback chat fail, short summary succeeds but context immediately overflows again; loop of summarizations never terminates until timeout |
| `har90-mimo-0758-d` | 0.0 (scored) | agent_timeout | trajectory.cont-1.json#5 tool_use/harness (R-TOOL-03, recovered=false) | trajectory.cont-1.json#5 tool_use/harness | R-TOOL-03 | trajectory.cont-1.json#5,trajectory.cont-1.json#6,trajectory.cont-1.json#181 |
| | | | | | note: | error_recovery/unclear secondary: 176 identical retries trajectory.cont-1.json#6-trajectory.cont-1.json#181 (feedback never named the cause) |
| | | | | | note: | normalizer allowlist (mimo_tool_calls.py:154, args <= {command|keystrokes, duration}) rejects the extra `description` param after summarization handoff while the same shape was accepted before; feedback is uninformative |

## No capability signal

Zero model turns (R-ENV-01); excluded from learnability attempts:

- `har90-mimo-0036-c`: AuthenticationError
- `har90-mimo-0758-a`: AuthenticationError

## Learnability: unknown (one attempt per key so far)

Every (task, treatment-key) cluster has a single attempt, so no within-key comparison is possible. No claim is ever made across keys:

| task | treatment key(s) | attempts | scored | passes | status | excluded |
| --- | --- | --- | --- | --- | --- | --- |
| `mimo-v2.6-rl/candidate-0036-software-data-engineering` | `sha256:643cf74131cab8a96…` | 1 | 0 | 0 | unknown | -- |
| `mimo-v2.6-rl/candidate-0036-software-data-engineering` | `sha256:df176e73d11df48e9…` | 1 | 1 | 0 | unknown | har90-mimo-0036-c__drbtqaU (no signal) |
| `mimo-v2.6-rl/candidate-0036-software-data-engineering` | `sha256:a2b8ed45431806892…` | 1 | 1 | 1 | unknown | -- |
| `mimo-v2.6-rl/candidate-0036-software-data-engineering` | `sha256:7929e9ee615edd67c…` | 1 | 1 | 0 | unknown | -- |
| `mimo-v2.6-rl/candidate-0036-software-data-engineering` | `sha256:478c43c9e2e11d114…` | 1 | 0 | 0 | unknown | -- |
| `mimo-v2.6-rl/candidate-0758-ml-inference` | `sha256:df176e73d11df48e9…` | 1 | 1 | 0 | unknown | har90-mimo-0758-a__TheXwyy (no signal) |
| `mimo-v2.6-rl/candidate-0758-ml-inference` | `sha256:a2b8ed45431806892…` | 1 | 1 | 0 | unknown | -- |
| `mimo-v2.6-rl/candidate-0758-ml-inference` | `sha256:7929e9ee615edd67c…` | 1 | 1 | 0 | unknown | -- |

## Loop cost (LOOP-COST)

Per-step prompt tokens over 8 trials: 57,016,404 total; 55,167,521 (96.8%) inside loops (identical runs >=10: 54,807,035; confirmation spans: 5,486,229; overlapping steps counted once). Proxy input (result.json, includes summarization calls): 57,242,789. 1 step(s) carry no metrics (summarization hand-off turns) and are uncounted, not 0. No metered steps at all: har90-mimo-0036-c, har90-mimo-0758-a.

| trial | reward | loop prompt tokens | share | spans |
| --- | --- | --- | --- | --- |
| `har90-mimo-0758-b` | 0.0 | 22,173,591 | 100% | identical trajectory.cont-11.json#8-trajectory.cont-11.json#760 (753) |
| `har90-mimo-0036-d` | 0.0 | 11,099,630 | 100% | identical head#7-head#530 (524) |
| `har90-mimo-0758-c` | 0.0 | 5,497,712 | 100% | identical trajectory.cont-31.json#8-trajectory.cont-31.json#183 (176) |
| `har90-mimo-0036-f` | 0.0 | 5,486,229 | 87% | identical head#55-head#172 (118); confirmation head#44-head#172 (129) |
| `har90-mimo-0758-d` | 0.0 | 4,468,250 | 95% | identical head#27-head#57 (31); identical trajectory.cont-1.json#6-trajectory.cont-1.json#181 (176) |
| `har90-mimo-0036-b` | None | 2,308,260 | 96% | identical head#32-head#201 (170) |
| `har90-mimo-0036` | None | 2,267,974 | 95% | identical head#17-head#121 (105) |
| `har90-mimo-0036-e` | 1.0 | 1,865,875 | 79% | identical head#35-head#84 (50) |

## Completion handshake (HANDSHAKE)

1 of 10 trials reached the harness prompt 'Are you sure you want to mark the task as complete? ... include "task_complete": true in your JSON response again.'; 0 confirmed, 1 never did and spent 5,477,211 per-step prompt tokens after the first prompt (9.6% of all per-step prompt tokens). The model writes native tool calls, not JSON; a native turn confirms only when it carries no tool call. 

| trial | reward | first prompt | prompts | confirmed | turns after | prompt tokens after |
| --- | --- | --- | --- | --- | --- | --- |
| `har90-mimo-0036-f` | 0.0 | head#45 | 2 | no | 128 | 5,477,211 |

## Grader notes (for Data's verifier audit; not R-ENV-02)

- `har90-mimo-0036-f` (outcome): source_text_assertion: failing verifier test asserts literal source strings (test_outputs.py::test_edge_all_categories_are_derived_from_expanded_item); asserted literal 'is_flagged(f_, \\"pipe\\")' is not in instruction.md; grader audit flag, not R-ENV-02

## Engineering findings

- `har90-mimo-0758-b`: old config (max_tokens 8192) summarization livelock: full summary and fallback chat fail, short summary succeeds but context immediately overflows again; loop of summarizations never terminates until timeout
- `har90-mimo-0758-c`: old config (max_tokens 8192) summarization livelock: full summary and fallback chat fail, short summary succeeds but context immediately overflows again; loop of summarizations never terminates until timeout
- `har90-mimo-0758-d`: normalizer allowlist (mimo_tool_calls.py:154, args <= {command|keystrokes, duration}) rejects the extra `description` param after summarization handoff while the same shape was accepted before; feedback is uninformative

## Acceptance: recorded vs inferred

No recorded step_layers in this batch; acceptance is observation-inferred (H-ACC) with normalizer replay for the proposed calls.

## Capture coverage

Episodes by count AND token-attributed share are both shown: a step count matching n_episodes is NOT full coverage when tokens are unattributed (har90-mimo-0036-c: conts -- missing, 13890 unattributed -> 0.0 share; har90-mimo-0036-e: conts -- missing, 50399 unattributed -> 0.979 share; har90-mimo-0036-f: conts -- missing, 191912 unattributed -> 0.9704 share; har90-mimo-0758-a: conts -- missing, 13617 unattributed -> 0.0 share; har90-mimo-0758-b: conts -- missing, 3646823 unattributed -> 0.8598 share; har90-mimo-0758-c: conts -- missing, 20637372 unattributed -> 0.211 share; har90-mimo-0758-d: conts -- missing, 33866 unattributed -> 0.993 share).

| trial | assembly | head | cont files | summ files | steps vs episodes | conts missing | token share (traj/proxy) | unattributed input | standins | notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `har90-mimo-0036-b` | single_head | True | -- | 0 | 200/201 | -- | 1.0 | 0 | -- | -- |
| `har90-mimo-0036-c` | single_head | True | -- | 0 | 0/1 | -- | 0.0 | 13890 | -- | -- |
| `har90-mimo-0036-d` | single_head | True | -- | 0 | 529/530 | -- | 1.0 | 0 | -- | -- |
| `har90-mimo-0036-e` | single_head | True | -- | 0 | 83/84 | -- | 0.979 | 50399 | -- | -- |
| `har90-mimo-0036-f` | duplicate | True | [1] | 0 | 172/173 | -- | 0.9704 | 191912 | -- | -- |
| `har90-mimo-0036` | single_head | True | -- | 0 | 120/121 | -- | 1.0 | 0 | -- | -- |
| `har90-mimo-0758-a` | single_head | True | -- | 0 | 0/1 | -- | 0.0 | 13617 | -- | -- |
| `har90-mimo-0758-b` | head_missing | False | [11] | 3 | 761/762 | -- | 0.8598 | 3646823 | 1 | head file missing; episodes covered 761 via cumulative trajectory.cont-11.json (starts at system prompt) |
| `har90-mimo-0758-c` | cumulative_superset | True | [31] | 0 | 212/213 | -- | 0.211 | 20637372 | 30 | -- |
| `har90-mimo-0758-d` | new_session | True | [1] | 3 | 234/234 | -- | 0.993 | 33866 | -- | -- |

## Provenance

- evallab-src: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har76-main-proof/src`
- evallab commit: `5a6f4be9d535f42fd1b96834b8a6040c9d03abd3`
- normalizer sha256 (`mimo_tool_calls.py`): `73297a676819b08b89ff4b6d7e74a168ee86c3bd146c706190c018b175d9ac56`
- normalizer mode: `evallab-normalizer`
- HAR-93 treatment parquet sha256: `9759017ffb8a4946aabf15ea3056316f788e479e1912ea58da463556f0ec9487`
- HAR-93 capture parquet sha256: `14f24c1b6469f3d38afc73d74dfe2c8047ce08deddc84dc26b655691c1db7b4a`

## Limits

- Replaying old messages through today's normalizer recovers what the model PROPOSED; it is never what the harness accepted/executed then. Acceptance comes only from that step's recorded observation, or from recorded step_layers where the harness wrote them.
- Anything absent is null/`unknown`, never 0.
- Treatment keys: Infra's pinned dispatch key (`<worktree>/derived/<lane>/treatment-key.json`) first, then Data's HAR-93 trial_treatment.parquet setup_key (sha256 recorded above; attached as a cross-check wherever it exists), then a lab-metadata pin; config-derived keys are only a fallback.
- A (task, key) cluster is `learnable` only with 2+ scored attempts and mixed outcomes; `all-pass`/`all-fail` need 2+ scored attempts; otherwise `unknown`. No claim is ever made across keys except a declared, recorded equivalence.
- 'Technical difficulties. Please continue with the task.' agent messages are Harbor stand-ins, not model output; they are excluded from model-behavior counts and loops (har90-mimo-0758-b trajectory.cont-11.json #761-761 (1); har90-mimo-0758-c trajectory.cont-31.json #184-213 (30)) and cited as the overflow/end-of-run pattern.
- R-CTX-01 livelock trials (har90-mimo-0758-b, har90-mimo-0758-c): steps after the first context-exceeded cycle were produced under degraded summarization context and are not attributed to the model; trial.log is the authority.
