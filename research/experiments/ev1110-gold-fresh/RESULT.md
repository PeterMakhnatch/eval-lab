# Gold fresh result

## Outcome

**The study closes with 0 eligible rows. No new trace receives a label or score.**

All 3,524 candidate IDs occur in a retained detector matrix receipt and a retained prediction cache.
All 3,524 raw-trajectory CAS hashes also occur in the retained full-DEV input allowlist.
These positive matches independently exclude the whole candidate population.
The study does not weaken eligibility to reach 400 rows.

PR #2060 registers `goodfire-dev-fresh-labels-v1` as a separate, empty source.
The data-2 removal closes that live registration and removes its empty Gold count entry.
The cohort manifest, pre-registration, and exclusion evidence remain as the record of the null result.
The scorer still refuses a selected `labelled-only, unscored` cohort without its named `--unlock-cohort`.
This removal adds no trajectory, label, or score.

## Prospective registration and earlier study

Registration and both prompts push in commit `a337eb66045191049f51bdb171062329ccf79ef5` before any reader call.
Both frozen prompt SHA-256 values match the required action-v2 values.
Prompt revisions: 0. Trajectories used for prompt development: 0. Reader calls: 0.

The earlier `ev1110-gold-grow` study remains closed under A1: 13/496 exact-quote failures, or 2.62%.
All coordinates resolve. CheatBench has 69/80 passing citation checks.
Its 69 remaining candidates stay unread, and its Part B stays cancelled.
This new study uses only Goodfire, where the earlier reproducibility gate passes 30/30 and 8/8 citations.
The lead authorizes a separate prospective study because that source-specific gate passes.
This does not erase the mixed-source study failure or establish accuracy on new data.

## Eligibility funnel

| Step | Rows |
|---|---:|
| Approved Goodfire DEV coding population | 3,674 |
| Existing panel removed | 150 |
| Candidates before exposure exclusions | 3,524 |
| First removed by origin/main record identities | 555 |
| First removed by retained results identities | 2,969 |
| Eligible | 0 |
| Sampled with seed 20261111 | 0 |
| Labelled / scored | 0 / 0 |

Every candidate receives the strict DEV split check using its run ID and task ID.
The inventory uses recorded raw-trajectory CAS hashes; it does not load actor trajectories to compute hashes.
Additional split removals: 0. Missing-hash removals: 0. Search errors: 0.
The prospective rule takes all eligible rows when fewer than 400 exist. Here, that means 0.
No stratum contains an eligible row. The study never substitutes other rows.

### Every searched store

The table reports total matching candidates and incremental removals in search order.
A matching candidate can occur in several stores. Do not add the match columns.
State paths below are relative to `~/.local/state/`.

| Store | Record files | ID matches | Hash matches | Incremental removals |
|---|---:|---:|---:|---:|
| origin/main `9125e8f98645d987a33629b840dcf1fab874d0b7` | 708 | 555 | 0 | 555 |
| pre-deslop-2026-11-10 `f69f4a8218e66cb282392c5f12a1a9f752bd7d84` | 967 | 555 | 0 | 0 |
| archive/research-pre-ev1110-2026-11-10 `e0364fbe327fc3c14bb5d78b2849f2bfa72f2c8c` | 958 | 555 | 0 | 0 |
| ~/Developer/eval-lab-results/ | 123,490 | 3,524 | 0 | 2,969 |
| evallab-cloud-trace-client | 0 | 0 | 0 | 0 |
| evallab-cm18 | 3 | 0 | 0 | 0 |
| evallab-cm18-b6 | 3 | 0 | 0 | 0 |
| evallab-cross-test | 1,555 | 0 | 0 | 0 |
| evallab-dashboard | 1,016 | 0 | 0 | 0 |
| evallab-ev1105 | 2,312 | 0 | 0 | 0 |
| evallab-ev1110 | 2,177 | 0 | 0 | 0 |
| evallab-failure-library | 1,087 | 0 | 0 | 0 |
| evallab-fleet | 412 | 0 | 0 | 0 |
| evallab-goodfire-cache | 0 | 0 | 0 | 0 |
| evallab-har104 | 0 | 0 | 0 | 0 |
| evallab-har116 | 0 | 0 | 0 | 0 |
| evallab-insights | 2,762 | 555 | 0 | 0 |
| evallab-judge | 0 | 0 | 0 | 0 |
| evallab-nightly | 5,769 | 0 | 0 | 0 |
| evallab-np21-cm1 | 4 | 0 | 0 | 0 |
| evallab-np32-ce1 | 82 | 0 | 0 | 0 |
| evallab-np33-cm1 | 6 | 0 | 0 | 0 |
| evallab-np38-cm1 | 9 | 0 | 0 | 0 |
| evallab-ov1104 | 0 | 0 | 0 | 0 |
| evallab-ov1106 | 65 | 0 | 0 | 0 |
| evallab-prod-live | 16,721 | 3,524 | 3,524 | 0 |
| evallab-ts11-worker-evidence | 2 | 0 | 0 | 0 |
| evallab-ts14-native-validation | 526 | 0 | 0 | 0 |
| evallab-wf | 12 | 0 | 0 | 0 |
| evallab-worker-evidence | 77 | 3,524 | 0 | 0 |

The inventory searches identity tokens, not detector decisions or score values.
It excludes actor-body directories, other chats, source custody records, and this study's own inventories.
It also excludes the earlier Part B exclusion inventory, which is not evidence of detector exposure.
Zero record files means no selected textual record file; it does not mean an empty directory.
SQLite payload tables and actor-body stores remain unopened by this search.
This is a positive exclusion proof, not a claim that all historical exposure is recoverable.
Additional stores cannot make an excluded candidate eligible.

### Decisive retained records

| Record | Matching candidates | SHA-256 |
|---|---:|---|
| `~/Developer/eval-lab-results/2026-11-02/goodfire-dev-master/detector-matrix-receipt.json` | 3,524 IDs | `37b97921baf7de56236ea69d929127f7199cb2fb31981f0b26a35cb8dbb96053` |
| `~/.local/state/evallab-worker-evidence/tw12-20261028/artifacts/global-cues-01/predictions.private.jsonl` | 3,524 IDs | `af235d033f6cf8f68f7eb3536ca70f58cb6535790a03727d46a6652d07f8063c` |
| `~/.local/state/evallab-prod-live/actor-bundles/goodfire-dev-full-20261026-inputs/goodfire-dev-cas-allowlist.json` | 3,524 hashes | `875538ca1ee3eb1532981706ef42fb709179ea847fd5dff26f8658e9e7262241` |

The first two records establish exclusion without interpreting the custody allowlist as detector execution.
The inventory never joins their detector values to candidate labels or trajectories.

## Labels and quality measures

| Measure | Result |
|---|---|
| Executed-positive | 0 |
| Reviewed-control | 0 |
| Attempted | 0 |
| Unjudged | 0 |
| Mechanism counts | Empty; no labelled row |
| Binary reader agreement | Not measured: 0 reader pairs |
| Mechanism reader agreement | Not measured: 0 reader pairs |
| Citation pass rate | Not measured: 0 checked citations |
| Executed-positive base rate among publisher-positive rows | Not measured: 0 sampled rows |
| Executed-positive base rate among publisher-negative rows | Not measured: 0 sampled rows |
| Random admitted-positive spot check | Unavailable: 0 admitted positives, not 20 |

Zero observations are not a 0% base rate or a 100% citation pass rate.
The study closes on empty eligibility, not a reader-quality stop.
No amendment, failed-response retry, prompt revision, or replacement sample occurs.

## Registry and score invariants

The registry remains at 853 rows and 460 binary rows.
`uv run evallab failures bank gold-counts --json` shows the separate source with 0 rows.
The source registration retains the exact exposure and scoring strings.

| Before/after object | SHA-256, identical on both sides |
|---|---|
| Canonical serialized 853 existing rows | `f4d05e3313431a5d4854a7239a6d3f7ef46ef9550b6346e1a6bb2b77256468f2` |
| All 49 existing score rows | `2f3858d1bdff188d11043e0bd3a9ef72b8febba80852eb12f3517753a15d12a8` |
| Existing 21 A/B/C score rows | `d7893df23aa2a77664ce92c5064abdf4afa212b69c7b37ea4ae7813bd13dde76` |
| All existing score cohort metadata | `2c9a431ef9c9c671fcec590abec4130d40dae3bddca1f22763d14142ba4758a2` |
| Original gold-grow scoreboard file | `b39b3d3dfbf4e3c50a8b99b0d9a3b44fcba14a3df04af7be0b59fbc82a3df81d` |

Canonical score hashes cover complete row values, not only selected metrics.
Both score proofs use only the existing frozen panels. They never select the new cohort.
The default freeze stays unchanged in membership and excludes this source.
Source-code hash refreshes create separate proof freezes; they do not overwrite the historical freezes.

Observed checks: 34 focused Gold/score tests; changed-source Ruff; changed-source `ty@0.0.71`.
The actual source build, `gold-counts`, and `score --help` commands complete.
The actual scoring command rejects a selected locked cohort with exit code 2, before prediction access.
A synthetic smoke checks the explicit unlock path for an empty cohort. It uses no study row.

## Peter's separate later scoring decision

The prepared freeze is not executed. The exact opt-in command is:

```sh
uv run evallab score \
  --freeze ~/Developer/eval-lab-results/gold/fresh-v1/single-pass-freeze.json \
  --unlock-cohort goodfire-dev-fresh-labels-v1 \
  --out ~/Developer/eval-lab-results/gold/fresh-v1/peter-single-pass
```

The unlock names one source and applies only to that invocation. It does not remove the registry lock.
The scorer refuses a selected labelled-only cohort without its matching named unlock.
An unlock without freeze selection does not include the cohort.

**Do not spend the later scoring decision on this empty cohort.**
That command can only reproduce existing panels and report an empty new panel with undefined performance.
It cannot establish detector accuracy, action base rates, reader reliability, or new-cohort generalization.
It uses stored predictions only; it does not run a detector or fill missing predictions.
A useful new scoring study needs a newly authorized eligible population, not relaxed exclusions here.

## Quota, custody, and delivery

New reader calls: 0. Paid inference: $0. Subagents: 0. Local inference: 0. Service changes: 0.
Shared Codex weekly usage changes from 65.0% to 66.0% during preparation.
Shared Gemini weekly usage stays at 3.1%. These shared changes are not lane-only reader usage.
No quota stop threshold is reached.

Receipts: `~/Developer/eval-lab-results/gold/fresh-v1/`.
Git retains the prompts, pre-registration, empty compact label view, registration, and receipt hashes.
The external artifact index binds the searched-file list, per-record membership matches, sample, and score proofs.
`RECEIPTS.json` in the experiment directory records its SHA-256 and the key receipt hashes.
The durable lane copy remains at `~/.local/state/evallab-ev1110/gold-fresh/`.

No U09-owned file changes. Registry changes stay limited to the new source and scoring lock.
The protected PR and Line-gate receipt are recorded at the delivery milestone below.
