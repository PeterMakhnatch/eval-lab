# Task Authoring Guidelines & Quality Checklist

## Architecture Rules
1. **Separation of Concerns**: Verifier runs in a separate container (`environment_mode = "separate"`).
2. **Hidden Test Inputs**: Ground truth and test assertions live in `tests/` and `solution/`. The agent only sees `instruction.md` and `environment/`.
3. **Deterministic Verification**: Tests output structured CTRF and check reports (`checks.json`, `reward.json`).
4. **No Answer Leakage**: Golden solutions, test fixtures, and answer keys are strictly prohibited inside `instruction.md` and `environment/`.

## File Conventions
- `task.toml`: Use schema version 1.4.
- `instruction.md`: Describe requirements, input paths, output paths, and schema.
- `environment/Dockerfile`: Minimal image definition with input files.
- `solution/solve.sh`: Executable reference solver.
- `tests/Dockerfile`: Image definition for separate verifier.
- `tests/test.sh`: Executable entrypoint for verifier.
- `tests/verify.py`: Scoring and assertion script.
