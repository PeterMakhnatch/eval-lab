# Trial and request budget (no prices)

OBSERVED — Counts below come from the in-process lab, with no Docker or model.
The family seeds are `depot-v4.1`, `dossier-v3.3` and `inquiry-v2.5`.
For each condition, run `uv run --no-sync python -m muster lab FAMILY --condition FACET-ARM --policy oracle --json`.
Sum `steps` over the real seats of each returned episode. The unit is a world API request, not a model call.
The command uses the default round schedule. Each condition contains 10 instances.
All 720 episodes across the 3 families and 4 arms per family ended with `all_done`.
The family adapters can change before landing. Recheck these counts against the approved task revision.

| Family | Facet | Probe requests/trial | Guard requests/trial | Solo requests/trial | Reader requests/trial |
|---|---|---:|---:|---:|---:|
| depot | calm | 17 | 17 | 47 | not an arm |
| depot | clash | 31 | 31 | 47 | not an arm |
| depot | skew | 72 | 72 | 92 | not an arm |
| depot | limbo | 38 | 37 | 98 | not an arm |
| depot | takeover | 44 | 43 | 53 | not an arm |
| depot | storm | 88 | 86 | 115 | not an arm |
| dossier | share | 17 | not an arm | 13 | 7 |
| dossier | scarce | 17 | not an arm | 13 | 7 |
| dossier | gone | 20 | not an arm | 16 | 10 |
| dossier | cascade | 22 | not an arm | 18 | 12 |
| dossier | dispute | 22 | not an arm | 16 | 10 |
| dossier | mix | 31 | not an arm | 22 | 13 |

| Family | Facet | Probe requests/trial | Fields requests/trial | Flagged requests/trial | Solo requests/trial |
|---|---|---:|---:|---:|---:|
| inquiry | plain | 18 | 18 | 18 | 11 |
| inquiry | month | 21 | 21 | 21 | 11 |
| inquiry | part | 21 | 21 | 21 | 11 |
| inquiry | range | 21 | 21 | 21 | 11 |
| inquiry | silent | 21 | 21 | 21 | 11 |
| inquiry | mixed | 21–22 (mean 21.6) | 21–22 (mean 21.6) | 21–22 (mean 21.6) | 11 |

Mixed probe, fields and flagged counts in instance order: 22, 21, 22, 21, 22, 22, 21, 22, 22, 21.
No lab adapter failed in this measurement. Every episode has reward 1.0.

| Family | Facet | Team requests/trial (range and mean over instances) |
|---|---|---:|
| depot | calm | 51 |
| depot | clash | 51–57 (53.4) |
| depot | skew | 100–104 (102.6) |
| depot | limbo | 102–108 (104.2) |
| depot | takeover | 57–59 (57.4) |
| depot | storm | 119–125 (121.2) |
| dossier | share | 59–65 (60.2) |
| dossier | scarce | 56–68 (59.0) |
| dossier | gone | 56–68 (62.0) |
| dossier | cascade | 64–76 (70.6) |
| dossier | dispute | 68 |
| dossier | mix | 90–96 (91.8) |

Inquiry has no team arm. The team counts describe the measured schedule, not every possible schedule.

## Planned trial counts

INFERRED — These counts assume every scheduled trial starts and no retry. They are not authorizations.
The oracle request totals are reference workload counts, not a cap on model requests.

| Cohort | Family | Instances × attempts × arms × facets | Trials | Oracle requests |
|---|---|---|---:|---:|
| pilot | depot | 10 × 1 × 2 × 4 | 80 | 4,800 |
| pilot | dossier | 10 × 1 × 2 × 3 | 60 | 1,100 |
| pilot | inquiry | 10 × 1 × 2 × 1 | 20 | 432 |
| pilot total | all 3 | | 160 | 6,332 |
| replication non-team | depot | 10 × 3 × 3 × 6 | 540 | 30,840 |
| replication non-team | dossier | 10 × 3 × 3 × 6 | 540 | 8,580 |
| replication non-team | inquiry | 10 × 3 × 4 × 6 | 720 | 13,104 |
| replication team | depot | 10 × 3 × 1 × 6 | 180 | 14,694 |
| replication team | dossier | 10 × 3 × 1 × 6 | 180 | 12,348 |
| replication total | all 3 | | 2,160 | 79,566 |
| smoke | all 3 | 10 × 1 × 4 × 18 | 720 | 26,522 |

The smoke is scripted, with no model. This measurement did not start a Docker smoke.
The replication is a separate cohort. It needs approval and a candidate gap from the pilot.

## Time and unknowns

OBSERVED — The built task files of each family have 1 distinct value of `[agent].timeout_sec`.
All 240 depot, 240 dossier and 240 inquiry task files set it to 900.0 s.
The lab has 1 real seat per non-team trial. The dossier team trials have 3 real seats.
Depot team trials have 2 real seats in takeover and storm, and 3 in the other facets.

INFERRED — A 900 s cap per real seat permits up to 20 agent-hours for 80 depot pilot trials.
The 60 dossier and 20 inquiry pilot trials permit up to 15 and 5 agent-hours, respectively.
The total pilot cap is 40 agent-hours. The replication cap is 705 agent-hours.
The cap is not the actual time. Team trials have more real seats and thus more aggregate agent time.
The depot oracle needs up to 88 world requests per real seat in a pilot trial (`storm-probe`) and 115 in `storm-solo`.
An agent that makes 1 request per turn then has about 10 s per turn under 900 s in `storm-probe`, and less in `storm-solo`.
A careful scripted clerk that reads the book and log around every write needed up to 206 requests in `storm-solo` (`REVIEWS.md`, round 3): about 4 s per request.
Decide the time cap before the Docker smoke; a time-out is a lost trial, not a 0.
Token counts per model request, calls per world request, failed calls, retries and actual model prices are unknown.
The lab cannot establish equal tokens, time, or dollar cost across arms.
