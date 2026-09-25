"""Focused artifact-validation tests for pinned Terminus harness trees."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.evidence_store import evidence_tree_digest
from evallab.terminus_harness import (
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
