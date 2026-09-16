| source:policy | runs | mean score | iters | $/run | runs w/ drift | drift turns | salvaged | REPL errs | llm_query steps | write_file | sandbox open() | errors |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| bench:orchestrator-lenient | 22 | 1.00 | 4.0 | 0.058 | 1 | 1 | 13 | 1 | 7 | 0 | 0 | 0 |
| bench:stock | 18 | 1.00 | 5.4 | 0.060 | 10 | 24 | 0 | 1 | 0 | 0 | 0 | 0 |
| bench:stock-lenient | 24 | 1.00 | 4.0 | 0.046 | 0 | 0 | 32 | 2 | 4 | 0 | 0 | 0 |
| bench:stock-markers | 13 | 1.00 | 4.3 | 0.035 | 3 | 3 | 0 | 1 | 1 | 0 | 0 | 0 |
| bench:stock-markers-lenient | 18 | 1.00 | 3.8 | 0.037 | 0 | 0 | 1 | 1 | 1 | 0 | 0 | 0 |
| harbor:bridge | 4 | 0.67 | 5.5 | 0.048 | 3 | 8 | 0 | 0 | 0 | 3 | 1 | 1 |
| harbor:compact-bridge | 4 | 0.67 | 4.8 | 0.046 | 3 | 8 | 0 | 0 | 0 | 3 | 1 | 1 |
| harbor:orchestrator-bridge | 4 | 0.67 | 4.5 | 0.038 | 3 | 6 | 0 | 0 | 0 | 3 | 0 | 1 |
| harbor:orchestrator-tools | 3 | 0.67 | 7.0 | 0.074 | 2 | 9 | 0 | 1 | 0 | 2 | 3 | 0 |
| harbor:stock | 4 | 0.50 | 9.5 | 0.089 | 3 | 17 | 0 | 2 | 0 | 3 | 7 | 0 |
| harbor:tools-bridge | 2 | 1.00 | 7.5 | 0.061 | 2 | 6 | 0 | 0 | 0 | 0 | 4 | 0 |
| harbor:tools-lenient | 2 | 0.50 | 6.0 | 0.064 | 0 | 0 | 8 | 0 | 0 | 1 | 2 | 0 |

Drift kinds:
- bench:orchestrator-lenient: prose-without-code 1
- bench:stock: reasoning-marker-plus-code-label 8, prose-without-code 8, mirrored-history-format 4, preamble-plus-fenced-code 4
- bench:stock-markers: preamble-plus-fenced-code 2, code-marker-without-reasoning-marker 1
- harbor:bridge: preamble-plus-fenced-code 4, code-marker-without-reasoning-marker 2, prose-without-code 1, markers-present-but-malformed 1
- harbor:compact-bridge: prose-without-code 7, preamble-plus-fenced-code 1
- harbor:orchestrator-bridge: preamble-plus-fenced-code 2, mirrored-history-format 2, markers-present-but-malformed 1, prose-without-code 1
- harbor:orchestrator-tools: preamble-plus-fenced-code 3, markers-present-but-malformed 2, reasoning-marker-plus-code-label 1, mirrored-history-format 1, prose-without-code 1, code-marker-without-reasoning-marker 1
- harbor:stock: preamble-plus-fenced-code 9, code-marker-without-reasoning-marker 4, prose-without-code 3, reasoning-marker-plus-code-label 1
- harbor:tools-bridge: preamble-plus-fenced-code 3, code-marker-without-reasoning-marker 2, markers-present-but-malformed 1
