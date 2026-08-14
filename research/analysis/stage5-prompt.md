Analyze the completed Harbor trial at `{source_trial_path}` as a bounded,
read-only evidence review. Do not modify any source file. Distinguish task,
environment, harness, verifier, and agent failures. Every substantive claim
must cite a trial-relative path; citations to an ATIF trajectory must include a
step ID and should include a tool-call ID when one is relevant.

Rubric:

{rubric}

Return only JSON matching this schema:

{output_schema}
