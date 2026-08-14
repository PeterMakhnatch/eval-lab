# Adapter Review: quixbugs

## Structural Validation

**30 passed | 0 errors | 3 warnings** [PASS]

### Warnings (recommended)

- [ ] **Parity PR link null**: Entry 0: `adapter_pr` is null — fill in the PR URL(s). (`adapters/quixbugs/parity_experiment.json:24`)
- [ ] **Parity PR link null**: Entry 0: `dataset_pr` is null — fill in the PR URL(s). (`adapters/quixbugs/parity_experiment.json:25`)
- [ ] **Parity PR link null**: Entry 0: `parity_pr` is null — fill in the PR URL(s). (`adapters/quixbugs/parity_experiment.json:26`)

### Passed

- [x] `README.md` exists
- [x] `parity_experiment.json` exists
- [x] `adapter_metadata.json` exists
- [x] `src/quixbugs/` package exists
- [x] `src/quixbugs/adapter.py` exists
- [x] `src/quixbugs/main.py` exists
- [x] `src/quixbugs/task-template/` directory exists
- [x] `src/quixbugs/task-template/task.toml` exists
- [x] `src/quixbugs/task-template/instruction.md` exists
- [x] `src/quixbugs/task-template/environment/Dockerfile` exists
- [x] `src/quixbugs/task-template/tests/test.sh` exists
- [x] `src/quixbugs/task-template/solution/solve.sh` exists
- [x] Template `[task].name` present
- [x] Template `[task].authors` present
- [x] `parity_experiment.json` is valid JSON array
- [x] `adapter_metadata.json` is valid JSON array
- [x] README section `Overview` present
- [x] README section `What is` present
- [x] README section `Adapter Features` present
- [x] README section `Generated Task Structure` present
- [x] README section `Run Evaluation` present
- [x] README section `Usage` present
- [x] README section `Parity` present
- [x] README section `Notes & Caveats` present
- [x] README section `Installation / Prerequisites` present
- [x] README section `Citation` present
- [x] README section `Authors & Contributions` present
- [x] Parity table column count correct
- [x] `test.sh` writes to reward path
- [x] No canary strings found
