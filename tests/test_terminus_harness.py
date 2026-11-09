"""Focused artifact-validation tests for pinned Terminus harness trees."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from evallab import terminus_harness
from evallab.evidence_store import evidence_tree_digest
from evallab.terminus_harness import (
    _check_fixed_paths,
    _publish_tree,
    load_harness_tree,
    retain_harness_tree_evidence,
    stage_harness_tree,
)


def _write_tree(
    root: Path,
    *,
    config: Any = ...,
    rules: str | None = "Be concise.\n",
    skills: dict[str, str] | None = None,
) -> Path:
    if config is ...:
        config = {"max_turns": 5, "temperature": 0.2}
    if config is not None:
        config_path = root / "terminus" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config), encoding="utf-8")
    if rules is not None:
        rules_path = root / "terminus" / "AGENTS.md"
        rules_path.parent.mkdir(parents=True, exist_ok=True)
        rules_path.write_text(rules, encoding="utf-8")
    for relative, body in (skills or {}).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


def _valid_skills() -> dict[str, str]:
    return {
        "terminus/skills/grep/SKILL.md": "# grep\nsearch\n",
        "terminus-commands/summarize/SKILL.md": "# summarize\nsummarize\n",
    }


def test_content_pin_moves_with_settings_rules_skills(tmp_path: Path) -> None:
    base = _write_tree(tmp_path / "base", skills=_valid_skills())
    pinned = evidence_tree_digest(base)

    altered_config = _write_tree(
        tmp_path / "config", config={"max_turns": 6, "temperature": 0.2}, skills=_valid_skills()
    )
    assert evidence_tree_digest(altered_config) != pinned
    assert load_harness_tree(altered_config).config["max_turns"] == 6

    altered_rules = _write_tree(
        tmp_path / "rules", rules="Be verbose.\n", skills=_valid_skills()
    )
    assert evidence_tree_digest(altered_rules) != pinned

    altered_skill = _write_tree(
        tmp_path / "skill",
        skills={
            "terminus/skills/grep/SKILL.md": "# grep\nchanged\n",
            "terminus-commands/summarize/SKILL.md": "# summarize\nsummarize\n",
        },
    )
    assert evidence_tree_digest(altered_skill) != pinned


def test_digest_stable_across_filesystem_order(tmp_path: Path) -> None:
    first = tmp_path / "first"
    _write_tree(first, skills=_valid_skills())
    (first / "terminus-commands" / "extra.txt").write_text("extra", encoding="utf-8")

    second = tmp_path / "second"
    second.mkdir()
    # Create the same bytes in reverse order.
    (second / "terminus-commands" / "extra.txt").parent.mkdir(parents=True, exist_ok=True)
    (second / "terminus-commands" / "extra.txt").write_text("extra", encoding="utf-8")
    _write_tree(second, skills=_valid_skills())

    assert evidence_tree_digest(first) == evidence_tree_digest(second)
    assert load_harness_tree(first).sha256 == load_harness_tree(second).sha256


def test_blank_rules_yield_no_instruction_and_empty_roots_skipped(tmp_path: Path) -> None:
    tree_dir = _write_tree(tmp_path / "tree", rules="  \n", skills=None)
    (tree_dir / "terminus" / "skills").mkdir(parents=True, exist_ok=True)
    (tree_dir / "terminus" / "skills" / "notes.txt").write_text("no skill here", encoding="utf-8")
    tree = load_harness_tree(tree_dir)
    assert tree.rules_path is None
    assert tree.skill_roots == ()

    full = _write_tree(tmp_path / "full", skills=_valid_skills())
    tree = load_harness_tree(full)
    assert tree.rules_path == full.resolve() / "terminus" / "AGENTS.md"
    assert [path.name for path in tree.skill_roots] == ["skills", "terminus-commands"]


def test_missing_config_is_stock_agent_with_native_types(tmp_path: Path) -> None:
    tree_dir = _write_tree(tmp_path / "tree", config=None, skills=_valid_skills())
    tree = load_harness_tree(tree_dir)
    assert tree.config == {}
    assert load_harness_tree(_write_tree(tmp_path / "t2", skills=_valid_skills())).config[
        "max_turns"
    ] == 5
    staged, metadata = stage_harness_tree(
        _write_tree(tmp_path / "t3", skills=_valid_skills()),
        evidence_tree_digest(tmp_path / "t3"),
        staging_root=tmp_path / "staging",
    )
    assert isinstance(metadata["config"]["temperature"], float)
    assert metadata["rules_path"] == "terminus/AGENTS.md"
    assert metadata["skill_roots"] == ["terminus/skills", "terminus-commands"]
    assert metadata["schema_version"] == 1
    assert metadata["artifact_path"] == "harness-tree"
    assert staged.is_dir()


def test_unknown_knob_refuses(tmp_path: Path) -> None:
    tree_dir = _write_tree(tmp_path / "tree", config={"max_turns": 5, "bogus_knob": 1})
    with pytest.raises(ValueError, match="not Terminus 2 arguments"):
        load_harness_tree(tree_dir)


def test_model_transport_binding_refuses_top_level_and_nested(tmp_path: Path) -> None:
    for knob in ("model_name", "api_base", "llm_kwargs"):
        tree_dir = _write_tree(tmp_path / f"b-{knob}", config={"max_turns": 5, knob: "x"})
        with pytest.raises(ValueError, match="binding"):
            load_harness_tree(tree_dir)
    nested = _write_tree(
        tmp_path / "nested",
        config={"max_turns": 5, "llm_call_kwargs": {"api_base": "http://x"}},
    )
    with pytest.raises(ValueError, match="binding"):
        load_harness_tree(nested)
    nested_model = _write_tree(
        tmp_path / "nested-model",
        config={"max_turns": 5, "llm_call_kwargs": {"model": "other"}},
    )
    with pytest.raises(ValueError, match="binding"):
        load_harness_tree(nested_model)


def test_code_extension_and_runtime_paths_refuse(tmp_path: Path) -> None:
    tree_dir = _write_tree(tmp_path / "tree", skills=_valid_skills())
    extension = tree_dir / "terminus" / "context" / "agent.py"
    extension.parent.mkdir(parents=True, exist_ok=True)
    extension.write_text("class Agent: pass\n", encoding="utf-8")
    with pytest.raises(ValueError, match="code_extension"):
        load_harness_tree(tree_dir)

    runtime = _write_tree(tmp_path / "runtime", skills=_valid_skills())
    session = runtime / "terminus" / "sessions" / "s.json"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="reserved runtime path"):
        load_harness_tree(runtime)


def test_symlink_refuses_and_path_escape_refuses(tmp_path: Path) -> None:
    tree_dir = _write_tree(tmp_path / "tree", skills=_valid_skills())
    (tree_dir / "terminus" / "link.md").symlink_to(tree_dir / "terminus" / "AGENTS.md")
    with pytest.raises(ValueError, match="symlink"):
        load_harness_tree(tree_dir)

    outside = _write_tree(tmp_path / "outside", skills=_valid_skills())
    with pytest.raises(ValueError, match="escapes repository root"):
        load_harness_tree(outside, repo_root=tmp_path / "elsewhere")


def test_argument_boundaries_refuse(tmp_path: Path) -> None:
    for bad_turns in (0, -2, "5", True):
        tree_dir = _write_tree(tmp_path / f"t-{bad_turns}", config={"max_turns": bad_turns})
        with pytest.raises(ValueError, match="max_turns"):
            load_harness_tree(tree_dir)
    bad_nested = _write_tree(
        tmp_path / "bad-nested", config={"max_turns": 5, "llm_call_kwargs": ["x"]}
    )
    with pytest.raises(ValueError, match="llm_call_kwargs"):
        load_harness_tree(bad_nested)
    bad_shape = _write_tree(tmp_path / "bad-shape", config=["max_turns"])
    with pytest.raises(ValueError, match="must be an object"):
        load_harness_tree(bad_shape)


def test_pin_mismatch_refuses_before_execution(tmp_path: Path) -> None:
    tree_dir = _write_tree(tmp_path / "tree", skills=_valid_skills())
    with pytest.raises(ValueError, match="digest mismatch"):
        load_harness_tree(tree_dir, "sha256:" + "0" * 64)
    with pytest.raises(ValueError, match="digest mismatch"):
        stage_harness_tree(
            tree_dir, "sha256:" + "0" * 64, staging_root=tmp_path / "staging"
        )


def test_stage_retain_roundtrip_with_tamper_refusal(tmp_path: Path) -> None:
    tree_dir = _write_tree(tmp_path / "tree", skills=_valid_skills())
    pinned = evidence_tree_digest(tree_dir)
    staged, metadata = stage_harness_tree(
        tree_dir, pinned, staging_root=tmp_path / "staging"
    )
    assert metadata["sha256"] == pinned

    retained = retain_harness_tree_evidence(tmp_path / "job", staged, metadata)
    assert retained == tmp_path / "job" / "harness-tree"
    assert evidence_tree_digest(retained) == pinned
    assert (retained / "terminus" / "config.json").read_bytes() == (
        tree_dir / "terminus" / "config.json"
    ).read_bytes()

    # Mutating staged bytes after staging must refuse at retention.
    (staged / "terminus" / "AGENTS.md").chmod(0o644)
    (staged / "terminus" / "AGENTS.md").write_text("Tampered.\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed before evidence retention"):
        retain_harness_tree_evidence(tmp_path / "job2", staged, metadata)

    # Tampered metadata must refuse even with pristine bytes.
    staged2, metadata2 = stage_harness_tree(
        tree_dir, pinned, staging_root=tmp_path / "staging2"
    )
    tampered = dict(metadata2)
    tampered["config"] = {"max_turns": 99}
    with pytest.raises(ValueError, match="metadata config"):
        retain_harness_tree_evidence(tmp_path / "job3", staged2, tampered)


def _stage_valid_tree(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    source = _write_tree(tmp_path / "tree", skills=_valid_skills())
    return stage_harness_tree(
        source, evidence_tree_digest(source), staging_root=tmp_path / "staging"
    )


# --- Malformed config, rules and digests -------------------------------------------------


def test_binding_key_matched_by_substring_refuses(tmp_path: Path) -> None:
    # `model_name` is an explicit binding key; this one is only binding because
    # it contains "model".
    tree_dir = _write_tree(tmp_path / "tree", config={"model_preference": "x"})
    with pytest.raises(ValueError, match="model/transport binding"):
        load_harness_tree(tree_dir)


@pytest.mark.parametrize("knob", ["loop_break", "completion_fix"])
def test_non_boolean_behavior_knobs_refuse(tmp_path: Path, knob: str) -> None:
    tree_dir = _write_tree(tmp_path / "tree", config={knob: "yes"})
    with pytest.raises(ValueError, match=f"{knob} must be a boolean"):
        load_harness_tree(tree_dir)


@pytest.mark.parametrize("bad_value", [0, 2.5, True, "4"])
def test_invalid_output_cap_refuses(tmp_path: Path, bad_value: Any) -> None:
    tree_dir = _write_tree(tmp_path / "tree", config={"output_cap_chars": bad_value})
    with pytest.raises(ValueError, match="output_cap_chars must be an integer of at least 2"):
        load_harness_tree(tree_dir)


@pytest.mark.parametrize(
    "knob", ["loop_command_run_min", "loop_message_run_min", "loop_grace_calls"]
)
@pytest.mark.parametrize("bad_value", [0, True, "3"])
def test_invalid_loop_threshold_refuses(tmp_path: Path, knob: str, bad_value: Any) -> None:
    tree_dir = _write_tree(tmp_path / "tree", config={knob: bad_value})
    with pytest.raises(ValueError, match=f"{knob} must be an integer of at least 2"):
        load_harness_tree(tree_dir)


@pytest.mark.parametrize(
    ("relative", "message"),
    [
        ("terminus/config.json", "terminus/config.json must be valid UTF-8"),
        ("terminus/AGENTS.md", "terminus/AGENTS.md must be valid UTF-8"),
    ],
)
def test_non_utf8_harness_inputs_refuse(tmp_path: Path, relative: str, message: str) -> None:
    tree_dir = _write_tree(tmp_path / "tree")
    (tree_dir / relative).write_bytes(b"\xff\xfe")
    with pytest.raises(ValueError, match=message):
        load_harness_tree(tree_dir)


def test_malformed_config_json_refuses(tmp_path: Path) -> None:
    tree_dir = _write_tree(tmp_path / "tree")
    (tree_dir / "terminus" / "config.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="is not valid JSON"):
        load_harness_tree(tree_dir)


def test_malformed_load_digest_refuses(tmp_path: Path) -> None:
    tree_dir = _write_tree(tmp_path / "tree", skills=_valid_skills())
    with pytest.raises(ValueError, match="expected digest is malformed"):
        load_harness_tree(tree_dir, "not-a-digest")


@pytest.mark.parametrize("digest", ["", "nope"])
def test_malformed_stage_digest_refuses(tmp_path: Path, digest: str) -> None:
    tree_dir = _write_tree(tmp_path / "tree")
    with pytest.raises(ValueError, match="expected digest is malformed"):
        stage_harness_tree(tree_dir, digest, staging_root=tmp_path / "staging")


def test_harness_file_becoming_unavailable_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree_dir = _write_tree(tmp_path / "tree")
    original_read_bytes = Path.read_bytes
    rules_reads = 0

    def read_bytes_with_unavailable_rules(path: Path) -> bytes:
        nonlocal rules_reads
        if path.name == "AGENTS.md":
            rules_reads += 1
            if rules_reads == 2:
                raise PermissionError("simulated unavailable file")
        return original_read_bytes(path)

    # Simulates an unavailable file (permission or IO race), which no portable
    # real-file setup produces as non-root. The digest read succeeds first.
    monkeypatch.setattr(Path, "read_bytes", read_bytes_with_unavailable_rules)
    with pytest.raises(ValueError, match="harness tree file unavailable: terminus/AGENTS.md"):
        load_harness_tree(tree_dir)
    assert rules_reads == 2


# --- Unsafe source shapes ----------------------------------------------------------------


def test_symlink_source_refuses(tmp_path: Path) -> None:
    tree_dir = _write_tree(tmp_path / "tree")
    source = tmp_path / "linked-tree"
    source.symlink_to(tree_dir, target_is_directory=True)
    with pytest.raises(ValueError, match="harness tree source must not be a symlink"):
        load_harness_tree(source)


@pytest.mark.parametrize("source_exists", [False, True])
def test_non_directory_source_refuses(tmp_path: Path, source_exists: bool) -> None:
    source = tmp_path / "not-a-tree"
    if source_exists:
        source.write_text("not a directory", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="harness tree root is not a directory"):
        load_harness_tree(source)


def test_special_file_in_tree_refuses(tmp_path: Path) -> None:
    tree_dir = _write_tree(tmp_path / "tree")
    if not hasattr(os, "mkfifo"):
        pytest.skip("os.mkfifo is unavailable")
    try:
        os.mkfifo(tree_dir / "events.fifo")
    except OSError as exc:
        pytest.skip(f"os.mkfifo is unavailable: {exc}")
    with pytest.raises(ValueError, match="unsupported special file"):
        load_harness_tree(tree_dir)


@pytest.mark.parametrize("relative", ["terminus/config.json", "terminus/AGENTS.md"])
def test_fixed_path_symlinks_refuse(tmp_path: Path, relative: str) -> None:
    tree_dir = _write_tree(tmp_path / "tree", config=None, rules=None, skills=_valid_skills())
    target = tmp_path / "target"
    target.write_text("{}", encoding="utf-8")
    (tree_dir / relative).symlink_to(target)
    with pytest.raises(ValueError, match="harness tree contains unsupported symlink"):
        load_harness_tree(tree_dir)
    # Collection catches this first at load; exercise the fixed-path guard directly.
    with pytest.raises(ValueError, match="harness tree contains unsupported symlink"):
        _check_fixed_paths(tree_dir)


@pytest.mark.parametrize("relative", ["terminus/config.json", "terminus/AGENTS.md"])
def test_fixed_path_directories_refuse(tmp_path: Path, relative: str) -> None:
    tree_dir = _write_tree(tmp_path / "tree", config=None, rules=None, skills=_valid_skills())
    (tree_dir / relative).mkdir()
    with pytest.raises(ValueError, match=f"{relative} must be a regular file"):
        load_harness_tree(tree_dir)


# --- Immutable publication of staged trees -----------------------------------------------


@pytest.mark.parametrize("relative", ["", "/abs.txt", "a/../../escape.txt", "../escape.txt"])
def test_publish_refuses_unsafe_relative_paths(tmp_path: Path, relative: str) -> None:
    # Real trees cannot produce these keys: _collect_files derives them from
    # relative_to(root), so only a direct call exercises this guard.
    destination = tmp_path / "destination"
    with pytest.raises(ValueError, match="refuses unsafe relative path"):
        _publish_tree({relative: b"x"}, destination)
    assert not destination.exists()


def test_stage_refuses_symlinked_destination(tmp_path: Path) -> None:
    tree_dir = _write_tree(tmp_path / "tree", skills=_valid_skills())
    pinned = evidence_tree_digest(tree_dir)
    real_directory = tmp_path / "real-staging"
    real_directory.mkdir()
    staging_root = tmp_path / "staging-link"
    staging_root.symlink_to(real_directory, target_is_directory=True)

    with pytest.raises(ValueError, match="destination must not contain a symlink"):
        stage_harness_tree(tree_dir, pinned, staging_root=staging_root)
    assert not (real_directory / pinned.removeprefix("sha256:")).exists()


def test_stage_refuses_destination_file(tmp_path: Path) -> None:
    tree_dir = _write_tree(tmp_path / "tree", skills=_valid_skills())
    pinned = evidence_tree_digest(tree_dir)
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    destination = staging_root / pinned.removeprefix("sha256:")
    destination.write_bytes(b"not a directory")

    with pytest.raises(ValueError, match="destination is not a directory"):
        stage_harness_tree(tree_dir, pinned, staging_root=staging_root)
    assert destination.read_bytes() == b"not a directory"


@pytest.mark.parametrize("change", ["extra", "missing"])
def test_stage_refuses_destination_file_set_change(tmp_path: Path, change: str) -> None:
    tree_dir = _write_tree(tmp_path / "tree", skills=_valid_skills())
    pinned = evidence_tree_digest(tree_dir)
    staging_root = tmp_path / "staging"
    destination = _write_tree(staging_root / pinned.removeprefix("sha256:"), skills=_valid_skills())
    if change == "extra":
        (destination / "extra.txt").write_text("unexpected\n", encoding="utf-8")
    else:
        (destination / "terminus" / "AGENTS.md").unlink()
    expected = sorted(
        path.relative_to(tree_dir).as_posix() for path in tree_dir.rglob("*") if path.is_file()
    )
    actual = sorted(
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    )

    with pytest.raises(ValueError, match="destination has unexpected files") as error:
        stage_harness_tree(tree_dir, pinned, staging_root=staging_root)
    assert f"expected {expected} actual {actual}" in str(error.value)


def test_stage_refuses_destination_content_change(tmp_path: Path) -> None:
    tree_dir = _write_tree(tmp_path / "tree", skills=_valid_skills())
    pinned = evidence_tree_digest(tree_dir)
    staging_root = tmp_path / "staging"
    destination, _ = stage_harness_tree(tree_dir, pinned, staging_root=staging_root)
    changed = destination / "terminus" / "AGENTS.md"
    changed.chmod(0o644)
    changed.write_text("Changed.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="content mismatch: terminus/AGENTS.md"):
        stage_harness_tree(tree_dir, pinned, staging_root=staging_root)
    assert changed.read_text(encoding="utf-8") == "Changed.\n"


def test_stage_accepts_identical_destination_without_rewriting(tmp_path: Path) -> None:
    tree_dir = _write_tree(tmp_path / "tree", skills=_valid_skills())
    pinned = evidence_tree_digest(tree_dir)
    staging_root = tmp_path / "staging"
    destination = _write_tree(staging_root / pinned.removeprefix("sha256:"), skills=_valid_skills())
    files = sorted(path for path in destination.rglob("*") if path.is_file())
    for path in files:
        path.chmod(0o444)
    before = {path: (path.stat().st_mtime_ns, path.stat().st_ino) for path in files}

    staged, metadata = stage_harness_tree(tree_dir, pinned, staging_root=staging_root)

    assert staged == destination
    assert metadata["sha256"] == pinned
    assert sorted(path for path in destination.rglob("*") if path.is_file()) == files
    assert {path: (path.stat().st_mtime_ns, path.stat().st_ino) for path in files} == before
    for path in files:
        assert path.read_bytes() == (tree_dir / path.relative_to(destination)).read_bytes()
        assert path.stat().st_mode & 0o777 == 0o444


def test_stage_refuses_digest_drift_during_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree_dir = _write_tree(tmp_path / "tree", skills=_valid_skills())
    pinned = evidence_tree_digest(tree_dir)
    digest_name = pinned.removeprefix("sha256:")
    staging_root = tmp_path / "staging"
    real_digest = terminus_harness.evidence_tree_digest

    def digest_with_staged_drift(path: Path) -> str:
        actual = real_digest(path)
        # Simulates a copy corrupted between write and verification.
        if Path(path).name == digest_name:
            return "sha256:" + "0" * 64
        return actual

    monkeypatch.setattr(terminus_harness, "evidence_tree_digest", digest_with_staged_drift)
    with pytest.raises(ValueError, match="digest drifted during copy"):
        stage_harness_tree(tree_dir, pinned, staging_root=staging_root)
    assert (staging_root / digest_name).is_dir()


# --- Retained evidence refusals ----------------------------------------------------------


def test_retain_staged_symlink_refuses(tmp_path: Path) -> None:
    staged, metadata = _stage_valid_tree(tmp_path)
    link = tmp_path / "staged-link"
    link.symlink_to(staged, target_is_directory=True)

    with pytest.raises(ValueError, match="staged harness tree must not be a symlink"):
        retain_harness_tree_evidence(tmp_path / "job", link, metadata)


@pytest.mark.parametrize("path_kind", ["missing", "file"])
def test_retain_staged_non_directory_refuses(tmp_path: Path, path_kind: str) -> None:
    staged, metadata = _stage_valid_tree(tmp_path)
    invalid = staged.with_name("invalid-stage")
    if path_kind == "file":
        invalid.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="staged harness tree is not a directory"):
        retain_harness_tree_evidence(tmp_path / "job", invalid, metadata)


@pytest.mark.parametrize("metadata_kind", ["empty", "none", "missing_skill_roots"])
def test_retain_missing_metadata_fields_refuses(tmp_path: Path, metadata_kind: str) -> None:
    staged, metadata = _stage_valid_tree(tmp_path)
    if metadata_kind == "empty":
        metadata = {}
    elif metadata_kind == "none":
        metadata = None
    else:
        del metadata["skill_roots"]

    with pytest.raises(ValueError, match="metadata is missing required fields"):
        retain_harness_tree_evidence(tmp_path / "job", staged, metadata)


def test_retain_metadata_schema_version_refuses(tmp_path: Path) -> None:
    staged, metadata = _stage_valid_tree(tmp_path)
    metadata["schema_version"] = 2

    with pytest.raises(ValueError, match="schema_version must be 1"):
        retain_harness_tree_evidence(tmp_path / "job", staged, metadata)


def test_retain_metadata_artifact_path_refuses(tmp_path: Path) -> None:
    staged, metadata = _stage_valid_tree(tmp_path)
    metadata["artifact_path"] = "other"

    with pytest.raises(ValueError, match="artifact_path must be 'harness-tree'"):
        retain_harness_tree_evidence(tmp_path / "job", staged, metadata)


@pytest.mark.parametrize("bad_sha256", ["nope", 5])
def test_retain_metadata_malformed_sha256_refuses(tmp_path: Path, bad_sha256: Any) -> None:
    staged, metadata = _stage_valid_tree(tmp_path)
    metadata["sha256"] = bad_sha256

    with pytest.raises(ValueError, match="metadata sha256 is malformed"):
        retain_harness_tree_evidence(tmp_path / "job", staged, metadata)


def test_retain_metadata_rules_path_mismatch_refuses(tmp_path: Path) -> None:
    staged, metadata = _stage_valid_tree(tmp_path)
    metadata["rules_path"] = None

    with pytest.raises(ValueError, match="rules_path does not match staged bytes"):
        retain_harness_tree_evidence(tmp_path / "job", staged, metadata)


@pytest.mark.parametrize("bad_skill_roots", [["other", "terminus-commands"], "terminus/skills"])
def test_retain_metadata_skill_roots_mismatch_refuses(
    tmp_path: Path, bad_skill_roots: Any
) -> None:
    staged, metadata = _stage_valid_tree(tmp_path)
    metadata["skill_roots"] = bad_skill_roots

    with pytest.raises(ValueError, match="skill_roots does not match staged bytes"):
        retain_harness_tree_evidence(tmp_path / "job", staged, metadata)


def test_retain_copy_digest_drift_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    staged, metadata = _stage_valid_tree(tmp_path)
    job_dir = tmp_path / "job"
    target = job_dir / "harness-tree"
    real_digest = terminus_harness.evidence_tree_digest

    def digest_with_corrupted_copy(path: Path) -> str:
        # Simulates a copy corrupted during publication.
        if path == target:
            return "sha256:" + "0" * 64
        return real_digest(path)

    monkeypatch.setattr(terminus_harness, "evidence_tree_digest", digest_with_corrupted_copy)
    with pytest.raises(ValueError, match="retained harness tree digest drifted during copy"):
        retain_harness_tree_evidence(job_dir, staged, metadata)
