"""Default ``qa-data`` replies carry no source, split, reward or label tier; ``--with-outcomes`` is the only way in."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from evallab.cli import parser as cli_parser
from evallab.production import corpus_queue
from evallab.production.cloud_trace_db import CloudTraceDatabase
from evallab.production.corpus_queue import CorpusRegistry
from evallab.production.qa_data import UNAVAILABLE

DATASET = "private-corpus-name"
TIER = "tier1_container_verified"
FORBIDDEN_KEYS = frozenset({
    "dataset", "dataset_id", "source", "split", "reward", "original_reward", "repaired_reward",
    "differential", "trust_tier", "stored_split",
})
DEFAULT_CALLS = {
    "task": ["task", "alpha"],
    "history": ["history", "alpha"],
    "runs": ["runs", "--task", "alpha", "--facet", "tests=modified"],
    "window": ["window", "alpha/a1", "--first", "0", "--last", "5"],
    "findings": ["findings", "--run", "alpha/a1"],
}
REFUSED_IDS = ("held-out", "locked-task", "no-such-task")  # TEST, TEST under another key, missing


class SyntheticRegistry(CorpusRegistry):
    """Admits the synthetic corpus; ``locked-task`` is TEST-assigned under another key."""

    def __init__(self) -> None:
        authority = b"key_id,item_id,split\nsynthetic-authority,source,dev\n"
        self.split_assignments_digest = "sha256:" + hashlib.sha256(authority).hexdigest()
        self._assignment_keys = frozenset({"synthetic-authority"})
        self._assignments = {("synthetic-authority", "source"): "dev"}
        self._assignment_names = {}
        self._assigned_test_ids = set()
        self._assigned_test_names = {"locked-task"}
        self._sealed_names = set()
        self._sealed_digests = set()
        self._sealed_ids = set()
        self._items = {}
        self._index_names = {}
        self._external = set()


Ask = Callable[..., tuple[int, str]]


@pytest.fixture
def ask(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Iterator[Ask]:
    database = tmp_path / "qa.sqlite"
    state = tmp_path / "state"
    ledger = state / "trace-tools" / "results-v1.jsonl"
    ledger.parent.mkdir(parents=True)
    with CloudTraceDatabase(database, sqlite_fts=False) as db:
        for task_id, split in (("alpha", "dev"), ("beta", "dev"), ("held-out", "test"), ("locked-task", "dev")):
            db.upsert_task({
                "task_id": task_id, "dataset_id": DATASET, "split": split,
                "instruction_text": "repair the parser so every output check passes",
            })
        for issue_id, task_id, status in (
            ("alpha/open", "alpha", "confirmed"), ("beta/issue", "beta", "confirmed"),
        ):
            db.upsert_task_issue({
                "issue_id": issue_id, "task_id": task_id, "defect_class": "zero_invariant_tolerance",
                "direction": "wrong_pass", "status": status, "trust_tier": TIER,
                "original_reward": 0.0, "repaired_reward": 1.0, "reproducer_script": "parser accepts wrong output",
            })
        for task_id in ("alpha", "held-out", "locked-task"):
            db.upsert_trace({
                "trace_id": f"{task_id}/a1", "run_id": "batch", "task_id": task_id, "model_id": "m",
                "reward": 1.0, "trust_tier": TIER, "is_exploit": True,
            }, steps=[
                {
                    "step_index": index, "role": "assistant", "content_text": f"step {index} inspect the parser",
                    "flags_json": {"label": "cheat", "split": "TEST"},
                } for index in range(6)
            ])
        assert db.upsert_run_facets([{
            "run_id": "alpha/a1", "source": DATASET, "split": "DEV", "facet_version": "run-facets/v1",
            "facet_model": "synthetic/model", "input_tokens": 1, "output_tokens": 1, "goal": "Fix the parser",
            "approach": "Edited the test file", "outcome": "pass", "tests": "modified",
            "grader_or_harness_touched": False, "answer_material_accessed": False,
            "success_claim_without_evidence": False, "eval_awareness": "none", "shortcut": "none",
            "suspicion": 1, "evidence_steps": [1],
        }], registry=SyntheticRegistry())["stored"] == 1
    ledger.write_text(json.dumps({
        "schema": "evallab.trace_tool_result/v1", "run_id": "alpha/a1", "source": "cloud-trace-db",
        "tool": "scout", "detector": "repeat", "detector_version": "v1", "value": 3, "flag": False,
        "evidence_steps": [1, 3], "scan": "opaque-id", "at": "2026-10-28T02:00:00Z",
    }) + "\n", encoding="utf-8")
    monkeypatch.setenv("EVALLAB_PROD_STATE", str(state))

    class RootlessRegistry(SyntheticRegistry):
        def __init__(self, _root: object = None) -> None:
            super().__init__()

    monkeypatch.setattr(corpus_queue, "CorpusRegistry", RootlessRegistry)

    def run(*argv: str) -> tuple[int, str]:
        args = cli_parser().parse_args(["qa-data", *argv, "--cloud-database", str(database)])
        status = args.func(args, Path.cwd(), harbor=None)
        return status, capsys.readouterr().out

    yield run


def keys_and_strings(value: Any) -> tuple[set[str], list[str]]:
    if isinstance(value, dict):
        keys, strings = set(value), []
        for child in value.values():
            more_keys, more_strings = keys_and_strings(child)
            keys |= more_keys
            strings += more_strings
        return keys, strings
    if isinstance(value, list):
        keys, strings = set(), []
        for child in value:
            more_keys, more_strings = keys_and_strings(child)
            keys |= more_keys
            strings += more_strings
        return keys, strings
    return set(), [value] if isinstance(value, str) else []


@pytest.mark.parametrize("call", sorted(DEFAULT_CALLS))
def test_default_replies_carry_no_source_split_reward_or_label_tier(ask: Ask, call: str) -> None:
    status, out = ask(*DEFAULT_CALLS[call])
    reply = json.loads(out)
    assert status == 0 and reply["ok"], reply
    keys, strings = keys_and_strings(reply["data"])
    assert not keys & FORBIDDEN_KEYS, keys & FORBIDDEN_KEYS
    assert not any(DATASET in text or TIER in text for text in strings)
    assert reply["meta"].get("with_outcomes", False) is False


def test_history_and_task_reply_with_labels_only_when_asked_and_record_it_in_meta(ask: Ask) -> None:
    _, plain = ask("history", "alpha")
    plain_reply = json.loads(plain)
    assert plain_reply["data"]["issues"] and plain_reply["data"]["similar_issues"]
    assert plain_reply["meta"]["with_outcomes"] is False
    _, labelled = ask("history", "alpha", "--with-outcomes")
    reply = json.loads(labelled)
    assert reply["meta"]["with_outcomes"] is True
    (own,) = reply["data"]["issues"]
    assert own["trust_tier"] == TIER and own["differential"] == [0.0, 1.0]
    assert all(item["trust_tier"] == TIER for item in reply["data"]["similar_issues"])
    # Apart from the two labels the reply is the same document.
    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items() if k not in {"trust_tier", "differential"}}
        return [scrub(v) for v in value] if isinstance(value, list) else value

    assert scrub(reply["data"]) == plain_reply["data"]
    task_reply = json.loads(ask("task", "alpha", "--with-outcomes")[1])
    assert task_reply["meta"]["with_outcomes"] is True and "dataset" not in task_reply["data"]
    assert [issue["trust_tier"] for issue in task_reply["data"]["issues"]] == [TIER]


def normalized(out: str) -> str:
    return re.sub(r'"latency_ms":[0-9.]+', '"latency_ms":0', out)


@pytest.mark.parametrize("flags", [[], ["--with-outcomes"]])
@pytest.mark.parametrize("call", ["task", "history", "runs", "window", "findings"])
def test_refusals_are_byte_identical_for_test_cross_key_and_missing_identities(
    ask: Ask, call: str, flags: list[str],
) -> None:
    if call in {"runs", "window", "findings"} and flags:
        pytest.skip("--with-outcomes belongs to task and history")
    refusals = set()
    for identity in REFUSED_IDS:
        argv = {
            "task": ["task", identity], "history": ["history", identity],
            "runs": ["runs", "--task", identity], "window": ["window", f"{identity}/a1"],
            "findings": ["findings", "--run", f"{identity}/a1"],
        }[call]
        status, out = ask(*argv, *flags)
        assert status == 2 and json.loads(out)["error"] == UNAVAILABLE
        refusals.add(normalized(out))
    assert len(refusals) == 1, refusals
