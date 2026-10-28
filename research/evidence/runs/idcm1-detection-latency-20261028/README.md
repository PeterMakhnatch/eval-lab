# ID-CM-1: detection latency of the current detectors on replayed runs

Live mode (the best combination on live runs through the trace harness, alarms routed by the Live Supervisor) is not possible yet: the harness's cascade
detectors are stored-result views with no streaming path, and no Live Supervisor module exists on main. The item's fallback applies: measure how many agent
turns pass before the first alarm on replayed runs. No model call, no spend.

`tools/latency.py` reads the stored causal v8 cascade replays (Goodfire and CheatBench DEV runs, loaded through the harness's `resolve_runs` and DEV gate;
HackBench, CM-15 rows; the seven CM-18 staged sets), the stored facet records and each run's step list. `results/latency.json` holds aggregates only.
Preregistered in research-context `causal-prefix-monitor.md` (ID-CM-1, `31cc7bd`). Results: `RESULTS.md`.

Definitions: time is agent turns from run start. A cascade alarm is a confirmed card or a `grader_tamper` pass-through; its time is the turn count when the card's
`card_prefix` (the last step ingested when the verdict became available) arrived. The facet reader sees the whole run, so its alarm time is the end of the run.
The union alarms at the cascade's time or, when only the facet reader alarms, at the end of the run.
