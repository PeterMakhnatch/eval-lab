"""Synthetic DEV package access, omission accounting and answer-material boundaries."""

from __future__ import annotations

import hashlib
import json
import tracemalloc
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from evallab.production.cloud_trace_db import CloudTraceDatabase, CloudTraceRetriever
from evallab.production.corpus_queue import CorpusItem, CorpusRegistry
from evallab.production.qa_data import UNAVAILABLE, Budget, QaContext, QaDataError, estimate_tokens
from evallab.production.qa_data_task import task
from evallab.registry import task_directory_digest


class SyntheticRegistry(CorpusRegistry):
    """Real registry admission with inert local package identities, not repository keys."""

    def __init__(self, root: Path, package: Path) -> None:
        authority = b"key_id,item_id,split\nsynthetic-authority,source,dev\n"
        self.split_assignments_digest = "sha256:" + hashlib.sha256(authority).hexdigest()
        self._assignment_keys = frozenset({"synthetic-authority"})
        self._assignments = {("synthetic-authority", "source"): "dev"}
        self._assignment_names = {}
        self._assigned_test_ids = set()
        self._assigned_test_names = {"locked-task"}
        self._sealed_names = {"sealed-task"}
        self._sealed_digests = set()
        self._sealed_ids = set()
        self._index_names = {}
        self._items = {}
        self._external = set()
        self.register_external(CorpusItem(
            dataset_id="synthetic-corpus", item_id="dev-task", split="dev", task_id="dev-task",
            task_digest=task_directory_digest(package), source_path=root / "registry.jsonl",
            metadata={"task_package": package.relative_to(root).as_posix()},
            source_record_sha256="sha256:" + "0" * 64,
        ))


@dataclass
class Case:
    database: CloudTraceDatabase
    root: Path
    package: Path

    def context(self, limit: int = 6000, *, repo_root: Path | None = None) -> QaContext:
        registry = SyntheticRegistry(self.root, self.package)
        return QaContext(
            database=self.database,
            retriever=CloudTraceRetriever(self.database, registry=registry),
            repo_root=repo_root or self.root, state_dir=self.root / "state", budget=Budget(limit),
        )


@pytest.fixture
def case(tmp_path: Path) -> Iterator[Case]:
    root = tmp_path / "checkout"
    package = root / "derived/task"
    contents = {
        "task.toml": 'version = "1.0"\n[verifier]\ntimeout_sec = 30\n',
        "environment/Dockerfile": "FROM python:3.12-slim\nCOPY setup.sh /setup.sh\n",
        "environment/nested/Dockerfile.extra": "FROM alpine:3.20\n",
        "environment/setup.sh": "echo environment\n",
        "tests/test.sh": "#!/bin/sh\npython -m pytest /tests\n",
        "tests/test_command.sh": "pytest tests/test_api.py\n",
        "tests/test_api.py": "def test_sum():\n    assert 1 + 1 == 2\n",
        "tests/test.patch": "diff --git a/tests/test_api.py b/tests/test_api.py\n+assert True\n",
        "grader.py": "def grade():\n    return True\n",
        "README.md": "Public package documentation.\n",
        "instruction.md": "Not the database instruction.\n",
        "solution/solve.sh": "PRIVATE-SOLUTION-BYTES\n",
        "tests/golden-files/result.txt": "PRIVATE-GOLDEN-BYTES\n",
        "tests/fixtures/example.json": "PRIVATE-FIXTURE-BYTES\n",
        "tests/answer_key.json": "PRIVATE-ANSWER-KEY-BYTES\n",
        "reference.patch": "PRIVATE-REFERENCE-PATCH-BYTES\n",
    }
    for name, text in contents.items():
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    with CloudTraceDatabase(tmp_path / "tasks.sqlite", sqlite_fts=False) as database:
        for task_id, split in (
            ("dev-task", "dev"), ("test-task", "test"), ("held-task", "heldout"),
            ("locked-task", "dev"), ("sealed-task", "dev"),
        ):
            database.upsert_task({
                "task_id": task_id, "dataset_id": "synthetic-corpus", "split": split,
                "instruction_text": "Use the database instruction, not instruction.md.",
                "original_grader_digest": "sha256:" + "1" * 64,
                "repaired_grader_digest": "sha256:" + "2" * 64,
            })
        database.upsert_task({
            "task_id": "goodfire-task", "dataset_id": "goodfire-reward-hack-data", "split": "DEV",
            "instruction_text": "Public instruction without a runnable package.",
        })
        yield Case(database, root, package)


def test_missing_test_held_sealed_and_cross_key_locked_refuse_identically(case: Case) -> None:
    messages = []
    for task_id in ("missing", "test-task", "held-task", "locked-task", "sealed-task"):
        with pytest.raises(QaDataError) as error:
            task(case.context(), task_id, files=("../outside",))
        messages.append(str(error.value))
    assert messages == [UNAVAILABLE] * 5


def test_bundles_database_instruction_and_whole_files_in_priority_order(case: Case) -> None:
    result = task(case.context(), "dev-task", files=("README.md", "environment/setup.sh"))
    assert result["task_id"] == "dev-task"
    assert result["dataset"] == "synthetic-corpus"
    assert result["instruction"] == "Use the database instruction, not instruction.md."
    assert result["instruction_chars"] == len(result["instruction"])
    assert result["instruction_truncated"] is False
    assert result["original_grader_digest"] == "sha256:" + "1" * 64
    assert result["repaired_grader_digest"] == "sha256:" + "2" * 64
    package = result["package"]
    assert [item["path"] for item in package["contents"]] == [
        "environment/Dockerfile", "environment/nested/Dockerfile.extra",
        "tests/test.sh", "tests/test_command.sh", "grader.py", "tests/test.patch",
        "tests/test_api.py", "task.toml", "README.md", "environment/setup.sh",
    ]
    for item in package["contents"]:
        assert item["text"] == (case.package / item["path"]).read_text(encoding="utf-8")
    inventory = {item["path"]: item["bytes"] for item in package["files"]}
    assert inventory["environment/Dockerfile"] == (case.package / "environment/Dockerfile").stat().st_size
    assert "environment/setup.sh" in inventory
    assert "instruction.md" not in inventory
    assert result["omitted"] == 0
    assert result["truncated"] is False


def test_answer_material_is_names_only_and_dev_test_patch_is_available(case: Case) -> None:
    result = task(case.context(), "dev-task")
    package = result["package"]
    assert package["withheld"] == [
        "reference.patch", "solution/", "tests/answer_key.json", "tests/fixtures/", "tests/golden-files/",
    ]
    assert all(isinstance(name, str) for name in package["withheld"])
    assert "tests/test.patch" in {item["path"] for item in package["contents"]}
    assert "PRIVATE-" not in json.dumps(result)
    assert all(not item["path"].startswith("solution/") for item in package["files"])


@pytest.mark.parametrize("kind", ["parent", "absolute", "file-link", "directory-link"])
def test_requested_paths_cannot_escape_or_echo_content(case: Case, tmp_path: Path, kind: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("OUTSIDE-SUPER-SECRET", encoding="utf-8")
    if kind == "parent":
        name = "../outside/secret.txt"
    elif kind == "absolute":
        name = str(secret)
    elif kind == "file-link":
        (case.package / "tests/escape.txt").symlink_to(secret)
        name = "tests/escape.txt"
    else:
        (case.package / "tests/escape-dir").symlink_to(outside, target_is_directory=True)
        name = "tests/escape-dir/secret.txt"
    with pytest.raises(QaDataError) as error:
        task(case.context(), "dev-task", files=(name,))
    assert str(error.value) == "file not available: expected an allowed relative task-package file"
    assert "OUTSIDE-SUPER-SECRET" not in str(error.value)
    assert name not in str(error.value)


@pytest.mark.parametrize("name", [
    "solution/solve.sh", "tests/answer_key.json", "tests/golden-files/result.txt", "reference.patch",
    "tests/innocent.txt",
])
def test_explicit_files_do_not_bypass_answer_material_withholding(case: Case, name: str) -> None:
    (case.package / "tests/innocent.txt").symlink_to(case.package / "solution/solve.sh")
    with pytest.raises(QaDataError, match="^file not available:"):
        task(case.context(), "dev-task", files=(name,))


def test_requested_symlink_can_target_an_allowed_file_inside_the_package(case: Case) -> None:
    (case.package / "public-alias.txt").symlink_to(case.package / "README.md")
    result = task(case.context(), "dev-task", files=("public-alias.txt",))
    contents = {item["path"]: item["text"] for item in result["package"]["contents"]}
    assert contents["public-alias.txt"] == "Public package documentation.\n"
    assert "public-alias.txt" not in result["package"]["withheld"]


@pytest.mark.parametrize("limit", [180, 300, 500, 800])
def test_budget_includes_json_escaping_framing_and_explicit_omission_counts(case: Case, limit: int) -> None:
    instruction = '\\"\n\t\x01😀' * 1000
    case.database.upsert_task({
        "task_id": "dev-task", "dataset_id": "synthetic-corpus", "split": "dev",
        "instruction_text": instruction,
    })
    ctx = case.context(limit)
    result = task(ctx, "dev-task")
    assert estimate_tokens(result) <= limit
    assert ctx.budget.used <= limit
    assert instruction.startswith(result["instruction"])
    assert result["instruction_chars"] == len(instruction)
    assert result["instruction_truncated"] is True
    assert result["truncated"] is True
    assert result["omitted"] == sum(result["package"]["omitted"].values()) + 1
    assert result["omitted"] == ctx.budget.omitted
    assert result["package"]["omitted"]["contents"] > 0
    assert result["package"]["omitted"]["files"] > 0


def test_large_files_are_omitted_whole_without_blocking_smaller_later_files(case: Case) -> None:
    (case.package / "tests/test_api.py").write_text("large file\n" * 20_000, encoding="utf-8")
    ctx = case.context(900)
    result = task(ctx, "dev-task")
    contents = {item["path"]: item["text"] for item in result["package"]["contents"]}
    assert "environment/Dockerfile" in contents
    assert "task.toml" in contents
    assert "tests/test_api.py" not in contents
    assert result["package"]["omitted"]["contents"] == 1
    assert result["omitted"] == ctx.budget.omitted == 1
    assert estimate_tokens(result) <= ctx.budget.limit


def test_instruction_only_tasks_explicitly_report_no_local_package(case: Case) -> None:
    result = task(case.context(), "goodfire-task")
    assert result["instruction"] == "Public instruction without a runnable package."
    assert result["package"] is None
    assert result["reason"] == "no_local_package"
    assert result["original_grader_digest"] is None
    assert result["repaired_grader_digest"] is None


def test_digest_mismatch_does_not_bundle_an_unbound_package(case: Case) -> None:
    ctx = case.context()
    (case.package / "tests/test.sh").write_text("changed after registration\n", encoding="utf-8")
    result = task(ctx, "dev-task")
    assert result["package"] is None
    assert result["reason"] == "no_local_package"


def test_relative_registry_locator_falls_back_to_primary_checkout(case: Case, tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    gitdir = case.root / ".git/worktrees/synthetic"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    result = task(case.context(repo_root=worktree), "dev-task")
    assert result["package"] is not None
    assert result["package"]["contents"][0]["path"] == "environment/Dockerfile"


def test_impossibly_small_budget_is_an_explicit_refusal(case: Case) -> None:
    with pytest.raises(QaDataError, match="^budget too small for task metadata$"):
        task(case.context(5), "dev-task")


@pytest.mark.parametrize(("name", "withheld_name"), [
    ("tests/AnswerKey.json", "tests/AnswerKey.json"),
    ("tests/answer.key.json", "tests/answer.key.json"),
    ("tests/answers-v2.json", "tests/answers-v2.json"),
    ("tests/answer_key_2.json", "tests/answer_key_2.json"),
    ("tests/ANSWERKEY-final.json", "tests/ANSWERKEY-final.json"),
    ("tests/test_answer_key.json", "tests/test_answer_key.json"),
    ("check-answer-key.txt", "check-answer-key.txt"),
    ("tests/test_AnswerKey2-final.json", "tests/test_AnswerKey2-final.json"),
    ("tests/test_solution_v2.py", "tests/test_solution_v2.py"),
    ("tests/test_oracle-v2.py", "tests/test_oracle-v2.py"),
    ("tests/GoldenData-final/private.txt", "tests/GoldenData-final/"),
    ("SolutionV2/solve.sh", "SolutionV2/"),
    ("tests/foo.solution/private.txt", "tests/foo.solution/"),
    ("tests/foo.answer", "tests/foo.answer"),
    ("tests/bar.answer_key", "tests/bar.answer_key"),
    ("tests/nested.Solution/private.txt", "tests/nested.Solution/"),
    ("tests/results.golden/private.txt", "tests/results.golden/"),
    ("tests/ground_truth.json", "tests/ground_truth.json"),
    ("solve.sh", "solve.sh"),
])
def test_normalized_answer_names_are_withheld_from_inventory_and_explicit_requests(
    case: Case, name: str, withheld_name: str,
) -> None:
    path = case.package / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("PRIVATE-NORMALIZED-ANSWER-MATERIAL", encoding="utf-8")
    result = task(case.context(), "dev-task")
    assert withheld_name in result["package"]["withheld"]
    assert name not in {item["path"] for item in result["package"]["files"]}
    assert name not in {item["path"] for item in result["package"]["contents"]}
    assert "PRIVATE-NORMALIZED-ANSWER-MATERIAL" not in json.dumps(result)
    with pytest.raises(QaDataError, match="^file not available:"):
        task(case.context(), "dev-task", files=(name,))


@pytest.mark.parametrize("name", ["solution/large.bin", "environment/large.bin"])
def test_large_package_files_are_hashed_with_bounded_memory_before_budgeting(
    case: Case, name: str,
) -> None:
    (case.package / name).write_bytes(b"\x00" * (8 * 1024 * 1024))
    # The fixture registers the original digest before measuring the production
    # call. Its digest must still match, including the full 8 MiB withheld file.
    ctx = case.context(180)
    tracemalloc.start()
    try:
        result = task(ctx, "dev-task")
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result["package"] is not None
    assert estimate_tokens(result) <= 180
    assert peak < 2 * 1024 * 1024


@pytest.mark.parametrize("directory_link", [False, True])
def test_package_digest_never_opens_external_link_targets(
    case: Case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory_link: bool,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("PRIVATE-EXTERNAL-BYTES", encoding="utf-8")
    link = case.package / "tests/escape"
    link.symlink_to(outside if directory_link else secret, target_is_directory=directory_link)
    ctx = case.context()
    original_open = Path.open

    def guarded_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        assert not path.resolve().is_relative_to(outside), "outside bytes were opened"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    result = task(ctx, "dev-task")
    if directory_link:
        # Linked directories are not eligible in the registry's Path.rglob
        # digest, but are still withheld from the default package inventory.
        assert result["package"] is not None
        assert "tests/escape/" in result["package"]["withheld"]
    else:
        assert result["package"] is None
        assert result["reason"] == "no_local_package"
    assert "PRIVATE-EXTERNAL-BYTES" not in json.dumps(result)


def test_streamed_digest_preserves_registry_order_and_file_exclusions(case: Case) -> None:
    for name in ("a/nested.txt", "a.z", "a0/first.txt", "z/.DS_Store", "z/ignored.tmp"):
        path = case.package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name, encoding="utf-8")
    result = task(case.context(), "dev-task")
    assert result["package"] is not None


@pytest.mark.parametrize(("task_id", "metadata"), [
    ("dev-task@v2", {}),
    ("other/dev-task", {}),
    ("dev-task@sha256:" + "f" * 64, {}),
    ("dev-task@v2", {"item_id": "dev-task"}),
])
def test_other_namespace_or_version_does_not_inherit_a_bare_name_package(
    case: Case, task_id: str, metadata: dict[str, Any],
) -> None:
    case.database.upsert_task({
        "task_id": task_id, "dataset_id": "synthetic-corpus", "split": "dev",
        "instruction_text": "This exact task identity has no registered package.",
        "metadata_json": metadata,
    })
    result = task(case.context(), task_id)
    assert result["package"] is None
    assert result["reason"] == "no_local_package"


@pytest.mark.parametrize("explicit_item_id", [False, True])
def test_exact_registered_version_uses_its_own_package(case: Case, explicit_item_id: bool) -> None:
    versioned = case.root / "derived/task-v2"
    (versioned / "environment").mkdir(parents=True)
    (versioned / "task.toml").write_text('version = "2.0"\n', encoding="utf-8")
    (versioned / "environment/Dockerfile").write_text("FROM alpine:3.20\n", encoding="utf-8")
    ctx = case.context()
    ctx.retriever.registry.register_external(CorpusItem(
        dataset_id="synthetic-corpus", item_id="package-v2", split="dev", task_id="dev-task@v2",
        task_digest=task_directory_digest(versioned), source_path=case.root / "registry.jsonl",
        metadata={"task_package": "derived/task-v2"}, source_record_sha256="sha256:" + "3" * 64,
    ))
    case.database.upsert_task({
        "task_id": "dev-task@v2", "dataset_id": "synthetic-corpus", "split": "dev",
        "instruction_text": "Version two.",
        "metadata_json": {"item_id": "package-v2"} if explicit_item_id else {},
    })
    result = task(ctx, "dev-task@v2")
    assert result["package"] is not None
    contents = {item["path"]: item["text"] for item in result["package"]["contents"]}
    assert contents["task.toml"] == 'version = "2.0"\n'


@pytest.mark.parametrize("name", [
    "tests/test_expected_output.json", "tests/expectedOutputV2.json", "tests/expected.json",
    "tests/foo.expected_output",
])
def test_expected_values_in_dev_tests_are_available_automatically_and_explicitly(
    case: Case, name: str,
) -> None:
    text = '{"expected": [1, 2, 3]}\n'
    (case.package / name).write_text(text, encoding="utf-8")
    for requested in ((), (name,)):
        result = task(case.context(), "dev-task", files=requested)
        contents = {item["path"]: item["text"] for item in result["package"]["contents"]}
        assert contents[name] == text
        assert name in {item["path"] for item in result["package"]["files"]}
        assert name not in result["package"]["withheld"]


@pytest.mark.parametrize(("name", "missing"), [
    ("tests/ignored.tmp", True), ("tests/ignored.tmp", False),
    ("tests/dangling.txt", True), ("tests/.DS_Store", False),
])
def test_digest_omits_ineligible_symlinks_before_strict_resolution(
    case: Case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, missing: bool,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.txt"
    if not missing:
        target.write_text("PRIVATE-INELIGIBLE-LINK-TARGET", encoding="utf-8")
    (case.package / name).symlink_to(target)
    ctx = case.context()
    original_open = Path.open

    def guarded_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        assert not path.resolve().is_relative_to(outside), "outside bytes were opened"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    result = task(ctx, "dev-task")
    assert result["package"] is not None
    assert name in result["package"]["withheld"]
    assert "PRIVATE-INELIGIBLE-LINK-TARGET" not in json.dumps(result)
    with pytest.raises(QaDataError, match="^file not available:"):
        task(case.context(), "dev-task", files=(name,))


def test_root_hint_package_symlink_resolves_before_hashing_and_file_containment(case: Case) -> None:
    alias = case.package.parent / "package-alias"
    alias.symlink_to(case.package, target_is_directory=True)
    ctx = case.context()
    registry = ctx.retriever.registry
    item = registry.get("synthetic-corpus", "dev-task")
    registry._items[("synthetic-corpus", "dev-task")] = replace(
        item, metadata={"package": {"root_hint": str(alias.parent), "name": alias.name}},
    )
    result = task(ctx, "dev-task", files=("README.md",))
    assert result["package"] is not None
    contents = {item["path"]: item["text"] for item in result["package"]["contents"]}
    assert contents["README.md"] == "Public package documentation.\n"
    assert contents["environment/Dockerfile"].startswith("FROM python:3.12-slim\n")


@pytest.mark.parametrize("stale_has_readme", [False, True])
def test_requested_file_is_checked_only_after_a_digest_matching_locator_is_selected(
    case: Case, stale_has_readme: bool,
) -> None:
    stale = case.root / "derived/stale"
    stale.mkdir()
    (stale / "task.toml").write_text('version = "0.0"\n', encoding="utf-8")
    if stale_has_readme:
        (stale / "README.md").write_text("STALE-UNMATCHED-PACKAGE-CONTENT", encoding="utf-8")
    ctx = case.context()
    registry = ctx.retriever.registry
    item = registry.get("synthetic-corpus", "dev-task")
    registry._items[("synthetic-corpus", "dev-task")] = replace(item, metadata={
        "materialization": {"resolved_locator": str(stale)},
        "task_package": case.package.relative_to(case.root).as_posix(),
    })
    result = task(ctx, "dev-task", files=("README.md",))
    assert result["package"] is not None
    contents = {item["path"]: item["text"] for item in result["package"]["contents"]}
    assert contents["README.md"] == "Public package documentation.\n"
    assert "STALE-UNMATCHED-PACKAGE-CONTENT" not in json.dumps(result)


@pytest.mark.parametrize("stale_exists", [False, True])
def test_duplicate_same_digest_registry_items_try_each_items_own_locators(
    case: Case, stale_exists: bool,
) -> None:
    stale = case.root / "derived/stale-finding"
    if stale_exists:
        stale.mkdir()
        (stale / "task.toml").write_text('version = "0.0"\n', encoding="utf-8")
    ctx = case.context()
    registry = ctx.retriever.registry
    item = registry.get("synthetic-corpus", "dev-task")
    del registry._items[("synthetic-corpus", "dev-task")]
    registry.register_external(replace(
        item, item_id="finding-one", metadata={"task_package": str(stale)},
    ))
    registry.register_external(replace(item, item_id="finding-two"))
    # Neither the row's absent item_id nor task_id directly selects a registry
    # record. Both finding records bind this exact task and the same digest.
    result = task(ctx, "dev-task", files=("README.md",))
    assert result["package"] is not None
    contents = {item["path"]: item["text"] for item in result["package"]["contents"]}
    assert contents["README.md"] == "Public package documentation.\n"
    assert contents["environment/Dockerfile"].startswith("FROM python:3.12-slim\n")


@pytest.mark.parametrize("selection", ["metadata", "task_id"])
@pytest.mark.parametrize("stale_kind", ["missing", "mismatched", "loop"])
def test_stale_direct_registry_selection_tries_same_digest_siblings(
    case: Case, selection: str, stale_kind: str,
) -> None:
    stale = case.root / "derived/direct-stale"
    if stale_kind == "mismatched":
        stale.mkdir()
        (stale / "task.toml").write_text('version = "0.0"\n', encoding="utf-8")
    elif stale_kind == "loop":
        stale.symlink_to(stale)
    ctx = case.context()
    registry = ctx.retriever.registry
    item = registry.get("synthetic-corpus", "dev-task")
    registry._items.clear()
    selected_id = "selected-record" if selection == "metadata" else "dev-task"
    registry.register_external(replace(
        item, item_id=selected_id, metadata={"task_package": str(stale)},
    ))
    registry.register_external(replace(item, item_id="same-digest-sibling"))
    case.database.upsert_task({
        "task_id": "dev-task", "dataset_id": "synthetic-corpus", "split": "dev",
        "instruction_text": "Use only a digest-bound package.",
        "metadata_json": {"item_id": selected_id} if selection == "metadata" else {},
    })
    result = task(ctx, "dev-task", files=("README.md",))
    assert result["package"] is not None
    contents = {item["path"]: item["text"] for item in result["package"]["contents"]}
    assert contents["README.md"] == "Public package documentation.\n"


@pytest.mark.parametrize("selection", ["metadata", "task_id"])
def test_stale_direct_registry_selection_never_switches_to_a_different_digest(
    case: Case, selection: str,
) -> None:
    other = case.root / "derived/other-digest"
    other.mkdir()
    (other / "task.toml").write_text('version = "9.0"\n', encoding="utf-8")
    (other / "README.md").write_text("UNSELECTED-DIGEST-PACKAGE", encoding="utf-8")
    ctx = case.context()
    registry = ctx.retriever.registry
    item = registry.get("synthetic-corpus", "dev-task")
    registry._items.clear()
    selected_id = "selected-record" if selection == "metadata" else "dev-task"
    registry.register_external(replace(
        item, item_id=selected_id, metadata={"task_package": "derived/missing-selected"},
    ))
    registry.register_external(replace(
        item, item_id="different-digest-sibling", task_digest=task_directory_digest(other),
        metadata={"task_package": str(other)},
    ))
    case.database.upsert_task({
        "task_id": "dev-task", "dataset_id": "synthetic-corpus", "split": "dev",
        "instruction_text": "Use only the explicitly selected digest.",
        "metadata_json": {"item_id": selected_id} if selection == "metadata" else {},
    })
    result = task(ctx, "dev-task")
    assert result["package"] is None
    assert result["reason"] == "no_local_package"
    assert "UNSELECTED-DIGEST-PACKAGE" not in json.dumps(result)


@pytest.mark.parametrize("source", ["materialization", "task_package", "package_path", "root_hint"])
@pytest.mark.parametrize("failure", ["loop", "nul"])
def test_locator_resolution_failure_does_not_discard_later_sources_or_siblings(
    case: Case, source: str, failure: str,
) -> None:
    loop = case.root / "derived/locator-loop"
    loop.symlink_to(loop)
    invalid = str(loop) if failure == "loop" else "invalid\x00locator"
    if source == "materialization":
        metadata = {
            "materialization": {"resolved_locator": invalid},
            "task_package": str(case.package),
        }
    elif source == "task_package":
        metadata = {"task_package": invalid, "package": {"path": str(case.package)}}
    elif source == "package_path":
        metadata = {"package": {
            "path": invalid, "root_hint": str(case.package.parent), "name": case.package.name,
        }}
    else:
        metadata = {"package": {"root_hint": invalid, "name": case.package.name}}
    ctx = case.context()
    registry = ctx.retriever.registry
    item = registry.get("synthetic-corpus", "dev-task")
    registry._items[("synthetic-corpus", "dev-task")] = replace(item, metadata=metadata)
    if source == "root_hint":
        registry.register_external(replace(item, item_id="valid-locator-sibling"))
    result = task(ctx, "dev-task", files=("README.md",))
    assert result["package"] is not None
    contents = {item["path"]: item["text"] for item in result["package"]["contents"]}
    assert contents["README.md"] == "Public package documentation.\n"
