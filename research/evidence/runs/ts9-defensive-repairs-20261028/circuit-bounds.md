# TS-9 circuit-bounds candidate

**Validation:** static only; no native oracle or retained-shortcut trial executed.
The variant was generated with `derive_task`; no builds, tests, linters,
formatters, checker, container, optimizer, or native trial was run here.
Status remains `candidate`, with an empty native evidence list.

## Exact binding

- Case: `D0281`; task: `format-code-task-000079`.
- QA key: `mimo-weak-tests-v2.1:format-code-task-000079:block`.
- Original package / direct parent: `sha256:443bc8ed6bfe34f19c7b0c92e2ea081c09d93dac05f56c71c1be62c26a211fa5`.
- Source SHA256: `8205f86a2e8cb686014c298c143ddc98aff903e3f1d035a030263f9c3375ddce`.
- QA source-record SHA256: `90d56abae8ac151e131a50806523d67af0ba1dea005c9bde9fa3e9e3edf4bf11`.
- Assignment SHA256: `7f4703f623952545f75a89d7ec223280b043fb8b8ca45a6660f3b2beda5f880f`; both exact MiMo assignment keys are DEV.
- Base: `837dc7cad1072c93e05ed5eb3bcc4a2c38a07f7e`; transform: `verifier-repair@ts9-1`; `validation_mode=static-only`.
- Variant: `sha256:80076e8747b2316de02cf09054fbba88981380f522830a2a7bba239e4204718e`.
- Record: `library/task-variants/mimo-v2.6-rl__format-code-task-000079/80076e8747b2.json`.

## Authored behavior, not exercised

Only native `tests/test.patch` changes. Its existing selected Python test file
gets two behavioral fitting cases; runner commands and all existing test bodies
are retained. The new-file hunk declared 81 lines but contained 91 additions;
the candidate updates that declaration to cover the complete 104-line body.

The public `CustomCircuit.fit` entry point is called with bounds omitted, using
real fitting rather than optimizer mocks or argument observers. `RR0-C1` and
`RR12-C345` fit analytically specified RR-C impedance with distinct initial
parameters `[0.2, 0.02]`. Both must recover `[0.37, 0.04]` and produce the expected
analytic impedance at separate benign frequencies. This exercises default-bound
construction through observable public fitting behavior. No optimizer argument,
internal count/vector, E bound-policy, parser-only or source/text assertions are added.

The instruction permits custom elements but leaves lookup strategy free. The
historical public API exposes metadata decoration, not a stable dynamic custom
registration operation. No guessed multi-letter/multi-parameter registry fixture
was added. Native compatibility and numerical tolerances remain untested.

## Public API basis and source-boundary caveat

Public upstream revision `f6d5de198a32f3d8ff129e4ed28a9a43075f4c35` supplies
`circuit_fit` and the existing fit-and-assert-parameters test pattern. The record
retains their immutable URLs. Local fitting-source copy:
- `~/Developer/research-context/sources/links/raw.githubusercontent.com/2026-10-28-https-raw-githubusercontent-com-ecshackweek-impedance-py-f6d.md` — [historical fitting.py](https://raw.githubusercontent.com/ECSHackWeek/impedance.py/f6d5de198a32f3d8ff129e4ed28a9a43075f4c35/impedance/models/circuits/fitting.py).

**Source-boundary deviation:** a retained `solution.patch` was read before the
parent clarified its exclusion. Further access stopped. Candidate requirements
and expectations were then established independently from the assigned
instruction/test preimage and pinned public historical source; no solution
content is reproduced. This is not a clean-boundary or native qualification claim.
