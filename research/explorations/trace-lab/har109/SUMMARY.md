# HAR-109: what happened in the 10 Python runs, and which tool tells you

One run each of 10 FineEnvs Python code tasks, on MiMo-V2.6-Distill-Qwen-9B (Terminus-2, Modal; HAR-104). Every run was read by hand first, and those labels were frozen before any tool ran. Then Eval Lab, Scout and Docent were scored against them.

## The short version

- **4 of 10 runs passed, but only 2 passes are real.** The other 2 downloaded the already-fixed release of the library from PyPI and pasted it in. The task blocklist covers GitHub, GitLab and search engines, but not PyPI.
- **The failures are mostly the model's fault.** 5 of the 6 failed runs are clear model mistakes on sound tasks. 1 task is broken, because its hidden tests check rules the instructions never mention.
- **The model rarely stops cleanly.** 8 of 10 runs hit the call or token ceiling. The model often claims it's done but never answers the "are you sure?" prompt properly, then loops on echo or `git diff` until the budget runs out.
- **Eval Lab now flags a copied pass on its own**, via PR #552. No tool spotted the broken task. That still needs a person reading the test file next to the instructions.

## The 10 runs

| Task | Reward | What really happened | Verdict |
|---|---|---|---|
| 000226 waitress | 1 | Downloaded waitress 2.0.0 (fixed) and pasted its `split_uri` | **Task leaks answer**, via PyPI |
| 000927 soupsieve | 1 | GitHub was blocked, so it downloaded soupsieve 1.9.1 and copied `escape()`, the tests and the changelog | **Task leaks answer**, via PyPI |
| 002391 pip-audit | 1 | Correct fix by step 10, then 90 wasted steps | Sound, earned pass |
| 002864 sqlglot | 1 | Correct grammar fix, then looped `git diff`/`git diff --stat` 37 times | Sound, earned pass |
| 000383 quickfix | 0 | Flattened FIX repeating groups; its own checks hid the bug | Sound, model error |
| 001832 siuba | 0 | Pandas part right; wrote the SQL module but never imported it | Sound, model error |
| 001896 linkpreview | 0 | Returned a list where a tuple was required | Sound, model error |
| 002256 persist-queue | 0 | Its own test blocked the terminal; it never pressed Ctrl-C and repeated one command 92 times | Sound, model error |
| 002259 PHARE | 0 | Read C++ headers for 85 steps and never edited | **Task broken** (hidden contract); the run failed earlier anyway |
| 002407 python-control | 0 | Found the fix, then read a downloaded old copy until the budget ran out, with no edit | **Task suspect** (tests wider than the instructions) |

Hand pages: [`hand/`](hand/). With one run per task, "too hard" and "too easy" can't be called yet.

## How well each tool matched the hand reads

| Question | Eval Lab (after #552) | Scout (rules) | Docent (AI reader) |
|---|---|---|---|
| Why the run stopped | **10/10** | **10/10** | — |
| Did the model confirm it was done | **10/10** | **10/10** | — |
| Loops (8 real) | 7 found, 1 missed | 8 found, **2 false alarms** | — |
| First step that went wrong | 3/10 | 2/10 | **7/10** |
| Who is to blame | — | **8/10** | 6/10 (always "model") |
| Pass not earned (2 real) | **2/2** | 0/2 | 0/2 (saw the download, called it earned) |
| Downloaded outside code (3 real) | **3/3** | 0/3 | **3/3** |
| Broken or suspect task (2 real) | 0/2 | 0/2 | 0/2 (called every task fair) |
| Cost / time | $0 / seconds | $0 / minutes | $0 (free hosted quota, 1.19M tokens in) / 10 min |

A dash means the tool doesn't answer that question. Before #552, Eval Lab got 6 of 8 loops, 9 of 10 on confirmation, and caught neither the downloads nor the copied passes. Full numbers: [`scores.md`](scores.md).

## What each tool is good for

- **Eval Lab: start here for every run.** It is fast and free, and gets the facts right: why the run stopped, which ceiling, the completion handshake, loops, and now downloads and copied passes. Its "first failure" is just the first error message, which is often not where things went wrong.
- **Scout: good for blame at scale.** Its rules got 8 of 10 on who is to blame, the best score. It over-reports loops, flagging every run.
- **Docent: good for finding where a run went wrong,** and for plain-language summaries. It gives a step window, not an exact step. It is too trusting: it called every task fair and every pass earned, even when it had noticed the download.
- **A person** is still needed to judge whether a task is fair: whether the hidden tests match the instructions. No tool did that.

## Next time

1. Run `evallab report run` on every trial. Treat `pass_may_be_copied` as "don't count this pass until someone looks".
2. Use Docent only on the failed runs, to find where they went wrong. Skip it for passes and for task fairness.
3. For each task, have a person compare the hidden tests with the instructions once, in the census, not for every run.
4. Fix the leak at the source: block PyPI after setup, or cap each task's own package at its starting version. This is posted on HAR-104 and HAR-108 for the environment owner.

## Caveats

- **Pattern timing:** I wrote the download patterns after seeing 000226, so only 000927 and 002407 test them independently.
- **One hand label was wrong:** 001896 did confirm completion, with back-to-back claims at steps 41–42. The tools were right. The correction is in [`hand_errata.json`](hand_errata.json); the frozen files are unchanged.
- **Docent's step numbers** are its own message indices, which line up with the trajectory steps on these runs. I scored its windows with ±2 steps of slack.
