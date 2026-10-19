# sqlite-db-truncate: no test repair proposed

Task: terminal-bench-2-1 `tasks/sqlite-db-truncate` (package digest
`sha256:8d709a9557d79121fae98628658b5a5a025464577d828e3a7c28aa1a64f3c26a`).

Instruction: "Recover as many of the rows as possible, and create a JSON file in
/app/recover.json" in the format `[{"word": ..., "value": ...}, ...]`.

The test lists 8 expected rows (`testword02`..`testword09`) and passes when
`score > 6`, i.e. at least 7 of the 8 `(word, value)` pairs appear in the output.

Five line mutants of the reference `recover.json` pass the test: one drops
`testword09`, two blank one word, and two change one value. Each one leaves exactly
7 of 8 correct pairs.

Why this looks like intentional slack, not an oversight:

- The 4096-byte `trunc.db` cuts off inside `testword00`/`testword01`. The test
  leaves those two out, so the author knowingly scored only rows that can be recovered.
- Rows 02–07 store their values as 1-byte integers, but `testword08` (99.99) and
  `testword09` (0.5) store theirs as 8-byte IEEE doubles. A reasonable recovery
  that mis-decodes one double gets exactly one row wrong. A threshold of 7/8
  tolerates exactly that.
- The test uses an explicit count threshold instead of set equality, which
  is a deliberate choice.

"As many of the rows as possible" asks for best effort. It does not clearly
require all 8 rows, so under the repair rule (repair only when the instruction
clearly requires full recovery) no repair is proposed. If the maintainers do
want full recovery, the one-line change is `assert matches == soln_tuples`.
That change would kill all five recorded mutants: each leaves only 7 of the 8 pairs.

Consequence for the 2026-10-19 weak-test survey: the five survivors here should be
labelled "within intentional grader slack" rather than violations of an explicit
requirement. That would lower the TB2.1 confirmed-weak count from 4/18 to 3/18
(Wilson 95% 0.058–0.392).
