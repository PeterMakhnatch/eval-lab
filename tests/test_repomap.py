"""Tests for the generated repository map and the three operator skills."""

from __future__ import annotations

import ast
from pathlib import Path

from evallab.contextpack import parse_front_matter, repo_root
from evallab.lineage import compute_file_digest, resolve_lineage
from evallab.repomap import (
    GENERATED_BY_MARKER,
    _function_map,
    check_map,
    generate_map,
    main,
    module_purpose,
    write_map,
)

SKILL_NAMES = ("lab-status", "mission-launch", "review")


def _write_module(src_dir: Path, name: str, body: str) -> Path:
    path = src_dir / f"{name}.py"
    path.write_text(body, encoding="utf-8")
    return path


def _sample_tree(tmp_path: Path) -> Path:
    src = tmp_path / "src" / "evallab"
    src.mkdir(parents=True)
    _write_module(
        src,
        "status",
        '"""Read-only operator snapshot of completed Harbor evidence."""\n\n'
        "def build_status_snapshot() -> str:\n"
        '    return "ok"\n',
    )
    _write_module(
        src,
        "cli",
        "from __future__ import annotations\n\n"
        "import argparse\n\n"
        "from evallab.status import build_status_snapshot\n\n\n"
        "def parser() -> argparse.ArgumentParser:\n"
        "    root = argparse.ArgumentParser(prog='evallab')\n"
        "    commands = root.add_subparsers(dest='command', required=True)\n"
        "    commands.add_parser('status', help='operator snapshot')\n"
        "    return root\n\n\n"
        "def run_cli() -> int:\n"
        "    args = parser().parse_args()\n"
        "    if args.command == 'status':\n"
        "        print(build_status_snapshot())\n"
        "        return 0\n"
        "    return 2\n",
    )
    sql = tmp_path / "sql"
    sql.mkdir()
    (sql / "schema.sql").write_text(
        "CREATE TABLE IF NOT EXISTS jobs (id uuid PRIMARY KEY);\n"
        "CREATE OR REPLACE VIEW trial_observations AS SELECT 1;\n",
        encoding="utf-8",
    )
    return src


def test_generation_is_deterministic(tmp_path: Path) -> None:
    src = _sample_tree(tmp_path)
    first = generate_map(src_dir=src, root=tmp_path)
    second = generate_map(src_dir=src, root=tmp_path)
    assert first == second
    assert GENERATED_BY_MARKER in first
    assert first.startswith("---\nstatus: living\n")
    assert "generated_at" not in first
    assert "timestamp" not in first.lower()


def test_every_module_appears_in_the_map(tmp_path: Path) -> None:
    src = _sample_tree(tmp_path)
    text = generate_map(src_dir=src, root=tmp_path)
    for path in src.glob("*.py"):
        assert f"`{path.stem}`" in text


def test_cli_subcommand_is_attributed_to_implementing_module(tmp_path: Path) -> None:
    src = _sample_tree(tmp_path)
    text = generate_map(src_dir=src, root=tmp_path)
    assert "| `status` | `status` |" in text
    status_row = next(
        line
        for line in text.splitlines()
        if line.startswith("| `status` |") and "Lines" not in line
    )
    assert status_row.count("`status`") >= 2


def _registry_tree(tmp_path: Path) -> Path:
    """A CLI that dispatches through `set_defaults(func=...)`, with no if-chain."""
    src = tmp_path / "src" / "evallab"
    src.mkdir(parents=True)
    _write_module(
        src,
        "status",
        '"""Read-only operator snapshot of completed Harbor evidence."""\n\n'
        "def build_status_snapshot() -> str:\n"
        '    return "ok"\n',
    )
    _write_module(
        src,
        "fetch",
        '"""Pinned Harbor acquisition."""\n\n\n'
        "class HarborBackend:\n    pass\n\n\n"
        "class FetchService:\n    pass\n",
    )
    _write_module(
        src,
        "cli",
        "from __future__ import annotations\n\n"
        "import argparse\n\n"
        "from evallab.fetch import FetchService, HarborBackend\n"
        "from evallab.status import build_status_snapshot\n\n\n"
        "def _snapshot_command(\n"
        "    args: argparse.Namespace,\n"
        "    harbor: HarborBackend,\n"
        "    service: FetchService,\n"
        ") -> int:\n"
        "    print(build_status_snapshot())\n"
        "    return 0\n\n\n"
        "def parser() -> argparse.ArgumentParser:\n"
        "    root = argparse.ArgumentParser(prog='evallab')\n"
        "    commands = root.add_subparsers(dest='command', required=True)\n"
        "    snapshot = commands.add_parser('snapshot', help='operator snapshot')\n"
        "    snapshot.set_defaults(func=_snapshot_command)\n"
        "    return root\n\n\n"
        "def run_cli() -> int:\n"
        "    args = parser().parse_args()\n"
        "    return int(args.func(args, HarborBackend(), FetchService()))\n",
    )
    sql = tmp_path / "sql"
    sql.mkdir()
    (sql / "schema.sql").write_text(
        "CREATE TABLE IF NOT EXISTS jobs (id uuid PRIMARY KEY);\n"
        "CREATE OR REPLACE VIEW trial_observations AS SELECT 1;\n",
        encoding="utf-8",
    )
    return src


def _command_row(text: str, command: str) -> str:
    return next(
        line
        for line in text.splitlines()
        if line.startswith(f"| `{command}` |") and "Lines" not in line
    )


def test_registry_dispatch_is_attributed_to_implementing_module(tmp_path: Path) -> None:
    """A `set_defaults(func=...)` command keeps its module edge with no if-chain.

    Without registry support every command in a converted CLI silently collapses to
    `cli`, which would make the map under-report reachability — the one signal used
    to find built-but-unreachable modules.
    """
    src = _registry_tree(tmp_path)
    text = generate_map(src_dir=src, root=tmp_path)
    assert "| `snapshot` | `status` |" in text


def test_handler_annotations_do_not_decide_attribution(tmp_path: Path) -> None:
    """Signature types must not outvote the body.

    `_snapshot_command` is annotated with two `fetch` types and calls exactly one
    `status` function. Counting annotations as references would attribute the command
    to `fetch` 2-to-1; only the body may decide.
    """
    src = _registry_tree(tmp_path)
    text = generate_map(src_dir=src, root=tmp_path)
    row = _command_row(text, "snapshot")
    assert "`status`" in row
    assert "`fetch`" not in row


def test_check_fails_on_stale_committed_map(tmp_path: Path) -> None:
    src = _sample_tree(tmp_path)
    map_path = tmp_path / "docs" / "repo-map.md"
    write_map(map_path, src_dir=src, root=tmp_path)
    assert check_map(src_dir=src, map_path=map_path, root=tmp_path) == []

    map_path.write_text(
        map_path.read_text(encoding="utf-8") + "\n<!-- hand edit -->\n",
        encoding="utf-8",
    )
    issues = check_map(src_dir=src, map_path=map_path, root=tmp_path)
    assert any("stale" in issue.message for issue in issues)
    assert main(["check", "--src-dir", str(src), "--map", str(map_path)]) == 1


def test_check_fails_on_module_missing_docstring(tmp_path: Path) -> None:
    src = _sample_tree(tmp_path)
    _write_module(src, "blank", "VALUE = 1\n")
    map_path = tmp_path / "docs" / "repo-map.md"
    write_map(map_path, src_dir=src, root=tmp_path)

    issues = check_map(src_dir=src, map_path=map_path, root=tmp_path)
    assert any(
        issue.path.endswith("blank.py") and "no docstring" in issue.message for issue in issues
    )
    assert main(["check", "--src-dir", str(src), "--map", str(map_path)]) == 1


def test_unusual_top_level_constructs_are_mapped_without_crashing(tmp_path: Path) -> None:
    src = _sample_tree(tmp_path)
    _write_module(
        src,
        "unusual",
        "1 + 1\n\n"
        "if True:\n"
        "    try:\n"
        "        import sys\n"
        "    except ImportError:\n"
        "        pass\n\n"
        "try:\n"
        "    x = 42\n"
        "except Exception:\n"
        "    x = 0\n",
    )
    map_path = tmp_path / "docs" / "repo-map.md"
    text = generate_map(src_dir=src, root=tmp_path)
    assert "`unusual`" in text
    assert "_(missing docstring)_" in text
    write_map(map_path, src_dir=src, root=tmp_path)

    issues = check_map(src_dir=src, map_path=map_path, root=tmp_path)
    assert any(
        issue.path.endswith("unusual.py") and "no docstring" in issue.message for issue in issues
    )


def test_ast_helpers_handle_non_module_nodes() -> None:
    node = ast.Constant(value=42)
    assert module_purpose("42", node) is None
    assert _function_map(node) == {}


def test_check_passes_on_real_repository_tree() -> None:
    root = repo_root()
    src = root / "src" / "evallab"
    map_path = root / "docs" / "repo-map.md"
    assert map_path.is_file(), "docs/repo-map.md must be generated and committed"
    issues = check_map(src_dir=src, map_path=map_path, root=root)
    assert issues == []
    assert main(["check"]) == 0


def test_real_map_lists_every_module_and_attributes_status() -> None:
    root = repo_root()
    text = generate_map(root=root)
    for path in (root / "src" / "evallab").glob("*.py"):
        assert f"`{path.stem}`" in text
    assert "| `status` | `status` |" in text
    assert GENERATED_BY_MARKER in text


def test_operator_skills_exist_with_name_and_description() -> None:
    root = repo_root()
    for name in SKILL_NAMES:
        path = root / ".claude" / "skills" / name / "SKILL.md"
        assert path.is_file(), f"missing skill {path}"
        front_matter, _body = parse_front_matter(path.read_text(encoding="utf-8"))
        assert front_matter is not None, f"{path} has no YAML front-matter"
        assert front_matter.get("name") == name
        description = front_matter.get("description")
        assert isinstance(description, str) and description.strip()


def test_front_matter_declares_valid_inputs_list(tmp_path: Path) -> None:
    src = _sample_tree(tmp_path)
    map_text = generate_map(src_dir=src, root=tmp_path)
    fm, _body = parse_front_matter(map_text)
    assert fm is not None
    assert "inputs" in fm
    assert isinstance(fm["inputs"], list)
    assert len(fm["inputs"]) > 0
    for item in fm["inputs"]:
        assert isinstance(item, dict)
        assert "path" in item and isinstance(item["path"], str)
        assert "digest" in item and isinstance(item["digest"], str)
        assert item["digest"].startswith("sha256:")
        assert len(item["digest"]) == 71


def test_generation_convergence_two_consecutive_runs(tmp_path: Path) -> None:
    src = _sample_tree(tmp_path)
    map_path = tmp_path / "docs" / "repo-map.md"
    map_path.parent.mkdir(parents=True, exist_ok=True)
    first = write_map(output=map_path, src_dir=src, root=tmp_path)
    second = write_map(output=map_path, src_dir=src, root=tmp_path)
    assert first == second
    assert map_path.read_text(encoding="utf-8") == first


def test_recorded_digests_match_actual_file_digests(tmp_path: Path) -> None:
    src = _sample_tree(tmp_path)
    map_text = generate_map(src_dir=src, root=tmp_path)
    fm, _body = parse_front_matter(map_text)
    assert fm is not None and "inputs" in fm
    for item in fm["inputs"]:
        target_file = tmp_path / item["path"]
        assert target_file.is_file()
        expected = compute_file_digest(target_file)
        assert item["digest"] == expected


def test_lineage_resolution_on_generated_map(tmp_path: Path) -> None:
    src = _sample_tree(tmp_path)
    map_path = tmp_path / "docs" / "repo-map.md"
    map_path.parent.mkdir(parents=True, exist_ok=True)
    write_map(output=map_path, src_dir=src, root=tmp_path)

    node = resolve_lineage("docs/repo-map.md", repo_root=tmp_path)
    assert node.status == "resolved"
    assert len(node.inputs) > 0
    assert any(child.path == "src/evallab/cli.py" for child in node.inputs)
    assert node.status != "unrecorded"
