"""Behavioral boundaries for the read-only task-dossier evidence slice."""

from __future__ import annotations




TASK_A = "format-code-task-000792"
TASK_B = "format-code-task-000788"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_OTHER = "sha256:" + "b" * 64
HARBOR_A = "sha256:" + "c" * 64
V_APPLIED = "sha256:" + "d" * 64
V_NEWER = "sha256:" + "e" * 64

LEDGER_COLUMNS = [
    "task_id",
    "status",
    "reason",
    "run",
    "run_digest",
    "run_variant_status",
    "verdict",
    "verdict_evidence",
    "leak_channel",
    "evidence",
    "census_label",
]
HISTORY_COLUMNS = [
    "task_id",
    "status",
    "runs",
    "clean_pass",
    "copied_pass",
    "fail",
    "infra",
    "last_run",
    "agents",
    "trials",
]
LOCKED_COLUMNS = ["task_id", "locked_nop", "note"]
FAILING_COLUMNS = [
    "task_id",
    "runs",
    "unique_runs",
    "models",
    "model_names",
    "unknown_model_runs",
    "ctrf_runs",
    "verifier_runs",
    "model_evidence_state",
    "always_failing_tests",
    "observed_common_failing_tests",
    "no_agent_runs",
    "no_agent_ctrf_runs",
    "no_agent_verifier_runs",
    "verifier_sources",
    "test_evidence",
    "candidate_broken_test",
    "candidate_tests",
    "model_trial_paths",
    "no_agent_trial_paths",
    "notes",
]







































