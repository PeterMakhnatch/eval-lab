| source:policy | runs | mean score | iters | $/run | runs w/ drift | drift turns | salvaged | REPL errs | llm_query steps | write_file | sandbox open() | errors |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| bench:stock | 12 | 1.00 | 5.5 | 0.051 | 7 | 19 | 0 | 0 | 0 | 0 | 0 | 0 |
| bench:stock-lenient | 14 | 1.00 | 4.0 | 0.034 | 0 | 0 | 19 | 1 | 0 | 0 | 0 | 0 |
| harbor:bridge | 3 | 0.50 | 4.7 | 0.045 | 2 | 4 | 0 | 0 | 0 | 2 | 0 | 1 |
| harbor:compact-bridge | 3 | 0.50 | 5.0 | 0.050 | 2 | 7 | 0 | 0 | 0 | 2 | 0 | 1 |
| harbor:orchestrator-bridge | 2 | 1.00 | 3.5 | 0.028 | 1 | 2 | 0 | 0 | 0 | 1 | 0 | 1 |
| harbor:stock | 3 | 0.67 | 8.0 | 0.068 | 2 | 11 | 0 | 2 | 0 | 2 | 6 | 0 |

Drift kinds:
- bench:stock: prose-without-code 8, reasoning-marker-plus-code-label 6, mirrored-history-format 4, preamble-plus-fenced-code 1
- harbor:bridge: preamble-plus-fenced-code 2, prose-without-code 1, markers-present-but-malformed 1
- harbor:compact-bridge: prose-without-code 7
- harbor:orchestrator-bridge: markers-present-but-malformed 1, mirrored-history-format 1
- harbor:stock: preamble-plus-fenced-code 6, code-marker-without-reasoning-marker 3, prose-without-code 2
