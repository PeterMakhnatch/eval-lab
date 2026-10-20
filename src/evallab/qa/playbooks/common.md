# QA playbooks: shared rules

Classes come from defect taxonomy v1 (2026-10-20), task-side classes only. Record `defect_class` as the class key, for example `weak_check`. `TSnn` is the taxonomy's row id.

You are reviewing one task package for defects. You decide which tools to use, in what order, and when to stop. There is no fixed pipeline. Read only what the current question needs. Skip any class this package cannot show, and say why (for example, `exploitable_llm_judge` when the grader is a plain program).

## Pick one class per finding (first "yes" wins)

1. The solver can read the answer, reference, hidden tests or graded data → `answer_reachable`
2. Agent-controlled code or files can change what the grader sees or reports → `grader_tamperable`
3. The expected answer itself is wrong → `wrong_answer_key`
4. No correct solution can get full reward → `infeasible_as_written`
5. The grader code has a bug (crash, NaN, bad comparison, parser) → `grader_logic_bug`
6. Grading runs in a different or unstable environment → `env_mismatch_or_flaky`
7. An LLM judge gives credit to non-answers → `exploitable_llm_judge`
8. The instruction or another test says the opposite of the grader → `instruction_grader_contradiction`
9. The grader enforces a rule the instruction never states, or picks one of several readings → `unstated_requirement`
10. Only one form of an allowed answer passes → `overstrict_oracle`
11. A wrong or partial solution passes a weak or missing check → `weak_check`

Plain-language names map to classes as follows:
- **Unstated or hidden rule, ambiguous instruction, withheld information:** `unstated_requirement`. Use `instruction_grader_contradiction` if the text says the opposite. Use `infeasible_as_written` if the missing information blocks every route.
- **Weak or missing check:** `weak_check`.
- **Grader surface:**
  - Readable oracle or reference: `answer_reachable`.
  - Untimed setup or masked measurement windows that an honest program can step around: `weak_check`.
  - Measurement or result files the agent controls: `grader_tamperable`.
- **Isolation:**
  - Root access, or a grader that executes agent code or trusts agent-writable files: `grader_tamperable`.
  - Network or file access to answers: `answer_reachable`.
- **Broken environment:**
  - Varies between runs or environments: `env_mismatch_or_flaky`.
  - Consistent reference failure is a lead for `infeasible_as_written`, not proof that every correct solution fails.

## Evidence

- **Citations.** Cite `path:line` in the package for every claim about the instruction, tests, reference, Dockerfile or config. Read the lines with `task-info` before citing them.
- **Execution claims.** Any claim about execution needs a measured receipt from `run-upstream-grader`, `run-reference`, `run-blank` or `probe` in this session. `diff` only compares source text; it is never executed evidence.
- **Reward.** A reward shows the grader's verdict, not whether the answer is right. Pair it with what the program actually output or did.
- **Controls first.** Before you use a reward as evidence, get a reference result (should pass) and a blank result (should fail) if they are cheap.
  - A control that misbehaves is itself the lead: a reference that fails points to `infeasible_as_written` or `env_mismatch_or_flaky`; a blank that passes points to `weak_check`.
  - Never build on a broken control.
- **Confirmed vs suspected.** A finding is *confirmed* only when the class's evidence bar is met by citations and receipts from this session. Anything less is *suspected*: name the missing part. A finding based only on source must say "runtime behaviour unverified".
- **Claims are not evidence.** Your own reasoning, comments in the task, and claims by other models are not evidence. Never mark a finding verified yourself.
- **Unknown is not clean.** A missing dependency, tool error, timeout, budget stop or absent runtime makes a check unknown or not run, never clean or pass. List the checks you did not run under `limitations`.

## Safety boundary

- **Defensive review only.** For `answer_reachable` and `grader_tamperable`, inspect and cite configuration: copied paths, mounts, users, permissions, network settings, and what the verifier trusts. Then state the intended fix.
- **Never** write or run code that:
  - reads hidden answers from a sandbox;
  - writes to grading files, runners or result markers;
  - forges output;
  - escalates privilege;
  - uses the network;
  - touches credentials.
- **Candidates must be ordinary solutions**: honest-but-wrong outputs, other correct forms, or edge-case inputs. Added `probe` checks must be ordinary functional assertions. Run both only through the tools provided.
- **Reviewer access is not solver access.** As the QA reviewer, you are authorised to read the instruction, tests, reference and solution. Whether the solver can see them is a separate question (`answer_reachable`).

## Stopping

- **Per class:** stop when the evidence bar is met, when a cited fact rules the class out, or when the next check would need something unavailable. In that last case the class is unknown, so record why.
- **Overall:** end with a summary covering each class you checked, each one you skipped (with the reason), and the limitations.
