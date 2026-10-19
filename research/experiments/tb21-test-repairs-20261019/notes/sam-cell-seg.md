# sam-cell-seg: bounding-box columns are never checked

Task: terminal-bench-2-1 `tasks/sam-cell-seg` (package digest
`sha256:57fe4ee6d3c51f10ef60aa9390a3def172b1002fd2a89de68bcbf6c41fe5a719`).
Patch: `../patches/sam-cell-seg.patch`. It is append-only; the only other change is
the newline the original file was missing at its end.

## Gap

The instruction defines `xmin, xmax, ymin, ymax` as the upper-left and
lower-right corners of each mask. It requires the output CSV to have "updated
xmin, xmax, ymin, ymax, coords_x, and coords_y columns". The tests check shape,
columns and polyline geometry (`coords_x`/`coords_y`), but they never compare the
four corner columns with the polyline. Mutant `175edfb9e4` (`x + w` becomes `x`)
passed while writing `xmax = xmin` for every mask.

## Repair

`test_bbox_columns_match_polylines` was added. For each output row, each corner
column must be within 1 px of the polyline's min/max extent. The tolerance allows
both inclusive and exclusive pixel conventions: the reference
`correct_output.csv` uses `xmax = max(coords_x) + 1` (from
`cv2.boundingRect`), and the input metadata uses `xmax = max(coords_x)`.
Before the native run, all 32 rows of `correct_output.csv` were checked and pass.

## Qualification (native Harbor; MobileSAM on CPU)

- solve.sh oracle: reward 1. Full reconstruction: reward 1. nop: reward 0.
- Recorded survivor `175edfb9e4`: now reward 0.
- In the oracle trial's verifier output, all 9 original tests and the new test passed.
- Round 2: the same seeded 8 mutants left 1 survivor, `25f5b35db4`. It runs one more
  refinement iteration (`range(3)` becomes `range(4)`). The instruction does not
  fix the iteration count, and every graded mask property still holds, so this is
  an allowed variant and needs no repair.
