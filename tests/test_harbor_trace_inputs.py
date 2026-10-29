"""Label-free whole-run Harbor input preparation against toy sources.

Tiny temporary SQLite/answer-key fixtures only — never the shared corpus, host
credentials, Docker, network or models. Real native Harbor parsers validate
staged artifacts when the pinned laminar extra is installed.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from evallab.production.cloud_trace_db import CloudTraceDatabase
from evallab.production.cloud_trace_export import _blind_id
from evallab.production.corpus_queue import CorpusRegistry
from evallab.readers import harbor_trace_inputs as hti

try:
    _HARBOR_VERSION = importlib.metadata.version("harbor")
except importlib.metadata.PackageNotFoundError:
    _HARBOR_VERSION = None

pytestmark = pytest.mark.skipif(
    _HARBOR_VERSION != hti.PINNED_HARBOR_VERSION,
    reason=f"requires installed Harbor {hti.PINNED_HARBOR_VERSION} (optional laminar extra)",
)

DATASET = "mimo-defect-map-v1"
SYSTEM_TEXT = "You are operating on the staged fixture."
USER_TEXT = "Inspect the local output and report it."
ASSISTANT_TEXT = "I will run the inspection command."
FINAL_TEXT = "The literal output is preserved."
COMMAND = "printf 'alpha  beta\\n'"
OBS_TEXT = "alpha  beta\n"
OBS_PREVIEW = "return code 0\n"
LONG_TEXT = "αβγ unicode observation line\n" * 400
SECRET = "sk-" + "9" * 24

# (task_id, stored split, item_id, assignment row or None)
TASKS = (
    ("task-dev-a", "DEV", "dev-item-a", f"{DATASET},dev-item-a,task-dev-a,dev"),
    ("task-dev-b", "DEV", "dev-item-b", f"{DATASET},dev-item-b,task-dev-b,dev"),
    ("task-test", "TEST", "test-item", f"{DATASET},test-item,task-test,test"),
    ("task-lower", "dev", "lower-item", f"{DATASET},lower-item,task-lower,dev"),
    ("task-crosskey", "DEV", "cross-item", None),
    ("task-unassigned", "DEV", "unassigned-item", None),
)
CROSS_KEY_ROW = "tb4-audit-v1,cross-item,task-crosskey,test"


def _repo(tmp_path: Path, *, assignments: bool = True) -> Path:
    root = tmp_path / "repo"
    keys = root / "research/answer-keys"
    for dataset in ("tb4-audit-v1", "cheatbench-actions-v1", "hacktrace-act-v1"):
        (keys / dataset).mkdir(parents=True, exist_ok=True)
        (keys / dataset / "items.jsonl").write_text("", encoding="utf-8")
    (keys / "tb4-audit-v1" / "tasks.jsonl").write_text("", encoding="utf-8")
    inventory = root / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory.parent.mkdir(parents=True, exist_ok=True)
    inventory.write_text(
        json.dumps({
            "dataset_id": DATASET,
            "tasks": [],
            "heldout_task_ids_excluded_from_training_all_versions": [],
        }),
        encoding="utf-8",
    )
    if assignments:
        rows = [row for *_rest, row in TASKS if row] + [CROSS_KEY_ROW]
        path = keys / "splits/assignments.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "key_id,item_id,task,split\n" + "\n".join(rows) + "\n", encoding="utf-8"
        )
    return root


def _step(
    index: int,
    role: str,
    *,
    tool: str | None = None,
    command: str | None = None,
    content: str | None = "",
    preview: str | None = None,
    cas: str | None = None,
    flags: dict[str, Any] | list[Any] | None = None,
) -> dict[str, Any]:
    return {
        "step_index": index,
        "role": role,
        "tool_name": tool,
        "command_or_action": command,
        "content_text": content,
        "observation_preview": preview,
        "observation_cas_uri": cas,
        "flags_json": {} if flags is None else flags,
    }


def _good_steps(*, base: int = 0) -> list[dict[str, Any]]:
    return [
        _step(base + 2, "system", content=SYSTEM_TEXT),
        _step(base + 7, "user", content=USER_TEXT),
        _step(
            base + 13, "assistant", tool="run_shell",
            command=COMMAND, content=ASSISTANT_TEXT,
        ),
        _step(base + 19, "tool", content=OBS_TEXT, preview=OBS_PREVIEW),
        _step(base + 31, "assistant", content=FINAL_TEXT),
    ]


def _trace(
    database: CloudTraceDatabase, trace_id: str, task_id: str, steps: list,
    *, model_id: str | None = None, reward: float | None = 1.0,
) -> None:
    database.upsert_trace(
        {
            "trace_id": trace_id,
            "run_id": "private-run-" + trace_id,
            "task_id": task_id,
            "model_id": model_id if model_id is not None else "private-model-" + trace_id,
            "outcome": "private-outcome",
            "reward": reward,
            "is_exploit": False,
            "is_task_defect_fail": False,
        },
        steps=steps,
    )


@pytest.fixture
def corpus(tmp_path: Path) -> tuple[Path, Path, CorpusRegistry]:
    """Repo answer keys, a toy cloud DB and a real registry rooted at the repo."""
    root = _repo(tmp_path)
    db_path = tmp_path / "cloud.sqlite"
    with CloudTraceDatabase(db_path, sqlite_fts=False) as database:
        for task_id, split, item_id, _row in TASKS:
            database.upsert_task({
                "task_id": task_id,
                "dataset_id": DATASET,
                "split": split,
                "instruction_text": "hidden task instruction",
                "metadata_json": {"item_id": item_id},
            })
        _trace(database, "trace-alpha", "task-dev-a", _good_steps())
        _trace(database, "trace-beta", "task-dev-b", _good_steps(base=1), reward=0.0)
        _trace(
            database, "trace-long", "task-dev-b",
            [
                _step(1, "user", content=USER_TEXT),
                _step(
                    2, "assistant", tool="run_shell", command=COMMAND,
                    content=LONG_TEXT,
                ),
                _step(3, "tool", content=LONG_TEXT),
            ],
        )
        for trace_id, task_id in (
            ("trace-test", "task-test"),
            ("trace-lower", "task-lower"),
            ("trace-crosskey", "task-crosskey"),
            ("trace-unassigned", "task-unassigned"),
        ):
            _trace(database, trace_id, task_id, _good_steps())
        _trace(
            database, "trace-journal", "task-dev-a",
            [
                _step(0, "assistant", content="controller event",
                      flags={"trace_kind": "seeker_event_journal",
                             "is_model_message": False}),
                _step(1, "tool", content="controller receipt",
                      flags={"trace_kind": "seeker_event_journal"}),
            ],
        )
        _trace(
            database, "trace-unknown-kind", "task-dev-a",
            [_step(0, "assistant", content="opaque record",
                   flags={"trace_kind": "future_unknown_source_kind"})],
        )
        _trace(
            database, "trace-incomplete", "task-dev-a",
            [_step(0, "user", content=USER_TEXT,
                   flags={"trace_kind": "retained_headless_prompt"})],
        )
        _trace(
            database, "trace-toolonly", "task-dev-a",
            [_step(0, "user", content=USER_TEXT), _step(1, "tool", content=OBS_TEXT)],
        )
        _trace(
            database, "trace-reasoning", "task-dev-a",
            [
                _step(0, "user", content=USER_TEXT),
                _step(1, "assistant", content=ASSISTANT_TEXT,
                      flags={"reasoning_content": "retained private chain"}),
            ],
        )
        _trace(database, "trace-evidence", "task-dev-a",
               _good_steps() + [
                   _step(41, "assistant", content="receipt row",
                         flags={"trace_kind": "native_execution_receipt_record"}),
               ])
        _trace(
            database, "trace-residual", "task-dev-a",
            [
                _step(0, "user", content=USER_TEXT),
                _step(1, "assistant", content=ASSISTANT_TEXT, flags={
                    "native_node_residual": {
                        "message": {
                            "tool_calls": [{
                                "id": "tc-1",
                                "function": {"name": "real_fn", "arguments": {"a": 1}},
                            }],
                        },
                    },
                }),
            ],
        )
        _trace(
            database, "trace-nativeargs", "task-dev-a",
            [
                _step(0, "user", content=USER_TEXT),
                _step(1, "assistant", content=ASSISTANT_TEXT,
                      command=json.dumps([{
                          "id": "call-1",
                          "function": {"name": "fn", "arguments": {"a": 1}},
                      }])),
            ],
        )
        _trace(
            database, "trace-toolassoc", "task-dev-a",
            [
                _step(0, "user", content=USER_TEXT),
                _step(1, "assistant", content=ASSISTANT_TEXT),
                _step(2, "tool", content=OBS_TEXT, flags={"tool_call_id": "call-9"}),
            ],
        )
        _trace(
            database, "trace-secret", "task-dev-a",
            [
                _step(0, "user", content=USER_TEXT),
                _step(1, "assistant", content=SECRET),
            ],
            model_id=SECRET,
        )
        _trace(
            database, "trace-secret-nested", "task-dev-a",
            [
                _step(0, "user", content=USER_TEXT),
                _step(1, "assistant", content=json.dumps({"wrapped": SECRET})),
            ],
        )
        _trace(
            database, "trace-secret-preview", "task-dev-a",
            [
                _step(0, "user", content=USER_TEXT),
                _step(1, "assistant", tool="run_shell", command="cat out",
                      content=ASSISTANT_TEXT),
                # The preview is exported but absent from blind_messages;
                # its credential value is also a redacted source identity.
                _step(2, "tool", content=OBS_TEXT, preview=SECRET),
            ],
            model_id=SECRET,
        )
        _trace(
            database, "trace-agentdup", "task-dev-a",
            [
                _step(0, "user", content=USER_TEXT),
                _step(1, "agent", content="observed line", preview="observed line"),
            ],
        )
        _trace(
            database, "trace-agentempty", "task-dev-a",
            [
                _step(0, "user", content=USER_TEXT),
                _step(1, "agent", content="observed line", preview=""),
            ],
        )
        _trace(
            database, "trace-notoolname", "task-dev-a",
            [
                _step(0, "user", content=USER_TEXT),
                _step(1, "assistant", command="run the check",
                      content=ASSISTANT_TEXT),
            ],
        )
        _trace(
            database, "trace-listflags", "task-dev-a",
            [
                _step(0, "user", content=USER_TEXT),
                _step(1, "assistant", content=ASSISTANT_TEXT),
                _step(2, "tool", content=OBS_TEXT, flags=[]),
            ],
        )
        _trace(
            database, "trace-agentgood", "task-dev-a",
            [
                _step(0, "user", content=USER_TEXT),
                _step(1, "agent", tool="run_shell", command="cat out",
                      content="observe line", preview="shell output 1\n"),
            ],
        )
        _trace(
            database, "trace-cas", "task-dev-a",
            [
                _step(0, "user", content=USER_TEXT),
                _step(1, "assistant", tool="run_shell", command="cat out",
                      content=ASSISTANT_TEXT),
                _step(2, "tool", content=OBS_TEXT, cas="cas://sha256/" + "0" * 64),
            ],
        )
    return root, db_path, CorpusRegistry(root)


def _cas_object(tmp_path: Path, text: str) -> Path:
    cas_root = tmp_path / "cas"
    digest = hashlib.sha256(text.encode()).hexdigest()
    (cas_root / "sha256").mkdir(parents=True, exist_ok=True)
    (cas_root / "sha256" / digest).write_text(text, encoding="utf-8")
    return cas_root


def _attach_cas(db_path: Path, trace_id: str, index: int, text: str) -> None:
    uri = "cas://sha256/" + hashlib.sha256(text.encode()).hexdigest()
    con = sqlite3.connect(db_path)
    con.execute(
        "UPDATE trace_steps SET observation_cas_uri = ? "
        "WHERE trace_id = ? AND step_index = ?",
        (uri, trace_id, index),
    )
    con.commit()
    con.close()


def _tree_bytes(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def test_preflight_reports_per_run_status_without_writes(corpus: tuple, tmp_path: Path) -> None:
    _root, db_path, registry = corpus
    before_files = {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}
    before_db = db_path.read_bytes()
    report = hti.preflight_harbor_inputs(
        db_path,
        trace_ids=[
            "trace-alpha", "trace-beta", "trace-long", "trace-listflags",
            "trace-agentgood", "trace-test", "trace-lower",
            "trace-crosskey", "trace-unassigned", "trace-missing",
            "trace-journal", "trace-unknown-kind", "trace-incomplete",
            "trace-toolonly", "trace-reasoning", "trace-evidence",
            "trace-residual", "trace-nativeargs", "trace-notoolname",
            "trace-toolassoc", "trace-agentdup", "trace-agentempty",
            "trace-secret", "trace-secret-nested", "trace-secret-preview",
            "trace-cas",
        ],
        registry=registry,
    )
    runs = report["runs"]
    for trace_id in (
        "trace-alpha", "trace-beta", "trace-long", "trace-listflags",
        "trace-agentgood",
    ):
        assert runs[trace_id]["stageable"] is True
        assert runs[trace_id]["admission"] == "admitted"
    assert runs["trace-alpha"]["actor_step_indices"] == [2, 7, 13, 19, 31]
    expected = {
        "trace-test": "db_non_dev_split",
        "trace-lower": "db_non_dev_split",
        "trace-crosskey": "registry_cross_key_or_sealed",
        "trace-unassigned": "registry_cross_key_or_sealed",
        "trace-missing": "unstored_run",
        "trace-journal": "actor_history_unavailable",
        "trace-unknown-kind": "actor_history_unavailable",
        "trace-incomplete": "actor_history_unavailable",
        "trace-toolonly": "no_actor_inputs",
        "trace-reasoning": "reasoning_not_exportable",
        "trace-evidence": "non_actor_records_exported",
        "trace-residual": "tool_calls_not_exportable",
        "trace-nativeargs": "tool_calls_not_exportable",
        "trace-notoolname": "tool_calls_not_exportable",
        "trace-toolassoc": "tool_call_association_not_exportable",
        "trace-agentdup": "actor_observation_not_exportable",
        "trace-agentempty": "actor_observation_not_exportable",
        "trace-secret": "secret_scan_rejected",
        "trace-secret-nested": "secret_scan_rejected",
        "trace-secret-preview": "secret_scan_rejected",
        "trace-cas": "cas_preview_unresolved",
    }
    for trace_id, category in expected.items():
        assert runs[trace_id]["stageable"] is False
        assert {ref["category"] for ref in runs[trace_id]["refusals"]} == {category}, (
            trace_id, runs[trace_id]["refusals"]
        )
        assert runs[trace_id]["admission"] == (
            "admitted" if runs[trace_id]["actor_messages"] is not None else category
        )
    assert report["summary"]["stageable"] == 5
    assert report["global_refusals"] == []
    assert report["backend"] == {
        "availability": "unknown",
        "approval": "unknown",
        "note": "",
        "evidence": "none",
    }
    assert report["funding"]["status"] == "unverified"
    assert report["cost"]["token_estimate_usd"] is None
    assert report["cost"]["total_cost_usd"] is None
    assert "input tokens" in report["cost"]["unknown"]
    assert "output tokens" in report["cost"]["unknown"]
    assert "input token rate" in report["cost"]["unknown"]
    assert "output token rate" in report["cost"]["unknown"]
    assert SECRET not in json.dumps(report)
    assert {p.relative_to(tmp_path) for p in tmp_path.rglob("*")} == before_files
    assert db_path.read_bytes() == before_db


def test_preflight_input_contract_and_backend_distinctions(corpus: tuple) -> None:
    _root, db_path, registry = corpus
    report = hti.preflight_harbor_inputs(
        db_path,
        trace_ids=["trace-alpha"],
        registry=registry,
        backend=hti.BackendInfo(availability="available", approval="unapproved", note="x"),
        cost=hti.CostAssumptions(
            input_tokens=1_000, output_tokens=500,
            pricing="flash-zai-list-2026-10-06",
        ),
    )
    contract = report["input_contract"]
    assert contract["available"] is True
    assert contract["harbor_version"] == "0.24.0"
    assert contract["pinned_version"] == hti.PINNED_HARBOR_VERSION
    assert contract["trajectory_contract_validates"] is True
    assert contract["task_config_validates"] is True
    assert contract["criteria"] == ["reward_hacking", "task_specification"]
    assert contract["template_assets_present"] is True
    backend = report["backend"]
    assert backend["availability"] == "available"
    assert backend["approval"] == "unapproved"
    assert backend["evidence"] == "caller-declared"
    cost = report["cost"]
    assert cost["supplied"]["input_tokens"] == 1_000
    assert cost["supplied"]["output_tokens"] == 500
    assert cost["input_component_usd"] == pytest.approx(1_000 * 0.15 / 1_000_000)
    assert cost["output_component_usd"] == pytest.approx(500 * 0.50 / 1_000_000)
    assert cost["token_estimate_usd"] == pytest.approx(
        (1_000 * 0.15 + 500 * 0.50) / 1_000_000
    )
    assert cost["total_cost_usd"] is None
    assert "retry/repeat attempts" in cost["unknown"]
    assert "sandbox/environment charges" in cost["unknown"]
    known = cost["known"]
    assert known["requested_runs"] == 1 and known["stageable_runs"] == 1
    assert known["projected_actor_messages"] == 5
    assert known["projected_actor_bytes"] > 0


def test_cost_components_report_input_only_knowledge(corpus: tuple) -> None:
    _root, db_path, registry = corpus
    report = hti.preflight_harbor_inputs(
        db_path,
        trace_ids=["trace-alpha"],
        registry=registry,
        cost=hti.CostAssumptions(input_tokens=2_000, input_rate_per_million=1.0),
    )
    cost = report["cost"]
    assert cost["input_component_usd"] == pytest.approx(2_000 * 1.0 / 1_000_000)
    assert cost["output_component_usd"] is None
    assert cost["token_estimate_usd"] is None
    assert cost["total_cost_usd"] is None
    assert "output tokens" in cost["unknown"]
    assert "output token rate" in cost["unknown"]


def test_cost_assumptions_reject_invalid_inputs(corpus: tuple) -> None:
    with pytest.raises(ValueError):
        hti.BackendInfo(availability="reachable")
    with pytest.raises(ValueError):
        hti.CostAssumptions(input_tokens=-1)
    with pytest.raises(ValueError):
        hti.CostAssumptions(input_tokens=True)
    with pytest.raises(ValueError):
        hti.CostAssumptions(cached_tokens=-1)
    with pytest.raises(ValueError):
        hti.CostAssumptions(cached_tokens=5)
    with pytest.raises(ValueError):
        hti.CostAssumptions(input_tokens=10, cached_tokens=11)
    with pytest.raises(ValueError):
        hti.CostAssumptions(input_rate_per_million=float("nan"))
    with pytest.raises(ValueError):
        hti.CostAssumptions(output_rate_per_million=-0.5)
    with pytest.raises(ValueError):
        hti.CostAssumptions(pricing="free")


def test_native_version_or_parser_absence_refuses_everything(
    corpus: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, db_path, registry = corpus
    monkeypatch.setattr(hti, "_native_version", lambda: "0.25.0")
    report = hti.preflight_harbor_inputs(
        db_path, trace_ids=["trace-alpha"], registry=registry,
    )
    assert report["input_contract"]["available"] is False
    assert report["global_refusals"][0]["category"] == "native_version_unsupported"
    assert report["runs"]["trace-alpha"]["stageable"] is False
    with pytest.raises(hti.HarborInputRefusal) as caught:
        hti.prepare_harbor_analysis_inputs(
            db_path, tmp_path / "staged", trace_ids=["trace-alpha"], registry=registry,
        )
    assert caught.value.refusals[0].category == "native_version_unsupported"
    assert not (tmp_path / "staged").exists()


def test_missing_native_parser_refuses_before_writes(
    corpus: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, db_path, registry = corpus
    monkeypatch.setitem(sys.modules, "harbor.models.task.task", None)
    with pytest.raises(hti.HarborInputRefusal) as caught:
        hti.prepare_harbor_analysis_inputs(
            db_path, tmp_path / "staged", trace_ids=["trace-alpha"], registry=registry,
        )
    assert caught.value.refusals[0].category == "native_contract_unavailable"
    assert not (tmp_path / "staged").exists()
    assert not list(tmp_path.glob(".harbor-trace-inputs-*"))


def test_missing_native_template_assets_are_contract_refusals(
    corpus: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyzer = pytest.importorskip("harbor.analyze.analyzer")
    _root, db_path, registry = corpus
    monkeypatch.setattr(
        analyzer, "ANALYZE_TASK_TEMPLATE_DIR", tmp_path / "absent-template"
    )
    report = hti.preflight_harbor_inputs(
        db_path, trace_ids=["trace-alpha"], registry=registry,
    )
    assert report["input_contract"]["available"] is False
    assert (
        report["global_refusals"][0]["category"] == "native_contract_incompatible"
    )
    assert report["runs"]["trace-alpha"]["stageable"] is False
    with pytest.raises(hti.HarborInputRefusal) as caught:
        hti.prepare_harbor_analysis_inputs(
            db_path, tmp_path / "staged", trace_ids=["trace-alpha"], registry=registry,
        )
    assert {ref.category for ref in caught.value.refusals} == {"native_assembly_failed"}
    assert not (tmp_path / "staged").exists()
    assert not list(tmp_path.glob(".harbor-trace-inputs-*"))


def test_native_response_model_accepts_withheld_outcomes() -> None:
    analyzer = pytest.importorskip("harbor.analyze.analyzer")
    rubric = analyzer.load_rubric(analyzer.DEFAULT_RUBRIC_PATH)
    response_model = analyzer.build_analyze_response_model(rubric)
    parsed = response_model.model_validate({
        "summary": "rewards, labels and task context are withheld and unknown",
        "checks": {
            criterion.name: {
                "outcome": "not_applicable",
                "explanation": "insufficient evidence by withheld design",
            }
            for criterion in rubric.criteria
        },
    })
    assert parsed.summary
    assert set(parsed.checks.model_dump()) == {
        criterion.name for criterion in rubric.criteria
    }


def test_native_verifier_and_trial_result_withheld_semantics() -> None:
    result_models = pytest.importorskip("harbor.models.trial.result")
    rewards_field = result_models.VerifierResult.model_fields["rewards"]
    assert type(None) in getattr(rewards_field.annotation, "__args__", ())
    result_models.VerifierResult.model_validate({"rewards": {"reward": 1.0}})
    withheld = result_models.VerifierResult.model_validate({"rewards": None})
    assert withheld.rewards is None
    with pytest.raises(ValueError):
        # Native rejects a numeric reward map containing a null reward: the
        # withheld contract omits verifier state, it never writes reward:null.
        result_models.VerifierResult.model_validate({"rewards": {"reward": None}})
    verifier_field = result_models.TrialResult.model_fields["verifier_result"]
    assert type(None) in getattr(verifier_field.annotation, "__args__", ())


@pytest.mark.parametrize("extra_row", [
    # item-only collision: dev-item-a marked TEST under another task
    "tb4-audit-v1,dev-item-a,other-task-x,test",
    # task-only collision: task-dev-a marked TEST under another item
    "tb4-audit-v1,other-item-x,task-dev-a,test",
])
def test_cross_key_test_mark_denies_dev_run_on_either_axis(
    corpus: tuple, tmp_path: Path, extra_row: str,
) -> None:
    root, db_path, _registry = corpus
    path = root / "research/answer-keys/splits/assignments.csv"
    path.write_text(path.read_text() + extra_row + "\n", encoding="utf-8")
    registry = CorpusRegistry(root)
    report = hti.preflight_harbor_inputs(
        db_path, trace_ids=["trace-alpha"], registry=registry,
    )
    run = report["runs"]["trace-alpha"]
    assert run["stageable"] is False
    assert {ref["category"] for ref in run["refusals"]} == {
        "registry_cross_key_or_sealed"
    }
    with pytest.raises(hti.HarborInputRefusal) as caught:
        hti.prepare_harbor_analysis_inputs(
            db_path, tmp_path / "staged", trace_ids=["trace-alpha"],
            registry=registry,
        )
    assert {ref.category for ref in caught.value.refusals} == {
        "registry_cross_key_or_sealed"
    }
    assert not (tmp_path / "staged").exists()
    assert not list(tmp_path.glob(".harbor-trace-inputs-*"))


def test_staged_input_ignores_local_grading_and_source_annotations(
    corpus: tuple, tmp_path: Path,
) -> None:
    _root, db_path, registry = corpus
    first, second = tmp_path / "staged-first", tmp_path / "staged-second"
    hti.prepare_harbor_analysis_inputs(
        db_path, first, trace_ids=["trace-alpha"], registry=registry,
    )
    con = sqlite3.connect(db_path)
    con.execute(
        "UPDATE agent_traces SET reward = 0.0, outcome = 'flagged-label', "
        "is_exploit = 1, is_task_defect_fail = 1, run_id = 'renamed-run', "
        "model_id = 'renamed-model', verified_receipt_json = '{}' "
        "WHERE trace_id = 'trace-alpha'"
    )
    con.execute(
        "UPDATE corpus_tasks SET original_grader_digest = 'digest-x', "
        "repaired_grader_digest = 'digest-y', instruction_text = 'other text' "
        "WHERE task_id = 'task-dev-a'"
    )
    con.commit()
    con.close()
    hti.prepare_harbor_analysis_inputs(
        db_path, second, trace_ids=["trace-alpha"], registry=registry,
    )
    assert _tree_bytes(first) == _tree_bytes(second)


def test_agent_named_call_and_complete_observation_stage(
    corpus: tuple, tmp_path: Path,
) -> None:
    trajectory_type = pytest.importorskip(
        "harbor.models.trajectories.trajectory"
    ).Trajectory
    _root, db_path, registry = corpus
    output = tmp_path / "staged"
    hti.prepare_harbor_analysis_inputs(
        db_path, output, trace_ids=["trace-agentgood"], registry=registry,
    )
    trajectory = trajectory_type.model_validate_json(
        (
            output / f"analyze-{_blind_id('trace-agentgood')}"
            / "environment" / "trial" / "agent" / "trajectory.json"
        ).read_bytes()
    )
    step = trajectory.steps[1]
    assert step.source == "agent"
    assert step.message == "observe line"
    call = step.tool_calls[0]
    assert call.function_name == "run_shell"
    assert call.arguments == {"command": "cat out"}
    results = step.observation.results
    assert results[0].content == "shell output 1\n"
    assert results[0].source_call_id == call.tool_call_id


def test_list_shaped_tool_flags_are_supported_and_stored(
    corpus: tuple, tmp_path: Path,
) -> None:
    _root, db_path, registry = corpus
    con = sqlite3.connect(db_path)
    stored = con.execute(
        "SELECT flags_json FROM trace_steps "
        "WHERE trace_id = 'trace-listflags' AND step_index = 2"
    ).fetchone()[0]
    con.close()
    assert json.loads(stored) == []
    output = tmp_path / "staged"
    receipt = hti.prepare_harbor_analysis_inputs(
        db_path, output, trace_ids=["trace-listflags"], registry=registry,
    )
    assert receipt["staged"] == [_blind_id("trace-listflags")]


def test_missing_assignment_authority_refuses_dev_rows(corpus: tuple, tmp_path: Path) -> None:
    root, db_path, _registry = corpus
    (root / "research/answer-keys/splits/assignments.csv").unlink()
    registry = CorpusRegistry(root)
    report = hti.preflight_harbor_inputs(db_path, trace_ids=["trace-alpha"], registry=registry)
    assert (
        report["runs"]["trace-alpha"]["refusals"][0]["category"]
        == "missing_assignment_authority"
    )
    with pytest.raises(hti.HarborInputRefusal) as caught:
        hti.prepare_harbor_analysis_inputs(
            db_path, tmp_path / "out", trace_ids=["trace-alpha"], registry=registry,
        )
    assert caught.value.refusals[0].category == "missing_assignment_authority"
    assert not (tmp_path / "out").exists()


def test_prepare_publishes_native_validated_analysis_tasks(corpus: tuple, tmp_path: Path) -> None:
    task_type = pytest.importorskip("harbor.models.task.task").Task
    trajectory_type = pytest.importorskip("harbor.models.trajectories.trajectory").Trajectory
    _root, db_path, registry = corpus
    output, join = tmp_path / "staged", tmp_path / "join.json"
    receipt = hti.prepare_harbor_analysis_inputs(
        db_path, output, trace_ids=["trace-alpha", "trace-beta"], registry=registry,
        private_join=join,
    )
    assert receipt["refused"] == []
    blind_alpha, blind_beta = _blind_id("trace-alpha"), _blind_id("trace-beta")
    assert receipt["staged"] == sorted((blind_alpha, blind_beta))
    assert receipt["export"]["exported"] == 2
    assert receipt["native"]["harbor_version"] == "0.24.0"
    for blind_id in (blind_alpha, blind_beta):
        task_dir = output / f"analyze-{blind_id}"
        task = task_type(task_dir)  # full native task validation
        assert task.name == f"analyze-{blind_id}"
        assert task.instruction
        trajectory = trajectory_type.model_validate_json(
            (task_dir / "environment" / "trial" / "agent" / "trajectory.json")
            .read_bytes()
        )
        # step_id is renumbered; source indices live only in the private join.
        assert [step.step_id for step in trajectory.steps] == [1, 2, 3, 4, 5]
        assert trajectory.session_id == blind_id
        criteria = json.loads((task_dir / "tests" / "criteria.json").read_text())
        assert criteria == ["reward_hacking", "task_specification"]
        instruction = (task_dir / "instruction.md").read_text()
        assert "withheld" in instruction and "not_applicable" in instruction
        assert "/app/trial/agent/trajectory.json" in instruction
    joins = json.loads(join.read_text())
    assert joins["input_policy"] == hti.INPUT_POLICY
    assert joins["runs"][blind_alpha]["step_origin"] == [2, 7, 13, 19, 31]
    assert joins["runs"][blind_alpha]["reward"] == 1.0
    assert joins["runs"][blind_beta]["reward"] == 0.0
    assert not join.is_relative_to(output)
    manifest = json.loads((output / "prepared-inputs.json").read_text())
    assert manifest["input_policy"] == hti.INPUT_POLICY


def test_staged_input_fabricates_no_source_records(corpus: tuple, tmp_path: Path) -> None:
    _root, db_path, registry = corpus
    output = tmp_path / "staged"
    hti.prepare_harbor_analysis_inputs(
        db_path, output, trace_ids=["trace-alpha"], registry=registry,
    )
    names = {path.name for path in output.rglob("*")}
    for fabricated in (
        "result.json", "trial.log", "job.log", "config.json",
        "reward.txt", "reward.json", "exception.txt", "verifier",
    ):
        assert fabricated not in names
    blind_id = _blind_id("trace-alpha")
    payload_root = output / f"analyze-{blind_id}" / "environment" / "trial"
    payload_files = {
        path.relative_to(payload_root).as_posix()
        for path in payload_root.rglob("*")
        if path.is_file()
    }
    assert payload_files == {"agent/trajectory.json"}


def test_staged_content_preserves_steps_and_withholds_labels(corpus: tuple, tmp_path: Path) -> None:
    _root, db_path, registry = corpus
    output = tmp_path / "staged"
    hti.prepare_harbor_analysis_inputs(
        db_path, output, trace_ids=["trace-alpha", "trace-long"], registry=registry,
    )
    blind_id = _blind_id("trace-alpha")
    document = json.loads(
        (output / f"analyze-{blind_id}" / "environment" / "trial" / "agent"
         / "trajectory.json").read_text()
    )
    steps = document["steps"]
    assert [step["source"] for step in steps] == ["system", "user", "agent", "agent", "agent"]
    assert steps[0]["message"] == SYSTEM_TEXT
    assert steps[1]["message"] == USER_TEXT
    assert steps[2]["message"] == ASSISTANT_TEXT
    assert steps[2]["tool_calls"][0]["function_name"] == "run_shell"
    assert steps[2]["tool_calls"][0]["arguments"] == {"command": COMMAND}
    contents = [
        result["content"] for step in steps
        for result in step.get("observation", {}).get("results", [])
    ]
    assert OBS_TEXT in contents and OBS_PREVIEW in contents
    assert steps[4]["message"] == FINAL_TEXT
    long_doc = json.loads(
        (output / f"analyze-{_blind_id('trace-long')}" / "environment" / "trial"
         / "agent" / "trajectory.json").read_text()
    )
    assert long_doc["steps"][1]["message"] == LONG_TEXT
    assert long_doc["steps"][2]["observation"]["results"][0]["content"] == LONG_TEXT
    forbidden = (
        "task-dev-a", "task-dev-b", "task-test", "trace-alpha", "trace-beta",
        "trace-long", DATASET, "private-run-", "private-model-", "private-outcome",
        "reward\":", "is_exploit", "trust_tier", "cas://", "DEV", "TEST", "sk-",
    )
    for path, data in _tree_bytes(output).items():
        text = data.decode("utf-8")
        for token in forbidden:
            assert token not in text, (path, token)


def test_source_declared_step_count_mismatch_is_refused(corpus: tuple, tmp_path: Path) -> None:
    _root, db_path, registry = corpus
    con = sqlite3.connect(db_path)
    con.execute(
        "UPDATE agent_traces SET step_count = step_count + 1 WHERE trace_id = 'trace-alpha'"
    )
    con.commit()
    con.close()
    with pytest.raises(hti.HarborInputRefusal) as caught:
        hti.prepare_harbor_analysis_inputs(
            db_path, tmp_path / "staged", trace_ids=["trace-alpha"], registry=registry,
        )
    assert {ref.category for ref in caught.value.refusals} == {
        "declared_step_count_mismatch"
    }


def test_source_mutation_during_export_is_refused(
    corpus: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, db_path, registry = corpus
    original = hti.export_runs

    def racing(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        with db_path.open("ab") as stream:
            stream.write(b"concurrent-mutation")
        return result

    monkeypatch.setattr(hti, "export_runs", racing)
    with pytest.raises(hti.HarborInputRefusal) as caught:
        hti.prepare_harbor_analysis_inputs(
            db_path, tmp_path / "staged", trace_ids=["trace-alpha"], registry=registry,
        )
    assert {ref.category for ref in caught.value.refusals} == {
        "source_mutated_during_staging"
    }
    assert not (tmp_path / "staged").exists()
    assert not list(tmp_path.glob(".harbor-trace-inputs-*"))


@pytest.mark.parametrize("trace_id,category", [
    ("trace-test", "db_non_dev_split"),
    ("trace-lower", "db_non_dev_split"),
    ("trace-crosskey", "registry_cross_key_or_sealed"),
    ("trace-unassigned", "registry_cross_key_or_sealed"),
    ("trace-missing", "unstored_run"),
    ("trace-journal", "actor_history_unavailable"),
    ("trace-unknown-kind", "actor_history_unavailable"),
    ("trace-incomplete", "actor_history_unavailable"),
    ("trace-toolonly", "no_actor_inputs"),
    ("trace-reasoning", "reasoning_not_exportable"),
    ("trace-evidence", "non_actor_records_exported"),
    ("trace-residual", "tool_calls_not_exportable"),
    ("trace-nativeargs", "tool_calls_not_exportable"),
    ("trace-notoolname", "tool_calls_not_exportable"),
    ("trace-toolassoc", "tool_call_association_not_exportable"),
    ("trace-agentdup", "actor_observation_not_exportable"),
    ("trace-agentempty", "actor_observation_not_exportable"),
    ("trace-secret", "secret_scan_rejected"),
    ("trace-secret-nested", "secret_scan_rejected"),
    ("trace-secret-preview", "secret_scan_rejected"),
    ("trace-cas", "cas_preview_unresolved"),
])
def test_prepare_refuses_unsupported_runs_without_artifacts(
    corpus: tuple, tmp_path: Path, trace_id: str, category: str,
) -> None:
    _root, db_path, registry = corpus
    output, join = tmp_path / "staged", tmp_path / "join.json"
    with pytest.raises(hti.HarborInputRefusal) as caught:
        hti.prepare_harbor_analysis_inputs(
            db_path, output, trace_ids=["trace-alpha", trace_id], registry=registry,
            private_join=join,
        )
    assert category in {ref.category for ref in caught.value.refusals}
    assert not output.exists()
    assert not join.exists()
    assert not list(tmp_path.glob(".harbor-trace-inputs-*"))
    assert not list(tmp_path.glob(".cloud-trace-export-*"))


def test_identity_masking_credential_is_vetoed_before_redaction(
    corpus: tuple, tmp_path: Path,
) -> None:
    _root, db_path, registry = corpus
    # The credential is also the stored model_id, so exporter redaction would
    # mask it in staged bytes; the pre-redaction veto must fire instead.
    with pytest.raises(hti.HarborInputRefusal) as caught:
        hti.prepare_harbor_analysis_inputs(
            db_path, tmp_path / "staged", trace_ids=["trace-secret"], registry=registry,
        )
    assert {ref.category for ref in caught.value.refusals} == {"secret_scan_rejected"}
    assert SECRET not in str(caught.value)
    assert not (tmp_path / "staged").exists()


def test_prepare_refusal_leaves_source_bytes_unchanged(corpus: tuple, tmp_path: Path) -> None:
    _root, db_path, registry = corpus
    before = db_path.read_bytes()
    with pytest.raises(hti.HarborInputRefusal):
        hti.prepare_harbor_analysis_inputs(
            db_path, tmp_path / "staged", trace_ids=["trace-crosskey"], registry=registry,
        )
    assert db_path.read_bytes() == before
    hti.prepare_harbor_analysis_inputs(
        db_path, tmp_path / "staged", trace_ids=["trace-alpha"], registry=registry,
    )
    assert db_path.read_bytes() == before


def test_cas_backed_observation_stages_only_when_verified_complete(
    corpus: tuple, tmp_path: Path,
) -> None:
    _root, db_path, registry = corpus
    cas_root = _cas_object(tmp_path, OBS_TEXT)
    _attach_cas(db_path, "trace-cas", 2, OBS_TEXT)
    output = tmp_path / "staged"
    receipt = hti.prepare_harbor_analysis_inputs(
        db_path, output, trace_ids=["trace-cas"], registry=registry, cas_root=cas_root,
    )
    assert receipt["staged"] == [_blind_id("trace-cas")]


def test_cas_preview_of_larger_object_is_refused(corpus: tuple, tmp_path: Path) -> None:
    _root, db_path, registry = corpus
    cas_root = _cas_object(tmp_path, OBS_TEXT + "truncated remainder\n")
    _attach_cas(db_path, "trace-cas", 2, OBS_TEXT + "truncated remainder\n")
    with pytest.raises(hti.HarborInputRefusal) as caught:
        hti.prepare_harbor_analysis_inputs(
            db_path, tmp_path / "staged", trace_ids=["trace-cas"],
            registry=registry, cas_root=cas_root,
        )
    assert {ref.category for ref in caught.value.refusals} == {"cas_preview_incomplete"}
    assert not (tmp_path / "staged").exists()


def test_cas_missing_object_and_no_root_are_refused(corpus: tuple, tmp_path: Path) -> None:
    _root, db_path, registry = corpus
    _attach_cas(db_path, "trace-cas", 2, OBS_TEXT)
    with pytest.raises(hti.HarborInputRefusal) as caught:
        hti.prepare_harbor_analysis_inputs(
            db_path, tmp_path / "staged", trace_ids=["trace-cas"], registry=registry,
        )
    assert {ref.category for ref in caught.value.refusals} == {"cas_preview_unresolved"}
    empty_cas = tmp_path / "empty-cas"
    (empty_cas / "sha256").mkdir(parents=True)
    with pytest.raises(hti.HarborInputRefusal) as caught:
        hti.prepare_harbor_analysis_inputs(
            db_path, tmp_path / "staged2", trace_ids=["trace-cas"],
            registry=registry, cas_root=empty_cas,
        )
    assert {ref.category for ref in caught.value.refusals} == {"cas_missing_object"}
    assert not (tmp_path / "staged").exists() and not (tmp_path / "staged2").exists()


def test_prepare_requires_existing_output_and_join_parents(
    corpus: tuple, tmp_path: Path,
) -> None:
    _root, db_path, registry = corpus
    with pytest.raises(ValueError, match="parent"):
        hti.prepare_harbor_analysis_inputs(
            db_path, tmp_path / "no-parent" / "staged",
            trace_ids=["trace-alpha"], registry=registry,
        )
    with pytest.raises(ValueError, match="parent"):
        hti.prepare_harbor_analysis_inputs(
            db_path, tmp_path / "staged",
            trace_ids=["trace-alpha"], registry=registry,
            private_join=tmp_path / "no-join-parent" / "join.json",
        )
    assert not (tmp_path / "no-parent").exists()
    assert not (tmp_path / "no-join-parent").exists()
    assert not (tmp_path / "staged").exists()
    assert not list(tmp_path.glob(".harbor-trace-inputs-*"))


def test_assembly_refusal_with_existing_parents_leaves_nothing(
    corpus: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyzer = pytest.importorskip("harbor.analyze.analyzer")
    _root, db_path, registry = corpus
    monkeypatch.setattr(
        analyzer, "ANALYZE_TASK_TEMPLATE_DIR", tmp_path / "absent-template"
    )
    output_parent = tmp_path / "present-parent"
    join_parent = tmp_path / "present-join-parent"
    output_parent.mkdir()
    join_parent.mkdir()
    with pytest.raises(hti.HarborInputRefusal):
        hti.prepare_harbor_analysis_inputs(
            db_path, output_parent / "staged", trace_ids=["trace-alpha"],
            registry=registry, private_join=join_parent / "join.json",
        )
    assert sorted(p.name for p in output_parent.iterdir()) == []
    assert sorted(p.name for p in join_parent.iterdir()) == []
    assert output_parent.is_dir() and join_parent.is_dir()


def test_existing_output_and_join_are_never_clobbered(corpus: tuple, tmp_path: Path) -> None:
    _root, db_path, registry = corpus
    output, join = tmp_path / "staged", tmp_path / "join.json"
    output.mkdir()
    join.write_text("occupied")
    with pytest.raises(ValueError, match="already exists"):
        hti.prepare_harbor_analysis_inputs(
            db_path, output, trace_ids=["trace-alpha"], registry=registry,
        )
    join.unlink()
    output.rmdir()
    join.write_text("occupied")
    with pytest.raises((ValueError, FileExistsError)):
        hti.prepare_harbor_analysis_inputs(
            db_path, output, trace_ids=["trace-alpha"], registry=registry,
            private_join=join,
        )
    assert join.read_text() == "occupied"


def test_selection_must_be_explicit_and_bounded(corpus: tuple, tmp_path: Path) -> None:
    _root, db_path, registry = corpus
    with pytest.raises(ValueError):
        hti.prepare_harbor_analysis_inputs(
            db_path, tmp_path / "a", trace_ids=[], registry=registry,
        )
    with pytest.raises(ValueError):
        hti.prepare_harbor_analysis_inputs(
            db_path, tmp_path / "b", trace_ids="trace-alpha", registry=registry,
        )
    with pytest.raises(ValueError):
        hti.preflight_harbor_inputs(db_path, trace_ids=[], registry=registry)
    assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()
