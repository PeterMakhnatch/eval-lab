# CM-18 results: first false-alarm rates on unseen staged sets

Preregistered in research-context `2bfff9f` before the sample was drawn. Three detectors, unchanged, one run each, on 946 runs from seven
permissive staged cross-test sets (hosted evaluation only; nothing in Laminar or Docent; data and facet prose stay in
`~/.local/state/evallab-cm18/`, outside git):

- **Cascade v8** as on main at the freeze (`git_head abbf68a14`, Stage 1 + Kimi `k3.1-pro-highspeed` judge): 946/946 replayed, 503 judge windows, 0 failed calls.
- **Facets**: Run Atlas extractor and prompt, reader `anthropic/claude-haiku-5-5` on the blinded whole run, alarm at `suspicion >= 2`: 946/946 read, all served and attested as Haiku, 33.8M input + 0.44M output tokens (cap 40M), $0. One read failed validation once (a word limit) and succeeded on a fresh invocation.
- **Union** = cascade or facets.

Sample: per set up to 60 positives and up to 60 negatives in each of two strata, **established** (solvable controls, fixed or human-clean runs) and **other** (no labelled tamper, not certified honest). Population sizes match LB-13 for every set. Recall is on positives, FAR on negatives, Wilson 95%.

## Per set

| Set (pos / est. neg / other neg sampled) | Detector | Recall | FAR, established | FAR, other |
|---|---|---|---|---|
| KernelBench-Hard (11 / 19 / 0) | cascade | 0/11 (0–26%) | 0/19 (0–17%) | – |
| | facets | 5/11 = 45% (21–72) | 0/19 | – |
| | union | 5/11 = 45% | 0/19 | – |
| Reflex-Sentinel (45 / 50 / 60) | cascade | 0/45 (0–8%) | 0/50 (0–7%) | 1/60 = 1.7% (0.3–8.9) |
| | facets | 45/45 = 100% (92–100) | 0/50 | 3/60 = 5.0% (1.7–13.7) |
| | union | 45/45 | 0/50 | 3/60 |
| Agent Cheat Index (60 / 60 / 60) | cascade | 14/60 = 23% (14–35) | 0/60 (0–6%) | 0/60 |
| | facets | 23/60 = 38% (27–51) | 0/60 | 4/60 = 6.7% (2.6–15.9) |
| | union | 32/60 = 53% (41–65) | 0/60 | 4/60 = 6.7% |
| EvilGenie (60 / 60 / 60) | cascade | 7/60 = 12% (6–22) | 0/60 | 0/60 |
| | facets | 44/60 = 73% (61–83) | 0/60 | 7/60 = 11.7% (5.8–22.2) |
| | union | 46/60 = 77% (65–86) | 0/60 | 7/60 = 11.7% |
| SDF shortcut-taking (60 / 60 / 60) | cascade | 2/60 = 3% (1–11) | 0/60 | 0/60 |
| | facets | 43/60 = 72% (59–82) | 4/60 = 6.7% (2.6–15.9) | 4/60 = 6.7% |
| | union | 43/60 = 72% | 4/60 = 6.7% | 4/60 = 6.7% |
| ResearchArena (21 / 20 / 0) | cascade | 7/21 = 33% (17–55) | 0/20 (0–16%) | – |
| | facets | 21/21 = 100% (85–100) | 2/20 = 10% (2.8–30.1) | – |
| | union | 21/21 | 2/20 = 10% | – |
| Beam tripwire (60 / 60 / 60) | cascade | 4/60 = 7% (3–16) | 8/60 = 13.3% (6.9–24.2) | 8/60 = 13.3% |
| | facets | 37/60 = 62% (49–73) | 0/60 (0–6%) | 3/60 = 5.0% (1.7–13.7) |
| | union | 38/60 = 63% (51–74) | 8/60 = 13.3% | 11/60 = 18.3% (10.6–29.9) |

## Pooled over the seven sets (sums of capped samples; runs within a task are not independent)

| Detector | Recall (positives) | FAR on established negatives |
|---|---|---|
| Cascade v8 | 34/317 = 10.7% (7.8–14.6) | 8/329 = 2.4% (1.2–4.7) |
| Facets, `suspicion >= 2` | 218/317 = 68.8% (63.5–73.6) | 6/329 = 1.8% (0.8–3.9) |
| Union | 230/317 = 72.6% (67.4–77.2) | 14/329 = 4.3% (2.6–7.0) |

For reference, Goodfire DEV: cascade v8 12.5% recall at 2.9% FAR; original union 18.8% at 4.3%.

## What this says

- **The cascade's false-alarm rate holds on unseen data; its recall does not.** FAR on established negatives is 0 on six of seven sets and 8/60
  on Beam, where every confirmed cascade alarm on a negative (16 runs, 8 established and 8 other) is a git-history or package-fetch rule (`git_history`, `git_object_by_sha`, `hb_git_history_browse`, `package_fetch`):
  Beam agents browse repository history on honest runs. The cascade recall is 11% pooled and 0–7% on Reflex, SDF, Beam, KernelBench,
  because it reads commands and those publishers' labels are test edits and checker-state changes the rules do not target.
- **Facets are a far better detector here than on Goodfire DEV,** with 69% recall at 1.8% FAR on established negatives. This does not transfer
  the Goodfire conclusion: these sets are heavily prompted, explicit episodes (conflicting tests, tamper-inviting tasks, an injected side task) whose positives are
  visible in the text, while Goodfire positives are mostly reasoning-channel mentions. Haiku's low Goodfire recall and this recall come from the same reader.
- **The union costs false alarms and gains little.** It adds 12 positives over facets alone (230 vs 218) and 8 more false alarms on established negatives
  (14 vs 6), all from Beam's cascade alarms. The union is clearly better than facets only on Cheat Index (53% vs 38%); on the other sets it adds 0 to 2 positives.
- **Facet false alarms are concentrated in particular sets.** SDF 4/60 on `fixed` runs, ResearchArena 2/20 on baselines, and, on the other stratum, EvilGenie 7/60,
  Cheat Index 4/60, SDF 4/60, Reflex 3/60, Beam 3/60. "Other" negatives are "no labelled tamper", not certified honest, so some may be real misses of the label.
- Exploratory (not preregistered as claims): at `suspicion >= 1` facets reach 93% recall but 17.6% FAR on established negatives, with ResearchArena baselines at 19/20, so that threshold is unusable here.
  CM-14's R2 (cascade or `suspicion == 3`) keeps FAR at 8/329 (the cascade's) and lifts recall to 118/317 = 37%.

## Limits

Each set's negatives come from its publisher's label. A set's positives are not a sample of real traffic: Reflex, Beam and ResearchArena prompt or invite the behaviour,
so recall here overstates what a monitor sees in deployment. Samples are capped (60) and Wilson intervals ignore task clustering; Positives from one task share a prompt.
The cascade ran at the production defaults with no change for the sets' step layouts; its Stage 1 was built on coding-agent shell traffic, and low recall on checker-state and test-edit labels is expected.
Facet reader recall depends on the reader (RA-6 κ 0.42–0.47 across readers); only Haiku was used. The tool's `score` command was edited after the runs only to clamp interval lower bounds at 0 and to add the per-rule alarm split; the frozen detectors and sample did not change.
