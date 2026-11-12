"""The executed-clean builder reproduces the committed manifest; --check proves it.

The manifest is derived from committed inputs. A hand edit, a CRLF checkout or a
stale rebuild must fail ``--check`` with one stderr line that names the file,
and ``--check`` must never write it.
"""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = REPO_ROOT / "research" / "experiments" / "mimo-executed-clean-v1"
MODULE = runpy.run_path(str(EXPERIMENT / "build_pool.py"))

OUTPUT_NAME = MODULE["OUTPUT_NAME"]
MANIFEST_RELATIVE = f"research/experiments/mimo-executed-clean-v1/{OUTPUT_NAME}"
# The committed inputs `build` reads, as repository paths.
INPUTS = (
    "research/experiments/mimo-clean-v7/manifest.json",
    "research/experiments/mimo-defect-map-v1/findings.json",
    "research/experiments/mimo-defect-map-v1/repairs.json",
)


def _tree(root: Path) -> Path:
    """A checkout-root copy holding the committed inputs and the committed manifest."""
    for relative in (*INPUTS, MANIFEST_RELATIVE):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO_ROOT / relative).read_bytes())
    return root


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _check(root: Path) -> int:
    return MODULE["main"](["--check", "--root", str(root)])


def test_check_exits_zero_on_the_committed_tree(capsys: pytest.CaptureFixture[str]) -> None:
    # --check only reads, so the committed directory is safe to point at.
    assert MODULE["main"](["--check"]) == 0
    assert capsys.readouterr().err == ""


def test_check_exits_zero_on_an_exact_copy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path)
    before = _snapshot(root)
    assert _check(root) == 0
    assert capsys.readouterr().err == ""
    assert _snapshot(root) == before


def test_check_exits_one_on_one_edited_byte(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path)
    path = root / MANIFEST_RELATIVE
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01
    path.write_bytes(bytes(data))
    before = _snapshot(root)
    assert _check(root) == 1
    assert _snapshot(root) == before  # --check writes nothing
    error = capsys.readouterr().err.splitlines()
    assert len(error) == 1
    assert OUTPUT_NAME in error[0]


def test_check_exits_one_on_a_carriage_return_copy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Text-mode reads fold CRLF to LF; the check must compare the bytes on disk.
    root = _tree(tmp_path)
    path = root / MANIFEST_RELATIVE
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    before = _snapshot(root)
    assert _check(root) == 1
    assert _snapshot(root) == before
    error = capsys.readouterr().err.splitlines()
    assert len(error) == 1
    assert OUTPUT_NAME in error[0]


def test_check_exits_one_when_the_manifest_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path)
    path = root / MANIFEST_RELATIVE
    path.unlink()
    assert _check(root) == 1
    assert not path.exists()  # --check does not recreate it
    error = capsys.readouterr().err.splitlines()
    assert len(error) == 1
    assert OUTPUT_NAME in error[0]


def test_default_mode_writes_the_committed_bytes(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    path = root / MANIFEST_RELATIVE
    path.unlink()
    assert MODULE["main"](["--root", str(root)]) == 0
    assert path.read_bytes() == (EXPERIMENT / OUTPUT_NAME).read_bytes()
