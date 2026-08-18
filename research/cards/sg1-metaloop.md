# Eval card: sg1-metaloop-task-synthesis

Status: automatically drafted from completed evidence; human review required before publication.

## Question

Does the SG-1 meta-loop task synthesis pipeline (`library/meta/synthesize-task@1`) produce structurally valid Terminal-Bench task packages that pass the automated 4-check completeness battery and preserve lineage provenance in quarantine?

## Configuration and evidence

- Task: `library/meta/synthesize-task@1`
- Completed spec: `agents/handoffs/sg1-metaloop.md`
- Config digest: `sha256:d8a9f24e1302b1154c1f8876adbc96486711a3b90038e9dc2a33f44358a9e083`
- Harbor job: `library/meta/synthesize-task@1` (Terminal-Bench package format)
- Harbor lock digest: `sha256:886e92a20de44384b7adaa8c623e96b17765ecfca8159b9aa515ba92f033cb41`

## Result

- Meta-task units ($n_{\text{meta}}$): **1** (`library/meta/synthesize-task@1`)
- Automated completeness checks ($n_{\text{checks}}$): **4**
- Completeness battery pass rate: **1.000** (4 of 4 checks passed; 95% Wilson interval: **[0.510, 1.000]** for $n=4$)
- Unit tests passing in authoring suite ($n_{\text{tests}}$): **17** (17 of 17 tests passed; 95% Wilson interval: **[0.816, 1.000]**)
- Execution/harness exceptions: **0**

### Automated Completeness Battery Results
1. `package_structure`: **PASS** (1.000 [0.207, 1.000] for n=1 package; valid `task.toml`, `instruction.md`, `environment/Dockerfile`, `solution/solve.sh`, `tests/Dockerfile`, `tests/test.sh`)
2. `no_answer_leakage`: **PASS** (1.000 [0.207, 1.000]; zero golden solution or verifier test fixture literals leaked into `instruction.md` or `environment/`)
3. `oracle_solution_runs`: **PASS** (1.000 [0.207, 1.000]; oracle reference solution executes cleanly within timeout)
4. `task_tests_pass`: **PASS** (1.000 [0.207, 1.000]; verifier returns code 0 on oracle outputs and code 1 on empty baseline)

### Pipeline Controls & Quarantine
Synthesized task packages are quarantined in `library/tasks/_proposed/<proposal_id>/`. Each proposal records full lineage inputs (`proposal.json` with spec and exemplar digests) and enters at state `proposed`. It must satisfy the 4-control battery (oracle pass, empty fail, corrupt fail, baseline run) before human craft review and registry promotion (`evallab registry promote`).

## Elicitation tuple and caveats

```json
{
  "component": "sg1-metaloop",
  "meta_task": "library/meta/synthesize-task@1",
  "pipeline": "evallab.authoring",
  "purpose": "craft",
  "battery_checks": [
    "package_structure",
    "no_answer_leakage",
    "oracle_solution_runs",
    "task_tests_pass"
  ],
  "k": 1
}
```

The tuple must name the agent version, model pin, preamble hash, configured toolset, and `k`. An unavailable tuple makes cross-cohort ranking non-reportable.

- Elicitation caveat: The meta-task was tested under local deterministic verification without billable model dispatch. Authoring proposals are submitted with `purpose="craft"` and quarantined until battery verification.

## Contamination note

- Contamination caveat: Meta-task templates, skeletons, and exemplars (`local-lab/event-summary`) reside entirely in the repository authoring plane. No external benchmark tasks or test sets were ingested into the meta-task image.

## Threats to validity

- Single exemplar template: Synthesis tests currently reference `local-lab/event-summary` as the sole exemplar; diverse task families (e.g. multi-container services, interactive CLI tasks) remain uncalibrated.
- Small verification battery sample size: $n=4$ completeness checks provide wide statistical intervals ([0.510, 1.000]), requiring multi-task batch evaluations in SG-2.
- Deterministic fixture testing: Initial verification evaluated static reference packages; live LLM agent authoring performance will depend on model generation quality.

## Regeneration query / command

```bash
uv run python -m library.meta.synthesize_task@1.tests.completeness_checker library/meta/synthesize-task@1/exemplar
uv run pytest tests/test_authoring.py
```

## Human review

- [ ] Confirm task and verifier identity.
- [ ] Confirm the elicitation tuple describes the actual run.
- [ ] Resolve the contamination note with evidence.
- [ ] Decide whether the interval supports the intended claim.
- [ ] Record reviewer, date, and publication disposition.
