# Ticket CALC-311: spreadsheet recalculation is wrong and slow

`/app/sheet` is the calculation core of a small spreadsheet product. Users
report stale totals after editing cells inside a `SUM` range, cells stuck on
`#CYCLE` after a circular reference was fixed, and edits that freeze on large
sheets. Profiling shows some formulas are evaluated many times per edit.

Rewrite the recalculation in `/app/sheet/engine.py` so that `Sheet` meets the
contract below. `/app/sheet/formula.py` (parsing and evaluation) is correct;
do not change it. The grader runs your `engine.py` against its own pristine
copy of `formula.py`, so `engine.py` must be self-contained: it may import
only the Python standard library and `formula` from its own package.

## API

- `Sheet.set(cell, text)` – set one cell. `text` is
  - a formula: `=` followed by an expression `formula.parse` accepts;
  - a number matching `-?\d+(\.\d+)?`;
  - or `""`, which clears the cell.
- `Sheet.set_many(edits)` – apply a `dict` of `cell -> text` as one atomic edit.
- `Sheet.get(cell)` – current value: a float, an error string (`#CYCLE`,
  `#DIV/0!`), or `0.0` for an empty cell.
- `Sheet.text(cell)` – the text last set for the cell, or `""`.

Cell names are `A1` … `Z999`. Any invalid cell name, number or formula makes
`set` / `set_many` raise `ValueError` and leave the sheet completely unchanged.
For `set_many` this applies to the whole batch.

## Values

- A formula's value is `formula.evaluate(cell, parsed, lookup)`, where
  `lookup(name)` returns the current value of that cell, or `None` if it is
  empty. Errors propagate through `evaluate`.
- A formula cell that lies on a dependency cycle (including a cell that refers
  to itself) has the value `#CYCLE`. Cells that merely read a cycle member get
  `#CYCLE` through normal propagation. When an edit breaks a cycle, every
  former member must get its correct value again.
- Ranges such as `SUM(A1:A5)` depend on every cell in the range, including
  cells that are empty right now.

## Recalculation contract (this is graded exactly)

Values must be fully up to date when `set` / `set_many` returns. `get` must
never evaluate anything. For each successful `set` / `set_many` call:

1. The **affected** cells are the edited cells that now hold formulas, plus
   every formula cell that reads, directly or transitively, any edited cell.
2. Every affected cell that is **not** on a cycle is evaluated exactly once,
   by calling `formula.evaluate` with its true cell name, after all of its
   inputs have their final values for this edit.
3. Cells on a cycle are not evaluated. Cells that are not affected are not
   evaluated.

A failed (`ValueError`) call evaluates nothing.

The engine must handle sheets with dependency chains at least 10,000 cells
deep and cells read by thousands of formulas within a few seconds per edit.

Tests in `/app/tests` cover a little of this; the grader checks much more.
