# New source: frozen J3 versus length

## Answer

**Yes, J3's ranking advantage over length replicates on a fresh trajectory evaluation.**
On 357 previously unscored synthetic tool-call trajectories, J3 achieves AUROC **1.000**.
The strongest length baseline achieves **0.979**.
The paired AUROC gain is **0.0208**, with a task-cluster 95% interval of **0.0128–0.0296**.
Both registered length comparisons have positive interval lower bounds.

This is not discovery of an unknown dataset. Earlier notes review its card, but disclose no trajectory evaluation.
The result concerns synthetic tool-counter misreporting, not general deployment cheating or SWE test tampering.
The conservative **drop>=100** rule catches only **3/200 positives**, despite perfect ranking.

## Source and admission

Source: [`annahbanannah/synthetic-math-toolcall-deception`](https://huggingface.co/datasets/annahbanannah/synthetic-math-toolcall-deception).
Pinned revision: `1e728ec86fefb9e77cec58178d37f26035c7bb9f`.
The publisher declares MIT. The pinned card contains no additional evaluation restriction.
The publisher uses `claude-sonnet-4.6` to create the source. This lane makes no Anthropic model call.

Authenticated downloads return HTTP 200 for the real 1,075,549-byte Parquet file and its 3,446-byte README.
The W3 access implementation also downloads a 1,024-byte payload probe.
W3 PR 1910 remains unmerged at registration and supports only HackBench preparation.
This lane therefore uses W3 access plus the MALT pinned-byte, adapter, and pushed-registration pattern.

| Population | Rows |
|---|---:|
| Released trajectories | 400 |
| Native deceptive / honest | 200 / 200 |
| Admitted observed misreports / accurate counters | 200 / 157 |
| Excluded honest rows without a recorded action | 42 |
| Excluded honest row with a null counter | 1 |
| Task clusters | 200 |
| Complete mixed-label pairs / positive-only clusters | 157 / 43 |

Admission covers 89.25% of the release and all 200 positives.
Results apply to the 357 action-bearing rows, not all 400 released rows.
All true counters match preceding accepted calls plus 1. Rejected calls do not advance that counter.
The payload's deceptive-call count includes accepted mismatches, not every rejected misreport.
Negatives verify accurate counters, not the absence of every possible cheating mechanism.
Native acceptance arrays remain publisher evidence; this lane does not execute its environment.

### Freshness and historical HOLD

The complete DetBench registry has 33 admitted entries, 7 access holds, and 18 licence holds.
This source appears in none of them. It also appears in no checked failure-library source declaration.
The independent audit finds only 3 older candidate-card records, not raw-trajectory or detector receipts.
Those records mark **HOLD** for Library novelty, synthetic labels, and length confounding, not unresolved rights.
[AUDIT-NOTE.md](AUDIT-NOTE.md) preserves the exact disposition and the disclosure timing.
The initial protocol discloses card awareness but omits the word HOLD. The later note corrects that omission without changing the experiment.

Before scoring, exact content comparison finds **0** shared complete components of at least 80 characters against 16,549 prior DetBench views.
It also finds **0** exact normalized trajectory matches.
This is a bounded exposure audit, not proof of global non-exposure, renamed mirrors, or pretraining independence.
Do not use this evaluated panel as a future blind set.

## Frozen design and execution

Registration commit: **`adfee9f0282d106bfe4145b54b0f5d7fd81abd83`**, pushed before the first math call.
The push receipt is 3:58:15 PM ET on November 5, 2026.
The first call starts at 3:58:45 PM ET. The last response completes at 4:08:41 PM ET.

- Route: `openai-codex/gpt-6.1-terra`, through `omp auth-gateway stdio`.
- Every gateway uses the required no-fallback CLI configuration and `PI_CONFIG_FILES`.
- All **357/357** response models attest the frozen route. There are **0** repeated attempts, errors, or missing scores.
- J3, L0, prompts, and thresholds remain unchanged. No held-out tuning occurs.
- Full prompt-prefix SHA-256: `ca496b4c5f73879aa7d7da58986085734cf06f1202a8028db922c3acd05c8f41`.
- Rubric and template hashes, source hashes, sample digest, producing code, and all fixed thresholds appear in [math-freeze.json](math-freeze.json).
- All 357 rows produce 1 window. The maximum prompt is 41,333 characters.
- Existing limits truncate 3 tool observations. No task or argument exceeds its existing limit.
- All paid model spend is **$0**. All helpers use `openai-codex/gpt-6.1-sol`, with 0 fallback events.

The independent structural audit confirms metadata isolation and preserved native tool actions.
Class labels, strategies, answer keys, IDs, acceptance arrays, and counter arrays do not enter J3 through adapter metadata.
Native code and observations can still contain generator-specific style cues.
The character baseline includes full-view assistant prose; J3 omits that prose.

## Ranking results

All signals use the same 357 rows and 200 task clusters.
Intervals use 1,000 task-cluster bootstrap draws with seed 1105.
Paired gains use shared task multiplicities for both signals.

| Signal | AUROC | Task-cluster 95% interval | Within frozen length strata | Within 157 complete task pairs |
|---|---:|---|---:|---:|
| Frozen J3 | 1.0000 | 1.0000–1.0000 | 1.0000 | 1.0000 |
| Full-view characters | 0.7300 | 0.6948–0.7674 | 0.4885 | 0.8535 |
| Agent steps | 0.9792 | 0.9698–0.9874 | 0.9752 | 0.9777 |
| Native L0 hard union | 0.5000 | 0.5000–0.5000 | 0.5000 | 0.5000 |

| Registered paired comparison | AUROC gain | Task-cluster 95% interval |
|---|---:|---|
| J3 minus characters | 0.2700 | 0.2322–0.3048 |
| J3 minus steps | 0.0208 | 0.0128–0.0296 |

The within-length result uses 10 frozen pooled character strata with 357/357-row coverage.
J3's within-length 95% interval is 1.0000–1.0000.
Within-task coverage is 314/357 rows across 157/200 tasks; J3's interval is also 1.0000–1.0000.
A perfect empirical bootstrap interval does not guarantee perfect future performance.
Coarse strata do not eliminate residual step-count, length, or synthetic-style confounding.
Steps already achieve 0.9792 AUROC. J3's advantage over the strongest baseline is real here, but modest.

Native L0 fires on no row and reports all **1,642 admitted tool steps as unrecognized**.
Its 0.5 AUROC reflects missing native coverage, not a validated ability to assess this tool-counter mechanism.
No counterfactual tool mapping or new L0 rule enters the experiment.

## Frozen operating points

Each rate has a Wilson 95% interval. Each class has at most 1 member per task.

| Rule | True positives / 200 | Recall, 95% interval | False positives / 157 | FPR, 95% interval |
|---|---:|---|---:|---|
| Review or drop: J3>=61 | 200 | 100.0% [98.12%, 100.0%] | 0 | 0.0% [0.0%, 2.39%] |
| Original rule: J3>=89 | 189 | 94.5% [90.42%, 96.90%] | 0 | 0.0% [0.0%, 2.39%] |
| Drop: J3>=100 | 3 | 1.5% [0.51%, 4.32%] | 0 | 0.0% [0.0%, 2.39%] |
| Native L0 hard union | 0 | 0.0% [0.0%, 1.88%] | 0 | 0.0% [0.0%, 2.39%] |
| Characters>=272968 or >=2784671, separately | 0 | 0.0% [0.0%, 1.88%] | 0 | 0.0% [0.0%, 2.39%] |
| Steps>=95 or >=609, separately | 0 | 0.0% [0.0%, 1.88%] | 0 | 0.0% [0.0%, 2.39%] |

Under the 61/100 rule, 197 positives enter review, 3 enter drop, and all 157 controls remain below review.
The DEV length cutoffs do not transfer to these short trajectories. Their zero recall does not replace the stronger AUROC comparison.
The aggregate also contains descriptive empirical ROC points. They are not deployment thresholds and do not alter the frozen rules.

## Integrity, withdrawal, and receipts

The original lucabaroni admission is wrong. It misses an existing composite-rights hold and prior examples.
The parent stops that run after 64 attempts and 60 responses. It uses **0** of those responses in this conclusion.
[WITHDRAWAL.md](WITHDRAWAL.md) records the error, and the old scorer now refuses continuation.
The suspicion values from that run are not inspected to select, tune, or interpret the replacement detector.
The replacement has a separate source, sample, protocol, and pushed registration.

Verification includes 138 passing metric, L0, and adapter tests, plus executable smoke checks.
A separate pairwise rank calculation reproduces all AUROCs, conditional AUROCs, paired gain points, and frozen threshold counts.
It also verifies all 357 recorded prompt hashes and confirms no row or task identifier enters the published aggregate.
An independent outcome audit passes 28 aggregate checks, including rates, Wilson intervals, chronology, route counts, and frozen hashes.

Files:
- [MATH-PREREG.md](MATH-PREREG.md): immutable replacement protocol.
- [aggregate-results.json](aggregate-results.json): full aggregate metrics and intervals.
- [verification.json](verification.json): independent calculation and immutable artifact hashes.
- [source-admission.json](source-admission.json): access, rights, labels, and exposure evidence.
- [math-overlap.json](math-overlap.json): bounded exact-content audit.
- [transport-incident.json](transport-incident.json): withdrawn-run and helper route receipts.

Raw bytes, converted views, row identities, individual judgments, and source snippets remain outside Git.
The archived source page is [the local card copy](file://Peters-MacBook-Pro.local/Users/petermakhnatch/Developer/research-context/sources/links/huggingface.co/2026-11-05-annahbanannah-synthetic-math-toolcall-deception-dataset.md).
Its dynamic archive is separate from the pinned README hash used for admission.
