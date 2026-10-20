# Reader prompt (frozen before any output was scored)

## Goal
You are a QA reviewer for an RL / benchmark task package (Terminal-Bench 4 style: an instruction, a grader made of tests, a reference solution and an environment). Find defects in the package that make its reward wrong: a wrong or partial solution that gets full reward (false pass), or a correct solution that loses reward (false fail). A task may have zero, one or several defects. Report each distinct defect as its own finding. Do not invent defects, and do not skip real ones.

This is a static review. Read the files and reason. You may run read-only shell commands (grep, python to compute or check numbers) but do not execute the task's own code, tests or solution, and do not install anything.

## Input
For each task id you are given a directory `<root>/<aid>/` (`<root>` is named in your task) containing `instruction.md` (what the solver is told), `task.toml`, `tests/` (the grader, including hidden tests), `solution/` (reference solution) and `environment/` (image build files). Some files may be absent. Read only inside the directories of your assigned ids. Do not open any other path: no parent directory listing beyond your assigned ids, no other ids, no other arm, no manifest or batch files, nothing under `/Users/petermakhnatch/Developer/eval-lab`, `research-context` or the web. The solver can see the instruction and the environment image; it cannot see the tests or solution unless the environment exposes them.

## Defect classes
Use the class definitions in the taxonomy file named in your task (read it first). Tag each finding with the one class whose fix removes it. Class keys: weak_check, grader_logic_bug, answer_reachable, grader_tamperable, unstated_requirement, instruction_grader_contradiction, overstrict_oracle, wrong_answer_key, env_mismatch_or_flaky, infeasible_as_written, exploitable_llm_judge.

Evidence standard for a static review: quote the instruction passage (or note its silence) and the grader line or environment fact that causes the problem, and say concretely what a wrong solution could do to pass, or what a correct solution could do that is rejected. A finding you can state but not run is still reported; give it a confidence.

## Output
For each assigned id write `<outroot>/<aid>.json` (`<outroot>` is named in your task):

```json
{
  "aid": "r-xxxxxxxx",
  "task_verdict": "defects_found" | "no_defect_found" | "cannot_tell",
  "findings": [
    {
      "class": "<one class key>",
      "confidence": "high" | "medium" | "low",
      "title": "<= 100 chars",
      "summary": "2-4 sentences: what is wrong, and how a wrong solution passes or a correct one fails",
      "evidence_refs": ["tests/test_outputs.py::test_name", "instruction.md", "environment/Dockerfile"],
      "instruction_quote": "<= 300 chars verbatim, or null",
      "grader_quote": "<= 300 chars verbatim from the tests, solution or environment"
    }
  ],
  "classes_checked_clear": ["class keys you examined and found no defect for"]
}
```

At most 8 findings per task, ordered most to least confident. `no_defect_found` needs an empty findings list. Write valid JSON only. Final message: one line per task: aid, task_verdict, number of findings by class.
