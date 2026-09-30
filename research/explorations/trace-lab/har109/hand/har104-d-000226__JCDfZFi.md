# har104-d-000226 — waitress: don't use urlsplit on request path (reward 1.0)

## What the task asked

Fix `waitress/parser.py` so a request like `GET //testing/whatever HTTP/1.1`
keeps the full path. Before, `urlsplit` read `testing` as a server name and
dropped it before the app ever saw it.

## What the model did

| Step | What happened |
|------|---------------|
| 2–3 | Read the parser and confirmed the bug. |
| 4–5 | **Downloaded the released waitress 2.0.0 from PyPI and printed its fixed `split_uri`.** |
| 8 | Pasted that fix: paths starting with `//` stay whole, `?` and `#` still split off. |
| 10–11 | Checked the fix directly; all 40 parser tests and all 691 suite tests passed. |
| 12–13 | Added 3 of its own tests for `//` paths; they passed. |
| 14 | Tried to finish, was asked to confirm, never did. |
| 15–24 | Re-ran the same checks and diff views a few times. |
| 25–121 | Sent `echo complete` about 100 times until the call budget ran out. |

## Why it passed, and why the pass doesn't count

The fix is correct, and all 13 grading tests passed. But the model didn't
work it out. At step 4 it downloaded the newer, already-fixed waitress
release from the internet, and at step 8 it copied that release's code. The
sandbox can reach PyPI, so for any task taken from a public project, the
answer can be downloaded.

(Lead's correction: the first hand read called the download harmless. The
output at steps 5 and 8 shows the fix was copied.)

## Who is to blame

The environment: network access lets the model download the answer. The
model also wasted about 100 calls afterwards because it never sent the
required confirmation, but that didn't change the score.

## Is the task fair?

The task text is fair: it names the file, shows the bad split, and says what
a valid request looks like. As run, though, it leaks its answer: the fix is
a public package release one `pip download` away. Count this as a tainted
pass, not a solve.
