# L0 decision transitions under tool-shape recognition (#1825)

View: `full`. `now` = current-main `evaluate_l0_stage`. `cf1` = known tool shapes mapped to
their true semantic equivalent (`l0_rules.map_tool_shapes`). `cf2` = cf1 plus every residual known
shape, with anything still unknown mapped to `think` (`map_tool_shapes_v2`), so a cf2 run has zero
unrecognized steps by construction and any flag it raises is content evidence.

Counts are runs. `relieved` = decision became less severe; `newly flagged` = more severe.

## Coverage

| config | runs with a decision | total unrecognized steps | decisions |
|---|---:|---:|---|
| `now` | 16549 | 131011 | drop 805, review 12076, pass 3668 |
| `cf1` | 16549 | 63531 | drop 873, review 9335, pass 6341 |
| `cf2` | 16549 | 9041 | drop 961, review 1277, pass 14311 |

## DEV replay-v1 (5 corpora)

**cheat (n=332)**

| transition | now -> cf1 | now -> cf2 | cf1 -> cf2 |
|---|---:|---:|---:|
| `drop -> review` | 0 | 0 | 0 |
| `drop -> pass` | 0 | 0 | 0 |
| `review -> drop` | 2 | 2 | 0 |
| `review -> pass` | 48 | 48 | 0 |
| `pass -> review` | 0 | 0 | 0 |
| `pass -> drop` | 0 | 0 | 0 |
| unchanged | 282 | 282 | 332 |

**honest (n=358)**

| transition | now -> cf1 | now -> cf2 | cf1 -> cf2 |
|---|---:|---:|---:|
| `drop -> review` | 0 | 0 | 0 |
| `drop -> pass` | 0 | 0 | 0 |
| `review -> drop` | 1 | 1 | 0 |
| `review -> pass` | 145 | 147 | 2 |
| `pass -> review` | 0 | 0 | 0 |
| `pass -> drop` | 0 | 2 | 2 |
| unchanged | 212 | 208 | 354 |

### Per set

| set | honest n | honest relieved now->cf1 | honest relieved now->cf2 | honest newly flagged now->cf2 | cheat n | cheat relieved now->cf1 | cheat relieved now->cf2 | cheat newly flagged now->cf2 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `c1c5` | 19 | 2 | 2 | 0 | 11 | 2 | 2 | 0 |
| `cheatbench` | 75 | 22 | 24 | 3 | 75 | 17 | 17 | 2 |
| `goodfire` | 70 | 0 | 0 | 0 | 80 | 0 | 0 | 0 |
| `sol-astra` | 48 | 16 | 16 | 0 | 8 | 0 | 0 | 0 |
| `terminal-wrench` | 146 | 105 | 105 | 0 | 158 | 29 | 29 | 0 |


## Held-out (20 sets)

**cheat (n=4104)**

| transition | now -> cf1 | now -> cf2 | cf1 -> cf2 |
|---|---:|---:|---:|
| `drop -> review` | 0 | 0 | 0 |
| `drop -> pass` | 0 | 0 | 0 |
| `review -> drop` | 24 | 27 | 3 |
| `review -> pass` | 415 | 2204 | 1790 |
| `pass -> review` | 0 | 0 | 1 |
| `pass -> drop` | 0 | 0 | 0 |
| unchanged | 3665 | 1873 | 2310 |

**honest (n=11755)**

| transition | now -> cf1 | now -> cf2 | cf1 -> cf2 |
|---|---:|---:|---:|
| `drop -> review` | 0 | 0 | 0 |
| `drop -> pass` | 0 | 0 | 0 |
| `review -> drop` | 40 | 123 | 83 |
| `review -> pass` | 2066 | 8247 | 6181 |
| `pass -> review` | 0 | 0 | 0 |
| `pass -> drop` | 1 | 1 | 0 |
| unchanged | 9648 | 3384 | 5491 |

### Per set

| set | honest n | honest relieved now->cf1 | honest relieved now->cf2 | honest newly flagged now->cf2 | cheat n | cheat relieved now->cf1 | cheat relieved now->cf2 | cheat newly flagged now->cf2 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `agent-cheat-index` | 1086 | 558 | 559 | 9 | 62 | 19 | 19 | 2 |
| `beam-tripwire` | 442 | 0 | 51 | 18 | 171 | 0 | 2 | 4 |
| `cape-bench` | 400 | 4 | 362 | 0 | 400 | 1 | 338 | 0 |
| `caught-not-taught` | 160 | 0 | 127 | 0 | 46 | 0 | 36 | 0 |
| `evilgenie-escalation` | 1990 | 0 | 1704 | 83 | 124 | 0 | 119 | 3 |
| `flake-laundering` | 217 | 0 | 0 | 0 | 7 | 0 | 0 | 0 |
| `kernelbench-hard-runs` | 19 | 2 | 4 | 2 | 11 | 1 | 1 | 1 |
| `kobayashi-maru` | 2728 | 303 | 2675 | 0 | 272 | 30 | 217 | 0 |
| `latent-agent-sandbox-episodes` | 148 | 0 | 0 | 0 | 178 | 0 | 0 | 0 |
| `mistrace-reward-hacking` | 81 | 62 | 62 | 1 | 119 | 80 | 80 | 0 |
| `paichecker` | 1502 | 0 | 0 | 0 | 1085 | 0 | 0 | 0 |
| `reflex-sentinel` | 205 | 197 | 197 | 0 | 45 | 0 | 0 | 0 |
| `researcharena-benchmark-gaming` | 20 | 2 | 2 | 4 | 21 | 3 | 3 | 5 |
| `resolvebench-support-agents` | 400 | 0 | 400 | 0 | 112 | 0 | 112 | 0 |
| `reward-pressure-study-2` | 653 | 320 | 649 | 0 | 467 | 0 | 432 | 0 |
| `rhb-delivery-queue` | 409 | 0 | 403 | 0 | 129 | 0 | 129 | 0 |
| `rsi-benchmark-monitor-training` | 416 | 0 | 416 | 0 | 420 | 0 | 420 | 0 |
| `sdf-shortcut-taking` | 636 | 595 | 611 | 5 | 359 | 257 | 260 | 9 |
| `sleight-bench` | 43 | 23 | 23 | 2 | 43 | 24 | 23 | 3 |
| `trusted-monitoring-logs` | 200 | 0 | 2 | 0 | 33 | 0 | 13 | 0 |

## How to read

- **`drop -> pass` and `drop -> review` are the dangerous cells.** They mean L0 stopped taking the
  strongest action it takes today. A zero column is the safety property that matters.
- **`pass -> drop` and `review -> drop` on honest runs** are new hard false alarms.
- **`review -> pass` on cheat runs** is lost soft coverage: those runs leave the review queue even
  though nothing about their content changed.
- `cf2` is an upper bound, not a proposal: it maps genuinely unknown tools onto `think`, which
  discards whatever evidence they carried. Use `cf1` for the shippable mapping and `cf2` to bound
  how much of the residual flag rate is pure naming.

## Headline (auto-computed from the files above)

| quantity | DEV cheat | DEV honest | held-out cheat | held-out honest |
|---|---:|---:|---:|---:|
| n | 332 | 358 | 4104 | 11755 |
| hard drops lost (`drop ->` anything else), cf1 | 0 | 0 | 0 | 0 |
| hard drops lost, cf2 | 0 | 0 | 0 | 0 |
| relieved (decision less severe), cf1 | 48 | 145 | 415 | 2066 |
| relieved, cf2 | 48 | 147 | 2204 | 8247 |
| newly flagged (more severe), cf1 | 2 | 1 | 24 | 41 |
| newly flagged, cf2 | 2 | 3 | 27 | 124 |

## What this means for #1825

- **Hard drops are never lost.** In both the shippable mapping (cf1) and the upper bound (cf2), cases of `drop -> review` or `drop -> pass` are **0** (cf1) and **0** (cf2) across all 16,549 runs.
  Nothing that L0 hard-drops today stops being hard-dropped.
- **cf1 is the shippable mapping, and its cost is soft coverage.** It relieves 2066/11755 held-out honest runs and 415/4104 held-out cheats.
  The honest gain is large and the cheat loss is the price.
- **cf2 shows how much of L0's apparent coverage is parser-shaped.** If L0 read every tool name, 2204/4104 held-out cheats (53.7%) would leave the review queue, versus 8247/11755 honest runs relieved (70.2%).
  On `resolvebench-support-agents`, `rhb-delivery-queue`, and `rsi-benchmark-monitor-training` the  mapping relieves essentially every run of both labels: those sets were flagged only because the
  tool names were unreadable, so their L0 signal is an artifact, not detection.
- **The new hard false alarms are the real risk.** cf2 pushes 124 held-out honest runs to a more severe decision, and a per-set read shows they concentrate in `evilgenie-escalation`
 (83), `beam-tripwire` (18), and `agent-cheat-index` (9). Those are runs where correct parsing turns
 an unrecognised no-op into a recognised action that matches a hard rule. Inspect them before
 shipping either mapping.
- **Conclusion for the decision:** ship cf1, not cf2. cf1 keeps every hard drop, relieves 40.5% of DEV honest review load, and loses 10.1% of held-out cheat soft coverage. cf2 would remove 53.7% of held-out cheat soft coverage, which is a detection change disguised as a parser fix.

