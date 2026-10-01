"""Behavioral checks for the OVN G5 paired-eval spec generator."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from contextlib import suppress
from pathlib import Path

import pytest

EXP = Path("research/experiments/ovn-sft-v0/make_g5_specs.py")


def _load():
    spec = importlib.util.spec_from_file_location("ovn_g5_make_specs", EXP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MAKE = _load()


def test_spec_names_are_deterministic_per_arm() -> None:
    assert MAKE.spec_name("format-code-task-002302", "stock") == "ovn-g5-002302-stock"
    assert MAKE.spec_name("format-code-task-002302", "tuned") == "ovn-g5-002302-tuned"
    assert MAKE.spec_name("format-code-task-002302", "gepa") == "ovn-g5-002302-gepa"


def test_arm_order_cycles_through_six_rows() -> None:
    assert MAKE.arm_order(1, True) == ["stock", "tuned", "gepa"]
    assert MAKE.arm_order(2, True) == ["tuned", "gepa", "stock"]
    assert MAKE.arm_order(3, True) == ["gepa", "stock", "tuned"]
    assert MAKE.arm_order(4, True) == ["gepa", "tuned", "stock"]
    assert MAKE.arm_order(5, True) == ["stock", "gepa", "tuned"]
    assert MAKE.arm_order(6, True) == ["tuned", "stock", "gepa"]
    # The cycle repeats through all 20 CSV rows.
    assert MAKE.arm_order(7, True) == MAKE.arm_order(1, True)
    assert MAKE.arm_order(20, True) == MAKE.arm_order(2, True)


def test_arm_order_two_arm_fallback_alternates_stock_first() -> None:
    assert MAKE.arm_order(1, False) == ["stock", "tuned"]
    assert MAKE.arm_order(2, False) == ["tuned", "stock"]
    assert MAKE.arm_order(19, False) == ["stock", "tuned"]
    assert MAKE.arm_order(20, False) == ["tuned", "stock"]


def test_gepa_flags_are_required_together(tmp_path: Path) -> None:
    candidate = tmp_path / "addendum.txt"
    candidate.write_text("prompt addendum")
    with pytest.raises(SystemExit):
        MAKE._gepa(candidate, None)
    with pytest.raises(SystemExit):
        MAKE._gepa(None, "sha256:" + "ab" * 32)


PLACEHOLDER_TREE = Path("research/experiments/har116-loopfix-leak/harness-loopfix")
_CHECK_TASK = "format-code-task-002302"
_CHECK_PACKAGE = "sha256:" + "ab" * 32
_CHECK_VERIFIER = "sha256:" + "cd" * 32
_CHECK_ARMS = ("stock", "tuned", "gepa")


class _Digests:
    def __init__(self, package: str, verifier: str) -> None:
        self.package = package
        self.verifier = verifier


@pytest.fixture
def check_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A minimal valid 3-arm check() setup: one kept task, real tree and route.

    The staged dir and the GEPA candidate live in the generator's real
    gitignored tasks/ dir (check() resolves task dirs against it) and are
    removed afterwards; the specs go to a tmp dir.
    """
    tasks = MAKE.TASKS
    tasks.mkdir(exist_ok=True)
    staged_dir = tasks / _CHECK_TASK
    staged_dir.mkdir(exist_ok=True)
    candidate = tasks / ".g5-test-candidate.txt"
    candidate.write_text("test addendum")
    import hashlib

    candidate_sha = "sha256:" + hashlib.sha256(candidate.read_bytes()).hexdigest()
    candidate_rel = candidate.relative_to(MAKE.REPO).as_posix()
    monkeypatch.setattr(
        MAKE,
        "compute_task_digests",
        lambda _dest: _Digests(_CHECK_PACKAGE, _CHECK_VERIFIER),
    )
    try:
        kept = [
            {
                "task_id": _CHECK_TASK,
                "run_digest": _CHECK_PACKAGE,
                "run": "original",
                "repo": "powerline",
            }
        ]
        route = MAKE._route(MAKE.BASE_SPEC)
        tree = MAKE._tree(PLACEHOLDER_TREE, None)
        out_dir = tmp_path / "specs"
        out_dir.mkdir()
        staged = {
            _CHECK_TASK: {
                "staged_dir": staged_dir,
                "package_digest": _CHECK_PACKAGE,
                "verifier": _CHECK_VERIFIER,
            }
        }
        MAKE.generate(kept, staged, route, tree, out_dir, _CHECK_ARMS, candidate_rel, candidate_sha)
        MAKE.write_cohort(out_dir, kept, staged, tree, _CHECK_ARMS, candidate_rel, candidate_sha)
        yield out_dir, kept, route, tree, candidate_rel, candidate_sha
    finally:
        shutil.rmtree(staged_dir, ignore_errors=True)
        with suppress(OSError):
            candidate.unlink()
        with suppress(OSError):
            tasks.rmdir()


def _rewrite_spec(out_dir: Path, stem: str, **overrides) -> None:
    path = out_dir / f"{stem}.json"
    doc = json.loads(path.read_text())
    doc.update(overrides)
    path.write_text(json.dumps(doc, indent=2) + "\n")


def test_check_accepts_matching_three_arm_specs(check_setup, capsys) -> None:
    out_dir, kept, route, tree, candidate_rel, candidate_sha = check_setup
    assert MAKE.check(out_dir, kept, route, tree, _CHECK_ARMS, candidate_rel, candidate_sha) == 0


def test_check_refuses_arm_with_different_limits(check_setup, capsys) -> None:
    out_dir, kept, route, tree, candidate_rel, candidate_sha = check_setup
    _rewrite_spec(out_dir, MAKE.spec_name(_CHECK_TASK, "tuned"), max_requests=119)
    assert MAKE.check(out_dir, kept, route, tree, _CHECK_ARMS, candidate_rel, candidate_sha) == 1
    out = capsys.readouterr().out
    assert "max_requests" in out


def test_check_refuses_arm_with_different_tree(check_setup, capsys) -> None:
    out_dir, kept, route, tree, candidate_rel, candidate_sha = check_setup
    _rewrite_spec(
        out_dir, MAKE.spec_name(_CHECK_TASK, "tuned"), harness_tree_sha256="sha256:" + "ef" * 32
    )
    assert MAKE.check(out_dir, kept, route, tree, _CHECK_ARMS, candidate_rel, candidate_sha) == 1
    out = capsys.readouterr().out
    assert "harness digest" in out or "harness_tree_sha256" in out


def test_check_refuses_arm_with_different_task_digest(check_setup, capsys) -> None:
    out_dir, kept, route, tree, candidate_rel, candidate_sha = check_setup
    _rewrite_spec(
        out_dir, MAKE.spec_name(_CHECK_TASK, "gepa"), task_package_digest="sha256:" + "ef" * 32
    )
    assert MAKE.check(out_dir, kept, route, tree, _CHECK_ARMS, candidate_rel, candidate_sha) == 1
    out = capsys.readouterr().out
    assert "task_package_digest" in out or "package digest" in out
