<!-- REVISION 2. An independent review found five blockers in revision 1, all
valid, all fixed at root. Two numbers I reported to Main and Tutor were WRONG:
the universe is 183 items in 20 clusters after clone dedup. See §0. -->

---
type: protocol
topic: goldset-item-selection-and-taxonomy
author: analyst
date: 2026-08-28
status: revision-10-security-round-4
readiness: NOT_READY (5 blockers)
epistemic: measured - every census figure computed from artifacts on disk
collection: trajectory-analysis
owns: [item_selection, provenance, label_taxonomy]
delegated_to_tutor: [agreement_statistic, acceptance_threshold, rater_qualification, adjudication_rule, power_argument]
package: research/goldset/labeling_package.json
labeling_package_file_sha256: af040dd0471da40f5442e1b1bc3ee0c2efda5ddcad5dab429c90e8556f797d59
package_digest_in_band: 8e9148d6a6949dbb8fdf173609e73fecc0f0cf77c23c2d451a3a446b62aad8ac
build_id: b414467ffd3b32de438f214f1e6f6b2cf77d1fe2104ec64c094f9094c258f373
revision: 10
blockers_fixed_from: independent review (Grok) - 5 blockers, all root-caused
---

# Gold-Set Item Selection and Label Taxonomy

Analyst half of the 3-rater gold-set protocol. Tutor (`wK:p4`) owns the statistical
half; those parameters are present in the package as **explicitly null**, not
guessed.

**No ratings exist in this package and none can be produced by it.** The builder
has no code path that writes a rating. `LabelItem` has no rating field at all.
Ratings arrive only as separate typed `RatingRecord` sidecars through a
human-ingest path gated on a qualified rater ID.

**No LLM judge output was read, imported, or consulted.** Judge output is not gold
and is not permitted as a substitute at any stage.

## 0. Revision 2 — five blockers found by independent review, all valid

Revision 1 shipped a package that **could not be labelled**. Every blocker below
was found by independent review (Grok), and all five were real.

| # | Blocker | Root cause | Fix |
|---|---|---|---|
| B1 | Rater context unlabelable | `task_instruction` was **always `None`** — no such key exists in ATIF, the instruction lives in the trailing user step. Prior steps carried only a digest with no message/arguments/observation. 69 % of agent steps have an empty message, so **126 of 183 items showed the rater nothing judgeable**. | Extract instruction from user steps; render full prior-step content |
| B2 | Machine truth leaked | I put the attention check's own answer and `prior_error_exists` **into the rater-facing context** — destroying the check and priming `error_response` | Machine truth moves to `machine_truth_WITHHELD.json`, never shipped to raters |
| B3 | Byte-identical trajectories double-counted | 29 files carry only **26 distinct sha256**; 3 shas appear at 2 paths each. Keying on relpath inflated 183 unique steps to 237 and corrupted every cluster statistic | Identity is `(source_sha256, step_index)`; relpaths become `source_aliases` | <!--hist-->
| B4 | Ratings mutated the frozen digest; readiness never validated labels | `ratings` was a field **inside** the item, so labelling changed the package digest. Readiness counted rater IDs while ignoring whether labels existed or were in-enum | Items immutable with no rating field; typed sidecar `RatingRecord`; readiness validates enum + completeness |
| B5 | Facets lacked `CANNOT_JUDGE` | I argued at length that the escape hatch prevents false agreement, then omitted it from both facets | `CANNOT_JUDGE` on **every** human-judged field |

**Two numbers I reported to Main and Tutor were wrong.** The universe is **183
items in 20 clusters**, not 237 in 23. Both were inflated by B3. Corrected <!--hist-->
throughout, and re-paged.

Two further corrections to revision 1's own claims, found while fixing B1:

- The task instruction **is** recoverable — it is the trailing user step before the
  agent turn. Revision 1 read a non-existent `instruction` key and silently got
  `None` for all 237 items. <!--hist-->
- `model_name` **is** present at `doc["agent"]["model_name"]` (e.g. `gpt-5.6-terra`).
  Revision 1 claimed it was absent and that stratification needed a `traj_features`
  join. Wrong; it is in the trajectory.

## 0.1 Four digests, named apart

Two families, and conflating them caused two separate review blockers.

**Contract digest — what a rater signs. Immutable, computed BEFORE intake.**

| Name | Covers | Value |
|---|---|---|
| `rating_contract_digest` | ordered rater-visible item contexts + codebook + rating-schema + package-schema versions | `d6898d1b85c89775eb3180b643c4adf3b0c1cda0c9971486c385cf51ec493fb1` |
| `item_context_digest` | per item: trial identity + step ordinal + instruction + every prior and current field | per item |

**Artifact digests — identify a build. May include readiness and rating summaries.**

| Name | Covers | Value |
|---|---|---|
| `labeling_package_file_sha256` | sha256 of the file **bytes on disk** | `af040dd0471da40f5442e1b1bc3ee0c2efda5ddcad5dab429c90e8556f797d59` |
| `package_digest` (in-band) | serialized package minus its own key | `8e9148d6a6949dbb8fdf173609e73fecc0f0cf77c23c2d451a3a446b62aad8ac` |
| `build_id` | ser(pkg − {build_id, package_digest}) + ser(truth − {build_id}) | `b414467ffd3b32de438f214f1e6f6b2cf77d1fe2104ec64c094f9094c258f373` |

**Why the split is load-bearing.** Ratings previously had to bind `package_digest`,
which covers readiness and rating summaries — so it **changes as ratings arrive**.
Requiring a rating to sign it was circular: the value only existed after the
ratings were counted. `rating_contract_digest` covers exactly what a rater is shown
and judged against, in order, and nothing downstream of intake can alter it.

The file SHA is deliberately not stored in-band, because a file cannot contain its
own hash. `package_digest`, `build_id` and every `item_context_digest` are
**recomputed, never trusted**, by `load_paired_artifacts`.

```bash
sha256sum research/goldset/labeling_package.json      # file SHA
python3 research/goldset/test_labeling_package.py     # verifies all four
uv run pytest tests/test_goldset_labeling_package.py  # independent reimplementation
```

## 0.2 Canonical census — machine-governed

Every field below is compared to the live package by
`research/goldset/check_doc_consistency.py`. A wrong value fails the check, not
merely a *known-stale* value: token-searching for old strings could not catch a new
wrong number, so changing `distinct_content_digests` from 26 to 27 previously
passed.

<!--census-->
```
trajectory_files_seen: 29
distinct_content_digests: 26
duplicate_paths_dropped: 3
agent_steps_unique: 183
clusters_with_agent_steps: 20
items_with_instruction_present: 183
context_complete: 183
raw_clusters: 20
n_items: 183
n_blockers: 5
```

## 1. Readiness — NOT_READY, fail-closed, 5 blockers

```
readiness NOT_READY
EFFECTIVE_CLUSTERS_BELOW_FLOOR: K_eff=13.33 < 20.0
CLUSTER_CONCENTRATION_TOO_HIGH: 16.9% > 5%
QUALIFIED_RATER_POOL_TOO_SMALL: have 0, need >= 3
ITEMS_WITH_ZERO_VALID_RATINGS: 183
REGISTRY: REGISTRY_ABSENT
```

**Recruiting raters clears only two of these.** The cluster, context and registry
blockers are independent of rater supply — see §7 for the required order.

`evaluate_readiness` fails closed on every condition and cannot return `READY`
while any holds. It validates label presence and enum membership, not merely the
presence of a rater key ID, and it rejects duplicate or conflicting submissions
from one `(item, rater)` rather than collapsing them into a set.

## 2. Item universe — measured, deduplicated

| Quantity | Value |
|---|---|
| `trajectory.json` files seen | 29 |
| **Distinct content digests** | **26** |
| **Duplicate paths dropped** | **3** |
| **Unique agent steps (the universe)** | **183** |
| **Clusters contributing >= 1 agent step** | **20** |
| Items with a task statement | **183 / 183** |
| Items with an empty message | 126 (**68.9 %**) |

Only agent-source steps are items. `system` and `user` steps carry no agent
decision to judge, so labelling them would measure the harness, not the agent.

**68.9 % of items have an empty message.** Those are tool-call-only steps, and they
are judgeable *only* because the package now renders tool arguments and
observations in full for both the item and every prior step. Revision 1 rendered
neither for prior steps, which is why B1 was fatal rather than cosmetic.

### 2.1 Provenance key — basename is not an identity

Twenty-nine files collapse to twenty-six basenames. Keying items on trial basename
would silently merge three distinct trials and corrupt a frozen package in a way no
later check could detect. Items are therefore keyed on

```
(source_sha256, step_index)
```

**Item identity is the full `item_context_digest`**, not step content. Deduping on
step content alone wrongly merged **16 distinct contexts** — worst case 6 steps
across 6 *different* trials sharing one terminal message, plus consecutive indices
17/18 inside a single trial. Trial identity and step ordinal are inside the digest,
so distinct contexts never merge; only genuine byte-identical copies of the same
logical item alias.

With `item_id = sha256(source_sha256#step_index)[:16]`. Provenance is **content-
addressed**: relpaths are recorded as `source_aliases` and in the top-level alias
manifest, never as identity. Any edit to a source trajectory changes its digest and
therefore invalidates its items, rather than silently changing their content.

Revision 1 keyed on `(source_relpath, step_index, source_sha256)`. Including the
relpath made byte-identical trajectories at two paths into distinct items, which is
what inflated 183 to 237. <!--hist-->

### 2.2 Strata — label-independent by construction

Stratification uses only `has_tool_calls` and step position. It deliberately uses
**nothing resembling a label**, because stratifying on a label-correlated variable
biases the prevalence estimate it is meant to support.

| Stratum | n |
|---|---|
| `tool:late` | 112 |
| `tool:early` | 50 |
| `notool:terminal` | 20 |
| `notool:early` | 1 |

Two facts Tutor needs:

- **`notool:early` has n = 1.** No per-stratum quantity is estimable there. Either
  merge it into `notool:terminal` or exclude and report the exclusion.
- **90 % of items are tool-bearing** (of 183). The corpus cannot support a
  contrast between tool-using and non-tool-using steps.

### 2.3 Sampling design and weights

Two arms, recorded per item:

- `prevalence_core` — random, `sampling_weight = |universe| / core_n`, valid for
  base-rate estimation.
- `rare_cell_boost` — targeted, `sampling_weight = 0.0`, **excluded from prevalence
  arithmetic** and used only for agreement power on thin cells.

The current build takes the **entire universe** as core (`core_n = None`), so the
boost arm is empty and every weight is 1.0. That is the honest configuration at
this corpus size: there is nothing to sample from.

Seed is derived from the sha256 of the sorted candidate `item_id`s, so selection is
reproducible and **cannot be re-rolled to taste**. Verified: two runs produce a
byte-identical package, `labeling_package_file_sha256 af040dd0…`.

### 2.3 Alias manifest — the dedup is auditable

`census.alias_manifest` maps every content digest to its canonical relpath, all
relpaths, and a duplicate count. 26 entries; duplicate counts sum to exactly the
3 dropped paths, asserted by test.

The manifest also exposes the *cause* of the duplication: three trials appear both
under a campaign prefix and at top level, e.g.

```
minimal-harbor-luna-20260824/minimal-luna-gaia2-ambiguous-amd64/gaia2-ambiguous__np3YExC/...
                             minimal-luna-gaia2-ambiguous-amd64/gaia2-ambiguous__np3YExC/...
```

A nested copy, not two distinct runs. Content addressing collapses them correctly
and the manifest records that it happened, so the dedup is checkable rather than
asserted.

### 2.4 `CANNOT_JUDGE` vs `INSUFFICIENT_CONTEXT` — different failures

Every human-judged field now offers **both**, and they must never be pooled:

| Value | Meaning | What its rate measures |
|---|---|---|
| `CANNOT_JUDGE` | Context **is** present; the step is genuinely ambiguous | **Taxonomy** ambiguity |
| `INSUFFICIENT_CONTEXT` | Context is **absent or truncated in the package** | **Package** completeness — a builder-fixable defect |

Conflating them hides package defects inside a taxonomy-ambiguity number. A rising
`CANNOT_JUDGE` rate says the label set needs work; a rising `INSUFFICIENT_CONTEXT`
rate says the builder does.

**Cross-check, and this is the point of the pair.** Each item carries
`context_completeness.builder_verdict` — `COMPLETE` or `INCOMPLETE` — declared by the
builder, not the rater. Current distribution:

```
COMPLETE 34    INCOMPLETE 149
```

**Current distribution is 34 COMPLETE /
149 INCOMPLETE — 81.4% incomplete.** <!--hist-->
Revision 5 reported 158/25, which was false: the verdict examined only the item's
own truncation and ignored prior-step truncation entirely.

**This is now a readiness gate.** `MAX_INCOMPLETE_CONTEXT_FRACTION = 20%`;
at 81.4% the package emits <!--hist-->
`CONTEXT_INCOMPLETE_TOO_HIGH` and refuses. A rater forced into
`INSUFFICIENT_CONTEXT` on four items in five is measuring the builder, not the
agent.

If raters mark `INSUFFICIENT_CONTEXT` on items the builder called `COMPLETE`, **the
builder missed a defect it believed it had detected.** That disagreement is a
measurement of the package, obtainable only because the two signals are separate.

### 2.5 Delivery guard — exact allowlist, coordinator-signed

The default tree co-locates `labeling_package.json` and
`machine_truth_WITHHELD.json`. **That directory must never be handed to a rater.**

```bash
export GOLDSET_DISTRIBUTION_SECRET=...        # required; unsigned export refuses
python3 research/goldset/build_labeling_package.py \
  --runs-root runs \
  --out research/goldset/labeling_package.json \
  --machine-truth-out research/goldset/machine_truth_WITHHELD.json \
  --export-rater-bundle <empty-or-absent-dir>
```

**Isolation is an EXACT ALLOWLIST, not a pathname denylist.** The earlier denylist
was bypassed by renaming: the withheld truth copied to `deep/answers.json` exported
cleanly with the full truth in the destination. A denylist can never be complete,
and content-matching only moves the goalposts. So:

- the bundle is generated into a **fresh temporary directory**
- that directory is verified to contain **exactly** `BUNDLE_ALLOWLIST`
  (`rater_bundle.json`) — every extra path, unowned directory and symlink is
  rejected
- the destination must be **empty or absent**; a bundle is never merged into
  pre-existing paths
- the allowlist is **re-verified after publish**

Filename and content are therefore irrelevant: anything the bundle does not own is
rejected.

**The export is coordinator-signed and the CLI refuses to produce an unsigned
bundle**, because a rater cannot verify a bundle that carries no signature.

**Withheld from the bundle:** `builder_verdict`, `degraded_reasons`, the
attention-check identity, and every truth row. Bundle items expose exactly
`item_id`, `item_context_digest`, `cluster_id`, `step_index`, `rater_context` — the
last two are present precisely so a rater can **recompute** the context digest
rather than copy a supplied value.

**The bundle carries no artifact digest.** `package_digest` and `build_id` identify
a *build*; the contract is what a rater is shown. Mixing them made the contract
digest circular.

**Open follow-up, recorded not fixed:** `builder_verdict` priming risk is resolved
by withholding it and taking the signal post-hoc via the 2×2 diagnostic.

## 3. Cluster adequacy — Tutor's power verdict: HOLD LABELING

**183 items nest inside 20 clusters, but the design effect is what binds.**

| Quantity | Value | Target |
|---|---|---|
| Raw clusters | 20 | — |
| **Kish $K_{\text{eff}}$** | **13.33** | $\ge$ 20 |
| Max cluster concentration | **16.9%** | $\le$ 5% |

$$K_{\text{eff}} = \frac{\left(\sum n_i\right)^2}{\sum n_i^2} = \frac{183^2}{2513} = 13.33$$

Independently reproduced from Tutor's verdict. Even a perfectly balanced 20-cluster
split reaches only **19.97**, so 20 raw clusters cannot clear the floor at any
concentration. Largest cluster carries 31 of 183 items
(16.9%) against a 5% target.

**Labelling is on HOLD. A data campaign is required before raters are recruited.**
Recruiting three raters now would spend human time on a package that cannot yield a
usable interval.

### 3.1 Campaign target

- **~35–50 new distinct trajectory digests**
- **$\le$ 5 % concentration** per cluster
- **$K = \max(30,\; 96\rho)$** after an ICC pilot establishes $\rho$

### 3.2 The gate is in code, not prose

`evaluate_cluster_adequacy` computes $K_{\text{eff}}$ and concentration and emits
readiness blockers. It fails closed and is asserted by test, including that a
balanced 40-cluster design clears it. Current output:

```
blocker EFFECTIVE_CLUSTERS_BELOW_FLOOR: K_eff=13.33 < 20.0
blocker CLUSTER_CONCENTRATION_TOO_HIGH: 16.9% > 5%
blocker QUALIFIED_RATER_POOL_TOO_SMALL: have 0, need >= 3
blocker ITEMS_WITH_ZERO_VALID_RATINGS: 183
blocker REGISTRY: REGISTRY_ABSENT
```

An agreement interval must still come from a **cluster bootstrap resampling
`cluster_id`** (the content digest) even once the floor is met. The floor makes the
bootstrap possible; it does not make item-level independence true.

## 4. Label codebook

**Every human-judged field carries BOTH escape values.** They are different
failures and must never be pooled:

| Value | Meaning | Its rate measures |
|---|---|---|
| `CANNOT_JUDGE` | The rater **has** the context; the step is **genuinely ambiguous** | **Taxonomy** ambiguity |
| `INSUFFICIENT_CONTEXT` | The context needed is **missing or truncated in the package** | **Package** completeness — a builder-fixable defect |

Neither is penalised. Conflating them hides package defects inside a
taxonomy-ambiguity number: a rising `CANNOT_JUDGE` rate says the label set needs
work; a rising `INSUFFICIENT_CONTEXT` rate says the builder does.

### 4.1 Complete value lists

| Field | Allowed values |
|---|---|
| `abstention` | `ACTED` · `DECLINED_WITH_REASON` · `DECLINED_NO_REASON` · `CANNOT_JUDGE` · `INSUFFICIENT_CONTEXT` |
| `error_response` | `NO_PRIOR_ERROR` · `ACKNOWLEDGED_AND_CHANGED` · `ACKNOWLEDGED_NOT_CHANGED` · `IGNORED_PRIOR_ERROR` · `CANNOT_JUDGE` · `INSUFFICIENT_CONTEXT` |
| `repeats_prior_action` | `YES` · `NO` · `CANNOT_JUDGE` · `INSUFFICIENT_CONTEXT` |
| `step_contribution` | `PROGRESS` · `NEUTRAL` · `HARMFUL` · `CANNOT_JUDGE` · `INSUFFICIENT_CONTEXT` |

### 4.2 Primary label — `step_contribution`

| Value | Definition |
|---|---|
| `PROGRESS` | Moves the task closer to the stated goal: acquires information the agent lacked, or changes state in a direction the goal requires. |
| `NEUTRAL` | Neither advances nor sets back. Valid exploration, re-reading held information, no-op confirmations. |
| `HARMFUL` | Sets the task back: destroys needed state, introduces an error to be undone, or commits to an excluded path. |
| `CANNOT_JUDGE` | Context present; step genuinely ambiguous. |
| `INSUFFICIENT_CONTEXT` | Context missing or truncated in the package. |

### 4.3 Facets — each with both escapes

**`error_response`** — `NO_PRIOR_ERROR`, `ACKNOWLEDGED_AND_CHANGED`,
`ACKNOWLEDGED_NOT_CHANGED`, `IGNORED_PRIOR_ERROR`, `CANNOT_JUDGE`,
`INSUFFICIENT_CONTEXT`.

`ACKNOWLEDGED_NOT_CHANGED` is the blind retry a machine gate can miss when an
environment event reports recovery.

**`abstention`** — `ACTED`, `DECLINED_WITH_REASON`, `DECLINED_NO_REASON`,
`CANNOT_JUDGE`, `INSUFFICIENT_CONTEXT`.

**`repeats_prior_action`** — `YES`, `NO`, `CANNOT_JUDGE`, `INSUFFICIENT_CONTEXT`.

### 4.4 Attention check — identity withheld from the bundle

`repeats_prior_action` is mechanically decidable and its machine ground truth lives
in the **separate withheld artifact**, never on the item. **The rater bundle does
not name which field is the check** — naming it is itself a leak, so
`attention_check_field` is stripped from the bundle along with any prose revealing
that a withheld truth exists. Rater disagreement measures **attention**, not item
ambiguity, and must never be pooled into taxonomy agreement.

### 4.5 Withdrawn machine truth — `prior_error_visible`

Computed by substring matching for `traceback` / `error` / `exit code 1`, **audited
at 88 % false positives** (38 of 43 hits) — it fired on `'Script completed / Wall
time 0.1 seconds'`. ATIF observations carry **no structured exit codes**, so no
deterministic implementation exists. **Withdrawn, not tightened.** The
`error_response` facet remains — a human can read the observation — but no machine
truth is claimed.

### 4.6 Excluded labels

| Excluded | Reason |
|---|---|
| `step_necessity` | Requires an oracle optimal path. None exists. |
| `step_efficiency` | Same defect, plus it presumes an unstated cost model. |
| `unrecoverability` | Counterfactual — quantifies over all continuations. Exactly the label one would reach for to unblock T1.3 `t_lock`, and exactly the one a human cannot supply. |

## 5. Statistical parameters — Tutor decided 2026-08-28

| Parameter | Value |
|---|---|
| Primary statistic | **`gwet_ac1_multirater_nominal`** |
| Declared universe $q$ | **12** |
| Interval method | **percentile_cluster_bootstrap**, 4000 resamples |
| Target 95 % CI half-width | **0.05** |
| Cluster unit | cluster_id (canonical logical trajectory digest) |
| Complementary | `krippendorff_alpha_nominal`, `fleiss_kappa`, `pairwise_cohen_kappa` |
| Prevalence-valid core | **required**, with sampling weights |

**`acceptance_threshold` is EXPLICITLY null by Tutor's decision, not an oversight.**
An interval will be reported without a pass/fail verdict until a threshold is
justified. `adjudication_rule` and `rater_qualification_criteria` also remain null.

Gwet's AC1 over a declared universe of $q = 12$ categories is the right
primary here: it is far less sensitive than $\kappa$ to the skewed marginals this
taxonomy will produce, and the declared universe makes the chance-correction
explicit rather than estimated from observed marginals. The complementary
statistics are reported alongside, never instead.

The **prevalence-valid random core** requirement is satisfied at present by taking
the entire universe as the core (a census, `sampling_weight = 1.0`); weights are
recorded per item so a future subsample stays valid for base-rate estimation, and
the `rare_cell_boost` arm carries weight `0.0` and is excluded from prevalence
arithmetic.

## 5b. The rating contract and the append-only ledger

### 5b.1 One canonical contract digest, recomputed by everyone

A rater must be able to verify what they were asked to judge, and the coordinator
must be able to verify what came back. That needs **one** value computed by **one**
function on **both** sides.

`compute_bundle_contract_digest` is that function. The canonical rater-visible
contract payload — schema version, codebook version, taxonomy contract block,
instructions, and the ordered deliverable items with their `item_context_digest` —
is built once inside `build_package`, and the digest is derived from it. Package,
export, client and server all recompute the same value from the same bytes.

Two facts make this load-bearing rather than decorative:

- The bundle carries **no artifact digest**. `package_digest` and `build_id`
  identify a *build*; putting either inside the bundle made the contract digest
  circular (the digest covered a field derived from the digest). Build identity is
  not contract.
- `export_rater_bundle` **refuses** when the package's declared digest and the
  exported bundle's recomputed digest diverge.

That second guard exists because they *did* diverge. Two digest functions
coexisted: the server validated an item-only digest while the export overwrote the
bundle with a full-bundle digest that the client signed. Every genuine client
rating was rejected `RATING_CONTRACT_DIGEST_MISMATCH` — the distributed path had
never once worked. Each half's tests passed against its own digest; **no test
crossed the seam.** The acceptance E2E below exists specifically to cross it.

### 5b.2 What a `RatingRecord` signs

```
schema_version, rating_contract_digest, item_id, item_context_digest,
rater_key_id, step_contribution, error_response, abstention,
repeats_prior_action, supersedes
```

`supersedes` is signed **whether null or set**, so its absence is signed too. It
was previously attached after signing, which let a proxy inject or alter correction
intent on a rater's behalf.

**Correction intent is read solely from the signed record.** `append_rating_record`
has no `supersedes` parameter. While one existed, every authorization check was
gated on it, so a caller who prepared a record carrying a signed `supersedes` and
appended with the default `None` skipped the cross-item, cross-rater and
already-superseded checks entirely — and the effective view, which reads the
*stored* field, dropped the victim anyway. Two sources of authority for one
decision was the defect; the duplicate is gone rather than reconciled.

A correction must name a **live** record with the same `item_id`, the same
`rater_key_id` and the same `rating_contract_digest`, and its chain must not cycle.
Without that, `supersedes` was a deletion primitive.

### 5b.3 Intake is an authenticated append-only ledger

Records are content-addressed, written `O_CREAT|O_EXCL` at mode `0444`, carry
`record_id` / `created_at` / `previous_entry_hash`, and are appended under lock to
an fsync'd hash chain with a head manifest. Load verifies the chain, the head, and
every record's inclusion and content hash. A missing head manifest beside existing
records **fails closed**; entry `seq` is checked as a real integer, not coerced.

**Append authenticates.** A record is validated — signature, contract digest, item
context digest, and rater qualification against the trusted registry and keystore —
*before* acceptance. Without that, an unauthenticated spoof suppressed honest work:
append performed no signature check, so a record signed with any secret at all was
written, resolution dropped the victim, and the spoof was rejected only downstream.
The valid rating was already gone.

**Resolution validates first.** `effective_ratings` requires the verifier context
and drops invalid records *before* applying supersession, so an invalid or
unqualified correction can never remove anything. The order is the control: the
reverse order is what made the spoof effective. The verifier context is required
rather than optional for the same reason the `supersedes` parameter was removed —
an optional guard is a guard someone omits.

Rating identity is append-only-unique per `(item, rater)`. A second submission is
admissible **only** as an explicit correction naming a live record; corrections
append and the superseded record stays on disk. Replay needed this separate
constraint: `previous_entry_hash` sits *inside* the record, so the same rating at a
new chain position gets a different `record_id` and `O_EXCL` never collides.

**Intake is LEDGER-ONLY. The choice by sniffing is gone.** Production intake
used to pick between the ledger and a loose-JSON glob by looking for `head.json`
/ `ledger.jsonl` markers. The choice itself was the bug: deleting those two
markers downgraded a ledger to the glob path, ingested a flat directory of
record files with **no anchor and no problem reported**, and bypassed the
mandatory signed anchor entirely. Any marker-sniffing heuristic is bypassable by
removing the markers, so the choice was removed rather than the heuristic
improved. `ratings_dir` now means "here is a complete, anchored ledger";
anything less is refused, and a flat directory of individually valid records is
refused too (`LEDGER_INCOMPLETE`). There is no loose-JSON intake to fall back
to. `readiness.intake_mode` reports `ledger` or `no_ratings_dir`, so the mode is
observable rather than assumed.

**Every read/parse failure is a fail-closed diagnostic, never an exception.** A
hostile or corrupt ledger must refuse the package, not crash the build — a
single malformed, deleted or unreadable file would otherwise be a denial of
service. `_read_ledger_json` funnels every read failure (absent, unreadable,
non-UTF-8, malformed JSON, wrong shape) into one exception type, and
`load_intake` converts it into a readiness blocker with zero records accepted. A
record that is structurally corrupt but parseable — a container where
`rater_key_id` belongs, for instance — fails the whole intake closed rather
than being silently dropped beside accepted records. Failures are converted by
an explicit `INTAKE_FAILURE_TYPES` tuple, never a bare `except Exception`, so a
genuine defect in our own code still surfaces as a crash.

### 5b.4 Rollback needs an external anchor, and the anchor is REQUIRED

**The ledger alone is locally tamper-evident. It is not rollback-proof.**

Detected locally: overwritten record, deleted record, tampered record, orphan
record file, head/chain mismatch, missing head manifest, malformed head or entry
schema, replay, supersede of an unknown record, double-supersede.

**Not** detected locally: an operator who truncates `ledger.jsonl` **and** rewrites
`head.json` consistently. The result is a shorter, internally valid ledger, and no
amount of local hashing can distinguish it from a ledger that was always short.

An earlier revision of this document claimed truncation defense. That claim was
overstated: the evidence behind it truncated the log without also rewriting the
manifest.

So the anchor is no longer optional. **A nonempty ledger intake requires a
coordinator-signed external head anchor** (`goldset-ledger-anchor/v1`,
`--ledger-anchor` + `--anchor-secret-env`), and it is always verified. A missing,
unsigned, wrongly-signed or tampered anchor is a **readiness blocker**, and the
intake yields no ratings.

The check compares the entry at the **exact anchored position**, not merely counts.
A count-only check passes a fork that truncates and then re-appends: the length is
plausible and the local chain is consistent. Comparing position `entry_count`
against the anchored head is what reveals it. Tests cover truncation,
fork-with-extra-entry, wrong trust key, tampered count, unsigned anchor, and the
missing-anchor readiness blocker.

## 6. Reproduce

```bash
python3 research/goldset/build_labeling_package.py \
  --runs-root runs \
  --out research/goldset/labeling_package.json \
  --machine-truth-out research/goldset/machine_truth_WITHHELD.json \
  --boost-per-stratum 3 \
  --export-rater-bundle /tmp/rater-bundle

python3 research/goldset/test_labeling_package.py   # 251 standalone checks + 24 pytest
```

Expect `labeling_package_file_sha256 af040dd0471da40f5442e1b1bc3ee0c2efda5ddcad5dab429c90e8556f797d59`
and `readiness NOT_READY`. A differing digest means the source corpus changed;
re-pin before labelling.

## 7. Blockers — labeling must NOT start

**5 blockers. Each is first-class; none is a prerequisite of another.**

```
EFFECTIVE_CLUSTERS_BELOW_FLOOR: K_eff=13.33 < 20.0
CLUSTER_CONCENTRATION_TOO_HIGH: 16.9% > 5%
QUALIFIED_RATER_POOL_TOO_SMALL: have 0, need >= 3
ITEMS_WITH_ZERO_VALID_RATINGS: 183
REGISTRY: REGISTRY_ABSENT
```

### 7.1 Design blockers — a data campaign, not recruitment

| Quantity | Value | Target |
|---|---|---|
| Kish $K_{\text{eff}}$ | **13.33** | $\ge$ 20 |
| Max cluster concentration | **16.9%** | $\le$ 5% |

Even a perfectly balanced 20-cluster split reaches only **19.97**, so 20 raw
clusters cannot clear the floor at any concentration. **No amount of rater
recruitment fixes this.**

### 7.2 Context self-containment — was a blocker, now CLEARED, and how

Revision 7 reported `CONTEXT_INCOMPLETE_TOO_HIGH: 149/183 (81.4%)`. That was real: <!--hist-->
`MAX_TEXT_CHARS`/`MAX_OBS_CHARS` were **4000**, truncating prior tool arguments and
observations, so four items in five shipped with context the builder itself knew
was incomplete.

**Adding clusters would not have fixed it** — truncation is an export defect, not a
sampling one. The corpus is only **756 KB** in total, so truncating it was
gratuitous. Limits raised to 262 144 chars; measured maximum payload is 42 387.

Result: **{'COMPLETE': 183} — nothing truncated, verified by test in both
suites.** The gate remains in force at
`MAX_INCOMPLETE_CONTEXT_FRACTION = 20%` and
**any future item that truncates is excluded from delivery entirely** — a rater is
never asked to flag context we already know is missing.

**Standing requirement for the campaign:** new collection and export must produce
self-contained contexts. If a future corpus is large enough to truncate, the
correct response is to raise the limit or exclude the item, never to ship it and
rely on `INSUFFICIENT_CONTEXT`.

### 7.3 Rater blockers — external

Three qualified independent rater key IDs per item, from a **signed roster**
(`goldset-rater-registry/v1`, `key_id` + `qualified` only) plus a **separate
never-exported keystore** (`goldset-rater-keystore/v1`). Absent, unsigned, or
tampered roster yields an empty pool plus an explicit problem. A roster containing
secret material is rejected outright.

Every submission binds the **one canonical `rating_contract_digest`** plus its own
`item_context_digest`, and is HMAC-signed over the field list in 5b.2. An earlier
revision bound three digests including an `item_set_digest`; that function is gone,
because `package_digest` is stamped after readiness (binding to it was circular)
and the item-set digest duplicated what the contract digest already covers.
Altering the task instruction or any prior observation invalidates the record.
Duplicate or conflicting submissions from one `(item, rater)` **fail**; they never
collapse into a set. A correction is admissible only under 5b.2.

No substitute is permitted: not LLM judges, not the Analyst, not synthetic labels.

### 7.4 Internal — design FIXED, observations null until labels exist

The agreement **design** is decided and is not an open question (§5): the
statistic is **Gwet's AC1** over a declared universe of $q = 12$ categories, with a
**required CI half-width of 0.05** from a cluster bootstrap, and the
`acceptance_threshold` recorded **explicitly null** by Tutor's decision rather than
imported from a floor nobody publishes.

What remains null is the **observed** statistic and the **observed** interval.
Those are measurements, not choices: they cannot exist until labels do. Reporting
them as pending is not a missing decision.

An earlier revision of this section listed the design itself as "pending Tutor",
which contradicted §5 recording those parameters as decided.

### 7.5 Order

1. Run the data campaign (§7.1) — **~35–50 new distinct logical digests,
   $\le$ 5 % concentration, $K = \max(30,\; 96\rho)$ after an ICC pilot**
2. Verify the export is self-contained (§7.2) before anything ships
3. Re-cut; confirm the cluster **and** context gates clear
4. Execute the prechosen agreement design (§7.4): compute AC1 and its bootstrap
   interval against the required 0.05 half-width. The design is already fixed;
   this step measures against it and does not reopen the choice.
5. Publish the signed roster and provision the keystore
6. Recruit and qualify three raters
7. Label

Steps 1–3 are **prerequisites** for 6–7, not parallel to them.
