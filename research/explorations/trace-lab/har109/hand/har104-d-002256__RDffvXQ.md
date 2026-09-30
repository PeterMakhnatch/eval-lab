# har104-d-002256 — persist-queue: full() wrong when unbounded (reward 0.0)

## What the task asked

Fix `Queue.full()` so an unbounded queue (default or `maxsize=0`) never reports full. The report shows empty means `(0, True, True)` instead of `(0, True, False)`, and gives the correct bounded behavior for comparison. Hidden tests check both the sync `Queue` and the `AsyncQueue`, empty, with items, after drain, explicit zero, and bounded cases.

## What the model did

| Step | What happened |
|------|---------------|
| 2 | Found the bug: `full()` was `qsize == maxsize`. |
| 3 | Applied the right fix to sync `queue.py` only: if `maxsize` is None or <= 0 return False, else `qsize >= maxsize`. Never looked at the async file. |
| 4 | Tried to verify. Output came back split across messages, and the model read it as broken. |
| 5–29 | Terminal returned only echoed commands, no results. Model tried flushing, files, and new scripts, but kept feeding the same stuck shell. |
| 30–121 | Sent the identical two-line `print(open(...).read())` command 92 times. Never ran the repo tests. Never called finish. Budget ran out. |

## Why it failed

The sync fix was correct. The async class was never fixed, so 2 hidden async tests failed (`full() is True` on an empty unbounded queue). The run then burned its whole budget repeating one command into a terminal that only echoed, so it never discovered the miss.

## Who is to blame

The model. It diagnosed correctly and fixed half the bug, then wedged its own verification and looped instead of recovering or checking the async file. No harness, task, or grader fault.

## Is the task fair?

Yes. The instruction shows the bug exactly, and the fix is one line that covers both classes. The instruction never names `AsyncQueue`, but the generalization is obvious. A run that checks both files passes.
