# ov1106 Gold public-contract repair

## Scope and frozen rules

The task cohort is `000682`, `002139`, `000081`, `001267`, and `003007`.
All tasks must have existing strictly-DEV scorecard evidence of original-grader mutation weakness.
Do not replace a failing task with an easier task.

The code worker makes certification safe by default.
Seven historical gates retain their meanings.
Missing integrity controls remain unexercised and cannot produce a certificate.
This study does not create or execute grader-bypass payloads.
No full certification claim is permitted without all 7 measured gates.

## Gold acceptance

Each assertion must follow the instruction or a documented public interface in pristine source.
Reference-only helper, module, configuration, schema, exact message, and sentinel choices are not acceptance requirements.
Contract authors freeze each task contract before inspecting any new alternative output.
After any scored alternative informs a repair, label that contract alternative-informed.
Do not report that reused alternative as newly held out.

## Blind alternatives

Generate 4 alternatives per task in independent fresh source containers.
Use `openai-codex/gpt-6.1-terra` through `omp auth-gateway stdio`.
The model sees only the instruction and pristine public production source.
The only tool executes through the hash-bound retained `ov1104-impossible/container_tool.py` executor.
The writer container uses a minimal cached Alpine image, no network, a read-only root, no credentials, and only its own public-source workspace mount.
The parent gateway has credentials, but no credential value enters the container or model content.
A new model-driven exact-transport canary must pass before generation.
Every forbidden-path probe must return an actual denial, not the missing-cwd code 125.
Indices 0–1 form the validation cohort; 2–3 form the held-out cohort.
Keep all generated candidates and failures in the denominator.
No test, reference, prior alternative, or grader feedback reaches a writer.
Keep raw model messages outside Git; commit hashes, scripts, and aggregates only.

## Measurements and delivery

Report a per-task 7-gate table, exact source/contract/patch hashes, accepted alternatives, and mutant kills.
Report original-grader and repaired-Gold outcomes separately.
Correctness requires instruction-grounded checks; original grader acceptance alone is insufficient.
Report Wilson 95% intervals for proportions.
No detector is evaluated, so length-baseline comparisons do not apply.

Modal cap: $8 for the lane, with $2 initially allocated to the contract worker.
Log each paid allocation before and after in the shared ov1106 spend ledger.
Local writer generation has no paid model API or Modal charge.
Stop owned Modal apps.
Write EARLY.md when the first complete gate result exists; never manufacture a complete result.
Deliver the implementation and measured evidence through green exact-head PR checks and guarded merge.
