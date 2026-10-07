"""Behavioral boundaries for the read-only task-dossier evidence slice."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from evallab.audit_mimo import task_evidence

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


def _write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def _ledger_row(**overrides: str) -> dict:
    row = {
        "task_id": TASK_A,
        "status": "usable",
        "reason": "nop sound",
        "run": "original",
        "run_digest": DIGEST_A,
        "run_variant_status": "",
        "verdict": "keep",
        "verdict_evidence": "",
        "leak_channel": "none_found",
        "evidence": "research/experiments/har108-python-census/task_health.parquet",
        "census_label": "sound",
    }
    row.update(overrides)
    return row


def _history_row(**overrides: str) -> dict:
    row = {
        "task_id": TASK_A,
        "status": "usable",
        "runs": "3",
        "clean_pass": "0",
        "copied_pass": "1",
        "fail": "1",
        "infra": "1",
        "last_run": "2026-10-06T22:15:53Z",
        "agents": "evallab.harbor_mimoagent:NativeMimoAgent",
        "trials": "2026-10-06/HAR-168-a1/har168-a1__XavEDNw",
    }
    row.update(overrides)
    return row


def _failing_row(**overrides: str) -> dict:
    row = {column: "" for column in FAILING_COLUMNS}
    row.update(
        {
            "task_id": TASK_A,
            "runs": "2",
            "unique_runs": "2",
            "models": "1",
            "model_names": json.dumps(["MiMo-V2.6-Distill-Qwen-9B"]),
            "verifier_runs": "1",
            "model_evidence_state": "partial",
            "verifier_sources": json.dumps(["missing", "test-stdout.unittest"]),
            "candidate_broken_test": "False",
            "notes": json.dumps(["incomplete_verifier_evidence:base"]),
        }
    )
    row.update(overrides)
    return row


def _probe_row(**overrides) -> dict:
    row = {
        "verdict": "leak-found-not-cracked",
        "reward": None,
        "image": ["git past base (beyond_base=0 unreachable_commits=1768)"],
        "signals": [],
        "copied": None,
        "task_package_digest": DIGEST_A,
        "trial": "/nonexistent/har161r-exploit-000792/trial__mZf2vzf",
        "probe_job": "har161r-exploit-000792",
    }
    row.update(overrides)
    return row


def _strip_record(
    *,
    status: str = "candidate",
    variant: str = DIGEST_OTHER,
    parent: str = DIGEST_A,
    created: str = "2026-10-06T22:21:28Z",
    transform: str = "strip-future-history@1",
) -> dict:
    return {
        "schema": "evallab.task_variant/v1",
        "task_name": f"mimo-v2.6-rl/{TASK_A}",
        "variant_digest": variant,
        "variant_harbor_digest": HARBOR_A,
        "parent": {
            "digest": parent,
            "harbor_digest": HARBOR_A,
            "source": {
                "kind": "hf",
                "repo": "FineEnvs/MiMo-V2.6-RL-harbor-code",
                "revision": "5746e2f0",
                "path": f"tasks/{TASK_A}",
                "record": None,
            },
        },
        "transform": transform,
        "components_changed": ["environment"],
        "files": [
            {
                "path": "environment/setup/setup.sh",
                "before_sha256": parent,
                "after_sha256": variant,
                "content": "# stripped\n",
            }
        ],
        "rationale": "strip git history beyond the recorded base",
        "inputs": {"leak_patterns": ["refs-beyond-base", "unreachable-commits"]},
        "created_by": "har177-default-strip",
        "created_at": created,
        "status": status,
        "evidence": [],
    }


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    _write_csv(
        tmp_path / "research/experiments/python-task-ledger/ledger.csv",
        LEDGER_COLUMNS,
        [_ledger_row()],
    )
    _write_csv(
        tmp_path / "research/experiments/python-task-ledger/task_history.csv",
        HISTORY_COLUMNS,
        [_history_row()],
    )
    _write_csv(
        tmp_path / "research/experiments/har122-egress-lock/har146-locked-nop.csv",
        LOCKED_COLUMNS,
        [{"task_id": TASK_A, "locked_nop": "sound", "note": ""}],
    )
    _write_csv(
        tmp_path / "research/experiments/python-task-ledger/failing_tests.csv",
        FAILING_COLUMNS,
        [_failing_row()],
    )
    probe_path = tmp_path / "research/experiments/har161-exploit/probe_verdicts.json"
    probe_path.parent.mkdir(parents=True, exist_ok=True)
    probe_path.write_text(json.dumps({TASK_A: _probe_row()}), encoding="utf-8")
    record_dir = tmp_path / f"library/task-variants/mimo-v2.6-rl__{TASK_A}"
    record_dir.mkdir(parents=True, exist_ok=True)
    record = _strip_record()
    filename = record["variant_digest"].removeprefix("sha256:")[:12] + ".json"
    (record_dir / filename).write_text(json.dumps(record), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "unsafe",
    ["../evil", "task/id", "task\\id", "task*id", "task?id", "task\nid", "", ".hidden"],
)
def test_unsafe_task_id_rejected(repo_root: Path, unsafe: str) -> None:
    with pytest.raises(ValueError, match="invalid task id"):
        task_evidence(unsafe, repo_root=repo_root)


def test_non_mimo_leaf_id_returns_nulls(tmp_path: Path) -> None:
    _write_csv(
        tmp_path / "research/experiments/python-task-ledger/ledger.csv",
        LEDGER_COLUMNS,
        [],
    )
    result = task_evidence("event-summary", repo_root=tmp_path)
    assert result["health"] is None
    assert result["leak"] is None
    assert result["repair"] is None


def test_truly_missing_task_stays_null(tmp_path: Path) -> None:
    _write_csv(
        tmp_path / "research/experiments/python-task-ledger/ledger.csv",
        LEDGER_COLUMNS,
        [],
    )
    _write_csv(
        tmp_path / "research/experiments/python-task-ledger/task_history.csv",
        HISTORY_COLUMNS,
        [],
    )
    _write_csv(
        tmp_path / "research/experiments/har122-egress-lock/har146-locked-nop.csv",
        LOCKED_COLUMNS,
        [],
    )
    _write_csv(
        tmp_path / "research/experiments/python-task-ledger/failing_tests.csv",
        FAILING_COLUMNS,
        [],
    )
    result = task_evidence(TASK_A, repo_root=tmp_path)
    assert result["health"] is None
    assert result["verdict"] is None
    assert result["tags"] is None
    assert result["leak"] is None
    assert result["repair"] is None
    assert result["static_flags"] is None
    assert result["failing_tests"] is None
    assert result["exploit_probes"] is None
    assert result["sources"]["exploit"]["available"] is False


def test_zero_model_runs_distinct_from_unknown(tmp_path: Path) -> None:
    _write_csv(
        tmp_path / "research/experiments/python-task-ledger/ledger.csv",
        LEDGER_COLUMNS,
        [_ledger_row(task_id=TASK_B, run_digest=DIGEST_OTHER)],
    )
    _write_csv(
        tmp_path / "research/experiments/python-task-ledger/task_history.csv",
        HISTORY_COLUMNS,
        [
            {
                "task_id": TASK_B,
                "status": "usable",
                "runs": "0",
                "clean_pass": "0",
                "copied_pass": "0",
                "fail": "0",
                "infra": "0",
                "last_run": "",
                "agents": "",
                "trials": "",
            }
        ],
    )
    _write_csv(
        tmp_path / "research/experiments/har122-egress-lock/har146-locked-nop.csv",
        LOCKED_COLUMNS,
        [{"task_id": TASK_B, "locked_nop": "sound", "note": ""}],
    )
    _write_csv(
        tmp_path / "research/experiments/python-task-ledger/failing_tests.csv",
        FAILING_COLUMNS,
        [
            _failing_row(
                task_id=TASK_B,
                runs="0",
                unique_runs="0",
                models="0",
                model_names="[]",
                model_evidence_state="no_model_runs",
                verifier_sources="[]",
                candidate_broken_test="",
                notes="",
            )
        ],
    )
    zero = task_evidence(TASK_B, repo_root=tmp_path)
    assert zero["tags"] is not None and "solve:never-run" in zero["tags"]
    assert zero["failing_tests"] is not None
    assert zero["failing_tests"]["model_evidence_state"] == "no_model_runs"
    assert zero["failing_tests"]["model_names"] == []  # recorded empty, kept
    assert zero["failing_tests"]["notes"] is None  # missing cell stays null
    assert zero["exploit_probes"] is None  # not probed: null, not fabricated

    unknown = task_evidence(TASK_A, repo_root=tmp_path)
    assert unknown["health"] is None  # no ledger row at all
    assert unknown["failing_tests"] is None


def test_bound_leak_overrides_stale_channel(repo_root: Path) -> None:
    result = task_evidence(TASK_A, repo_root=repo_root)
    assert result["tags"] is not None and "health:leak-found" in result["tags"]
    assert result["health"] is not None
    assert result["health"]["exploit_verdict"] == "leak-found-not-cracked"
    assert result["health"]["digest_match"] is True
    leak = result["leak"]
    assert leak is not None
    assert leak["found"] is True
    assert leak["channel"] == "observed:leak-found-not-cracked"
    assert leak["ledger_channel"] == "none_found"  # stale channel preserved
    assert result["exploit_probes"] is not None
    assert result["exploit_probes"][0]["bound"] is True


def test_candidate_repair_never_called_validated(repo_root: Path) -> None:
    result = task_evidence(TASK_A, repo_root=repo_root)
    repair = result["repair"]
    assert repair is not None
    assert repair["variant_digest"] == DIGEST_OTHER
    assert repair["parent_digest"] == DIGEST_A
    assert repair["status"] == "candidate"
    assert repair["transform"] == "strip-future-history@1"
    assert repair["leak_patterns"] == ["refs-beyond-base", "unreachable-commits"]


def test_digest_mismatched_probe_not_claimed_current(repo_root: Path) -> None:
    probe_path = repo_root / "research/experiments/har161-exploit/probe_verdicts.json"
    probe_path.write_text(
        json.dumps({TASK_A: _probe_row(task_package_digest=DIGEST_OTHER)}),
        encoding="utf-8",
    )
    result = task_evidence(TASK_A, repo_root=repo_root)
    assert result["exploit_probes"] is not None
    assert result["exploit_probes"][0]["bound"] is False
    assert result["exploit_probes"][0]["digest_match"] is False
    assert result["tags"] is not None and "health:sound" in result["tags"]
    assert result["leak"] is not None
    assert result["leak"]["found"] is False
    assert result["leak"]["channel"] == "none_found"


def test_unbound_probe_shown_as_unbound(repo_root: Path) -> None:
    probe_path = repo_root / "research/experiments/har161-exploit/probe_verdicts.json"
    row = _probe_row()
    del row["task_package_digest"]
    row["trial"] = "/nonexistent/nowhere/trial__zzz"
    probe_path.write_text(json.dumps({TASK_A: row}), encoding="utf-8")
    result = task_evidence(TASK_A, repo_root=repo_root)
    assert result["exploit_probes"] is not None
    probe = result["exploit_probes"][0]
    assert probe["bound"] is False
    assert probe["binding"] == {"package": None, "harbor": None}
    assert result["leak"] is not None and result["leak"]["found"] is False


def test_static_absent_and_stale_labels_ignored(repo_root: Path, tmp_path: Path) -> None:
    assert task_evidence(TASK_A, repo_root=repo_root)["static_flags"] is None

    audit = tmp_path / "static-task-audit.csv"
    with audit.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "task_id",
                "ledger_status",
                "health",
                "solve",
                "a_unstated_literal",
                "b_network",
                "c_nondeterminism",
                "d_env_coupled",
                "e_tiny_suite",
                "f_repo_file_read",
                "g_polyglot_toolchain",
                "ev_a",
                "ev_b",
                "ev_c",
                "ev_d",
                "ev_e",
                "ev_f",
                "ev_g",
                "instr_bytes",
                "patch_bytes",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "task_id": TASK_A,
                "ledger_status": "usable",
                "health": "health:sound",  # stale: predates probe evidence
                "solve": "solve:never-run",  # stale: predates recorded runs
                "a_unstated_literal": "1",
                "b_network": "1",
                "c_nondeterminism": "1",
                "d_env_coupled": "0",
                "e_tiny_suite": "0",
                "f_repo_file_read": "0",
                "g_polyglot_toolchain": "0",
                "ev_a": "n=3",
                "ev_b": "",
                "ev_c": "",
                "ev_d": "",
                "ev_e": "",
                "ev_f": "",
                "ev_g": "",
                "instr_bytes": "3150",
                "patch_bytes": "25978",
            }
        )
    result = task_evidence(TASK_A, repo_root=repo_root, static_audit=audit)
    static = result["static_flags"]
    assert static is not None
    assert static["flags"]["a_unstated_literal"] is True
    assert static["flags"]["d_env_coupled"] is False
    assert static["instr_bytes"] == 3150
    # Stale labels carry no authority: echoed only under ignored_stale.
    assert "tag" not in static
    assert static["ignored_stale"] == {
        "health": "health:sound",
        "solve": "solve:never-run",
        "ledger_status": "usable",
    }
    # The computed facet still reflects the bound probe, not the stale audit.
    assert result["tags"] is not None and "health:leak-found" in result["tags"]


def test_malformed_static_reported_never_clean(repo_root: Path, tmp_path: Path) -> None:
    audit = tmp_path / "static-task-audit.csv"
    with audit.open("w", newline="", encoding="utf-8") as handle:
        handle.write(
            "task_id,a_unstated_literal,b_network,instr_bytes\n"
            f"{TASK_A},maybe,0,abc\n"
        )
    static = task_evidence(TASK_A, repo_root=repo_root, static_audit=audit)[
        "static_flags"
    ]
    assert static is not None
    assert "error" in static
    assert static["flags"]["a_unstated_literal"] is None
    assert static["instr_bytes"] is None


def test_failing_lists_and_coverage_retained(repo_root: Path) -> None:
    failing = task_evidence(TASK_A, repo_root=repo_root)["failing_tests"]
    assert failing is not None
    assert failing["runs"] == 2
    assert failing["model_names"] == ["MiMo-V2.6-Distill-Qwen-9B"]
    assert failing["verifier_sources"] == ["missing", "test-stdout.unittest"]
    assert failing["candidate_broken_test"] is False
    assert failing["model_evidence_state"] == "partial"
    assert failing["notes"] == ["incomplete_verifier_evidence:base"]
    assert failing["parse_errors"] == []

@pytest.mark.parametrize(
    ("channel", "expected_channel"),
    [("unknown", "unknown"), ("", None)],
)
def test_unknown_or_missing_channel_is_not_a_finding(
    tmp_path: Path, channel: str, expected_channel: str | None
) -> None:
    _write_csv(
        tmp_path / "research/experiments/python-task-ledger/ledger.csv",
        LEDGER_COLUMNS,
        [_ledger_row(leak_channel=channel)],
    )
    result = task_evidence(TASK_A, repo_root=tmp_path)
    assert result["leak"] is not None
    assert result["leak"]["found"] is None
    assert result["leak"]["channel"] == expected_channel
    assert result["leak"]["ledger_channel"] == expected_channel


def test_applied_repair_preferred_over_newer_parent_candidate(tmp_path: Path) -> None:
    _write_csv(
        tmp_path / "research/experiments/python-task-ledger/ledger.csv",
        LEDGER_COLUMNS,
        [
            _ledger_row(
                run="repair",
                run_digest=V_APPLIED,
                run_variant_status="candidate",
                reason="repaired candidate",
            )
        ],
    )
    record_dir = tmp_path / f"library/task-variants/mimo-v2.6-rl__{TASK_A}"
    record_dir.mkdir(parents=True, exist_ok=True)
    (record_dir / "applied.json").write_text(
        json.dumps(
            _strip_record(
                variant=V_APPLIED, parent=DIGEST_A, created="2026-10-05T00:00:00Z"
            )
        ),
        encoding="utf-8",
    )
    (record_dir / "newer.json").write_text(
        json.dumps(
            _strip_record(
                variant=V_NEWER, parent=V_APPLIED, created="2026-10-06T00:00:00Z"
            )
        ),
        encoding="utf-8",
    )
    result = task_evidence(TASK_A, repo_root=tmp_path)
    repair = result["repair"]
    assert repair is not None
    assert repair["variant_digest"] == V_APPLIED
    assert repair["parent_digest"] == DIGEST_A
    assert repair["status"] == "candidate"
    digests = [item["variant_digest"] for item in repair["records"]]
    assert digests == [V_APPLIED, V_NEWER]
    assert {item["status"] for item in repair["records"]} == {"candidate"}
