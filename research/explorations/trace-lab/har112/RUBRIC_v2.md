# HAR-111 task-soundness rubric v2 (tightened after the 20/30 agreement audit)

The question is unchanged: **would a competent engineer who reads only the instruction, and can explore the repository, write a fix that passes the hidden tests?**

For one task, read `instruction.md`, then the hidden tests (the test patch and the test runner). The environment Dockerfile and the repo at the base commit may be consulted to see what already exists. List every behaviour the hidden tests require, and check each one against the instruction.

## Labels (unchanged)

- **sound:** every behaviour the tests check is stated in the instruction, clearly implied by it, or already fixed by the existing code or public API. This includes names, signatures, exception types, messages, return types and file paths. A careful engineer would pass.
- **suspect:** at least one checked behaviour is unstated but plausibly guessable. For example: a natural exception type, a conventional return type, or a sibling function the instruction's wording could cover. A careful engineer might well miss it.
- **broken:** at least one checked behaviour cannot reasonably be inferred from the instruction plus the repo. For example: an exact error-message string, an extra API or contract the instruction never mentions, a specific file, output or line format, or tests that contradict the instruction. A correct fix to the stated problem would still fail.

Not in scope: answer leaks (e.g. the fix being installable from PyPI), difficulty, and environment or build breakage. Record those in `notes` only.

## Decision procedure (new — this is what v1 left vague)

For each checked behaviour, classify it in order. The first rule that fires decides; severity still drives the label (any `not_inferable` item makes the task broken; else any `guessable` item makes it suspect; else sound). Item count never changes the label: one `guessable` item is suspect, five `guessable` items are still suspect.

**R1 — Repo-given facts are covered, never items.** Names, signatures, parameters, file paths and behaviours already present in the repo at the base commit are *given*, not unstated — the engineer is assumed to have explored the repo, and the labeler MUST check the repo before listing such an item. In particular, "preserving their public API" covers exact signature spellings (e.g. an existing `**kwargs` must be kept verbatim). If you did not verify the repo, you may not list the item.

**R2 — Siblings are items; grep decides severity.** A hidden test on a function, method, option value or config variant the instruction never names is always an `unstated` item. It is `guessable` iff (a) grepping the repo for the named entity's core pattern surfaces the sibling (same `def`/table/call-site family), AND (b) the identical or directly analogous change fixes it with no new semantics. Otherwise it is `not_inferable`. New semantics that fail this test include index-based subsetting/slicing logic, new constructor parameters, new filtering/dedup criteria, and new APIs or flags.

**R3 — Preservation checks are covered.** A hidden test that guards pre-existing behaviour the base repo already satisfies, and that any correct fix of the stated bug preserves without extra work, is covered (sound), not an item — provided the labeler verifies the behaviour pre-exists. (A general fix that keeps working code working needs no instruction sentence per preserved case.) This does NOT apply to siblings that are broken at base (those fall under R2).

**R4 — Quoted/linked specs cover only what they say.** If the instruction quotes spec/grammar rules or links the normative grammar, only the quoted/linked content counts as stated. Serialization formats, code values and literal spellings NOT in that text are items. Severity is `not_inferable` when the quoted text admits two or more reasonable implementations (write down a second one — if you can, it is not inferable); `guessable` when the quoted/linked text plus repo determines the answer up to a single convention (e.g. further rows of the same linked options table).

**R5 — Exact strings and channels.** Any hidden test requiring specific message/file/column substrings in output, or a specific output channel (stdout vs stderr), beyond the instruction's stated words, is an item. It is `guessable` iff every required substring is either stated in the instruction or produced by running the underlying tool the instruction names (channel choice alone is then conventional and at most `guessable`, never `not_inferable`). Otherwise it is `not_inferable`: a minimal fix that exits non-zero / reports failure with a generic message would still fail the test.

**Tie-breaks.** Between `guessable` and `not_inferable`, apply the minimal-fix test literally: write down the minimal correct fix of exactly what the instruction asks (plus mechanical propagation per R2). If it would pass the hidden test, the item is `guessable`; if it would fail, `not_inferable`. Between sound and suspect — i.e. "clearly implied" vs "merely guessable" — default to suspect and list the item: sound means no doubt.

## Output: one JSON object per task (unchanged, plus optional `names`)

```
{"task_id": "...", "label": "sound|suspect|broken",
 "unstated": [{"what": "...", "test_ref": "file:line or test name", "quote": "<=200 chars",
               "severity": "guessable|not_inferable", "names": ["optional identifiers/paths the fix must provide"]}],
 "instruction_covers": ["short list of what the instruction does state that the tests check"],
 "notes": "...", "read_minutes": n}
```

Severity drives the label: any `not_inferable` item makes the task broken; else any `guessable` item makes it suspect; else it is sound.

## Worked examples (from the HAR-111 disagreements)

1. **candidate-1634 → sound (R1).** The hidden test asserts the exact string `def atomic(self, transaction_type=None, **kwargs):` in the vendored source. That signature pre-exists in the repo and the instruction says "preserving their public API". Per R1 the labeler must check the repo; verified pre-existing, it is covered, not an item. No items → sound.
2. **format-code-task-000927 → broken (R4).** The instruction quotes the CSS rules ("escaped as code point", "the escaped character") but no serialization. Backslash + lowercase hex + trailing space (`\1f `, `\31 abc`, `\-`) is one of several reasonable readings of those words (e.g. `\x`-escapes, uppercase hex, no terminator all satisfy the quote). A second implementation is easy to write down → `not_inferable` → broken.
3. **format-code-task-002256 → suspect (R2).** The instruction shows only sync `Queue`, but hidden tests repeat the `full()` lifecycle on `AsyncQueue`. Grep for `def full` surfaces the twin and the identical guard fixes it — no new semantics → `guessable` → suspect. (Not R3: the async twin is broken at base, not preserved.)
4. **format-code-task-002377 → sound (R3).** The instruction reports only `Extra.forbid`; hidden tests also pin `extra=allow` (expose extras) and `extra=ignore` (drop extras). Both are pre-existing library semantics satisfied at base and preserved by any general fix of the stated bug → covered, not items → sound.
5. **format-code-task-002407 → broken (R2).** The instruction names only `forced_response`, but hidden tests pin labels on `step_response`, `impulse_response`, `initial_response` and `input_output_response`, including `input=`/`output=` index-subsetting. Plain label threading is `guessable`, but the subsetting logic is new semantics the instruction never hints at → `not_inferable` → broken.
6. **format-code-task-001868 → suspect (R5).** The instruction says only "should fail"; hidden tests grep stderr for the CSV paths and column names. All required substrings are stated in the instruction (the paths/columns it names), and a failing Django command reports to stderr by convention → `guessable` → suspect. Had the tests required strings stated nowhere, it would be broken.
