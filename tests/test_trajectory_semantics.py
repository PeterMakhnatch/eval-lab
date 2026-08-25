"""Tests defending trajectory semantics profiles, action extraction, and Parquet projections."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from evallab.atif import project_jobs
from evallab.cli import run_cli
from evallab.results import load_job
from evallab.trajectory_semantics import (
    BASH_RESOLVER_SPEC,
    BENCHMARK_PROFILES,
    GENERIC_POSIX_PROFILE,
    SEMANTIC_ACTION_FACT_SCHEMA,
    ResolverConformanceVector,
    ResolverRef,
    ResolverRegistry,
    ResolverResult,
    ResolverSpec,
    SemanticReasonCode,
    TaskProfileBinding,
    ToolMappingRule,
    TrajectorySemanticsProfile,
    UnmappedActionError,
    extract_semantic_actions,
    project_job_semantics,
    project_semantic_actions_parquet,
    query_semantic_coverage,
    semantic_coverage,
)

FIXTURES = Path(__file__).parent / "fixtures" / "explorer" / "jobs"


def _extract(
    payload: dict[str, object],
    profile: TrajectorySemanticsProfile,
    *,
    strict: bool = True,
):
    return extract_semantic_actions(
        payload,
        profile,
        job_id="job-1",
        trial_id=str(payload.get("trial_id") or "trial-1"),
        task_id="task-1",
        binding_digest="sha256:" + "b" * 64,
        document_id=str(payload.get("document_id") or "document-1"),
        source_sha256="sha256:" + "a" * 64,
        source_ref="agent/trajectory.json",
        strict=strict,
    )


def test_structured_vs_bash_equivalent_profile() -> None:
    """A structured read tool and a bash cat command map to equivalent read roles."""
    structured = GENERIC_POSIX_PROFILE.resolve_action(
        "read",
        {"path": "/app/config.py"},
        {"content": "API_KEY=123", "status": "ok"},
    )
    assert structured.role == "read"
    assert structured.outcome == "success"

    bash = GENERIC_POSIX_PROFILE.resolve_action(
        "bash",
        {"command": "cat /app/config.py"},
        {"output": "API_KEY=123", "exit_code": 0},
    )
    assert bash.role == "inspect"
    assert bash.outcome == "success"


def test_grep_diff_cmp_expected_negative_vs_real_failure() -> None:
    """Status-owning search and comparison commands distinguish negatives from errors."""
    grep_found = GENERIC_POSIX_PROFILE.resolve_action(
        "bash",
        {"command": "grep -rn 'TODO' src/"},
        {"exit_code": 0, "output": "src/main.py:10: TODO"},
    )
    assert grep_found.role == "search"
    assert grep_found.outcome == "success"
    assert grep_found.reason_code is SemanticReasonCode.MATCH_FOUND

    grep_none = GENERIC_POSIX_PROFILE.resolve_action(
        "bash",
        {"command": "grep -rn 'NONEXISTENT_SYMBOL' src/"},
        {"exit_code": 1, "output": ""},
    )
    assert grep_none.outcome == "expected_negative"
    assert grep_none.reason_code is SemanticReasonCode.PATTERN_NOT_FOUND

    grep_error = GENERIC_POSIX_PROFILE.resolve_action(
        "bash",
        {"command": "grep --invalid-flag"},
        {"exit_code": 2, "output": "grep: unrecognized option"},
    )
    assert grep_error.outcome == "error"
    assert grep_error.reason_code is SemanticReasonCode.GREP_ERROR_EXIT_CODE
    assert grep_error.detail_digest is not None
    assert grep_error.detail_size is not None

    diff_identical = GENERIC_POSIX_PROFILE.resolve_action(
        "bash",
        {"command": "diff a.txt b.txt"},
        {"exit_code": 0, "output": ""},
    )
    assert diff_identical.outcome == "success"
    assert diff_identical.reason_code is SemanticReasonCode.IDENTICAL

    diff_different = GENERIC_POSIX_PROFILE.resolve_action(
        "bash",
        {"command": "diff a.txt b.txt"},
        {"exit_code": 1, "output": "1c1\n< a\n---\n> b"},
    )
    assert diff_different.outcome == "expected_negative"
    assert diff_different.reason_code is SemanticReasonCode.DIFFERENCES_FOUND

    cmp_different = GENERIC_POSIX_PROFILE.resolve_action(
        "bash",
        {"command": "cmp a.bin b.bin"},
        {"exit_code": 1, "output": "a.bin b.bin differ"},
    )
    assert cmp_different.outcome == "expected_negative"
    assert cmp_different.reason_code is SemanticReasonCode.DIFFERENCES_FOUND

    diff_error = GENERIC_POSIX_PROFILE.resolve_action(
        "bash",
        {"command": "diff nonexistent1 nonexistent2"},
        {"exit_code": 2, "output": "diff: missing"},
    )
    assert diff_error.outcome == "error"
    assert diff_error.reason_code is SemanticReasonCode.DIFF_ERROR_EXIT_CODE
    assert diff_error.detail_digest is not None

    compound = GENERIC_POSIX_PROFILE.resolve_action(
        "bash",
        {"command": "cd work && grep -q absent file.txt"},
        {"exit_code": 1, "output": ""},
    )
    assert compound.outcome == "unknown_semantics"
    assert compound.reason_code is SemanticReasonCode.SHELL_COMPOUND_AMBIGUOUS


def test_structured_search_requires_declared_cardinality_for_expected_negative() -> None:
    structured = GENERIC_POSIX_PROFILE.resolve_action(
        "grep",
        {"pattern": "absent"},
        {"matches": []},
    )
    bash = GENERIC_POSIX_PROFILE.resolve_action(
        "bash",
        {"command": "LC_ALL=C grep absent file.txt"},
        {"exit_code": 1, "output": ""},
    )
    undeclared_empty_content = GENERIC_POSIX_PROFILE.resolve_action(
        "grep",
        {"pattern": "absent"},
        {"content": ""},
    )
    assert structured.role == bash.role == "search"
    assert structured.outcome == bash.outcome == "expected_negative"
    assert undeclared_empty_content.outcome == "unknown_semantics"
    assert (
        undeclared_empty_content.reason_code
        is SemanticReasonCode.STRUCTURED_SEARCH_RESULT_SHAPE_UNKNOWN
    )


def test_user_assisted_vs_autonomous_recovery_intervention() -> None:
    """Intervening user guidance marks subsequent action as user-assisted; autonomous retry stays autonomous."""
    profile = GENERIC_POSIX_PROFILE

    atif_data = {
        "trial_id": "trial_001",
        "steps": [
            {
                "step_id": -1,
                "source": "user",
                "message": "Initial task prompt is not an intervention.",
            },
            # Step 0: Initial autonomous attempt fails
            {
                "step_id": 0,
                "source": "agent",
                "tool_calls": [{"tool_name": "bash", "command": "python -m broken_module"}],
                "observations": [{"exit_code": 1, "error": "ModuleNotFoundError"}],
            },
            # Step 1: Autonomous retry without intervention
            {
                "step_id": 1,
                "source": "agent",
                "tool_calls": [{"tool_name": "bash", "command": "pip install broken_module"}],
                "observations": [{"exit_code": 0, "output": "Installed"}],
            },
            # Step 2: User interjection / hint
            {
                "step_id": 2,
                "source": "user",
                "message": "Remember to run tests with pytest -v",
            },
            # Step 3: Action following user intervention
            {
                "step_id": 3,
                "source": "agent",
                "tool_calls": [{"tool_name": "bash", "command": "pytest -v"}],
                "observations": [{"exit_code": 0, "output": "5 passed"}],
            },
        ],
    }

    facts = _extract(atif_data, profile, strict=True)
    assert len(facts) == 3

    # Action 0 was autonomous
    assert facts[0].sequence == 0
    assert facts[0].intervention_provenance == "autonomous"
    assert facts[0].outcome == "error"

    # Action 1 was autonomous recovery
    assert facts[1].sequence == 1
    assert facts[1].intervention_provenance == "autonomous"
    assert facts[1].outcome == "success"

    # Action 2 was user-assisted recovery
    assert facts[2].sequence == 2
    assert facts[2].intervention_provenance == "user_assisted"
    assert facts[2].intervention_sha256 is not None
    assert facts[2].intervention_length == len(b"Remember to run tests with pytest -v")
    assert facts[2].intervention_reason == "post_action_user_message"
    assert facts[2].outcome == "success"


def test_environment_recovery_and_parallel_action_interventions() -> None:
    """Only an explicit post-action environment error marks recovery actions."""
    profile = GENERIC_POSIX_PROFILE
    atif_data = {
        "trial_id": "trial_env_rec",
        "steps": [
            {
                "step_id": 0,
                "source": "agent",
                "tool_calls": [
                    {
                        "tool_name": "bash",
                        "command": "python stalled.py",
                        "tool_call_id": "c0",
                    }
                ],
                "observations": [{"tool_call_id": "c0", "exit_code": 1, "output": "stalled"}],
            },
            {
                "step_id": 1,
                "source": "environment",
                "error": "TimeoutException: command timed out",
            },
            {
                "step_id": 2,
                "source": "agent",
                "tool_calls": [
                    {
                        "tool_name": "bash",
                        "command": "cat /var/log/syslog",
                        "tool_call_id": "c1",
                    },
                    {
                        "tool_name": "bash",
                        "command": "grep -rn ERROR /var/log/",
                        "tool_call_id": "c2",
                    },
                ],
                "observations": [
                    {
                        "tool_call_id": "c1",
                        "exit_code": 0,
                        "output": "log entries",
                    },
                    {
                        "tool_call_id": "c2",
                        "exit_code": 0,
                        "output": "ERROR: none",
                    },
                ],
            },
            {
                "step_id": 3,
                "source": "agent",
                "tool_calls": [
                    {
                        "tool_name": "bash",
                        "command": "echo recovered",
                        "tool_call_id": "c3",
                    }
                ],
                "observations": [
                    {
                        "tool_call_id": "c3",
                        "exit_code": 0,
                        "output": "recovered",
                    }
                ],
            },
        ],
    }

    facts = _extract(atif_data, profile, strict=True)
    assert len(facts) == 4
    assert facts[0].intervention_provenance == "autonomous"
    assert facts[1].intervention_provenance == "environment_recovery"
    assert facts[1].intervention_reason == "explicit_environment_error"
    assert facts[1].intervention_sha256 is not None
    assert facts[2].intervention_provenance == "environment_recovery"
    assert facts[3].intervention_provenance == "autonomous"
    assert facts[3].intervention_sha256 is None


def test_unmatched_observation_is_reason_coded_unknown() -> None:
    facts = _extract(
        {
            "trial_id": "trial-unmatched",
            "steps": [
                {
                    "step_id": 0,
                    "source": "agent",
                    "tool_calls": [
                        {
                            "tool_name": "read",
                            "tool_call_id": "call-expected",
                            "arguments": {"path": "a.txt"},
                        }
                    ],
                    "observations": [
                        {
                            "source_call_id": "call-other",
                            "content": "wrong result",
                        }
                    ],
                }
            ],
        },
        GENERIC_POSIX_PROFILE,
    )
    assert facts[0].observation_correlation == "unknown_unmatched"
    assert facts[0].correlation_reason == "tool_call_id_not_found"
    assert facts[0].outcome == "unknown_semantics"
    assert facts[0].reason_code is SemanticReasonCode.OBSERVATION_CORRELATION_UNKNOWN
    assert facts[0].detail_digest is None


def test_canonical_atif_nested_observation_results() -> None:
    """Canonical ATIF payloads with step.observation.results are correctly correlated."""
    profile = GENERIC_POSIX_PROFILE

    atif_data = {
        "trial_id": "trial_atif_nested",
        "document_id": "doc_123",
        "steps": [
            {
                "step_id": 0,
                "source": "agent",
                "tool_calls": [
                    {
                        "tool_name": "read",
                        "arguments": {"path": "src/main.py"},
                        "call_id": "call_1",
                    },
                ],
                "observation": {
                    "results": [
                        {"source_call_id": "call_1", "content": "print('hello')", "status": "ok"},
                    ],
                },
            },
        ],
    }

    facts = _extract(atif_data, profile, strict=True)
    assert len(facts) == 1
    assert facts[0].role == "read"
    assert facts[0].outcome == "success"
    assert facts[0].observation_sha256 != "sha256:" + "0" * 64


def test_unknown_tool_strict_vs_permissive() -> None:
    """Strict mode raises UnmappedActionError; permissive mode emits unknown_semantics with reason."""
    profile = TrajectorySemanticsProfile(
        profile_id="custom-restricted",
        version="1.0.0",
        description="Restricted profile with only read capability",
        tool_rules=(ToolMappingRule("read", "read"),),
    )

    atif_data = {
        "trial_id": "trial_unk",
        "steps": [
            {
                "step_id": 0,
                "source": "agent",
                "tool_calls": [{"tool_name": "unregistered_custom_tool", "arg": "val"}],
                "observations": [{"status": "done"}],
            }
        ],
    }

    # Strict mode fails closed
    with pytest.raises(
        UnmappedActionError, match="has no mapping rule for tool 'unregistered_custom_tool'"
    ):
        _extract(atif_data, profile, strict=True)

    # Permissive mode emits unknown_semantics fact
    facts = _extract(atif_data, profile, strict=False)
    assert len(facts) == 1
    fact = facts[0]
    assert fact.role == "other"
    assert fact.outcome == "unknown_semantics"
    assert fact.reason_code is SemanticReasonCode.UNMAPPED_TOOL
    assert fact.detail_digest is None


def test_profile_version_digest_change() -> None:
    """Any modification in profile rules or version changes the deterministic profile_digest."""
    p1 = TrajectorySemanticsProfile(
        profile_id="posix",
        version="1.0.0",
        description="Version 1",
        tool_rules=(ToolMappingRule("read", "read"),),
    )

    p2 = TrajectorySemanticsProfile(
        profile_id="posix",
        version="1.0.1",
        description="Version 1.0.1 patch",
        tool_rules=(ToolMappingRule("read", "read"),),
    )

    p3 = TrajectorySemanticsProfile(
        profile_id="posix",
        version="1.0.0",
        description="Version 1 with added rule",
        tool_rules=(ToolMappingRule("read", "read"), ToolMappingRule("write", "write")),
    )

    assert p1.digest.startswith("sha256:")
    assert p2.digest.startswith("sha256:")
    assert p3.digest.startswith("sha256:")

    assert p1.digest != p2.digest
    assert p1.digest != p3.digest
    assert p2.digest != p3.digest


def test_resolver_conformance_vectors_cover_transitive_helper_paths() -> None:
    vector_ids = {vector.vector_id for vector in BASH_RESOLVER_SPEC.conformance_vectors}
    assert {
        "environment_assignment_no_match",
        "pipeline_status_owner",
        "nested_exit_code",
        "compound_command_ambiguous",
        "shell_parse_error",
        "empty_command",
        "incomplete_pipeline",
        "missing_shell_program",
    } <= vector_ids


def test_helper_behavior_change_changes_behavior_and_profile_digests() -> None:
    vector = ResolverConformanceVector.from_inputs(
        "nested_exit_code",
        {},
        {"extra": {"exit_code": 7}},
    )

    def nested_exit(observation):
        return observation["extra"]["exit_code"]

    def ignored_exit(observation):
        del observation
        return 0

    def resolver_using(helper):
        def resolve(arguments, observation):
            del arguments
            assert observation is not None
            code = helper(observation)
            if code == 0:
                return ResolverResult("success")
            return ResolverResult(
                "error",
                SemanticReasonCode.EXIT_CODE_ERROR,
                {"exit_code": code},
            )

        return resolve

    first_spec = ResolverSpec(
        "test-resolver",
        "1.0.0",
        resolver_using(nested_exit),
        (vector,),
    )
    second_spec = ResolverSpec(
        "test-resolver",
        "1.0.0",
        resolver_using(ignored_exit),
        (vector,),
    )
    reference = ResolverRef("test-resolver", "1.0.0")
    first_profile = TrajectorySemanticsProfile(
        profile_id="digest-test",
        version="1.0.0",
        description="digest test",
        tool_rules=(ToolMappingRule("search", "search", reference),),
        resolver_registry=ResolverRegistry((first_spec,)),
    )
    second_profile = TrajectorySemanticsProfile(
        profile_id="digest-test",
        version="1.0.0",
        description="digest test",
        tool_rules=(ToolMappingRule("search", "search", reference),),
        resolver_registry=ResolverRegistry((second_spec,)),
    )
    assert first_spec.behavior_digest != second_spec.behavior_digest
    assert first_profile.digest != second_profile.digest


def test_behavior_identical_refactor_keeps_behavior_digest_stable() -> None:
    first_vector = ResolverConformanceVector.from_inputs(
        "canonical_input",
        {"b": 2, "a": 1},
        {"status": "ok"},
    )
    second_vector = ResolverConformanceVector.from_inputs(
        "canonical_input",
        {"a": 1, "b": 2},
        {"status": "ok"},
    )

    def direct(arguments, observation):
        del arguments, observation
        return ResolverResult("success")

    def refactored(arguments, observation):
        del observation
        if arguments:
            return ResolverResult(outcome="success")
        return ResolverResult("success")

    direct_spec = ResolverSpec(
        "test-resolver",
        "1.0.0",
        direct,
        (first_vector,),
    )
    refactored_spec = ResolverSpec(
        "test-resolver",
        "1.0.0",
        refactored,
        (second_vector,),
    )
    assert direct_spec.behavior_digest == refactored_spec.behavior_digest


def test_profile_digest_includes_resolver_identity_and_version() -> None:
    vector = ResolverConformanceVector.from_inputs("success", {}, None)

    def resolve(arguments, observation):
        del arguments, observation
        return ResolverResult("success")

    first_spec = ResolverSpec("resolver-a", "1.0.0", resolve, (vector,))
    second_spec = ResolverSpec("resolver-b", "2.0.0", resolve, (vector,))
    assert first_spec.behavior_digest == second_spec.behavior_digest
    first_profile = TrajectorySemanticsProfile(
        "digest-test",
        "1.0.0",
        "digest test",
        (ToolMappingRule("search", "search", ResolverRef("resolver-a", "1.0.0")),),
        ResolverRegistry((first_spec,)),
    )
    second_profile = TrajectorySemanticsProfile(
        "digest-test",
        "1.0.0",
        "digest test",
        (ToolMappingRule("search", "search", ResolverRef("resolver-b", "2.0.0")),),
        ResolverRegistry((second_spec,)),
    )
    assert first_profile.digest != second_profile.digest


def test_custom_resolver_detail_is_digested_and_invalid_payloads_are_rejected(
    tmp_path: Path,
) -> None:
    secret = "TOP-SECRET resolver path=/private/hidden"
    vector = ResolverConformanceVector.from_inputs("error", {}, {"status": "failed"})

    def custom_resolver(arguments, observation):
        del arguments, observation
        return ResolverResult(
            "error",
            SemanticReasonCode.OBSERVATION_ERROR_STATUS,
            secret,
        )

    spec = ResolverSpec("custom", "1.0.0", custom_resolver, (vector,))
    profile = TrajectorySemanticsProfile(
        "custom",
        "1.0.0",
        "custom",
        (ToolMappingRule("custom", "execute", ResolverRef("custom", "1.0.0")),),
        ResolverRegistry((spec,)),
    )
    facts = _extract(
        {
            "steps": [
                {
                    "step_id": 0,
                    "source": "agent",
                    "tool_calls": [{"tool_name": "custom"}],
                    "observations": [{"status": "failed"}],
                }
            ]
        },
        profile,
    )
    assert facts[0].reason_code is SemanticReasonCode.OBSERVATION_ERROR_STATUS
    assert facts[0].detail_digest is not None
    assert facts[0].detail_size == len(secret.encode())
    output = project_semantic_actions_parquet(facts, tmp_path / "custom.parquet")
    assert secret.encode() not in output.read_bytes()

    def tuple_resolver(arguments, observation):
        del arguments, observation
        return "error", secret

    with pytest.raises(TypeError, match="must return ResolverResult"):
        ResolverSpec("tuple", "1.0.0", tuple_resolver, (vector,))  # type: ignore[arg-type]

    def freeform_reason(arguments, observation):
        del arguments, observation
        return ResolverResult("error", "raw secret reason")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="SemanticReasonCode"):
        ResolverSpec("freeform", "1.0.0", freeform_reason, (vector,))


def test_real_atif_projection_preserves_mechanical_bytes_and_queries_coverage(
    tmp_path: Path,
) -> None:
    job = load_job(FIXTURES / "job-fail")
    mechanical_tables, failures = project_jobs([job], tmp_path)
    assert failures == ()
    mechanical_path = next(
        table.path for table in mechanical_tables if table.table == "agent_actions"
    )
    mechanical_before = mechanical_path.read_bytes()

    binding = TaskProfileBinding.from_profile("lab/demo", GENERIC_POSIX_PROFILE)
    result = project_job_semantics(
        [job],
        bindings=[binding],
        output_root=tmp_path,
        query_threshold=1.0,
    )
    assert mechanical_path.read_bytes() == mechanical_before
    assert result.coverage[0].status == "analysis_ready"
    assert result.coverage[0].query_threshold == 1.0
    assert any(path.name == "semantic_action_facts.parquet" for path in result.files)

    queried = query_semantic_coverage(tmp_path, query_threshold=0.75)
    assert len(queried) == 1
    assert queried[0].query_threshold == 0.75
    assert queried[0].coverage_fraction == 1.0
    assert queried[0].status == "analysis_ready"


def test_unknown_semantics_below_threshold_is_screening_only() -> None:
    facts = _extract(
        {
            "steps": [
                {
                    "step_id": 0,
                    "source": "agent",
                    "tool_calls": [{"tool_name": "unknown-tool"}],
                    "observations": [{"status": "ok"}],
                }
            ]
        },
        GENERIC_POSIX_PROFILE,
        strict=False,
    )
    binding = TaskProfileBinding.from_profile("task-1", GENERIC_POSIX_PROFILE)
    coverage = semantic_coverage(
        facts,
        job_id="job-1",
        trial_id="trial-1",
        binding=binding,
        query_threshold=0.5,
    )
    assert coverage.coverage_fraction == 0.0
    assert coverage.status == "screening_only"


def test_query_marks_historical_profile_evidence_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = load_job(FIXTURES / "job-fail")
    binding = TaskProfileBinding.from_profile("lab/demo", GENERIC_POSIX_PROFILE)
    project_job_semantics(
        [job],
        bindings=[binding],
        output_root=tmp_path,
        query_threshold=1.0,
    )
    evolved_profile = TrajectorySemanticsProfile(
        profile_id=GENERIC_POSIX_PROFILE.profile_id,
        version="99.0.0",
        description="intentionally evolved profile",
        tool_rules=GENERIC_POSIX_PROFILE.tool_rules,
    )
    monkeypatch.setitem(
        BENCHMARK_PROFILES,
        GENERIC_POSIX_PROFILE.profile_id,
        evolved_profile,
    )
    queried = query_semantic_coverage(tmp_path, query_threshold=0.5)
    assert queried[0].coverage_fraction == 1.0
    assert queried[0].status == "stale_profile"


def test_projection_requires_explicit_task_binding(tmp_path: Path) -> None:
    job = load_job(FIXTURES / "job-fail")
    with pytest.raises(
        ValueError,
        match="task has no semantics profile binding: lab/demo",
    ):
        project_job_semantics(
            [job],
            bindings=[],
            output_root=tmp_path,
            query_threshold=1.0,
        )


def test_semantics_project_and_coverage_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = Path(__file__).parents[1]
    result = run_cli(
        [
            "semantics",
            "project",
            str(FIXTURES / "job-fail"),
            "--bind",
            "lab/demo=posix",
            "--output-dir",
            str(tmp_path),
            "--coverage-threshold",
            "1.0",
            "--json",
        ],
        workspace=workspace,
    )
    assert result == 0
    projected = json.loads(capsys.readouterr().out)
    assert projected["coverage"][0]["query_threshold"] == 1.0

    result = run_cli(
        [
            "semantics",
            "coverage",
            "--derived-dir",
            str(tmp_path),
            "--threshold",
            "0.5",
        ],
        workspace=workspace,
    )
    assert result == 0
    coverage = json.loads(capsys.readouterr().out)
    assert coverage[0]["query_threshold"] == 0.5
    assert coverage[0]["status"] == "analysis_ready"


def test_deterministic_shuffled_input_parquet_projection(tmp_path: Path) -> None:
    """Projecting shuffled action lists produces identical Parquet bytes."""
    profile = GENERIC_POSIX_PROFILE
    atif_data = {
        "trial_id": "trial_det",
        "steps": [
            {
                "step_id": i,
                "source": "agent",
                "tool_calls": [{"tool_name": "bash", "command": f"echo step_{i}"}],
                "observations": [{"exit_code": 0, "output": f"step_{i}"}],
            }
            for i in range(10)
        ],
    }

    facts_original = _extract(atif_data, profile)

    facts_shuffled = list(facts_original)
    random.seed(42)
    random.shuffle(facts_shuffled)

    out1 = project_semantic_actions_parquet(facts_original, tmp_path / "t1.parquet")
    out2 = project_semantic_actions_parquet(facts_shuffled, tmp_path / "t2.parquet")

    bytes1 = out1.read_bytes()
    bytes2 = out2.read_bytes()

    assert bytes1 == bytes2
    assert len(bytes1) > 0

    table = pq.read_table(out1)
    assert table.num_rows == 10
    assert table.schema == SEMANTIC_ACTION_FACT_SCHEMA


def test_no_raw_secret_or_argument_text_in_projection(tmp_path: Path) -> None:
    """Sensitive argument strings and passwords are never written to the Parquet projection."""
    secret_token = "SUPER_SECRET_OAUTH_BEARER_TOKEN_999888"
    secret_prompt = "INTERNAL_SYSTEM_PROMPT_WITH_CONFIDENTIAL_INSTRUCTIONS"

    atif_data = {
        "trial_id": "trial_sec",
        "steps": [
            {
                "step_id": 0,
                "source": "agent",
                "tool_calls": [
                    {
                        "tool_name": "bash",
                        "command": f"curl -H 'Authorization: Bearer {secret_token}' https://api.example.com",
                    }
                ],
                "observations": [
                    {
                        "exit_code": 2,
                        "error": secret_prompt,
                    }
                ],
            }
        ],
    }

    facts = _extract(atif_data, GENERIC_POSIX_PROFILE)
    out = project_semantic_actions_parquet(facts, tmp_path / "sec.parquet")

    raw_bytes = out.read_bytes()
    raw_text = raw_bytes.decode("latin1", errors="ignore")

    assert secret_token not in raw_text
    assert secret_prompt not in raw_text

    # The SHA256 digest is present instead
    assert facts[0].arguments_sha256.startswith("sha256:")
    assert facts[0].observation_sha256.startswith("sha256:")


def test_benchmark_profiles_resolution() -> None:
    """LOCA, AgentAbstain, and DeepPlanning profiles resolve domain actions with no fallback."""
    from evallab.trajectory_semantics import (
        AGENTABSTAIN_PROFILE,
        DEEPPLANNING_PROFILE,
        LOCA_PROFILE,
        get_profile,
    )

    assert get_profile("loca") == LOCA_PROFILE
    assert get_profile("agentabstain") == AGENTABSTAIN_PROFILE
    assert get_profile("deepplanning") == DEEPPLANNING_PROFILE

    # 1. LOCA long-context tool sequence
    loca_atif = {
        "trial_id": "loca_trial_1",
        "steps": [
            {
                "step_id": 0,
                "source": "agent",
                "tool_calls": [{"tool_name": "search_files", "pattern": "*.py"}],
                "observations": [{"status": "ok", "files": ["main.py", "eval.py"]}],
            },
            {
                "step_id": 1,
                "source": "agent",
                "tool_calls": [{"tool_name": "retrieve_context", "query": "find auth function"}],
                "observations": [{"status": "ok", "context": "def authenticate(): ..."}],
            },
            {
                "step_id": 2,
                "source": "agent",
                "tool_calls": [{"tool_name": "submit", "answer": "auth in main.py"}],
                "observations": [{"status": "ok"}],
            },
        ],
    }
    loca_facts = _extract(loca_atif, LOCA_PROFILE, strict=True)
    assert len(loca_facts) == 3
    assert [f.role for f in loca_facts] == ["search", "read", "terminate"]
    assert [f.outcome for f in loca_facts] == ["success", "success", "success"]

    # 2. AgentAbstain ambiguous vs critical actions
    abstain_atif = {
        "trial_id": "agentabstain_trial_1",
        "steps": [
            {
                "step_id": 0,
                "source": "agent",
                "tool_calls": [{"tool_name": "spotify.get_playlist_tracks", "playlist_id": "pl1"}],
                "observations": [{"tracks": ["Song A", "Song B"]}],
            },
            {
                "step_id": 1,
                "source": "agent",
                "tool_calls": [
                    {"tool_name": "spotify.write_gmail_draft", "subject": "Songs", "body": "Tracks"}
                ],
                "observations": [{"draft_id": "d1", "status": "draft_saved"}],
            },
            {
                "step_id": 2,
                "source": "agent",
                "tool_calls": [{"tool_name": "abstain", "reason": "Ambiguous action avoided"}],
                "observations": [{"status": "recorded"}],
            },
        ],
    }
    abstain_facts = _extract(abstain_atif, AGENTABSTAIN_PROFILE, strict=True)
    assert len(abstain_facts) == 3
    assert [f.role for f in abstain_facts] == ["read", "write", "terminate"]
    assert [f.outcome for f in abstain_facts] == ["success", "success", "success"]

    # 3. DeepPlanning shopping and constraints workflow
    planning_atif = {
        "trial_id": "deepplanning_trial_1",
        "steps": [
            {
                "step_id": 0,
                "source": "agent",
                "tool_calls": [{"tool_name": "search_items", "category": "electronics"}],
                "observations": [{"items": [{"id": 1, "price": 500}]}],
            },
            {
                "step_id": 1,
                "source": "agent",
                "tool_calls": [{"tool_name": "check_constraints", "budget": 1000, "items": [1]}],
                "observations": [{"valid": True, "remaining": 500}],
            },
            {
                "step_id": 2,
                "source": "agent",
                "tool_calls": [{"tool_name": "add_to_cart", "item_id": 1}],
                "observations": [{"status": "added"}],
            },
            {
                "step_id": 3,
                "source": "agent",
                "tool_calls": [{"tool_name": "submit_plan", "plan_id": "p1"}],
                "observations": [{"status": "accepted"}],
            },
        ],
    }
    planning_facts = _extract(planning_atif, DEEPPLANNING_PROFILE, strict=True)
    assert len(planning_facts) == 4
    assert [f.role for f in planning_facts] == ["search", "inspect", "execute", "terminate"]
    assert [f.outcome for f in planning_facts] == ["success", "success", "success", "success"]
