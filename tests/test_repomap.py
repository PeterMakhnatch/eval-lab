"""Tests for the generated repository map and the three operator skills."""

from __future__ import annotations

from pathlib import Path

from evallab.contextpack import parse_front_matter, repo_root
from evallab.repomap import (
    GENERATED_BY_MARKER,
    check_map,
    generate_map,
    main,
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
    assert (
        main(["check", "--src-dir", str(src), "--map", str(map_path)]) == 1
    )


def test_check_fails_on_module_missing_docstring(tmp_path: Path) -> None:
    src = _sample_tree(tmp_path)
    _write_module(src, "blank", "VALUE = 1\n")
    map_path = tmp_path / "docs" / "repo-map.md"
    write_map(map_path, src_dir=src, root=tmp_path)

    issues = check_map(src_dir=src, map_path=map_path, root=tmp_path)
    assert any(
        issue.path.endswith("blank.py") and "no docstring" in issue.message
        for issue in issues
    )
    assert (
        main(["check", "--src-dir", str(src), "--map", str(map_path)]) == 1
    )


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
