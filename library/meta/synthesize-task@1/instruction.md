# Synthesize an Evaluation Task

Author ONE complete evaluation task package in Terminal-Bench format inside `/app/output/task/`.

## Inputs Available

You have been provided with:
1. `/app/spec.json` — The task specification declaring `name`, `category`, `scenario`, `difficulty`, and requirements.
2. `/app/skeleton/` — Skeleton directory structure and starter templates for each required file.
3. `/app/exemplar/` — A verified working exemplar task showing standard packaging conventions.
4. `/app/templates/guidelines.md` — Authoring rules, checklist, and anti-patterns.

## Target Output

Author a complete task package in `/app/output/task/` containing:
- `task.toml`: Valid task configuration with `[task]`, `[metadata]`, `[environment]`, `[agent]`, and `[verifier]` tables.
- `instruction.md`: Clear, unambiguous instructions for an agent solving the task.
- `environment/Dockerfile`: Build instructions for the target agent container.
- `solution/solve.sh`: Executable oracle solution script that solves the task.
- `tests/Dockerfile`: Build instructions for the separate verifier container.
- `tests/test.sh`: Executable verifier script that evaluates candidate work.
- `tests/verify.py` (or test files): The verification logic scoring candidate output.

## Quality and Isolation Requirements

1. **Self-Validation**: The oracle solution in `solution/` must execute successfully and produce the expected outputs.
2. **Verifier Contract**: The verifier in `tests/` must pass when the oracle solution has run, and must fail on empty/incomplete work.
3. **No Answer Leakage**: The agent-visible surface (`instruction.md` and `environment/`) must NEVER contain solution code, golden data, or answer keys. All verification ground truth must remain inside `tests/` and `solution/`.
4. **Permissions**: All `.sh` scripts must be executable (`chmod +x`).
