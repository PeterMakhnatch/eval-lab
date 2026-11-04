# Honest certification re-attempt

Registered 2026-11-04 before generation or new grading.

## Hypotheses and population

The fixed DEV task population is format-code-task-000682, 002391, and 002139. Honest instruction-only alternatives can expose over-strict synthesized assertions. A certification requires all seven gates, exact input binding, and at least two distinct correct instruction-only alternatives. Missing gates are unmeasured, never passes.

## Frozen generation design

Generate four independent alternatives per task (indices 0–3), each in a fresh tool-free auth-gateway chat using only the task instruction, pristine baseline source, and generic source-only output instructions. The route must attest openai-codex/gpt-6.1-terra. No tools, hidden tests, reference patch, previous alternatives, or grading feedback enter any solver request. Use source-file replacement JSON as the textual patch format; the controller converts it to a source-only unified patch. Preserve exact requests, responses, model identity, input hashes, and baseline provenance. Infrastructure or served-model failures may retry the identical request up to three times; never retry a semantically failed solution.

Indices 0 and 1 are development alternatives; indices 2 and 3 are frozen evaluation alternatives. No failed alternative may be removed or replaced after scoring. Report original-grader correctness for all generated alternatives, and hardened acceptance both unconditionally and conditional on independently original-correct alternatives. A gate-7 claim requires at least two distinct original-correct alternatives, including both frozen evaluation alternatives. Duplication is reported and cannot create two independent solutions.

## Frozen correction rule

Before scoring alternatives, retain the 000682-r3 contract unchanged. For 002391-r2 remove only assertions that require preservation of the non-specified published field, retaining identifier/alias/PYSEC and transitive-merge requirements. For 002139-r2 replace the fake storage client's artificial project default sentinel with None, which matches an omitted optional argument; remove no required existence, size, network, or filename checks. Freeze and hash all contract bytes before grading. Do not tune tests after seeing current alternative results. Existing contracts contain prior-lane knowledge; this is explicitly a re-attempt, not a blind first synthesis.

## Safety and gate execution

Do not execute certify-gold's default witness or exit0_pth arms: they install prohibited harness/startup hooks. Do not construct these payloads. Run safe reference, nop, source-only train/evaluation mutants, and alternative arms using existing native Modal cell infrastructure. Reuse immutable previous control metadata only when the full package digest and control identity bind exactly. Changed packages cannot inherit control results. Therefore corrected packages remain partial/provisional unless every required gate has valid independent evidence. Never publish a false seven-gate certificate.

## Spend and reporting

One campaign ledger, ~/.local/state/evallab-ov1104/wave2-grader-spend.json, has a $2 Modal cap. Subscription generation costs $0 incremental. Log every Modal allocation to the shared spend JSONL, with actual_usd null unless billing provides an actual. Report runtime-based estimates separately from billed actuals, include failed allocations, and inspect owned active applications at completion. Use no paid external model APIs.

Report each task and all seven gates, patch/application failures, original-correct counts, frozen evaluation acceptance, exact commands, immutable receipt paths, provenance limitations, and 95% Wilson intervals. Certification is descriptive for these three tasks, not a generalization estimate. Main agent owns integration and CI; this worker runs assigned experimental runtime only.
