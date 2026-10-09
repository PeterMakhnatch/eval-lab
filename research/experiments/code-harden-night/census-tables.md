# Code-harden-night static census

Snapshot tasks scanned: 2698; non-Python fleet: 1519.

## 1. Language recount (task.toml [metadata] category)

| Language | Tasks |
|---|---|
| Python | 1179 |
| Go | 722 |
| JavaScript | 388 |
| TypeScript | 166 |
| Unknown | 130 |
| Ruby | 25 |
| PHP | 23 |
| Java | 22 |
| C++ | 13 |
| C | 7 |
| Rust | 7 |
| Scala | 6 |
| Kotlin | 3 |
| Dart | 3 |
| Lua | 1 |
| Elixir | 1 |
| Swift | 1 |
| Svelte | 1 |

## 2. Grading-time downloads, non-Python fleet (extends mimo-verify-network-dep)

Mode key: **unconditional** = bare fetch in the grading path, breaks under the egress lock; **conditional** = guarded by a cache check (`if [ ! -d … ]`, `||`, `command -v`), offline-safe when baked; **offline-flag** = hermetic flag on the invocation (`GOPROXY=off`, `mvn -o`, `cargo --offline`, `-mod=vendor`). Counts are tasks with >=1 match of that mode for the tool.

| Tool | Unconditional | Conditional | Offline-flag |
|---|---|---|---|
| apt | 9 | 9 | 0 |
| bundler | 4 | 0 | 0 |
| cargo | 8 | 0 | 0 |
| composer | 6 | 3 | 0 |
| curl-fetch | 1 | 16 | 0 |
| go-get | 6 | 2 | 0 |
| go-install | 1 | 1 | 0 |
| go-mod | 12 | 1 | 1 |
| gradle | 6 | 4 | 0 |
| julia-pkg | 2 | 1 | 0 |
| maven | 14 | 1 | 2 |
| npm | 47 | 31 | 0 |
| pip | 11 | 5 | 0 |
| yarn/pnpm | 36 | 8 | 22 |

Non-Python tasks with >=1 unconditional grading fetch: **154** (prefetch-port candidates; dynamic-C4 truth still needs the locked nop census).

| Category | Tasks with unconditional fetch |
|---|---|
| Go | 21 |
| JavaScript | 57 |
| TypeScript | 25 |
| Unknown | 20 |
| Ruby | 4 |
| PHP | 6 |
| Java | 13 |
| C++ | 0 |
| C | 0 |
| Rust | 6 |
| Scala | 0 |
| Kotlin | 2 |
| Dart | 0 |
| Lua | 0 |
| Elixir | 0 |
| Swift | 0 |
| Svelte | 0 |

## 3. Rust toolchain-PATH screen (static candidates)

- path-missing-candidate: 7

- format-code-task-000681: path-missing-candidate
- format-code-task-000797: path-missing-candidate
- format-code-task-001083: path-missing-candidate
- format-code-task-001540: path-missing-candidate
- format-code-task-001649: path-missing-candidate
- format-code-task-001652: path-missing-candidate
- format-code-task-002548: path-missing-candidate

## 4. Unknown-130 triaged by grading runner

| Runner bucket | Tasks |
|---|---|
| usercase | 65 |
| lint-mypy | 15 |
| pytest | 7 |
| python | 6 |
| julia | 4 |
| go-test | 3 |
| npm-run | 3 |
| jest | 3 |
| node | 2 |
| forge | 2 |
| bats | 2 |
| custom | 2 |
| cargo-test | 2 |
| make | 2 |
| tap | 1 |
| nodeunit | 1 |
| wake | 1 |
| ansible-test | 1 |
| tsx | 1 |
| dart | 1 |
| mocha | 1 |
| karma | 1 |
| gradle-runner | 1 |
| shelltest | 1 |
| sbt | 1 |
| nim | 1 |

### Task ids per bucket

- ansible-test: format-code-task-000388
- bats: format-code-task-000567, format-code-task-002966
- cargo-test: format-code-task-000798, format-code-task-002793
- custom: format-code-task-000740, format-code-task-002480
- dart: format-code-task-000790
- forge: format-code-task-000277, format-code-task-000638
- go-test: format-code-task-000078, format-code-task-001738, format-code-task-002947
- gradle-runner: format-code-task-002472
- jest: format-code-task-002318, format-code-task-002319, format-code-task-002330
- julia: format-code-task-000020, format-code-task-000130, format-code-task-001667, format-code-task-002591
- karma: format-code-task-001678
- lint-mypy: format-code-task-002428, format-code-task-002429, format-code-task-002430, format-code-task-002432, format-code-task-002433, format-code-task-002434, format-code-task-002435, format-code-task-002436, format-code-task-002437, format-code-task-002438, format-code-task-002439, format-code-task-002440, format-code-task-002441, format-code-task-002442, format-code-task-002444
- make: format-code-task-001802, format-code-task-002471
- mocha: format-code-task-001468
- nim: format-code-task-002740
- node: format-code-task-000123, format-code-task-000764
- nodeunit: format-code-task-000073
- npm-run: format-code-task-000280, format-code-task-001937, format-code-task-001968
- pytest: format-code-task-001520, format-code-task-001908, format-code-task-001986, format-code-task-002715, format-code-task-002716, format-code-task-002717, format-code-task-002720
- python: format-code-task-000391, format-code-task-000741, format-code-task-001339, format-code-task-002528, format-code-task-002620, format-code-task-002621
- sbt: format-code-task-002682
- shelltest: format-code-task-002652
- tap: format-code-task-000064
- tsx: format-code-task-000630
- usercase: format-code-task-000303, format-code-task-000361, format-code-task-000362, format-code-task-000381, format-code-task-000382, format-code-task-000394, format-code-task-000395, format-code-task-000462, format-code-task-000463, format-code-task-000464, format-code-task-000468, format-code-task-000476, format-code-task-000485, format-code-task-000486, format-code-task-000535, format-code-task-000537, format-code-task-000607, format-code-task-000629, format-code-task-000673, format-code-task-000676, format-code-task-000820, format-code-task-000829, format-code-task-000885, format-code-task-000893, format-code-task-000913, format-code-task-000950, format-code-task-001190, format-code-task-001195, format-code-task-001201, format-code-task-001214, format-code-task-001335, format-code-task-001336, format-code-task-001350, format-code-task-001424, format-code-task-001448, format-code-task-001524, format-code-task-001525, format-code-task-001589, format-code-task-001599, format-code-task-001669, format-code-task-001679, format-code-task-001684, format-code-task-001696, format-code-task-001784, format-code-task-001843, format-code-task-001900, format-code-task-001935, format-code-task-002088, format-code-task-002108, format-code-task-002130, format-code-task-002252, format-code-task-002253, format-code-task-002365, format-code-task-002469, format-code-task-002518, format-code-task-002541, format-code-task-002598, format-code-task-002673, format-code-task-002834, format-code-task-002842, format-code-task-002849, format-code-task-002877, format-code-task-002997, format-code-task-003051, format-code-task-003054
- wake: format-code-task-000291

