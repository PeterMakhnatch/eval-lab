# Swarm QA: sealed comparison at one cap

**Status: design only. No paid run is approved.** Build and check the measurement offline first. Do not buy repeats on the stored cohorts or rerun MAC Bench to answer this QA question.

**OBSERVED.** The [stored verdicts](README.md) lack independently verified labels, matched alarm budgets, and comparable dollar costs across all arms. The stored cascades do not give the strong model access to the cheap model's claims.

**INFERRED.** Run the smallest paired comparison that can change a production choice. The first 8 bundles qualify the method and estimate spread. They do not settle a small effect. Use a new sealed set for the decision if the pilot cannot resolve it.

## 1. Seal items and give each arm the same evidence

Start with 8 bundles of 4 items, for 32 items total. Use 16 known defects and 16 matched clean decoys. Cover task packages, graders, execution traces, and source-linked data rows. Match decoys to the defects by family, kind, and visible input length. A clean decoy passes an independent check for the tested mechanism. It is not certified bug-free.

- Use private or fresh cases for the decision set. Use public and DEV cases only for offline checks and calibration. Keep calibration and decision families separate.
- Have 2 independent reviewers confirm the key, direction, mechanism, and evidence before launch. Resolve disagreements with a third reviewer. Do not admit unresolved cases.
- Bind task and grader keys to the exact package and reference receipts. Bind trace keys to an executed action and its effect. Bind data keys to source and row versions.
- Freeze item IDs, hashes, family blocks, reference keys, exclusions, and expected control outcomes. Seal the key manifest for the scorer. Export only the evidence, neutral IDs, and permitted provenance to agents.
- Give every arm the same read-only bundle bytes and tool access. Use a fixed partition as an assignment, not a change in evidence access. Record the hash of each mount.
- Strip Git objects, remotes, repaired artifacts, hidden tests, prior verdicts, retrieval indexes, labels, and revealing names or metadata. Block network and memory except the metered model route. Use isolated workspaces and caches, with no shared paths across arms. The board is the only seat-to-seat channel in the board arm.

Use deterministic controls for missing keys, role leaks, visible metadata, duplicate claims, and decoys. Test that a known supported claim passes and an unsupported claim fails. Check that stopped seats cannot submit, post, or start calls. Confirm that the scorer can read sealed evidence and that agents cannot.

## 2. Pair 4 arms on each bundle

Use the same hard dollar cap and deadline for every arm on a bundle. Run all 4 arms on every bundle. Randomize isolated launch slots within each bundle. Freeze model routes, prompts, tool policy, roster, partitions, merge rules, and the Harbor revision before launch. Start with k = 2 cheap seats. Do not add seats or change votes after seeing test results.

| Arm | Work under the bundle cap |
|---|---|
| 1 stronger model | 1 agent reviews the full bundle. Fix the comparator before the pilot; model price alone does not establish strength. |
| k cheap, fixed partition, no talk | Each seat has a fixed assignment. Merge the sealed submissions with a frozen, label-free deduplication rule. |
| k cheap, shared board | Use the same cheap model, roster, initial partition, merge rule, and deadline. Permit requests and findings on the board. Charge model use for reading and writing messages. |
| Cheap finders → 1 stronger reviewer | Use the silent finder setup. Reserve reviewer dollars first. The reviewer sees deduplicated claims and their cited evidence, not keys. The reviewer can reject or correct claims before the deadline. |

The board versus silent pair changes only the channel policy. The strong versus cheap comparison changes the model and work split together. The reviewer comparison evaluates a policy that trades some finder budget for review. It does not isolate a model-only effect.

## 3. Score claims outside the agent sandbox

Require each claim to name the item, location, direction, and mechanism. Require a bounded evidence citation and a rank or confidence. Freeze ranking and tie rules without access to the keys. Count duplicate claims once for defect credit. Record their review burden before deduplication.

The scorer checks the cited location against the frozen evidence. Match at most 1 claim to each key and at most 1 key to each claim. Match on item, direction, mechanism, and evidence, not words alone. Give ambiguous and novel claims to independent reviewers who cannot see arm identity, cost, or rank. Use exact replay where it can resolve a claim. Separate confirmed novel defects from known-key recall. Do not count unmatched claims as false until review rejects them. Report unresolved claims and their effect on the result.

| Measure | Rule |
|---|---|
| Verified defects for each dollar | Count distinct verified defects, then divide by attributable billed model and backend dollars. Report each cost component and known-key and novel counts separately. Keep costs for failed and incomplete starts. Unknown charges block a dollar claim. |
| False alarms for each true defect | Use rejected claims divided by verified defects, at the same predeclared alarm budget. Report raw counts and rates on tested clean decoys too. When there are no true hits, report the counts without a ratio. |
| Review time | Time a blinded reviewer from first opening a claim to final disposition. Report total and minutes for each verified defect, with review of unresolved claims included. Measure machine wall time separately. |
| Coverage and loss | Report known-key recall, false alarms on controls, novelty, missing grades, all starts, and receipts. Do not score a timeout, transport failure, or lost sandbox as a clean result. |

Set the alert budget before the decision run. Compare the same top-K alert policy in each arm, with K fixed per bundle and no padding when an arm emits fewer claims. Report actual alert counts. Use disjoint calibration to set thresholds for the target control-alarm budget. Report decision-set results at those frozen thresholds. Report a prespecified sweep at matches such as 0 control alarms where both arms can reach the count. Report a shared cap separately from an exact match. Do not tune a production threshold on the decision labels or claim an operating-point win when the match fails.

Pair differences by bundle. Use family blocks for uncertainty if bundles share a family. Do not count seats or votes as independent items. Before a fresh decision test, freeze the minimum useful gain, tolerated false-alarm and review-time differences, start limit, and analysis rule. Use pilot spread and missingness to size it. Skip the test if its approved ceiling cannot support a decision.

## 4. Dollars, stop rules, and receipts

All prices here are estimates for planning, not quotes or authorization.

| Estimate | Source and limit |
|---|---|
| $0 in new model and sandbox charges for offline preparation | Plan skeptical review, rank 1. The stored replay needs neither service. Staff time is not priced. |
| $0.018494 Mini v1 and $0.005149 Flash v2 per trace screen at historical p95 | Nearest-rank estimates from the worker's `trace-costs.csv`, reported in Part 3. 4 Mini screens at that estimate cost about $0.074. |
| $0.25 per arm per bundle, or $8 across 8 bundles and 4 arms | Worker report, Part 3. This is an estimated trace-screen planning ceiling. It is not an all-in quote for a tool-using model. |
| About $10 all-in for a focused 2-arm comparison | Skeptic, rank 2: 20 tasks × 2 arms × an estimated $0.25 ceiling. This is an alternative scope, not funding for the 4-arm comparison. |
| Full tool-using model, backend, and human review costs unknown | No comparable tariff or receipt in the stored evidence. Price and verify the chosen routes, tools, and sandbox before requesting approval. |

Use `campaign` to admit arms, keep reserves, and stop spending across bundles. Campaign admission does not enforce an in-flight call limit. Before each call, atomically reserve its maximum charge against the bundle's shared balance. Bound input, output, tool, cache, retry, and sandbox exposure. Reserve the review leg before dispatching finders. Reconcile against verified peak tariffs and attributable receipts. Keep proxy estimates separate from provider charges. If usage is unknown, retain the full reserve and stop new admissions. Do not release a reserve on timeout or assume a failed call costs zero.

Apply a common freeze. Revoke call, claim, and board access at stop. Record admitted calls that settle after freeze and their charges. A cap binds only when a budget refusal or a verified budget-only denial prevents work before freeze. Publish binding counts and both freeze and final spend by arm. For a binding-cap claim, require a budget stop in at least 90% of scored bundles per arm. Claim matched realized dollars only with complete receipts and an arm mean spend ratio of at most 1.05. These proposed checks follow the skeptic's matched-spend rules. If the cap does not bind, report an assigned-cap comparison only.

Each item receipt links bundle, arm, item, evidence hash, seat, ranked claims, verdict, review time, and status. Each call receipt carries request ID, attempt, charge kind, reserve, settlement, and route. Attribute shared overhead by a frozen rule and reconcile item totals to the bundle. Log late settlements, denials, timeouts, and failures. Keep every start. Do not replace whole bundles unless a predeclared start limit permits it. Stop on leaks, arm-dependent missingness, or an unenforceable cap.

## 5. Production decision

**INFERRED.** Apply this table only after the validity, cost, and uncertainty checks. A win needs a useful gain in verified defects for each dollar, a matched alarm budget, and acceptable review time. The offline results and a spread pilot do not qualify.

| Decision result | What the owner runs in production |
|---|---|
| Strong solo wins | Run 1 strong reviewer at the measured scope and cap. Do not pay for extra seats. |
| Silent cheap wins | Run the fixed cheap partition and label-free merge. Keep review of alerts. |
| Board beats silent and strong and passes the review guard | Enable the board for the tested roster and work mix. Keep the silent configuration as a control. |
| Cheap → strong reviewer wins | Route deduplicated cheap claims to the stronger reviewer. Reserve review dollars first. |
| Union raises recall but fails the alarm or review guard | Use it only in a reviewed screen trial. Do not use it for automatic rejection. |
| No useful difference resolved or several arms remain indistinguishable | Keep the current policy. Do not claim equivalence or buy more seats from this result. |
| Labels leak, receipts fail, or dollars or alarms do not match | Make no equal-dollar change. Fix the measurement offline. Do not rerun automatically. |

## 6. Build in order

Sizes are estimates of new code, not commitments. The sources are the worker's Part 3 and the skeptic's review. Manual adjudication is extra.

| Step | Reuse landed tools | Gap and rough size |
|---|---|---|
| 1. Seal and score offline | `evallab qa score`, Eval Lab Gold tables and evidence bindings, task and grader receipts | Sealed bundle manifest, label-free claim merge, scorer adapter, and adjudication export: 200–350 lines. Verify reference and clean-decoy outcomes first. |
| 2. Package arms and prove isolation | `muster matrix`, `muster run`, and `muster regrade`; reuse the fixed-plan and board patterns from `muster/tasks/crowdrules` | QA adapter, equal evidence export, reviewer interface, and scorer-only mount: 150–250 lines. Verify both channel modes with no-model controls before dispatch. |
| 3. Bound dollars and link evidence | `campaign check`, `campaign run`, `campaign status`, `campaign spend`; `muster events` and `muster attribute` | Per-call budget guard with receipts for each item and per-bundle reconciliation: at least 100–200 lines for receipt joins. Size the guard after checking the provider contract. Existing lane gates alone are insufficient. |
| 4. Verify and compare | `tidegauge audit` for seat health and `tidegauge compare` for paired results | Emit compatible paired bundle records and join alarm, spend, and review fields. Allow about 100 lines for an adapter, a planning estimate. Replay faults for leaks, stopped seats, refusals, unknown costs, and late charges. |

Sources: `/tmp/fable-day/swarmqa/report.md` Part 3 and `trace-costs.csv`; `/tmp/fable-day/swarmqa-audit/report.md` sections 2–3; `/tmp/fable-day/plan-skeptic/report.md` sections 4–6. The next step is offline build and verification, not a paid run.
