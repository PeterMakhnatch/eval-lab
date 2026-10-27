"""The four swarm strategies, fallback quarantine, citation checks, and split locks.

Every case runs against a scripted transport and a synthetic corpus, so no model is
called, no task executes, and no sealed answer-key bytes are opened.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from evallab.production import swarm_coordinator as swarm
from evallab.production.cloud_trace_db import CloudTraceDatabase
from evallab.production.corpus_queue import ContinuousWorkQueue, CorpusRegistry, SplitLockError

DATASET = "tb4-audit-v1"
FROZEN_TOKEN = "test-only-explicit-frozen-evaluation"
DEV = {"dataset_id": DATASET, "item_id": "finding-dev"}
SEALED = {"dataset_id": DATASET, "item_id": "finding-test"}
CHEAP = swarm.CHEAP_MODELS
KIMI, MUSE, SONNET = CHEAP
STRONG = swarm.DEFAULT_STRONG_MODEL
SUBJECT = "terminal-bench/dev-task"

GRADER = """import subprocess


def grade(workspace):
    result = subprocess.run(["bash", "solve.sh"], cwd=workspace)
    return result.returncode == 0
"""
SOLVE = "#!/bin/bash\necho done\n"
TEST_FILE = "def test_grade_passes():\n    assert True\n"
GRADER_QUOTE = "return result.returncode == 0"


# --------------------------------------------------------------------------- #
# Fixtures and doubles
# --------------------------------------------------------------------------- #


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _item(item_id: str, split: str, task: str, digest: str | None = None) -> dict[str, Any]:
    return {"item_id": item_id, "split": split, "subject": {"task_id": task, "task_digest": digest}}


@pytest.fixture
def corpus_root(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    keys = root / "research/answer-keys"
    assignments = keys / "splits/assignments.csv"
    assignments.parent.mkdir(parents=True)
    assignments.write_text(
        "key_id,item_id,task,split\n"
        f"{DATASET},finding-dev,{SUBJECT},dev\n"
        f"{DATASET},finding-test,terminal-bench/sealed-task,test\n"
        "cheatbench-actions-v1,cheat-dev,cheatbench/independent,dev\n"
        "hacktrace-act-v1,hack-dev,hacktrace/independent,dev\n"
        "mimo-defect-map-v1,mimo-dev@sha256:version1,mimo-dev,dev\n",
        encoding="utf-8",
    )
    _write_jsonl(
        keys / DATASET / "items.jsonl",
        [
            _item("finding-dev", "dev", SUBJECT, "sha256:dev"),
            _item("finding-test", "test", "terminal-bench/sealed-task", "sha256:sealed"),
        ],
    )
    _write_jsonl(
        keys / DATASET / "tasks.jsonl",
        [
            _item("ignored", "dev", SUBJECT, "sha256:dev"),
            _item("ignored", "test", "terminal-bench/sealed-task", "sha256:sealed"),
        ],
    )
    _write_jsonl(
        keys / "cheatbench-actions-v1/items.jsonl",
        [_item("cheat-dev", "dev", "cheatbench/independent")],
    )
    _write_jsonl(
        keys / "hacktrace-act-v1/items.jsonl",
        [_item("hack-dev", "dev", "hacktrace/independent")],
    )
    inventory_path = root / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory_path.parent.mkdir(parents=True)
    inventory_path.write_text(
        json.dumps(
            {
                "dataset_id": "mimo-defect-map-v1",
                "heldout_task_ids_excluded_from_training_all_versions": ["mimo-held"],
                "tasks": [
                    {
                        "record_id": "mimo-dev@sha256:version1",
                        "task_id": "mimo-dev",
                        "package_digest": "sha256:mimo-dev",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def registry(corpus_root: Path) -> CorpusRegistry:
    return CorpusRegistry(corpus_root, frozen_eval_token=FROZEN_TOKEN)


@pytest.fixture
def package(tmp_path: Path) -> Path:
    root = tmp_path / "package"
    (root / "task").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "task/grader.py").write_text(GRADER, encoding="utf-8")
    (root / "task/solve.sh").write_text(SOLVE, encoding="utf-8")
    (root / "tests/test_grader.py").write_text(TEST_FILE, encoding="utf-8")
    return root


class ScriptedTransport:
    """Deterministic transport: one scripted response per route, in call order."""

    def __init__(self, script: Mapping[str, Sequence[swarm.ModelResponse]]) -> None:
        self.script = {model: list(responses) for model, responses in script.items()}
        self.requests: list[swarm.ModelRequest] = []

    def complete(self, request: swarm.ModelRequest) -> swarm.ModelResponse:
        self.requests.append(request)
        responses = self.script.get(request.model)
        if not responses:
            raise AssertionError(f"unexpected model route: {request.model}")
        return responses.pop(0) if len(responses) > 1 else responses[0]

    @property
    def routes(self) -> list[str]:
        return [request.model for request in self.requests]

    @property
    def prompts(self) -> list[str]:
        return [request.prompt for request in self.requests]


class FakeRetriever:
    """A retriever whose records the test controls exactly."""

    def __init__(self, records: Sequence[swarm.RetrievalRecord] = ()) -> None:
        self.records = tuple(records)
        self.calls: list[tuple[swarm.WorkKind, str, str]] = []

    def retrieve(
        self,
        *,
        work_kind: swarm.WorkKind,
        subject_id: str,
        query: str,
        limit: int = swarm.MAX_RETRIEVAL_RECORDS,
    ) -> Sequence[swarm.RetrievalRecord]:
        self.calls.append((work_kind, subject_id, query))
        return self.records[:limit]


def _finding(
    *,
    path: str = "task/grader.py",
    line: int = 6,
    end_line: int | None = None,
    quote: str | None = GRADER_QUOTE,
    direction: str = "wrong_pass",
    mechanism: str = "grader trusts the solve script exit code",
    confidence: float = 0.9,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    citation: dict[str, Any] = {"path": path, "line": line}
    if end_line is not None:
        citation["end_line"] = end_line
    if quote is not None:
        citation["quote"] = quote
    finding: dict[str, Any] = {
        "direction": direction,
        "mechanism": mechanism,
        "confidence": confidence,
        "citations": [citation],
    }
    if payload is not None:
        finding["payload"] = dict(payload)
    return finding


def _reply(*findings: Mapping[str, Any]) -> str:
    return json.dumps({"findings": [dict(finding) for finding in findings]})


def _response(
    text: str,
    *,
    served: str,
    tokens_in: int | None = 1_200,
    tokens_out: int | None = 300,
    latency_ms: int = 42,
    error: str | None = None,
) -> swarm.ModelResponse:
    return swarm.ModelResponse(
        text=text,
        served_model=served,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
        error=error,
    )


def _coordinator(
    transport: ScriptedTransport | swarm.ModelTransport,
    registry: CorpusRegistry,
    *,
    queue: ContinuousWorkQueue | None = None,
    retriever: FakeRetriever | None = None,
    threshold: float = 0.8,
    strong: str = STRONG,
    cheap: Sequence[str] = CHEAP,
) -> swarm.SwarmCoordinator:
    return swarm.SwarmCoordinator(
        transport=transport,
        registry=registry,
        queue=queue,
        retriever=retriever,
        strong_model=strong,
        cheap_models=cheap,
        confidence_threshold=threshold,
        run_id="test-run",
    )


def _run(
    coordinator: swarm.SwarmCoordinator,
    package: Path,
    *,
    mode: swarm.SwarmMode,
    work_kind: swarm.WorkKind = swarm.WorkKind.TASK_QUALITY_AUDIT,
    **overrides: Any,
) -> swarm.SwarmResult:
    arguments: dict[str, Any] = {
        "mode": mode,
        "work_kind": work_kind,
        "subject_id": SUBJECT,
        "prompt": "Audit this package for grader defects.",
        "dataset_id": DEV["dataset_id"],
        "item_id": DEV["item_id"],
        "workspace": package,
    }
    arguments.update(overrides)
    return coordinator.run(**arguments)


def _agreed(*, confidence: float = 0.9, served: str) -> swarm.ModelResponse:
    return _response(_reply(_finding(confidence=confidence)), served=served)


# --------------------------------------------------------------------------- #
# Citations
# --------------------------------------------------------------------------- #


def test_verify_citation_accepts_a_real_range_and_quote(package: Path) -> None:
    check = swarm.verify_citation(
        package, swarm.Citation("task/grader.py", 4, 6, "def grade(workspace)")
    )
    assert check.verified
    assert check.file_exists and check.line_range_exists
    assert check.quote_found is True
    assert check.detail is None


def test_verify_citation_accepts_a_citation_without_a_quote(package: Path) -> None:
    check = swarm.verify_citation(package, swarm.Citation("task/solve.sh", 2))
    assert check.verified
    assert check.quote_found is None


@pytest.mark.parametrize(
    "citation",
    [
        swarm.Citation("/etc/passwd", 1),
        swarm.Citation("../outside.txt", 1),
        swarm.Citation("task/../../outside.txt", 1),
    ],
)
def test_verify_citation_rejects_paths_that_leave_the_package(
    package: Path, citation: swarm.Citation
) -> None:
    check = swarm.verify_citation(package, citation)
    assert not check.verified
    assert not check.file_exists
    assert "escapes the package" in str(check.detail)


def test_verify_citation_rejects_a_symlink_that_escapes_the_package(
    package: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("sealed answer key\n", encoding="utf-8")
    (package / "task/linked.py").symlink_to(secret)
    check = swarm.verify_citation(package, swarm.Citation("task/linked.py", 1))
    assert not check.verified
    assert "outside the package" in str(check.detail)


def test_verify_citation_reports_missing_file_range_and_quote(package: Path) -> None:
    missing = swarm.verify_citation(package, swarm.Citation("task/nope.py", 1))
    assert not missing.file_exists and not missing.verified

    outside = swarm.verify_citation(package, swarm.Citation("task/grader.py", 40))
    assert outside.file_exists and not outside.line_range_exists
    assert "outside 1-6" in str(outside.detail)

    wrong_quote = swarm.verify_citation(
        package, swarm.Citation("task/grader.py", 1, 2, "return result.returncode == 0")
    )
    assert wrong_quote.file_exists and wrong_quote.line_range_exists
    assert wrong_quote.quote_found is False
    assert not wrong_quote.verified


def test_citation_rejects_impossible_ranges() -> None:
    with pytest.raises(ValueError):
        swarm.Citation("task/grader.py", 0)
    with pytest.raises(ValueError):
        swarm.Citation("task/grader.py", 5, 3)
    with pytest.raises(ValueError):
        swarm.Citation("", 1)


# --------------------------------------------------------------------------- #
# Model identity
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("requested", "served", "expected"),
    [
        (STRONG, STRONG, True),
        (STRONG, "anthropic/claude-opus-5-5", True),
        ("openai-codex/gpt-6.1-sol:max", "openai-codex/gpt-6.1-sol", True),
        ("openai-codex/gpt-6-astra:max", "openai-codex/gpt-6.1-sol", True),
        ("openai-codex/gpt-6.1-sol:high", "openai-codex/gpt-6.1-sol", True),
        ("openai-codex/gpt-6.1-sol:medium", "openai-codex/gpt-6.1-sol", True),
        ("openai-codex/gpt-6.1-sol:low", "openai-codex/gpt-6.1-sol", True),
        ("openai-codex/gpt-6.1-sol:max", "gpt-6.1-sol:max", False),
        ("openai-codex/gpt-6.1-sol:max", "gpt-6.1-sol", False),
        ("anthropic/claude-opus-5-5", "claude-opus-5-5", False),
        ("anthropic/claude-opus-5-5", "anthropic/claude-opus-5-5", True),
        ("anthropic/claude-opus-5-5", "claude-sonnet-5-5", False),
        ("anthropic/claude-opus-5-5", "anthropic/claude-opus-5-5-preview", False),
        ("anthropic/claude-opus-5-5", None, False),
        ("anthropic/claude-opus-5-5", "  ", False),
    ],
)
def test_model_identity_matches(requested: str, served: str | None, expected: bool) -> None:
    assert swarm.model_identity_matches(requested, served) is expected

# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _verify(package: Path):  # noqa: ANN202 - a local closure is the clearest form
    return lambda citation: swarm.verify_citation(package, citation)


def test_parse_candidates_reads_fenced_and_prefixed_replies(package: Path) -> None:
    fenced = "Working...\n```json\n" + _reply(_finding()) + "\n```"
    candidates = swarm.parse_candidates(
        fenced,
        model=KIMI,
        rank=1,
        work_kind=swarm.WorkKind.TASK_QUALITY_AUDIT,
        subject_id=SUBJECT,
        verify=_verify(package),
    )
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.candidate_id == f"{KIMI}#1"
    assert candidate.citation_verified
    assert candidate.mechanism_tokens


def test_parse_candidates_treats_an_empty_list_as_abstention(package: Path) -> None:
    candidates = swarm.parse_candidates(
        '{"findings": []}',
        model=KIMI,
        rank=1,
        work_kind=swarm.WorkKind.TASK_QUALITY_AUDIT,
        subject_id=SUBJECT,
        verify=_verify(package),
    )
    assert candidates == ()


def test_parse_candidates_fails_when_nothing_usable_survives(package: Path) -> None:
    unusable = json.dumps({"findings": [{"direction": "wrong_pass", "mechanism": "x"}]})
    with pytest.raises(swarm.CandidateParseError):
        swarm.parse_candidates(
            unusable,
            model=KIMI,
            rank=1,
            work_kind=swarm.WorkKind.TASK_QUALITY_AUDIT,
            subject_id=SUBJECT,
            verify=_verify(package),
        )


def test_parse_candidates_rejects_a_response_that_is_not_json(package: Path) -> None:
    with pytest.raises(swarm.CandidateParseError):
        swarm.parse_candidates(
            "I could not find anything to report.",
            model=KIMI,
            rank=1,
            work_kind=swarm.WorkKind.TASK_QUALITY_AUDIT,
            subject_id=SUBJECT,
            verify=_verify(package),
        )


@pytest.mark.parametrize(
    "raw",
    [
        {"mechanism": "no direction", "confidence": 0.5, "citations": [{"path": "a", "line": 1}]},
        {"direction": "wrong_pass", "confidence": 0.5, "citations": [{"path": "a", "line": 1}]},
        {"direction": "wrong_pass", "mechanism": "x", "citations": [{"path": "a", "line": 1}]},
        {"direction": "wrong_pass", "mechanism": "x", "confidence": 1.5, "citations": [{"path": "a", "line": 1}]},
        {"direction": "wrong_pass", "mechanism": "x", "confidence": 0.5, "citations": []},
        {"direction": "wrong_pass", "mechanism": "x", "confidence": 0.5},
        {"direction": "wrong_pass", "mechanism": "x", "confidence": 0.5, "citations": [{"line": 1}]},
        "not an object",
    ],
)
def test_build_candidate_rejects_incomplete_findings(package: Path, raw: Any) -> None:
    candidate = swarm.build_candidate(
        raw,
        index=1,
        model=KIMI,
        rank=1,
        work_kind=swarm.WorkKind.TASK_QUALITY_AUDIT,
        subject_id=SUBJECT,
        verify=_verify(package),
    )
    assert candidate is None


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #


def _omp_stream(model: str, text: str, *, provider: str = "anthropic") -> str:
    return "\n".join(
        [
            json.dumps({"type": "session", "id": "session-1"}),
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "provider": provider,
                        "model": model,
                        "content": [{"type": "text", "text": text}],
                        "usage": {"input": 900, "output": 120, "cacheRead": 100, "cacheWrite": 0},
                    },
                }
            ),
        ]
    )


def test_omp_transport_reports_the_served_model_and_usage() -> None:
    captured: dict[str, Any] = {}

    def runner(argv: Sequence[str], cwd: Path | None, timeout: float) -> tuple[int, str, str]:
        captured["argv"] = list(argv)
        captured["cwd"] = cwd
        captured["timeout"] = timeout
        return 0, _omp_stream("claude-opus-5-5", '{"findings": []}'), ""

    transport = swarm.OmpCliTransport(runner=runner, cwd=Path("/private/tmp"))
    response = transport.complete(
        swarm.ModelRequest(
            model=STRONG,
            prompt="audit",
            role=swarm.Role.STRONG,
            work_kind=swarm.WorkKind.TASK_QUALITY_AUDIT,
            subject_id=SUBJECT,
        )
    )
    assert response.served_model == "anthropic/claude-opus-5-5"
    assert response.text == '{"findings": []}'
    assert (response.tokens_in, response.tokens_out) == (1_000, 120)
    assert response.error is None
    argv = captured["argv"]
    assert argv[0] == "omp" and argv[-1] == "audit"
    for flag in ("--no-tools", "--no-rules", "--no-skills", "--no-extensions", "--no-session"):
        assert flag in argv
    assert argv[argv.index("--model") + 1] == STRONG


def test_omp_transport_reports_failure_without_a_served_model() -> None:
    def runner(argv: Sequence[str], cwd: Path | None, timeout: float) -> tuple[int, str, str]:
        return 2, "", "provider refused the request"

    transport = swarm.OmpCliTransport(runner=runner)
    response = transport.complete(
        swarm.ModelRequest(
            model=KIMI,
            prompt="audit",
            role=swarm.Role.PROPOSER,
            work_kind=swarm.WorkKind.TASK_QUALITY_AUDIT,
            subject_id=SUBJECT,
        )
    )
    assert response.served_model is None
    assert response.text == ""
    assert "provider refused" in str(response.error)
    assert not swarm.model_identity_matches(KIMI, response.served_model)


def test_omp_stream_without_a_message_is_not_attributed() -> None:
    served, text, tokens_in, tokens_out = swarm.parse_omp_stream(
        json.dumps({"type": "session", "id": "s"})
    )
    assert (served, text, tokens_in, tokens_out) == (None, "", None, None)


# --------------------------------------------------------------------------- #
# SINGLE_STRONG
# --------------------------------------------------------------------------- #


def test_single_strong_makes_one_frontier_call_and_keeps_verified_findings(
    registry: CorpusRegistry, package: Path
) -> None:
    transport = ScriptedTransport({STRONG: [_response(_reply(_finding()), served=STRONG)]})
    coordinator = _coordinator(transport, registry)
    result = _run(coordinator, package, mode=swarm.SwarmMode.SINGLE_STRONG)

    assert transport.routes == [STRONG]
    assert result.round.frontier_calls == 1
    assert result.round.cheap_calls == 0
    assert result.accepted
    assert [candidate.candidate_id for candidate in result.selected] == [f"{STRONG}#1"]
    assert result.escalated is False
    assert result.aggregation is None


def test_single_strong_keeps_its_own_rejected_citations_out_of_the_selection(
    registry: CorpusRegistry, package: Path
) -> None:
    transport = ScriptedTransport(
        {
            STRONG: [
                _response(
                    _reply(
                        _finding(),
                        _finding(path="task/missing.py", line=1, quote=None, mechanism="invented check"),
                    ),
                    served=STRONG,
                )
            ]
        }
    )
    result = _run(_coordinator(transport, registry), package, mode=swarm.SwarmMode.SINGLE_STRONG)

    assert [candidate.candidate_id for candidate in result.selected] == [f"{STRONG}#1"]
    assert [candidate.candidate_id for candidate in result.round.rejected] == [f"{STRONG}#2"]
    assert result.round.rejected[0].checks[0].detail == "file does not exist"


def test_single_strong_attests_every_field_of_the_call(
    registry: CorpusRegistry, package: Path
) -> None:
    transport = ScriptedTransport({STRONG: [_response(_reply(_finding()), served=STRONG)]})
    result = _run(_coordinator(transport, registry), package, mode=swarm.SwarmMode.SINGLE_STRONG)
    call = result.round.calls[0]

    assert call.requested_model == STRONG
    assert call.served_model == STRONG
    assert call.attributed_model == STRONG
    assert call.prompt_sha256 == swarm._sha256_text(transport.prompts[0])
    assert call.response_sha256 == swarm._sha256_text(transport.script[STRONG][0].text)
    assert (call.tokens_in, call.tokens_out, call.latency_ms) == (1_200, 300, 42)
    assert call.quarantined_fallback is False
    payload = call.as_payload()
    for field in (
        "requested_model",
        "served_model",
        "prompt_sha256",
        "response_sha256",
        "tokens_in",
        "tokens_out",
        "latency_ms",
    ):
        assert field in payload


def test_single_strong_fallback_is_quarantined_and_never_attributed(
    registry: CorpusRegistry, package: Path
) -> None:
    transport = ScriptedTransport(
        {STRONG: [_response(_reply(_finding()), served="anthropic/claude-sonnet-5-5")]}
    )
    result = _run(_coordinator(transport, registry), package, mode=swarm.SwarmMode.SINGLE_STRONG)

    call = result.round.calls[0]
    assert call.quarantined_fallback is True
    assert call.attributed_model is None
    assert call.error is None
    assert result.round.candidates == ()
    assert result.selected == ()
    assert result.accepted is False
    assert result.round.attributed_models == ()


# --------------------------------------------------------------------------- #
# CHEAP_SWARM
# --------------------------------------------------------------------------- #


def _distinct_swarm() -> dict[str, list[swarm.ModelResponse]]:
    return {
        KIMI: [_response(_reply(_finding()), served=KIMI)],
        MUSE: [
            _response(
                _reply(
                    _finding(
                        path="tests/test_grader.py",
                        line=2,
                        quote="assert True",
                        mechanism="test file asserts a constant instead of the artifact",
                    )
                ),
                served=MUSE,
            )
        ],
        SONNET: [
            _response(
                _reply(
                    _finding(
                        path="task/solve.sh",
                        line=2,
                        quote="echo done",
                        mechanism="solve script echoes success without producing output",
                    )
                ),
                served=SONNET,
            )
        ],
    }


def test_cheap_swarm_union_keeps_every_workers_cluster(
    registry: CorpusRegistry, package: Path
) -> None:
    transport = ScriptedTransport(_distinct_swarm())
    result = _run(
        _coordinator(transport, registry),
        package,
        mode=swarm.SwarmMode.CHEAP_SWARM,
        aggregation=swarm.Aggregation.UNION,
    )

    assert sorted(transport.routes) == sorted(CHEAP)
    assert result.round.frontier_calls == 0
    assert len(result.round.clusters) == 3
    assert len(result.selected) == 3
    assert result.aggregation is swarm.Aggregation.UNION
    assert result.accepted


def test_cheap_swarm_majority_vote_requires_a_strict_majority(
    registry: CorpusRegistry, package: Path
) -> None:
    transport = ScriptedTransport(
        {
            KIMI: [_response(_reply(_finding()), served=KIMI)],
            MUSE: [_response(_reply(_finding(confidence=0.85)), served=MUSE)],
            SONNET: [
                _response(
                    _reply(_finding(path="task/solve.sh", line=2, quote="echo done", mechanism="script lies")),
                    served=SONNET,
                )
            ],
        }
    )
    result = _run(
        _coordinator(transport, registry),
        package,
        mode=swarm.SwarmMode.CHEAP_SWARM,
        aggregation=swarm.Aggregation.MAJORITY_VOTE,
    )

    assert len(result.round.clusters) == 2
    assert [cluster.support for cluster in result.round.clusters] == [2, 1]
    assert len(result.selected) == 1
    assert result.selected[0].model in {KIMI, MUSE}
    assert result.selected[0].citation_verified


def test_cheap_swarm_citation_verified_union_drops_an_invented_citation(
    registry: CorpusRegistry, package: Path
) -> None:
    script = _distinct_swarm()
    script[SONNET] = [
        _response(
            _reply(_finding(path="task/invented.py", line=3, quote=None, mechanism="phantom check")),
            served=SONNET,
        )
    ]
    result = _run(
        _coordinator(ScriptedTransport(script), registry),
        package,
        mode=swarm.SwarmMode.CHEAP_SWARM,
        aggregation=swarm.Aggregation.CITATION_VERIFIED_UNION,
    )

    assert len(result.round.candidates) == 3
    assert len(result.round.rejected) == 1
    assert result.round.rejected[0].model == SONNET
    assert len(result.selected) == 2
    assert all(candidate.citation_verified for candidate in result.selected)


def test_cheap_swarm_quarantines_one_route_and_keeps_the_others(
    registry: CorpusRegistry, package: Path
) -> None:
    script = _distinct_swarm()
    script[KIMI] = [_response(_reply(_finding()), served=MUSE)]
    transport = ScriptedTransport(script)
    result = _run(
        _coordinator(transport, registry),
        package,
        mode=swarm.SwarmMode.CHEAP_SWARM,
        aggregation=swarm.Aggregation.UNION,
    )

    quarantined = result.round.quarantined
    assert len(quarantined) == 1
    assert quarantined[0].requested_model == KIMI
    assert quarantined[0].served_model == MUSE
    assert quarantined[0].attributed_model is None
    assert KIMI not in {candidate.model for candidate in result.round.candidates}
    assert set(result.round.attributed_models) == {MUSE, SONNET}
    assert len(result.selected) == 2


def test_cheap_swarm_records_a_transport_failure_as_an_attested_error(
    registry: CorpusRegistry, package: Path
) -> None:
    script = _distinct_swarm()
    script[KIMI] = [_response("", served="", error="omp exited 1: rate limited")]
    result = _run(
        _coordinator(ScriptedTransport(script), registry),
        package,
        mode=swarm.SwarmMode.CHEAP_SWARM,
    )

    failed = next(call for call in result.round.calls if call.requested_model == KIMI)
    assert failed.error == "omp exited 1: rate limited"
    assert failed.quarantined_fallback is True
    assert failed.usable is False


def test_cheap_swarm_reports_a_parse_failure_without_dropping_the_round(
    registry: CorpusRegistry, package: Path
) -> None:
    script = _distinct_swarm()
    script[KIMI] = [_response("no json here", served=KIMI)]
    result = _run(
        _coordinator(ScriptedTransport(script), registry),
        package,
        mode=swarm.SwarmMode.CHEAP_SWARM,
    )

    failed = next(call for call in result.round.calls if call.requested_model == KIMI)
    assert failed.error is not None and failed.error.startswith("parse_error:")
    assert failed.quarantined_fallback is False
    assert failed.usable is False
    assert len(result.selected) == 2


# --------------------------------------------------------------------------- #
# HYBRID_ADJUDICATOR
# --------------------------------------------------------------------------- #


def _adjudication_reply(
    *verdicts: Mapping[str, Any], final: Sequence[Mapping[str, Any]] = ()
) -> str:
    return json.dumps({"verdicts": [dict(verdict) for verdict in verdicts], "final": list(final)})


def test_hybrid_sends_one_compact_call_to_a_single_adjudicator(
    registry: CorpusRegistry, package: Path
) -> None:
    script = _distinct_swarm()
    script[STRONG] = [
        _response(
            _adjudication_reply(
                {"candidate_id": f"{KIMI}#1", "decision": "accept", "reason": "real exit-code check"},
                {"candidate_id": f"{SONNET}#1", "decision": "reject", "reason": "echo is not a check"},
                final=[_finding(confidence=0.95)],
            ),
            served=STRONG,
        )
    ]
    transport = ScriptedTransport(script)
    result = _run(_coordinator(transport, registry), package, mode=swarm.SwarmMode.HYBRID_ADJUDICATOR)

    assert sorted(transport.routes) == sorted([*CHEAP, STRONG])
    assert result.round.frontier_calls == 1
    assert result.round.cheap_calls == 3
    assert result.escalated is True
    assert result.accepted
    assert [candidate.candidate_id for candidate in result.selected] == [f"{STRONG}#1"]
    adjudication = result.adjudication
    assert adjudication is not None
    assert adjudication["status"] == "completed"
    assert adjudication["accepted_candidate_ids"] == [f"{STRONG}#1"]
    assert f"{SONNET}#1" in adjudication["rejected_candidate_ids"]

    adjudicator_prompt = transport.prompts[-1]
    for candidate_id in (f"{KIMI}#1", f"{MUSE}#1", f"{SONNET}#1"):
        assert candidate_id in adjudicator_prompt
    assert "citation_verified: true" in adjudicator_prompt
    assert "grader trusts the solve script exit code" in adjudicator_prompt


def test_hybrid_adjudicator_rejecting_everything_returns_no_selection(
    registry: CorpusRegistry, package: Path
) -> None:
    script = _distinct_swarm()
    script[STRONG] = [
        _response(
            _adjudication_reply(
                *[
                    {"candidate_id": f"{model}#1", "decision": "reject", "reason": "not the failure"}
                    for model in CHEAP
                ],
                final=[],
            ),
            served=STRONG,
        )
    ]
    result = _run(
        _coordinator(ScriptedTransport(script), registry),
        package,
        mode=swarm.SwarmMode.HYBRID_ADJUDICATOR,
    )

    assert result.selected == ()
    assert result.accepted is False
    assert result.adjudication is not None
    assert result.adjudication["status"] == "completed"
    assert result.adjudication["accepted_candidate_ids"] == []


def test_hybrid_rechecks_the_adjudicators_own_citations(
    registry: CorpusRegistry, package: Path
) -> None:
    script = _distinct_swarm()
    script[STRONG] = [
        _response(
            _adjudication_reply(
                {"candidate_id": f"{STRONG}#1", "decision": "accept", "reason": "trust me"},
                final=[_finding(path="task/invented.py", line=3, quote=None, confidence=0.99)],
            ),
            served=STRONG,
        )
    ]
    result = _run(
        _coordinator(ScriptedTransport(script), registry),
        package,
        mode=swarm.SwarmMode.HYBRID_ADJUDICATOR,
    )

    assert result.selected == ()
    assert result.accepted is False
    assert result.adjudication is not None
    assert result.adjudication["dropped"] == [
        {"candidate_id": f"{STRONG}#1", "reason": "citation_unverified"}
    ]


def test_hybrid_adjudicator_fallback_is_quarantined(
    registry: CorpusRegistry, package: Path
) -> None:
    script = _distinct_swarm()
    script[STRONG] = [_response(_adjudication_reply(final=[]), served="openai-codex/gpt-6.1-sol:max")]
    result = _run(
        _coordinator(ScriptedTransport(script), registry),
        package,
        mode=swarm.SwarmMode.HYBRID_ADJUDICATOR,
    )

    adjudicator = result.round.calls[-1]
    assert adjudicator.role is swarm.Role.ADJUDICATOR
    assert adjudicator.quarantined_fallback is True
    assert result.adjudication is not None
    assert result.adjudication["status"] == "unavailable"
    assert result.selected == ()
    assert result.accepted is False


def test_hybrid_retrieval_evidence_reaches_the_adjudicator(
    registry: CorpusRegistry, package: Path
) -> None:
    retriever = FakeRetriever(
        [
            swarm.RetrievalRecord(
                kind="issue",
                task_id="terminal-bench/other-dev-task",
                record_id="retained-issue:dev",
                summary="issue retained-issue:dev on terminal-bench/other-dev-task: grader_wrong_pass at task/grader.py:6",
                fields={"defect_class": "grader_wrong_pass", "direction": "wrong_pass"},
            ),
            swarm.RetrievalRecord(
                kind="issue",
                task_id=SUBJECT,
                record_id="retained-issue:subject",
                summary="issue retained-issue:subject on terminal-bench/dev-task: self-leak",
            ),
            swarm.RetrievalRecord(
                kind="issue",
                task_id="another-benchmark/sealed-task",
                record_id="retained-issue:sealed",
                summary="issue retained-issue:sealed on another-benchmark/sealed-task: sealed evidence",
            ),
        ]
    )
    script = _distinct_swarm()
    script[STRONG] = [
        _response(
            _adjudication_reply(final=[_finding(confidence=0.95)]),
            served=STRONG,
        )
    ]
    transport = ScriptedTransport(script)
    result = _run(
        _coordinator(transport, registry, retriever=retriever),
        package,
        mode=swarm.SwarmMode.HYBRID_ADJUDICATOR,
    )

    assert retriever.calls == [
        (
            swarm.WorkKind.TASK_QUALITY_AUDIT,
            SUBJECT,
            "Audit this package for grader defects.",
        )
    ]
    assert result.retrieval_dropped_sealed == 1
    assert result.retrieval_dropped_subject == 1
    assert [record.record_id for record in result.retrieval] == ["retained-issue:dev"]
    assert "retained-issue:dev" in transport.prompts[-1]
    assert "retained-issue:subject" not in transport.prompts[-1]
    assert "retained-issue:sealed" not in transport.prompts[-1]
    for prompt in transport.prompts:
        assert "sealed-task" not in prompt

# --------------------------------------------------------------------------- #
# ADAPTIVE_CASCADE
# --------------------------------------------------------------------------- #


def test_cascade_accepts_a_unanimous_verified_swarm_at_zero_frontier_cost(
    registry: CorpusRegistry, package: Path
) -> None:
    transport = ScriptedTransport(
        {
            KIMI: [_agreed(confidence=0.9, served=KIMI)],
            MUSE: [_agreed(confidence=0.95, served=MUSE)],
            SONNET: [_agreed(confidence=0.85, served=SONNET)],
        }
    )
    result = _run(
        _coordinator(transport, registry), package, mode=swarm.SwarmMode.ADAPTIVE_CASCADE
    )

    assert transport.routes == list(CHEAP)
    assert result.round.frontier_calls == 0
    assert result.escalated is False
    assert result.adjudication is None
    assert result.round.cascade_confidence == 0.85
    assert result.accepted
    assert len(result.round.clusters) == 1
    assert result.round.clusters[0].support == 3
    assert result.selected[0].citation_verified


def test_cascade_escalates_when_the_swarm_disagrees(
    registry: CorpusRegistry, package: Path
) -> None:
    script = {
        KIMI: [_response(_reply(_finding()), served=KIMI)],
        MUSE: [_response(_reply(_finding(confidence=0.88)), served=MUSE)],
        SONNET: [
            _response(
                _reply(_finding(path="task/solve.sh", line=2, quote="echo done", mechanism="script lies")),
                served=SONNET,
            )
        ],
        STRONG: [_response(_adjudication_reply(final=[_finding(confidence=0.97)]), served=STRONG)],
    }
    result = _run(
        _coordinator(ScriptedTransport(script), registry),
        package,
        mode=swarm.SwarmMode.ADAPTIVE_CASCADE,
    )

    assert result.escalated is True
    assert result.round.cascade_confidence is None
    assert result.round.frontier_calls == 1
    assert result.accepted


def test_cascade_escalates_below_the_confidence_threshold(
    registry: CorpusRegistry, package: Path
) -> None:
    script = {
        KIMI: [_agreed(confidence=0.5, served=KIMI)],
        MUSE: [_agreed(confidence=0.6, served=MUSE)],
        SONNET: [_agreed(confidence=0.7, served=SONNET)],
        STRONG: [_response(_adjudication_reply(final=[_finding(confidence=0.95)]), served=STRONG)],
    }
    result = _run(
        _coordinator(ScriptedTransport(script), registry),
        package,
        mode=swarm.SwarmMode.ADAPTIVE_CASCADE,
    )

    assert result.escalated is True
    assert result.round.cascade_confidence == 0.5
    assert result.round.frontier_calls == 1


def test_cascade_escalates_when_a_unanimous_citation_does_not_verify(
    registry: CorpusRegistry, package: Path
) -> None:
    invented = _finding(path="task/invented.py", line=2, quote=None, confidence=0.95)
    script = {
        KIMI: [_response(_reply(invented), served=KIMI)],
        MUSE: [_response(_reply(invented), served=MUSE)],
        SONNET: [_response(_reply(invented), served=SONNET)],
        STRONG: [_response(_adjudication_reply(final=[_finding(confidence=0.9)]), served=STRONG)],
    }
    result = _run(
        _coordinator(ScriptedTransport(script), registry),
        package,
        mode=swarm.SwarmMode.ADAPTIVE_CASCADE,
    )

    assert len(result.round.clusters) == 1
    assert result.round.clusters[0].support == 3
    assert result.round.clusters[0].citation_verified is False
    assert result.escalated is True
    assert result.round.cascade_confidence is None


def test_cascade_escalates_when_one_route_was_quarantined(
    registry: CorpusRegistry, package: Path
) -> None:
    script = {
        KIMI: [_response(_reply(_finding()), served=MUSE)],
        MUSE: [_agreed(confidence=0.95, served=MUSE)],
        SONNET: [_agreed(confidence=0.95, served=SONNET)],
        STRONG: [_response(_adjudication_reply(final=[_finding(confidence=0.9)]), served=STRONG)],
    }
    result = _run(
        _coordinator(ScriptedTransport(script), registry),
        package,
        mode=swarm.SwarmMode.ADAPTIVE_CASCADE,
    )

    assert result.escalated is True
    assert result.round.cascade_confidence is None
    assert len(result.round.quarantined) == 1
    assert result.round.frontier_calls == 1


def test_cascade_threshold_is_configurable(registry: CorpusRegistry, package: Path) -> None:
    script = {
        KIMI: [_agreed(confidence=0.5, served=KIMI)],
        MUSE: [_agreed(confidence=0.6, served=MUSE)],
        SONNET: [_agreed(confidence=0.7, served=SONNET)],
    }
    result = _run(
        _coordinator(ScriptedTransport(script), registry, threshold=0.4),
        package,
        mode=swarm.SwarmMode.ADAPTIVE_CASCADE,
    )

    assert result.escalated is False
    assert result.round.frontier_calls == 0
    assert result.round.cascade_confidence == 0.5


# --------------------------------------------------------------------------- #
# Split lock
# --------------------------------------------------------------------------- #


def test_sealed_test_item_is_refused_before_any_model_call(
    registry: CorpusRegistry, package: Path
) -> None:
    transport = ScriptedTransport({STRONG: [_response(_reply(_finding()), served=STRONG)]})
    coordinator = _coordinator(transport, registry)
    with pytest.raises(SplitLockError):
        _run(
            coordinator,
            package,
            mode=swarm.SwarmMode.SINGLE_STRONG,
            dataset_id=SEALED["dataset_id"],
            item_id=SEALED["item_id"],
        )
    assert transport.requests == []


def test_a_matching_frozen_token_admits_a_sealed_item(
    registry: CorpusRegistry, package: Path
) -> None:
    transport = ScriptedTransport({STRONG: [_response(_reply(_finding()), served=STRONG)]})
    result = _run(
        _coordinator(transport, registry),
        package,
        mode=swarm.SwarmMode.SINGLE_STRONG,
        dataset_id=SEALED["dataset_id"],
        item_id=SEALED["item_id"],
        frozen_eval_token=FROZEN_TOKEN,
    )
    assert result.round.split == "test"
    assert len(transport.requests) == 1


def test_a_wrong_frozen_token_does_not_admit_a_sealed_item(
    registry: CorpusRegistry, package: Path
) -> None:
    transport = ScriptedTransport({STRONG: [_response(_reply(_finding()), served=STRONG)]})
    with pytest.raises(SplitLockError):
        _run(
            _coordinator(transport, registry),
            package,
            mode=swarm.SwarmMode.SINGLE_STRONG,
            dataset_id=SEALED["dataset_id"],
            item_id=SEALED["item_id"],
            frozen_eval_token="guessed-token",
        )
    assert transport.requests == []


def test_an_unregistered_item_is_refused(registry: CorpusRegistry, package: Path) -> None:
    transport = ScriptedTransport({STRONG: [_response(_reply(_finding()), served=STRONG)]})
    with pytest.raises(KeyError):
        _run(
            _coordinator(transport, registry),
            package,
            mode=swarm.SwarmMode.SINGLE_STRONG,
            item_id="never-registered",
        )
    assert transport.requests == []


# --------------------------------------------------------------------------- #
# Journal events
# --------------------------------------------------------------------------- #


def _swarm_events(queue: ContinuousWorkQueue) -> list[dict[str, Any]]:
    return [event for event in queue.read_events() if event["system"] == "swarm"]


def test_round_and_adjudication_events_carry_the_attestation(
    registry: CorpusRegistry, package: Path, tmp_path: Path
) -> None:
    queue = ContinuousWorkQueue(tmp_path / "queue.sqlite", registry=registry)
    script = _distinct_swarm()
    script[STRONG] = [_response(_adjudication_reply(final=[_finding(confidence=0.95)]), served=STRONG)]
    result = _run(
        _coordinator(ScriptedTransport(script), registry, queue=queue),
        package,
        mode=swarm.SwarmMode.HYBRID_ADJUDICATOR,
    )

    events = _swarm_events(queue)
    assert [event["event_type"] for event in events] == [
        swarm.EVENT_ROUND_COMPLETED,
        swarm.EVENT_ADJUDICATED,
    ]
    assert [event["status"] for event in events] == ["applied", "confirmed"]
    assert result.events == tuple(event["event_id"] for event in events)

    round_payload = events[0]["payload"]
    assert round_payload["split"] == "dev"
    assert round_payload["mode"] == swarm.SwarmMode.HYBRID_ADJUDICATOR.value
    assert round_payload["frontier_calls"] == 1
    assert round_payload["quarantined_calls"] == []
    assert len(round_payload["calls"]) == 4
    for call in round_payload["calls"]:
        assert call["requested_model"] == call["served_model"] == call["attributed_model"]
        assert len(call["prompt_sha256"]) == 64
        assert len(call["response_sha256"]) == 64
    assert len(round_payload["selected"]) == 1

    adjudicated_payload = events[1]["payload"]
    assert adjudicated_payload["adjudication"]["status"] == "completed"
    assert adjudicated_payload["selected"][0]["citation_verified"] is True


def test_rerunning_the_same_round_is_idempotent(
    registry: CorpusRegistry, package: Path, tmp_path: Path
) -> None:
    queue = ContinuousWorkQueue(tmp_path / "queue.sqlite", registry=registry)
    transport = ScriptedTransport(
        {
            KIMI: [_agreed(confidence=0.9, served=KIMI)],
            MUSE: [_agreed(confidence=0.9, served=MUSE)],
            SONNET: [_agreed(confidence=0.9, served=SONNET)],
        }
    )
    coordinator = _coordinator(transport, registry, queue=queue)
    first = _run(coordinator, package, mode=swarm.SwarmMode.ADAPTIVE_CASCADE)
    second = _run(coordinator, package, mode=swarm.SwarmMode.ADAPTIVE_CASCADE)

    assert first.events == second.events
    assert len(_swarm_events(queue)) == 1


def test_adjudicated_event_is_refuted_when_nothing_survives(
    registry: CorpusRegistry, package: Path, tmp_path: Path
) -> None:
    queue = ContinuousWorkQueue(tmp_path / "queue.sqlite", registry=registry)
    script = _distinct_swarm()
    script[STRONG] = [
        _response(
            _adjudication_reply(
                {"candidate_id": f"{KIMI}#1", "decision": "reject", "reason": "not a defect"}
            ),
            served=STRONG,
        )
    ]
    _run(
        _coordinator(ScriptedTransport(script), registry, queue=queue),
        package,
        mode=swarm.SwarmMode.HYBRID_ADJUDICATOR,
    )

    events = _swarm_events(queue)
    assert [event["status"] for event in events] == ["applied", "refuted"]


def test_no_queue_means_no_events(registry: CorpusRegistry, package: Path) -> None:
    transport = ScriptedTransport({STRONG: [_response(_reply(_finding()), served=STRONG)]})
    result = _run(_coordinator(transport, registry), package, mode=swarm.SwarmMode.SINGLE_STRONG)
    assert result.events == ()


# --------------------------------------------------------------------------- #
# Clustering, aggregation, and receipts
# --------------------------------------------------------------------------- #


def _candidate(
    package: Path,
    *,
    model: str = KIMI,
    rank: int = 1,
    path: str = "task/grader.py",
    line: int = 6,
    direction: str = "wrong_pass",
    mechanism: str = "grader trusts the solve script exit code",
    confidence: float = 0.9,
    index: int = 1,
) -> swarm.Candidate:
    candidate = swarm.build_candidate(
        _finding(
            path=path,
            line=line,
            direction=direction,
            mechanism=mechanism,
            confidence=confidence,
            quote=None,
        ),
        index=index,
        model=model,
        rank=rank,
        work_kind=swarm.WorkKind.TASK_QUALITY_AUDIT,
        subject_id=SUBJECT,
        verify=_verify(package),
    )
    assert candidate is not None
    return candidate


def test_clustering_joins_nearby_loci_and_keeps_directions_apart(package: Path) -> None:
    nearby = _candidate(package, model=MUSE, rank=2, line=10)
    far = _candidate(package, model=SONNET, rank=3, path="task/solve.sh", line=2, mechanism="echo lies")
    opposite = _candidate(package, model=SONNET, rank=3, direction="unfair_fail")
    base = _candidate(package)

    clusters = swarm.cluster_candidates([base, nearby, far, opposite])
    assert [cluster.support for cluster in clusters] == [2, 1, 1]
    assert clusters[0].models == {KIMI, MUSE}
    assert clusters[0].representative.rank == 1


def test_clustering_joins_the_same_mechanism_at_a_different_path(package: Path) -> None:
    left = _candidate(package)
    right = _candidate(
        package,
        model=MUSE,
        rank=2,
        path="tests/test_grader.py",
        line=2,
        mechanism="the grader trusts the solve script exit code",
    )
    clusters = swarm.cluster_candidates([left, right])
    assert len(clusters) == 1
    assert clusters[0].support == 2


def test_aggregate_policies_are_distinct(package: Path) -> None:
    clusters = swarm.cluster_candidates(
        [
            _candidate(package),
            _candidate(package, model=MUSE, rank=2, confidence=0.95),
            _candidate(package, model=SONNET, rank=3, path="task/solve.sh", line=2, mechanism="echo lies"),
        ]
    )
    responding = list(CHEAP)
    assert len(swarm.aggregate(clusters, aggregation=swarm.Aggregation.UNION, responding_models=responding)) == 2
    assert (
        len(
            swarm.aggregate(
                clusters, aggregation=swarm.Aggregation.MAJORITY_VOTE, responding_models=responding
            )
        )
        == 1
    )
    assert (
        len(
            swarm.aggregate(
                clusters,
                aggregation=swarm.Aggregation.CITATION_VERIFIED_UNION,
                responding_models=responding,
            )
        )
        == 2
    )


def test_unknown_usage_stays_unknown_in_the_receipt(
    registry: CorpusRegistry, package: Path
) -> None:
    transport = ScriptedTransport(
        {STRONG: [_response(_reply(_finding()), served=STRONG, tokens_in=None, tokens_out=None)]}
    )
    result = _run(_coordinator(transport, registry), package, mode=swarm.SwarmMode.SINGLE_STRONG)

    assert result.tokens_in is None
    assert result.tokens_out is None
    assert result.attestation()["tokens_in"] is None


def test_adjudicator_prompt_stays_within_its_candidate_budget(package: Path) -> None:
    runaway = _candidate(package, mechanism="x" * 5_000)
    prompt = swarm.build_adjudicator_prompt(
        work_kind=swarm.WorkKind.TASK_QUALITY_AUDIT,
        subject_id=SUBJECT,
        clusters=[swarm.Cluster((runaway,))],
        unverified=[runaway],
    )
    assert runaway.candidate_id in prompt
    assert len(prompt) < swarm.MAX_ADJUDICATOR_CANDIDATE_CHARS + 3_000


def test_cloud_trace_database_retriever_reads_real_issues_and_traces_with_leave_one_out(
    registry: CorpusRegistry, tmp_path: Path
) -> None:
    other_task = "cheatbench/independent"
    database = CloudTraceDatabase(tmp_path / "cloud-trace-db.sqlite")
    try:
        for task_id, ds in ((SUBJECT, DATASET), (other_task, "cheatbench-actions-v1")):
            database.upsert_task(
                {
                    "task_id": task_id,
                    "dataset_id": ds,
                    "split": "DEV",
                    "instruction_text": "Grade the produced artifact, not the exit code.",
                }
            )
            database.upsert_task_issue(
                {
                    "issue_id": f"retained-issue:{task_id}",
                    "task_id": task_id,
                    "defect_class": "grader_wrong_pass",
                    "direction": "wrong_pass",
                    "status": "confirmed",
                    "citation_file": "task/grader.py",
                    "citation_line": 6,
                    "original_reward": 1.0,
                    "repaired_reward": 0.0,
                }
            )
            database.upsert_trace(
                {
                    "trace_id": f"retained-trace:{task_id}",
                    "run_id": "run-dev",
                    "task_id": task_id,
                    "model_id": KIMI,
                    "outcome": "audit_proposals_not_execution",
                    "step_count": 1,
                },
                steps=[
                    {
                        "trace_id": f"retained-trace:{task_id}",
                        "step_index": 0,
                        "role": "assistant",
                        "tool_name": "read",
                        "content_text": "the grader trusts the exit code",
                    }
                ],
            )
        retriever = swarm.CloudTraceDatabaseRetriever(database, registry=registry)
        records = retriever.retrieve(
            work_kind=swarm.WorkKind.TASK_QUALITY_AUDIT,
            subject_id=SUBJECT,
            query="grader exit code",
        )
    finally:
        database.close()

    assert [record.task_id for record in records] == [other_task]
    issue = records[0]
    assert issue.record_id == f"retained-issue:{other_task}"
    assert "grader_wrong_pass" in issue.summary
    assert issue.fields["citation"] == "task/grader.py:6"
    assert issue.fields["trace_id"] == f"retained-trace:{other_task}"

def test_attestation_receipt_is_json_serializable(
    registry: CorpusRegistry, package: Path
) -> None:
    transport = ScriptedTransport({STRONG: [_response(_reply(_finding()), served=STRONG)]})
    result = _run(_coordinator(transport, registry), package, mode=swarm.SwarmMode.SINGLE_STRONG)
    receipt = result.attestation()

    encoded = json.dumps(receipt, sort_keys=True)
    assert json.loads(encoded) == receipt
    assert receipt["schema"] == "swarm.coordinator/v1"
    assert receipt["accepted"] is True
    assert receipt["round"]["split"] == "dev"
    assert receipt["selected"][0]["payload_sha256"] == swarm._sha256_text("{}")
    assert result.tokens_in == 1_200 and result.tokens_out == 300


def test_oversized_payloads_are_digested_not_inlined(
    registry: CorpusRegistry, package: Path
) -> None:
    transport = ScriptedTransport(
        {
            STRONG: [
                _response(
                    _reply(_finding(payload={"patch": "x" * (swarm.MAX_INLINE_PAYLOAD_CHARS + 10)})),
                    served=STRONG,
                )
            ]
        }
    )
    result = _run(_coordinator(transport, registry), package, mode=swarm.SwarmMode.SINGLE_STRONG)
    payload = result.selected[0].as_payload()

    assert payload["payload"] == {
        "omitted": True,
        "sha256": swarm._sha256_text(swarm._canonical({"patch": "x" * (swarm.MAX_INLINE_PAYLOAD_CHARS + 10)})),
    }


def test_cascade_saves_the_frontier_call_the_strong_arm_spends(
    registry: CorpusRegistry, package: Path
) -> None:
    """The scientific claim, measured: same subject, same finding, fewer frontier calls."""
    strong = _run(
        _coordinator(
            ScriptedTransport({STRONG: [_response(_reply(_finding()), served=STRONG)]}), registry
        ),
        package,
        mode=swarm.SwarmMode.SINGLE_STRONG,
    )
    cascade = _run(
        _coordinator(
            ScriptedTransport(
                {
                    KIMI: [_agreed(confidence=0.9, served=KIMI)],
                    MUSE: [_agreed(confidence=0.9, served=MUSE)],
                    SONNET: [_agreed(confidence=0.9, served=SONNET)],
                }
            ),
            registry,
        ),
        package,
        mode=swarm.SwarmMode.ADAPTIVE_CASCADE,
    )

    assert strong.round.frontier_calls == 1
    assert cascade.round.frontier_calls == 0
    assert cascade.round.cheap_calls == 3
    assert cascade.escalated is False
    assert cascade.selected[0].direction == strong.selected[0].direction
    assert cascade.selected[0].citations == strong.selected[0].citations
    assert cascade.round.cascade_confidence == 0.9


def test_hybrid_adjudicator_never_receives_raw_package_bodies(
    registry: CorpusRegistry, package: Path
) -> None:
    script = _distinct_swarm()
    script[STRONG] = [_response(_adjudication_reply(final=[_finding(confidence=0.95)]), served=STRONG)]
    transport = ScriptedTransport(script)
    _run(_coordinator(transport, registry), package, mode=swarm.SwarmMode.HYBRID_ADJUDICATOR)

    adjudicator_prompt = transport.prompts[-1]
    assert GRADER.strip() not in adjudicator_prompt
    assert SOLVE.strip() not in adjudicator_prompt
    assert len(adjudicator_prompt) < 12_000


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


def test_constructor_rejects_routes_and_budgets_it_cannot_honour(
    registry: CorpusRegistry,
) -> None:
    transport = ScriptedTransport({STRONG: [_response(_reply(_finding()), served=STRONG)]})
    with pytest.raises(ValueError, match="strong_model"):
        _coordinator(transport, registry, strong="claude-opus")
    with pytest.raises(ValueError, match="cheap_models must not repeat"):
        _coordinator(transport, registry, cheap=(KIMI, KIMI))
    with pytest.raises(ValueError, match="must not include the strong model"):
        _coordinator(transport, registry, cheap=(KIMI, STRONG))
    with pytest.raises(ValueError, match="confidence_threshold"):
        _coordinator(transport, registry, threshold=1.5)
    with pytest.raises(ValueError, match="workspace directory is required"):
        swarm.SwarmCoordinator(transport=transport, registry=registry).run(
            mode=swarm.SwarmMode.SINGLE_STRONG,
            work_kind=swarm.WorkKind.TASK_QUALITY_AUDIT,
            subject_id=SUBJECT,
            prompt="audit",
            dataset_id=DEV["dataset_id"],
            item_id=DEV["item_id"],
        )


def test_work_kinds_are_all_usable(registry: CorpusRegistry, package: Path) -> None:
    for work_kind in swarm.WorkKind:
        transport = ScriptedTransport({STRONG: [_response(_reply(_finding()), served=STRONG)]})
        result = _run(
            _coordinator(transport, registry),
            package,
            mode=swarm.SwarmMode.SINGLE_STRONG,
            work_kind=work_kind,
        )
        assert result.round.work_kind is work_kind
        assert swarm.WORK_BRIEF[work_kind] in transport.prompts[0]


# --------------------------------------------------------------------------- #
# Verifier pW-SELF-4 regressions (a)-(e)
# --------------------------------------------------------------------------- #


def test_resolved_model_is_fallback_quarantines_call_even_when_served_matches(
    registry: CorpusRegistry, package: Path
) -> None:
    stream = "\n".join(
        [
            json.dumps(
                {
                    "type": "model_change",
                    "model": STRONG,
                    "resolvedModelIsFallback": True,
                }
            ),
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "provider": "anthropic",
                        "model": "claude-opus-5-5",
                        "content": [{"type": "text", "text": _reply(_finding())}],
                        "usage": {"input": 100, "output": 20, "cacheRead": 0, "cacheWrite": 0},
                    },
                }
            ),
        ]
    )
    transport = swarm.OmpCliTransport(runner=lambda *_: (0, stream, ""))
    result = _run(
        _coordinator(transport, registry),
        package,
        mode=swarm.SwarmMode.SINGLE_STRONG,
    )
    call = result.round.calls[0]
    assert call.served_model == STRONG
    assert call.quarantined_fallback is True
    assert call.attributed_model is None
    assert result.selected == ()


def test_reasoning_suffix_stripped_and_providerless_rejected(
    registry: CorpusRegistry, package: Path
) -> None:
    transport = ScriptedTransport(
        {
            "openai-codex/gpt-6-astra:max": [
                _response(_reply(_finding()), served="openai-codex/gpt-6.1-sol")
            ]
        }
    )
    coordinator = _coordinator(
        transport,
        registry,
        strong="openai-codex/gpt-6-astra:max",
    )
    result = _run(coordinator, package, mode=swarm.SwarmMode.SINGLE_STRONG)
    assert result.round.calls[0].quarantined_fallback is False
    assert len(result.selected) == 1

    providerless = ScriptedTransport(
        {
            "openai-codex/gpt-6.1-sol:max": [
                _response(_reply(_finding()), served="gpt-6.1-sol")
            ]
        }
    )
    quarantined = _run(
        _coordinator(providerless, registry, strong="openai-codex/gpt-6.1-sol:max"),
        package,
        mode=swarm.SwarmMode.SINGLE_STRONG,
    )
    assert quarantined.round.calls[0].quarantined_fallback is True
    assert quarantined.selected == ()


def test_subject_task_verified_against_registry_and_assignments_before_spend(
    corpus_root: Path, package: Path
) -> None:
    splits = corpus_root / "research/answer-keys/splits"
    splits.mkdir(parents=True, exist_ok=True)
    (splits / "assignments.csv").write_text(
        "key_id,item_id,task,unit,split,expected_verdict,exposures\n"
        "tb2-to-tb21-v1,tb2-to-tb21-v1:tb21:fix-git,fix-git,tb2/fix-git,test,pass,\n",
        encoding="utf-8",
    )
    reg = CorpusRegistry(corpus_root, frozen_eval_token=FROZEN_TOKEN)
    transport = ScriptedTransport({STRONG: [_response(_reply(_finding()), served=STRONG)]})
    coordinator = _coordinator(transport, reg)

    # Sealed subject passed under a DEV item_id must fail before any call.
    with pytest.raises(SplitLockError):
        _run(
            coordinator,
            package,
            mode=swarm.SwarmMode.SINGLE_STRONG,
            subject_id="terminal-bench/sealed-task",
        )
    # tb2-to-tb21-v1 TEST ledger task must fail before any call.
    with pytest.raises(SplitLockError):
        _run(
            coordinator,
            package,
            mode=swarm.SwarmMode.SINGLE_STRONG,
            dataset_id="tb2-to-tb21-v1",
            item_id="tb2-to-tb21-v1:tb21:fix-git",
            subject_id="fix-git",
        )
    assert transport.requests == []


def test_retrieval_swarm_mode_injects_evidence_while_cheap_swarm_stays_unaugmented(
    registry: CorpusRegistry, package: Path
) -> None:
    retriever = FakeRetriever(
        [
            swarm.RetrievalRecord(
                kind="issue",
                task_id="cheatbench/independent",
                record_id="retained-issue:other",
                summary="issue retained-issue:other on cheatbench/independent: grader_wrong_pass",
            )
        ]
    )
    cheap_transport = ScriptedTransport(
        {
            KIMI: [_agreed(served=KIMI)],
            MUSE: [_agreed(served=MUSE)],
            SONNET: [_agreed(served=SONNET)],
        }
    )
    cheap_res = _run(
        _coordinator(cheap_transport, registry, retriever=retriever),
        package,
        mode=swarm.SwarmMode.CHEAP_SWARM,
    )
    assert cheap_res.retrieval == ()
    assert all("retained-issue:other" not in p for p in cheap_transport.prompts)

    retrieval_transport = ScriptedTransport(
        {
            KIMI: [_agreed(served=KIMI)],
            MUSE: [_agreed(served=MUSE)],
            SONNET: [_agreed(served=SONNET)],
        }
    )
    ret_res = _run(
        _coordinator(retrieval_transport, registry, retriever=retriever),
        package,
        mode=swarm.SwarmMode.RETRIEVAL_SWARM,
    )
    assert len(ret_res.retrieval) == 1
    assert all("retained-issue:other" in p for p in retrieval_transport.prompts)


def test_hybrid_preserves_proposer_payload_and_reruns_keep_idempotent_event_ids(
    registry: CorpusRegistry, package: Path, tmp_path: Path
) -> None:
    queue = ContinuousWorkQueue(tmp_path / "queue.sqlite", registry=registry)
    patch_payload = {"patch": "--- a/task/grader.py\n+++ b/task/grader.py\n"}
    script = {
        KIMI: [
            _response(
                _reply(_finding(payload=patch_payload)),
                served=KIMI,
                latency_ms=10,
            ),
            _response(
                _reply(_finding(payload=patch_payload)),
                served=KIMI,
                latency_ms=99,
            ),
        ],
        MUSE: [
            _response(_reply(_finding(payload=patch_payload)), served=MUSE, latency_ms=12),
            _response(_reply(_finding(payload=patch_payload)), served=MUSE, latency_ms=88),
        ],
        SONNET: [
            _response(_reply(_finding(payload=patch_payload)), served=SONNET, latency_ms=15),
            _response(_reply(_finding(payload=patch_payload)), served=SONNET, latency_ms=77),
        ],
        STRONG: [
            # Adjudicator final omits payload; proposer payload must be preserved.
            _response(
                _adjudication_reply(final=[_finding(confidence=0.95)]),
                served=STRONG,
                latency_ms=20,
            ),
            _response(
                _adjudication_reply(final=[_finding(confidence=0.95)]),
                served=STRONG,
                latency_ms=120,
            ),
        ],
    }
    coordinator = _coordinator(ScriptedTransport(script), registry, queue=queue)
    first = _run(coordinator, package, mode=swarm.SwarmMode.HYBRID_ADJUDICATOR)
    second = _run(coordinator, package, mode=swarm.SwarmMode.HYBRID_ADJUDICATOR)

    assert dict(first.selected[0].payload) == patch_payload
    assert dict(second.selected[0].payload) == patch_payload
    assert first.events == second.events
    assert len(_swarm_events(queue)) == 2
