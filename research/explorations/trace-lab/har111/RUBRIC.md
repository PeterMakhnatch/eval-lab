# HAR-111 task-soundness rubric (frozen before the checker exists)

The question: **would a competent engineer who reads only the instruction, and can explore the repository, write a fix that passes the hidden tests?**

For one task, read `instruction.md`, then the hidden tests (the test patch and the test runner). The environment Dockerfile and the repo at the base commit may be consulted to see what already exists. List every behaviour the hidden tests require, and check each one against the instruction.

## Labels

- **sound:** every behaviour the tests check is stated in the instruction, clearly implied by it, or already fixed by the existing code or public API. This includes names, signatures, exception types, messages, return types and file paths. A careful engineer would pass.
- **suspect:** at least one checked behaviour is unstated but plausibly guessable. For example: a natural exception type, a conventional return type, or a sibling function the instruction's wording could cover. A careful engineer might well miss it.
- **broken:** at least one checked behaviour cannot reasonably be inferred from the instruction plus the repo. For example: an exact error-message string, an extra API or contract the instruction never mentions, a specific file, output or line format, or tests that contradict the instruction. A correct fix to the stated problem would still fail.

Not in scope: answer leaks (e.g. the fix being installable from PyPI), difficulty, and environment or build breakage. Record those in `notes` only.

## Output: one JSON object per task

```
{"task_id": "...", "label": "sound|suspect|broken",
 "unstated": [{"what": "...", "test_ref": "file:line or test name", "quote": "<=200 chars",
               "severity": "guessable|not_inferable"}],
 "instruction_covers": ["short list of what the instruction does state that the tests check"],
 "notes": "...", "read_minutes": n}
```

Severity drives the label: any `not_inferable` item makes the task broken; else any `guessable` item makes it suspect; else it is sound.
