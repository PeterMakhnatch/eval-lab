Status: done
Last: merged as PR #122 (`76513a3`)
Next: none
Blockers: none

# M021 CLI-REGISTRY

Status: complete — ready for review
Last: converted `cli.py` dispatch to `set_defaults(func=...)`, then removed the dead
`if False:` attribution block and taught `repomap.py` to read the registry instead.
Next: nothing in this mission. Follow-up candidate recorded below (11 shifted
attributions in the generated command table).
Blockers: none.

## What landed

`src/evallab/cli.py` no longer dispatches through a linear string-comparison chain.
Every leaf command registers its handler with `set_defaults(func=...)`; `run_cli`
resolves `args.func(...)`. Handler bodies were moved, not rewritten.

Measured at `origin/main` (`d9dee45`) vs this branch:

| Metric | Before | After |
|---|---|---|
| `cli.py` lines | 2,192 | 2,456 |
| `set_defaults(func=` | 0 | 52 |
| `add_parser(` | 61 | 61 |
| `args.command ==` comparisons | 53 | 1 (a comment) |

## The load-bearing claim: behaviour is unchanged

The golden is `tests/golden/cli_surface.json`, generated from **`origin/main`'s own
`cli.py`** — the parser as it existed before this branch touched it — so it is evidence
rather than a self-portrait. It records all **62** parser nodes (61 subcommands plus the
root) and, per node, every argument: flags, dest, metavar, nargs, default, choices,
required, help string, type and action class.

```
$ uv run pytest tests/test_cli_registry.py -q
....................................................................  [100%]
68 passed
```

### A rendered-help golden was the wrong instrument, and CI caught it

The first version snapshotted `parser.format_help()` output. It passed on Python 3.12
and **failed on 3.14** in CI (`quality` job, run 32183395358): argparse changed its
help rendering between those versions, so the golden was pinning CPython's formatter
rather than this CLI's surface. Behaviour-preserving means the same commands, flags,
defaults and help strings — not the same line wrapping.

Replaced with the structural surface above, which is version-stable and strictly more
precise (it compares defaults and choices, which rendered help elides). Verified on
both interpreters:

```
$ uv run pytest tests/test_cli_registry.py -q                      # 3.12 -> 68 passed
$ uv run --python 3.14 pytest tests/test_cli_registry.py -q        # 3.14 -> 68 passed
```

`tests/golden/cli_help.json` is deleted rather than kept alongside: two goldens where
one is known version-fragile is how a suite starts getting skipped.

### Mutation evidence (integrator-run, independent of the authoring agent)

| Mutation | Result |
|---|---|
| rename one command (`tick` -> `tickk`) | 3 tests fail, incl. `Found unexpected commands outside golden: {'tickk'}` |
| change one flag's help string (`approve --actor`) | `test_every_command_surface_matches_golden[approve]` fails |

Restored -> 68 passed.

`tests/test_cli_registry.py` also asserts the registry is the only dispatch path: every
leaf command has a callable `func` default, the count of `set_defaults(func=...)` equals
the leaf count (52), and handlers share a uniform `(args, root, ...)` signature.

## The defect this mission nearly shipped

The conversion initially kept a 106-line `if False:` block in `run_cli` holding all
53 old `args.command == "..."` statements, commented "Static AST attribution mapping
for repomap". It existed because `repomap.py` discovers which module implements each
command by pattern-matching exactly those comparisons (`_compare_equals`,
`_command_keys`, `parse_cli_commands`). Without the block, `repomap check` still
passed, but every command would have been attributed to `cli` — the map would have
under-reported reachability.

That is unacceptable in this repo specifically: the generated map's reachability
signal is the tool used to find built-but-unreachable code, which was the defining
defect class of the last two waves (`parquet_compaction.py`, `lessons.py`,
`storm.py`, `status_generator.py`). Dead code retained to satisfy the detector makes
the detector lie.

**Fix:** `repomap.py` now reads the registry directly. `_registry_owners()` maps each
`add_parser("name")` variable to the handler named in its `set_defaults(func=...)`
and scores the implementing module from that handler. The `if False:` block is gone.

One real bug surfaced while doing it: `_called_names()` collects names from type
annotations as well as code, so handlers typed `harbor: HarborBackend` were being
attributed to `fetch` — 20 commands moved. `_body_names()` now excludes signature
annotations, so only the body decides.

### Attribution parity, measured

Generated command table on `origin/main` vs this branch: **84 commands both**,
**zero commands lost**, 11 attributions shifted.

```
`analyst list`  `paths`      -> `cli`
`analyst show`  `paths`      -> `cli`
`analyst`       `paths`      -> `cli`
`card validate` `paths`      -> `cli`
`card`          `paths`      -> `cli`
`digest`        `digest`     -> `gc`
`doctor`        `database`   -> `automation`
`nightly`       `gc`         -> `tracing`
`report`        `report`     -> `cli`
`tick`          `queue`      -> `automation`
`verdict`       `__version__`-> `fetch`
```

Stated honestly: this column is a heuristic, and it was already wrong on `main` in
places (`verdict` → `__version__` is not a module; `analyst` → `paths`). Some new
values are better (`doctor` → `automation` is where `HeadlessDoctor` lives), some are
worse (`tick` → `automation` where `queue` was right). Two alternative tie-breaks were
tried and measured: scoring through helper recursion gives 25 shifts, first-reference
order gives 20; body-name frequency gives these 11 and was kept. Making this column
exact would mean replacing the heuristic with real import-graph attribution — a
separate mission, recorded in `research/audits/board-notes.md`, not smuggled in here.

### Regression tests for the map itself

`tests/test_repomap.py` gains two tests, both mutation-verified by the integrator:

- `test_registry_dispatch_is_attributed_to_implementing_module` — deleting the
  `owners.update(_registry_owners(...))` line fails it.
- `test_handler_annotations_do_not_decide_attribution` — a handler annotated with two
  `fetch` types and one `status` call in its body must attribute to `status`;
  restoring `_called_names` (annotations counted) fails it 2-to-1.

## Gate

```
$ bash scripts/premerge.sh
1321 passed, 1 skipped, 1 xfailed
All checks passed!
Found 28 diagnostics
premerge green: Python 3.12; ty 28 <= 28
```

## Scope notes

- `cli.py` grew 264 lines. The conversion adds a handler `def` per command; the
  dispatch chain it replaced was denser but edited in one place by every new command.
- No split into a `cli/` package: handler bodies would have had to move across files
  to make it worthwhile, and this mission's contract was behaviour-preserving
  structure only. A split is now mechanical if wanted.
- `repomap.py` was unleased and is edited here deliberately, because the conversion is
  what breaks its assumption. Recorded in `research/audits/board-notes.md`.
