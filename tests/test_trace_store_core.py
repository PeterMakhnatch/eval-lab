"""Synthetic trace-store boundaries; all DB, split, evidence and CAS state is injected."""

from __future__ import annotations

import json
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from evallab.trace_store.core import (
    trace_uuid,
)
from evallab.trace_store.secrets import SECRET_RE, secret_hits

GOODFIRE = "goodfire-reward-hack-data"
MIMO = "mimo-defect-map-v1"
TB2 = "tb2-to-tb21-v1"
TB4 = "tb4-audit-v1"
SOURCE_RUNS = ("mimo30", "muse-r1", "muse-r2", "muse-w5", "tb2-to-tb21", "kimi-tb4")
ANNOTATION = "synthetic-evaluator-annotation-not-actor-content"
ACTOR_JSON = json.dumps({
    "prior_feedback": {"label": "previous attempt", "reward": 0.0, "is_exploit": False},
    "label": "actor configuration", "labels": ["input", "output"],
    "source_label": "a user-supplied field", "is_exploit": False,
    "trust_tier": "local", "flagged_passage_steps": [2],
    "receipt": {"message": "Keep the actual earlier feedback: café 完整."},
    "verified_receipt_json": {"reward": 0}, "answer-key": {"name": "configuration"},
    "answer_key": "actor data", "generation_receipt": {"status": "retry"},
    "source_receipt": "earlier actor input", "flags_json": {"label": "configuration"},
    "source_passages": ["prior feedback supplied to the actor"],
    "source_judge_flags": {"note": "supplied context"}, "passage_positive": False,
    "reward": 1, "rewards": [0, 1],
}, ensure_ascii=False)


def test_uuid_contract_is_shared_stable_and_namespaced() -> None:
    expected = str(uuid5(NAMESPACE_URL, "evallab-trace-store/v1:synthetic-trace"))
    assert trace_uuid("synthetic-trace") == expected
    assert UUID(expected).version == 5
    assert trace_uuid("other-trace") != expected


def test_real_shaped_provider_key_is_detected_in_free_text() -> None:
    key = "sk-" + "proj-" + "A7b" * 16
    assert secret_hits("example credential: " + key + "\nend") == ["secret-pattern"]


def test_canonical_task_hash_is_not_a_provider_credential() -> None:
    task_id = "goodfire-reward-hack-data/swebench_verified/task-" + "a" * 30
    assert secret_hits({"metadata": {"task_id": task_id}}) == []
    key = "sk-" + "proj-" + "A7b" * 16
    assert secret_hits({"metadata": {"task_id": task_id}, "content": "token: " + key}) == [
        "secret-pattern"
    ]


@pytest.mark.parametrize("secret", [
    "sk-" + "a" * 16, "dk_" + "a" * 10, "hf_" + "a" * 20,
    "AKIA" + "A" * 16, "ghp_" + "a" * 20, "gho_" + "a" * 20,
    "ghu_" + "a" * 20, "ghs_" + "a" * 20, "xoxb-" + "a" * 10,
    "xoxp-" + "a" * 10, "-----BEGIN " + "RSA PRIVATE KEY-----",
    "EXAMPLE_API_KEY=abcdefgh", "EXAMPLE_SECRET=abcdefgh", "PASSWORD=abcdefgh",
    "TOKEN=abcdefgh", "EXAMPLE_TOKEN=abcdefgh", "EXAMPLE_TOKENS_BACKUP=abcdefgh",
])
def test_secret_scan_recurses_and_never_returns_secret_prefixes(secret: str) -> None:
    payload = {"metadata": {"nested": [None, {"content": secret + " " + secret}]}}
    before = json.dumps(payload)
    assert secret_hits(payload) == ["secret-pattern", "secret-pattern"]
    assert json.dumps(payload) == before
    assert secret[:12] not in json.dumps(secret_hits(payload))


def test_secret_traversal_matches_original_dict_values_and_lists_only() -> None:
    secret = "sk-" + "a" * 20
    assert secret_hits({secret: "safe", "number": 1, "tuple": (secret,)}) == []
    assert secret_hits([{"tool_calls": [{"arguments": {"command": secret}}]}]) == ["secret-pattern"]
    assert secret_hits("TOKENIZER_INFINITY=abcdefgh\nMAX_TOKENS_PER_PAGE=128") == []
    assert secret_hits("hf_short sk-short TOKEN=$VARIABLE PASSWORD='literal'") == []


@pytest.mark.parametrize("wrappers", [1, 2, 3])
def test_secret_scan_decodes_nested_json_content_without_losing_raw_hits(wrappers: int) -> None:
    key = "sk-" + "proj-" + "A7b" * 16
    content = json.dumps({"stdout": "heading\n" + key})
    for _ in range(wrappers - 1):
        content = json.dumps(content)
    # The encoded newline's `n` masks the token boundary in a raw-only scan.
    assert SECRET_RE.search(content) is None
    payload = {"transcripts": [{"messages": [{"content": content}]}]}
    original = json.dumps(payload)
    assert secret_hits(payload) == ["secret-pattern"]
    assert json.dumps(payload) == original
    assert key not in json.dumps(secret_hits(payload))


def test_json_wrapping_never_relaxes_raw_hits_or_matches_task_hashes() -> None:
    key = "sk-" + "a" * 20
    # Retain the original raw-string scan even when the key is a JSON property.
    assert secret_hits(json.dumps({key: "safe"})) == ["secret-pattern"]
    task_id = "task-" + "a" * 30
    assert secret_hits({"content": json.dumps({"stdout": "heading\n" + task_id})}) == []
