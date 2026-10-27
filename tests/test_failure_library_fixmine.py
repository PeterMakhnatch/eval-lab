from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from evallab.failure_library.fixmine.miner import (
    canonical_repo,
    digest,
    mine_repository,
    protected_tasks,
    task_split,
)
from evallab.failure_library.fixmine.models import Candidate, Consensus
from evallab.failure_library.fixmine.publish import build_findings, publish_collection
from evallab.failure_library.store import LibraryStore


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    }
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Fixture Maintainer",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout.strip()


def _put(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def tiny_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[str]]:
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _put(repo, "tasks/demo/tests/test_output.py", "assert output.exists()\n")
    _put(repo, "tasks/demo/solution.sh", "echo 1\n")
    _put(
        repo,
        "data/retail/tasks.json",
        json.dumps([{"id": "0", "answer": "wrong"}, {"id": "1", "answer": "unchanged"}]),
    )
    revisions = [_commit(repo, "Introduce tasks")]
    _put(repo, "tasks/demo/tests/test_output.py", "assert output.read_text() == 'correct'\n")
    _put(
        repo,
        "data/retail/tasks.json",
        json.dumps(
            [
                {"id": "0", "answer": "correct"},
                {"id": "1", "answer": "unchanged"},
                {"id": "2", "answer": "new"},
            ]
        ),
    )
    revisions.append(_commit(repo, "Fix weak test and wrong task answer"))
    _put(repo, "tasks/demo/tests/test_extra.py", "assert output.stat().st_size > 0\n")
    _put(repo, "tasks/new-task/tests/test_new.py", "assert True\n")
    revisions.append(_commit(repo, "Fix missing coverage and introduce a new task"))
    _put(repo, "tasks/demo/solution.sh", "echo 2\n")
    revisions.append(_commit(repo, "Correct reference solution"))
    return repo, revisions


def _decisions(candidates: list[Candidate]) -> list[Consensus]:
    return [
        Consensus(
            candidate_id=candidate.candidate_id,
            accepted=True,
            defect_class="TS01",
            direction="wrong_pass",
            summary="The old test accepts an output without checking its contents.",
            disagreement=False,
            models=["openai-codex/gpt-6.1-sol", "anthropic/claude-opus-5-5"],
            label_refs=["state://a", "state://b"],
        )
        for candidate in candidates
    ]


def test_miner_pins_existing_tasks_and_added_tests_not_new_tasks(
    tiny_repo: tuple[Path, list[str]], tmp_path: Path
) -> None:
    repo, revisions = tiny_repo
    root, candidates = mine_repository(repo.as_uri(), "tasks/*", tmp_path / "state")
    assert len(candidates) == 3
    assert {candidate.task_id for candidate in candidates} == {"demo"}
    assert {candidate.fix_revision for candidate in candidates} == set(revisions[1:])
    for candidate in candidates:
        assert candidate.parent_revision == revisions[revisions.index(candidate.fix_revision) - 1]
        assert digest((root / candidate.diff_ref).read_bytes()) == candidate.diff_digest
        assert len(candidate.parent_revision) == len(candidate.fix_revision) == 40
    added_test = next(
        candidate for candidate in candidates if candidate.fix_revision == revisions[2]
    )
    assert added_test.changed_files == ["tasks/demo/tests/test_extra.py"]
    assert (root / "manifest.json").stat().st_mode & 0o222 == 0


def test_shared_json_table_maps_only_changed_existing_row(
    tiny_repo: tuple[Path, list[str]], tmp_path: Path
) -> None:
    repo, revisions = tiny_repo
    root, candidates = mine_repository(repo.as_uri(), "data/*/tasks.json", tmp_path / "state")
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.task_id == "0"
    assert candidate.task_path == "data/retail/tasks.json#0"
    assert candidate.parent_revision == revisions[0]
    patch = (root / candidate.diff_ref).read_text()
    assert "wrong" in patch and "correct" in patch
    assert "unchanged" not in patch and '"id": "2"' not in patch


def test_cache_and_publication_rebuild_are_byte_deterministic(
    tiny_repo: tuple[Path, list[str]], tmp_path: Path
) -> None:
    repo, _ = tiny_repo
    state = tmp_path / "state"
    root, candidates = mine_repository(repo.as_uri(), "tasks/*", state)
    cached_root, cached = mine_repository(repo.as_uri(), "tasks/*", state)
    assert root == cached_root and cached == candidates
    decisions = _decisions(candidates)
    store = LibraryStore.for_repo(tmp_path / "consumer", state_root=state)
    first_report = publish_collection(store, candidates, decisions, root)
    path = store.findings_path(first_report["collection"])
    first_bytes = path.read_bytes()
    second_report = publish_collection(
        store, list(reversed(candidates)), list(reversed(decisions)), root
    )
    assert path.read_bytes() == first_bytes
    assert first_report == second_report
    findings = list(store.iter_findings(first_report["collection"]))
    assert all(finding.tier == "V2_upstream_fixed" for finding in findings)
    assert all(finding.status_now == "fixed_upstream" for finding in findings)
    assert all(finding.task.upstream_revision != finding.fixed_in_revision for finding in findings)
    assert all(finding.evidence[0].fix_diff_digest for finding in findings)


def test_rejected_and_disputed_candidates_are_preserved_without_false_claims(
    tiny_repo: tuple[Path, list[str]], tmp_path: Path
) -> None:
    repo, _ = tiny_repo
    root, candidates = mine_repository(repo.as_uri(), "tasks/*", tmp_path / "state")
    decisions = _decisions(candidates)
    decisions[0] = decisions[0].model_copy(
        update={"accepted": False, "defect_class": "unclassified", "disagreement": True}
    )
    decisions[1] = decisions[1].model_copy(
        update={"defect_class": "unclassified", "direction": "other", "disagreement": True}
    )
    report = publish_collection(
        LibraryStore.for_repo(tmp_path / "consumer"), candidates, decisions, root
    )
    assert report["accepted"] == 2 and report["rejected"] == 1
    assert report["disagreements"] == 2 and report["by_class"]["unclassified"] == 1
    with pytest.raises(ValueError, match="different served model families"):
        build_findings(
            candidates,
            [
                decisions[2].model_copy(
                    update={"models": ["openai-codex/gpt-6.1-sol", "openai/gpt-5"]}
                )
            ],
            root,
        )


def test_digest_mismatch_refuses_publication(
    tiny_repo: tuple[Path, list[str]], tmp_path: Path
) -> None:
    repo, _ = tiny_repo
    root, candidates = mine_repository(repo.as_uri(), "tasks/*", tmp_path / "state")
    path = root / candidates[0].diff_ref
    path.chmod(0o644)
    path.write_text("tampered\n")
    with pytest.raises(ValueError, match="diff digest mismatch"):
        build_findings(candidates, _decisions(candidates), root)


def test_family_hash_and_existing_nondev_override(tmp_path: Path) -> None:
    assignments = tmp_path / "assignments.csv"
    assignments.write_text(
        "key_id,item_id,task,unit,split,expected_verdict,exposures\na,a,protected,x,dev,fail,\nb,b,protected,x,test,fail,\na,c,dev-only,x,dev,fail,\n"
    )
    protected = protected_tasks(assignments)
    assert task_split("repo", "family", "protected", protected) == "cross_test"
    assert task_split("repo", "family", "dev-only", protected) == "DEV"
    assert task_split("repo", "family", "task-v1", {}) == task_split(
        "repo", "family", "task-v2", {}
    )
    sample = [task_split("repo", f"family-{index}", str(index), {}) for index in range(1000)]
    assert 150 < sample.count("heldout") < 250


def test_repo_url_normalization_and_credentials_rejected() -> None:
    assert canonical_repo("git@github.com:Owner/Repo.git") == (
        "https://github.com/Owner/Repo",
        "owner-repo",
    )
    with pytest.raises(ValueError, match="HTTPS"):
        canonical_repo("https://secret@github.com/owner/repo")


def test_hf_probe_pins_metadata_without_claiming_verified_fixes(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from evallab.failure_library.fixmine.hf import probe_hf_history

    class FakeHub:
        def list_repo_commits(self, repo_id: str, *, repo_type: str) -> list[Any]:
            assert repo_id == "example/tasks" and repo_type == "dataset"
            return [
                SimpleNamespace(
                    commit_id="a" * 40, title="Upload parquet", message="upload", created_at=None
                )
            ]

        def list_repo_files(self, repo_id: str, *, repo_type: str, revision: str) -> list[str]:
            assert revision == "a" * 40
            return ["README.md", "data/train-00000.parquet"]

    report = probe_hf_history("example/tasks", tmp_path, api=FakeHub())
    assert report["commit_count"] == 1
    assert report["revision"] == "a" * 40
    assert "parquet" in json.dumps(report).lower()


def test_mine_cli_publishes_and_rebuilds_without_git_or_network(
    tiny_repo: tuple[Path, list[str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from evallab.cli import run_cli
    from evallab.failure_library.commands import mine

    repo, _ = tiny_repo
    consumer = tmp_path / "consumer"
    assignments = consumer / "research/answer-keys/splits/assignments.csv"
    assignments.parent.mkdir(parents=True)
    assignments.write_text("key_id,item_id,task,unit,split,expected_verdict,exposures\n")
    state = tmp_path / "state"
    offline_calls: list[bool] = []
    audit_calls: list[bool] = []

    def labels(candidates: list[Candidate], mine_root: Path, **kwargs: Any) -> list[Consensus]:
        offline_calls.append(kwargs["cache_only"])
        return _decisions(candidates)

    def audit_labels(
        candidates: list[Candidate], decisions: list[Consensus], mine_root: Path, **kwargs: Any
    ) -> dict[str, Any]:
        audit_calls.append(kwargs["cache_only"])
        report = {
            "model": kwargs["model"],
            "requested_sample_size": kwargs["sample_size"],
            "seed": kwargs.get("seed", 20261027),
            "agreement_rate": 1.0,
        }
        (mine_root / "audit.json").write_text(json.dumps(report))
        return report

    monkeypatch.setattr(mine, "audit_candidates", audit_labels)
    monkeypatch.setattr(mine, "classify_candidates", labels)
    code = run_cli(
        [
            "failures",
            "mine",
            "--repo",
            repo.as_uri(),
            "--task-glob",
            "tasks/*",
            "--state-root",
            str(state),
            "--limit",
            "2",
            "--audit",
            "--audit-model",
            "meta/muse-spark-1.3",
        ],
        workspace=consumer,
    )
    assert code == 0
    first = json.loads(capsys.readouterr().out)
    assert first["accepted"] == 2 and first["not_classified"] == 1
    path = consumer / "research/failure-library/findings" / (first["collection"] + ".jsonl")
    original = path.read_bytes()

    def forbid_git(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("offline rebuild must not call Git or network")

    monkeypatch.setattr(mine, "mine_repository", forbid_git)
    assert (
        run_cli(
            [
                "failures",
                "mine",
                "--cache",
                first["cache"],
                "--rebuild",
                "--state-root",
                str(state),
            ],
            workspace=consumer,
        )
        == 0
    )
    second = json.loads(capsys.readouterr().out)
    assert first == second and path.read_bytes() == original
    assert offline_calls == [False, True]
    assert audit_calls == [False, True]


def _fake_omp(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
    model = argv[argv.index("--model") + 1]
    provider, model_id = model.split("/", 1)
    prompt = Path(argv[-1][1:]).read_text()
    payload = json.loads(
        prompt.split("BEGIN_UNTRUSTED_CANDIDATES_JSON\n", 1)[1].split(
            "\nEND_UNTRUSTED_CANDIDATES_JSON", 1
        )[0]
    )
    assert all(
        "split" not in item and "task_id" not in item and "repo_url" not in item for item in payload
    )
    assert "--no-tools" in argv and "--no-rules" in argv
    rows = []
    for index, item in enumerate(payload):
        rejected = index == 1 and provider in {"anthropic", "xai-oauth"}
        rows.append(
            {
                "candidate_id": item["candidate_id"],
                "is_task_fix": "no" if rejected else "yes",
                "defect_class": "TS02" if index == 2 and provider == "anthropic" else "TS01",
                "direction": "wrong_pass",
                "summary": "The visible before/after check repairs the old acceptance rule.",
            }
        )
    text = json.dumps(rows)
    message = {
        "role": "assistant",
        "provider": provider,
        "model": model_id,
        "content": [{"type": "text", "text": text}],
        "stopReason": "stop",
        "resolvedModelIsFallback": False,
    }
    session_dir = Path(argv[argv.index("--session-dir") + 1])
    (session_dir / "fixture.jsonl").write_text(
        json.dumps({"type": "message", "message": message}) + "\n"
    )
    return subprocess.CompletedProcess(
        argv, 0, (json.dumps({"type": "message_end", "message": message}) + "\n").encode(), b""
    )


def test_two_family_consensus_cache_and_blind_third_family_audit(
    tiny_repo: tuple[Path, list[str]], tmp_path: Path
) -> None:
    from evallab.failure_library.fixmine.classify import audit_candidates, classify_candidates

    repo, _ = tiny_repo
    root, candidates = mine_repository(repo.as_uri(), "tasks/*", tmp_path / "state")
    models = ["openai-codex/gpt-6.1-sol", "anthropic/claude-opus-5-5"]
    decisions = classify_candidates(candidates, root, models=models, runner=_fake_omp)
    assert [item.accepted for item in decisions] == [True, False, True]
    assert [item.defect_class for item in decisions] == ["TS01", "unclassified", "unclassified"]
    assert [item.disagreement for item in decisions] == [False, True, True]
    assert all(ref.startswith("state://fixmine/") for item in decisions for ref in item.label_refs)

    def offline(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("cached model calls must be offline")

    assert (
        classify_candidates(candidates, root, models=models, runner=offline, cache_only=True)
        == decisions
    )
    audit = audit_candidates(
        candidates, decisions, root, model="xai-oauth/grok-4.7", sample_size=2, runner=_fake_omp
    )
    assert audit["sample_size"] == 2 and audit["agreement_rate"] == 0.5
    assert len(audit["disagreement_ids"]) == 1
    assert (
        audit_candidates(
            candidates,
            decisions,
            root,
            model="xai-oauth/grok-4.7",
            sample_size=2,
            runner=offline,
            cache_only=True,
        )
        == audit
    )
    with pytest.raises(ValueError, match="third family"):
        audit_candidates(candidates, decisions, root, model=models[0], runner=offline)


def test_classification_rejects_same_family_and_missing_or_tampered_cache(
    tiny_repo: tuple[Path, list[str]], tmp_path: Path
) -> None:
    from evallab.failure_library.fixmine.classify import CacheError, classify_candidates

    repo, _ = tiny_repo
    root, candidates = mine_repository(repo.as_uri(), "tasks/*", tmp_path / "state")
    models = ["openai-codex/gpt-6.1-sol", "anthropic/claude-opus-5-5"]

    def offline(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not call model")

    with pytest.raises(ValueError, match="distinct"):
        classify_candidates(candidates, root, models=[models[0], "openai/gpt-5"], runner=offline)
    with pytest.raises(CacheError, match="missing"):
        classify_candidates(candidates, root, models=models, runner=offline, cache_only=True)
    classify_candidates(candidates, root, models=models, runner=_fake_omp)
    session = next((root / "classifiers").glob("*/*/sessions/*.jsonl"))
    session.write_text(session.read_text() + '{"type":"new_event"}\n')
    with pytest.raises(CacheError, match="session hashes"):
        classify_candidates(candidates, root, models=models, runner=offline, cache_only=True)


@pytest.mark.parametrize("fallback,served", [(True, "gpt-6.1-sol"), (False, "gpt-5")])
def test_attestation_rejects_fallback_and_wrong_served_model(
    tmp_path: Path, fallback: bool, served: str
) -> None:
    from evallab.failure_library.fixmine.classify import AttestationError, attest_sessions

    message = {
        "role": "assistant",
        "provider": "openai-codex",
        "model": served,
        "content": [{"type": "text", "text": "[]"}],
        "resolvedModelIsFallback": fallback,
    }
    path = tmp_path / "session.jsonl"
    path.write_text(json.dumps({"type": "message", "message": message}) + "\n")
    with pytest.raises(AttestationError) as exc:
        attest_sessions(tmp_path, "openai-codex/gpt-6.1-sol")
    assert exc.value.attestation["status"] == "rejected"
    assert exc.value.attestation["sessions"]["session.jsonl"]
