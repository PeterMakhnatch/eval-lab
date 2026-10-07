"""Offline Git behavior models for the six retained HAR-191 pilot shapes.

pilots.json contains compact, hash-bound historical metadata, not executable
proof for this source revision. Histories below are deliberately synthetic;
real task CPU extraction and oracle/nop execution remain separate evidence.
"""

from __future__ import annotations

import importlib.util
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "research/experiments/leak-oracle/extract.py"
FIXTURE = Path(__file__).parent / "fixtures/leak_oracle/pilots.json"
PILOTS = json.loads(FIXTURE.read_text())["pilots"]
spec = importlib.util.spec_from_file_location("leak_oracle", SCRIPT)
oracle = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = oracle
spec.loader.exec_module(oracle)


class History:
    """All mutating Git commands are confined to pytest's disposable repo."""

    def __init__(self, path: Path):
        self.path = path
        path.mkdir()
        self.clock = 0
        self.git("init", "--quiet")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        self.git("config", "user.name", "Leak oracle fixture")
        self.git("config", "user.email", "fixture@example.invalid")

    @property
    def git_dir(self) -> str:
        return str(self.path / ".git")

    def git(self, *args: str, input: str | None = None) -> str:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_AUTHOR_DATE": f"2024-01-01T00:{self.clock:02d}:00+00:00",
                "GIT_COMMITTER_DATE": f"2024-01-01T00:{self.clock:02d}:00+00:00",
            }
        )
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=" + os.devnull, *args],
            cwd=self.path,
            env=env,
            input=input,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return result.stdout.strip()

    def commit(self, changes: dict[str, str], subject: str) -> str:
        for name, body in changes.items():
            path = self.path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        self.clock += 1
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", subject)
        return self.git("rev-parse", "HEAD")

    def checkout(self, sha: str) -> None:
        self.git("checkout", "--quiet", "--detach", sha)

    def fork(self, ref: str, sha: str) -> None:
        self.git("checkout", "--quiet", "-b", ref, sha)

    def dangling(self, base: str) -> None:
        self.checkout(base)
        self.git("update-ref", "-d", "refs/heads/main")


def package(
    tmp_path: Path,
    name: str,
    instruction: str,
    paths: list[str],
    added: str = "from package.core import VALUE\nassert VALUE == 2\n",
) -> Path:
    task = tmp_path / name
    (task / "tests").mkdir(parents=True)
    (task / "task.toml").write_text('title = "offline history fixture"\n')
    (task / "instruction.md").write_text(instruction)
    patch = ""
    for path in paths:
        patch += (
            f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -0,0 +1,{len(added.splitlines())} @@\n"
        )
        patch += "".join("+" + line + "\n" for line in added.splitlines())
    (task / "tests/test.patch").write_text(patch)
    return task


def snapshot(path: Path) -> dict[str, tuple[str, bytes | str]]:
    return {
        str(entry.relative_to(path)): (
            ("link", os.readlink(entry)) if entry.is_symlink() else ("file", entry.read_bytes())
        )
        for entry in sorted(path.rglob("*"))
        if entry.is_file() or entry.is_symlink()
    }


def extract_readonly(history: History, task: Path, output: Path, **kwargs) -> dict:
    task_before, git_before = snapshot(task), snapshot(Path(history.git_dir))
    result = oracle.extract(task, history.git_dir, output, **kwargs)
    assert snapshot(task) == task_before
    assert snapshot(Path(history.git_dir)) == git_before
    assert json.loads((output / "evidence.json").read_text()) == result
    if result["status"] in oracle.SUCCESS_STATUSES:
        assert (output / "solution.patch").is_file()
        paths = oracle.parse_test_patch((output / "solution.patch").read_text())
        assert paths
        assert set(paths) <= set(result["non_test_files"])
        assert all(not oracle.is_test_path(path) for path in paths)
    else:
        assert not (output / "solution.patch").exists()
    return result


def materialize(history: History, base: str, target: Path, solution: Path | None = None) -> Path:
    target.mkdir()
    oracle.unpack_base(history.git_dir, base, target)
    if solution is not None:
        subprocess.run(
            ["git", "apply", "--no-index", "-"],
            input=solution.read_text(),
            cwd=target,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        )
    return target


def source(tree: Path, path: str) -> dict:
    return runpy.run_path(str(tree / path))


def test_002552_dangling_fix_keeps_before_blob_continuity(tmp_path):
    pilot = PILOTS["002552"]
    history = History(tmp_path / "git")
    model, test = "miio/miot_models.py", "miio/tests/test_miot_models.py"
    original = 'def parse_action(values):\n    return len(values["in"]), len(values["out"])\n'
    fixed = (
        "def parse_action(values):\n"
        "    values = dict(values)\n"
        '    for key in ("in", "out"):\n'
        "        if values[key] is None:\n"
        "            values[key] = []\n"
        '    return len(values["in"]), len(values["out"])\n'
    )
    base = history.commit({model: original, test: "# baseline\n"}, "baseline")
    history.commit({"notes.txt": "pre-fix metadata\n"}, "metadata")
    fix = history.commit(
        {model: fixed, test: "# baseline\n# null action regression\n"}, pilot["fix"]["subject"]
    )
    history.commit(
        {
            model: fixed + '\ndef unique_identifier():\n    return "device"\n',
            test: "# baseline\n# null action regression\n# device identifier\n",
        },
        pilot["candidates"][1]["subject"],
    )
    history.dangling(base)
    task = package(
        tmp_path,
        pilot["task"],
        "Null action parsing in miio/miot_models.py; device details.",
        [test],
        'from miio.miot_models import parse_action\nassert parse_action({"in": None, "out": None}) == (0, 0)\n',
    )
    out = tmp_path / "out"
    result = extract_readonly(history, task, out)
    assert result["status"] == "ok"
    assert result["fix"]["sha"] == fix
    assert result["fix"]["continuous"] == [test]
    assert result["fix"]["on_ref"] is False
    assert result["n_future_on_ref"] == 0
    assert result["fix"]["distance"] == 2
    tree = materialize(history, base, tmp_path / "oracle", out / "solution.patch")
    namespace = source(tree, model)
    assert namespace["parse_action"]({"in": None, "out": None}) == (0, 0)
    assert "unique_identifier" not in namespace
    nop = materialize(history, base, tmp_path / "nop")
    with pytest.raises(TypeError):
        source(nop, model)["parse_action"]({"in": None, "out": None})


def test_002402_source_relevance_beats_test_only_continuity(tmp_path):
    pilot = PILOTS["002402"]
    history = History(tmp_path / "git")
    model, test = "numpyro/distributions/batch_util.py", "test/contrib/test_control_flow.py"
    base = history.commit(
        {
            model: "def promote_batch_shape(shapes):\n    return shapes[0]\n",
            test: "# baseline\n",
        },
        "baseline",
    )
    tolerance = history.commit(
        {test: "# baseline\n# tolerance stability\n"}, pilot["candidates"][1]["subject"]
    )
    fix = history.commit(
        {
            model: "def promote_batch_shape(shapes):\n    return tuple(max(values) for values in zip(*shapes))\n",
            test: "# baseline\n# tolerance stability\n# batch shapes\n",
        },
        pilot["fix"]["subject"],
    )
    history.dangling(base)
    task = package(
        tmp_path,
        pilot["task"],
        "promote_batch_shape must use all shapes in numpyro/distributions/batch_util.py.",
        [test],
        "from numpyro.distributions.batch_util import promote_batch_shape\nassert promote_batch_shape([(1, 2), (3, 1)]) == (3, 2)\n",
    )
    out = tmp_path / "out"
    result = extract_readonly(history, task, out)
    assert result["status"] == "ok"
    assert result["fix"]["sha"] == fix
    assert result["fix"]["continuous"] == []
    assert result["fix"]["source_hits"] == [model]
    assert any(
        candidate["sha"] == tolerance and candidate["continuous"]
        for candidate in result["candidates"]
    )
    tree = materialize(history, base, tmp_path / "oracle", out / "solution.patch")
    nop = materialize(history, base, tmp_path / "nop")
    shapes = [(1, 2), (3, 1)]
    assert source(tree, model)["promote_batch_shape"](shapes) == (3, 2)
    assert source(nop, model)["promote_batch_shape"](shapes) != (3, 2)


def test_002139_divergent_fetcher_scopes_top_source_only_fix(tmp_path):
    pilot = PILOTS["002139"]
    history = History(tmp_path / "git")
    init, model = "lib/bb/fetch2/__init__.py", "lib/bb/fetch2/gcp.py"
    root = history.commit(
        {init: "FETCHERS = []\n", "lib/bb/data.py": "DATA = {}\n"}, "common history"
    )
    base = history.commit({"base_only.py": "PRESERVE = True\n"}, "independent base")
    history.fork("future", root)
    fix = history.commit(
        {
            init: 'FETCHERS = ["gcp"]\n',
            model: (
                'def checkstatus(uri):\n    return uri.startswith("gs://")\n'
                "def downloadfilename(uri):\n"
                '    return uri.partition(";downloadfilename=")[2] or uri.rsplit("/", 1)[-1]\n'
                'def bucket(uri):\n    return uri.split("/")[2]\n'
            ),
            "doc/fetching.rst": "Google Cloud Platform GCP fetcher documentation\n",
        },
        pilot["fix"]["subject"],
    )
    history.commit(
        {"lib/bb/fetch2/s3.py": "def checkstatus(uri):\n    return False\n"}, "other fetcher"
    )
    history.checkout(base)
    task = package(
        tmp_path,
        pilot["task"],
        "Google Cloud Platform GCP fetcher with `downloadfilename`, `bucket`, and `checkstatus`.",
        ["lib/bb/tests/test_gcp_new.py"],
        "from bb import data\nfrom bb.fetch2 import FetchMethod\n",
    )
    out = tmp_path / "out"
    result = extract_readonly(history, task, out)
    assert result["status"] == "ok-divergent"
    assert result["fix"]["sha"] == fix
    assert result["fix"]["distance"] is None
    assert result["non_test_files"] == [init, model]
    assert "doc/fetching.rst" in result["s2b_excluded"]
    assert "lib/bb/fetch2/s3.py" not in oracle.parse_test_patch(
        (out / "solution.patch").read_text()
    )
    tree = materialize(history, base, tmp_path / "oracle", out / "solution.patch")
    assert (tree / "base_only.py").is_file()
    assert (
        source(tree, model)["downloadfilename"]("gs://bucket/source;downloadfilename=archive")
        == "archive"
    )
    assert source(tree, init)["FETCHERS"] == ["gcp"]


def test_002391_divergent_alias_dedup_excludes_hidden_harness(tmp_path):
    pilot = PILOTS["002391"]
    history = History(tmp_path / "git")
    model, interface = "pip_audit/_audit.py", "pip_audit/_service/interface.py"
    root = history.commit(
        {
            model: "def audit_reports(reports):\n    return reports\n",
            interface: "SERVICES = []\n",
            "pip_audit/_service/__init__.py": "SERVICES = []\n",
        },
        "common history",
    )
    base = history.commit({"base_only.py": "PRESERVE = True\n"}, "independent base")
    history.fork("future", root)
    fix = history.commit(
        {
            model: (
                "def dedupe_aliases(reports):\n"
                "    seen, kept = set(), []\n"
                "    for report in reports:\n"
                '        identifiers = {report["id"]} | set(report.get("aliases", []))\n'
                "        if not identifiers & seen:\n"
                "            kept.append(report)\n"
                "        seen.update(identifiers)\n"
                "    return kept\n"
                "def audit_reports(reports):\n    return dedupe_aliases(reports)\n"
            ),
            interface: (
                "SERVICES = []\nclass VulnerabilityResult:\n"
                "    def __init__(self, identifier, aliases):\n"
                "        self.identifier = identifier\n"
                "        self.aliases = tuple(aliases)\n"
            ),
            "CHANGELOG.md": "dedupe aliases\n",
            "test/test_audit.py": "from pip_audit._audit import dedupe_aliases\n",
            "test_commands.json": '{"test_commands": []}\n',
            "mimo_test_command.sh": "exit 0\n",
        },
        pilot["fix"]["subject"],
    )
    history.checkout(base)
    task = package(
        tmp_path,
        pilot["task"],
        "Dedupe vulnerability alias reports using `dedupe_aliases` and `VulnerabilityResult`.",
        ["test/test_alias_new.py"],
        "from pip_audit._audit import audit_reports\nfrom pip_audit._service import SERVICES\n",
    )
    out = tmp_path / "out"
    result = extract_readonly(history, task, out)
    assert result["status"] == "ok-divergent"
    assert result["fix"]["sha"] == fix
    assert result["non_test_files"] == [model, interface]
    reports = [{"id": "CVE-1", "aliases": ["PYSEC-1"]}, {"id": "PYSEC-1"}]
    tree = materialize(history, base, tmp_path / "oracle", out / "solution.patch")
    nop = materialize(history, base, tmp_path / "nop")
    assert len(source(tree, model)["audit_reports"](reports)) == 1
    assert len(source(nop, model)["audit_reports"](reports)) == 2


def test_000552_least_destructive_tip_preserves_base_source(tmp_path):
    pilot = PILOTS["000552"]
    history = History(tmp_path / "git")
    model = "src/nse/NSE.py"
    short = "def actions():\n    return []\n"
    retained = "".join(f"def retained_{index}():\n    return {index}\n" for index in range(30))
    additions = (
        "def announcements(records, from_date, to_date):\n"
        '    return [record for record in records if from_date <= record["date"] <= to_date]\n'
        "def boardMeetings(records):\n    return list(records)\n"
    )
    root = history.commit({model: short, "src/nse/__init__.py": "VERSION = 0\n"}, "common history")
    base = history.commit({model: short + retained}, "base retains later source")
    history.fork("stale", root)
    fix = history.commit(
        {model: short + additions, "README.md": "announcements boardMeetings\n"},
        pilot["fix"]["subject"],
    )
    history.fork("complete", root)
    complete = history.commit(
        {model: short + retained + additions}, "retain current implementation"
    )
    history.checkout(base)
    task = package(
        tmp_path,
        pilot["task"],
        "NSE `announcements`, `boardMeetings` and `from_date` corporate endpoints.",
        ["tests/test_endpoints_new.py"],
        "from nse import NSE\n",
    )
    out = tmp_path / "out"
    result = extract_readonly(history, task, out)
    assert result["status"] == "ok-divergent"
    assert result["fix"]["sha"] == fix
    assert result["tip"] == complete
    assert result["tip_candidates_cost"][fix][0] > result["tip_candidates_cost"][complete][0]
    tree = materialize(history, base, tmp_path / "oracle", out / "solution.patch")
    namespace = source(tree, model)
    assert namespace["retained_29"]() == 29
    assert namespace["announcements"]([{"date": 2}, {"date": 4}], 1, 3) == [{"date": 2}]
    # No request to NSE or network-dependent verifier is made by this fixture.


def test_001198_import_anchor_rejects_unrelated_profile_hit(tmp_path):
    pilot = PILOTS["001198"]
    history = History(tmp_path / "git")
    root = history.commit(
        {
            f"Lib/fontbakery/{name}.py": "PROFILE = {}\n"
            for name in ("checkrunner", "configuration", "fonts_profile", "status")
        },
        "common history",
    )
    base = history.commit({"base_only.py": "PRESERVE = True\n"}, "independent base")
    history.fork("future", root)
    unrelated = history.commit(
        {
            "Lib/fontbakery/profiles/microsoft.py": 'include_profiles = ["universal", "opentype"]\n',
            "CHANGELOG.md": "profile changes\n",
        },
        pilot["fix"]["subject"],
    )
    history.checkout(base)
    task = package(
        tmp_path,
        pilot["task"],
        "Profile composition with `include_profiles` in the core runner.",
        ["tests/test_profile_composition_new.py"],
        "from fontbakery.checkrunner import CheckRunner\nfrom fontbakery.configuration import Configuration\nfrom fontbakery.fonts_profile import profile_factory\nfrom fontbakery.status import PASS\n",
    )
    out = tmp_path / "out"
    out.mkdir()
    (out / "solution.patch").write_text("stale oracle\n")
    result = extract_readonly(history, task, out)
    assert result["status"] == "no-identifiable-fix"
    assert result["fix"]["sha"] == unrelated
    assert result["anchored_paths"]["fontbakery.fonts_profile"] == "Lib/fontbakery/fonts_profile.py"
    assert result["feature_files"] == []


def test_source_only_descendant_fix_searches_refs_and_dangling_commits(tmp_path):
    history = History(tmp_path / "git")
    model = "package/core.py"
    base = history.commit({model: "def normalize(value):\n    return value\n"}, "baseline")
    history.fork("noise", base)
    history.commit({"noise.py": "DISTRACTION = 1\n"}, "unrelated dangling future")
    history.checkout(base)
    history.git("update-ref", "-d", "refs/heads/noise")
    history.fork("future", base)
    fix = history.commit(
        {
            model: "def casefold_aliases(value):\n    return value.casefold()\ndef normalize(value):\n    return casefold_aliases(value)\n"
        },
        "normalize aliases by casefold",
    )
    history.checkout(base)
    task = package(
        tmp_path,
        "source-only",
        "Normalize aliases with `casefold_aliases`.",
        ["tests/test_new.py"],
        'from package.core import normalize\nassert normalize("ALIAS") == "alias"\n',
    )
    out = tmp_path / "out"
    result = extract_readonly(history, task, out)
    assert result["status"] == "ok"
    assert result["fix"]["sha"] == fix
    assert result["n_candidates"] == 0
    assert result["n_unreachable_commits"] == 1
    assert result["strategy"].startswith("S2a:")
    tree = materialize(history, base, tmp_path / "oracle", out / "solution.patch")
    assert source(tree, model)["normalize"]("ALIAS") == "alias"


def test_patch_conflict_is_not_an_oracle_patch(tmp_path):
    history = History(tmp_path / "git")
    model, test = "package/core.py", "tests/test_core.py"
    base = history.commit({model: "VALUE = 1\n", test: "# baseline\n"}, "baseline")
    history.commit({model: "VALUE = 2\n"}, "intermediate incompatible context")
    fix = history.commit({model: "VALUE = 3\n", test: "# baseline\n# fix\n"}, "correct value")
    history.checkout(base)
    task = package(tmp_path, "conflict", "Correct value in package/core.py.", [test])
    out = tmp_path / "out"
    out.mkdir()
    (out / "solution.patch").write_text("stale oracle\n")
    result = extract_readonly(history, task, out)
    assert result["status"] == "patch-no-apply"
    assert result["fix"]["sha"] == fix
    assert result["apply_check_on_base"] is False
    assert result["apply_check_error"]


def test_tests_and_nested_harness_are_never_solution_files(tmp_path):
    history = History(tmp_path / "git")
    model, test = "package/core.py", "tests/test_core.py"
    base = history.commit({model: "VALUE = 1\n", test: "# baseline\n"}, "baseline")
    history.commit(
        {
            model: "VALUE = 2\n",
            test: "# baseline\n# fix\n",
            "test_commands.json": "{}\n",
            "mimo_test_command.sh": "exit 0\n",
            "package/test_commands.json": "{}\n",
            "package/mimo_test_command.sh": "exit 0\n",
            "package/conftest.py": "VALUE = 99\n",
            "package/testing/helpers.py": "VALUE = 99\n",
        },
        "correct value",
    )
    history.checkout(base)
    task = package(tmp_path, "exclusions", "Correct value in package/core.py.", [test])
    out = tmp_path / "out"
    result = extract_readonly(history, task, out)
    assert result["status"] == "ok"
    assert result["non_test_files"] == [model]
    assert oracle.parse_test_patch((out / "solution.patch").read_text()) == [model]


def test_no_future_fix_and_test_only_fix_remove_stale_patches(tmp_path):
    history = History(tmp_path / "git")
    test = "tests/test_core.py"
    base = history.commit({test: "# baseline\n"}, "baseline")
    task = package(tmp_path, "none", "Missing behavior with no implementation.", [test])
    out = tmp_path / "out"
    out.mkdir()
    (out / "solution.patch").write_text("stale oracle\n")
    assert extract_readonly(history, task, out)["status"] == "no-identifiable-fix"
    history.commit({test: "# baseline\n# regression\n"}, "regression tests only")
    history.checkout(base)
    (out / "solution.patch").write_text("stale oracle\n")
    assert extract_readonly(history, task, out)["status"] == "test-only-fix"


def test_empty_manual_scope_does_not_expand_to_whole_tree(tmp_path):
    history = History(tmp_path / "git")
    base = history.commit({"package/core.py": "VALUE = 1\n"}, "baseline")
    history.commit(
        {"package/core.py": "FEATURE_IDENTIFIER = 2\n", "README.md": "feature\n"}, "feature"
    )
    history.checkout(base)
    task = package(
        tmp_path, "empty-scope", "Implement `FEATURE_IDENTIFIER`.", ["tests/test_new.py"]
    )
    result = extract_readonly(history, task, tmp_path / "out", files=["README.md"])
    assert result["status"] == "test-only-fix"


def test_input_errors_are_stable_and_remove_stale_solution(tmp_path):
    history = History(tmp_path / "git")
    history.commit({"package/core.py": "VALUE = 1\n"}, "baseline")
    task = package(tmp_path, "bad-input", "behavior", ["tests/test_new.py"])
    (task / "tests/test_commands.json").write_text('{"test_commands": [null]}')
    out = tmp_path / "out"
    out.mkdir()
    (out / "solution.patch").write_text("stale oracle\n")
    assert extract_readonly(history, task, out)["status"] == "input-error"
    assert oracle.extract(task, str(tmp_path / "missing-git"), out)["status"] == "input-error"
    (task / "tests/test_commands.json").unlink()
    assert oracle.extract(task, str(tmp_path / "missing-git"), out)["status"] == "git-error"


def test_output_overlap_does_not_modify_task_input(tmp_path):
    history = History(tmp_path / "git")
    history.commit({"package/core.py": "VALUE = 1\n"}, "baseline")
    task = package(tmp_path, "overlap", "behavior", ["tests/test_new.py"])
    (task / "solution.patch").write_text("immutable source artifact\n")
    before = snapshot(task)
    assert oracle.extract(task, history.git_dir, task)["status"] == "input-error"
    assert snapshot(task) == before


def test_base_symlink_escape_is_operational_not_no_fix(tmp_path):
    history = History(tmp_path / "git")
    model, test = "package/core.py", "tests/test_core.py"
    outside = tmp_path / "outside"
    outside.write_text("immutable\n")
    (history.path / "escape").symlink_to(outside)
    base = history.commit({model: "VALUE = 1\n", test: "# baseline\n"}, "unsafe base link")
    history.commit({model: "VALUE = 2\n", test: "# baseline\n# fix\n"}, "correct value")
    history.checkout(base)
    task = package(tmp_path, "unsafe-tree", "Correct value in package/core.py.", [test])
    result = extract_readonly(history, task, tmp_path / "out")
    assert result["status"] == "unsupported-tree"
    assert outside.read_text() == "immutable\n"


def test_raw_base_ignores_archive_attributes_and_smudge_filters(tmp_path):
    history = History(tmp_path / "git")
    model, test = "package/core.py", "tests/test_core.py"
    marker = tmp_path / "filter-executed"
    base = history.commit(
        {
            model: "VALUE = 1\n",
            test: "# baseline\n",
            ".gitattributes": "package/core.py export-ignore filter=hostile\n",
        },
        "base with archive and checkout attributes",
    )
    history.commit({model: "VALUE = 2\n", test: "# baseline\n# fix\n"}, "correct value")
    history.checkout(base)
    history.git("config", "filter.hostile.smudge", f"touch {marker}")
    history.git("config", "diff.external", f"touch {marker}")
    task = package(tmp_path, "raw-checkout", "Correct value in package/core.py.", [test])
    result = extract_readonly(history, task, tmp_path / "out")
    assert result["status"] == "ok"
    assert not marker.exists()


def test_literal_manual_paths_and_quoted_diff_paths_fail_closed(tmp_path):
    history = History(tmp_path / "git")
    history.commit({"package/core.py": "VALUE = 1\n"}, "baseline")
    task = package(tmp_path, "unsafe-path", "behavior", ["tests/test_new.py"])
    out = tmp_path / "out"
    for path in ("../outside", "/absolute", ":(glob)**", ".git/config"):
        assert (
            oracle.extract(task, history.git_dir, out, files=[path])["status"] == "unsupported-path"
        )
    (task / "tests/test.patch").write_text('diff --git "a/test\\tname.py" "b/test\\tname.py"\n')
    assert oracle.extract(task, history.git_dir, out)["status"] == "unsupported-path"


def test_divergent_dangling_fix_needs_tip_not_no_fix(tmp_path):
    history = History(tmp_path / "git")
    root = history.commit({"package/core.py": "VALUE = 1\n"}, "common history")
    base = history.commit({"base_only.py": "PRESERVE = True\n"}, "independent base")
    history.fork("future", root)
    fix = history.commit({"package/core.py": "FEATURE_IDENTIFIER = 2\n"}, "implement feature")
    history.checkout(base)
    history.git("update-ref", "-d", "refs/heads/future")
    task = package(tmp_path, "needs-tip", "Implement `FEATURE_IDENTIFIER`.", ["tests/test_new.py"])
    result = extract_readonly(history, task, tmp_path / "out")
    assert result["status"] == "needs-tip-decision"
    assert result["fix"]["sha"] == fix


def test_harness_only_future_is_not_a_candidate(tmp_path):
    history = History(tmp_path / "git")
    base = history.commit(
        {
            "mimo_test_command.sh": "exit 1\n",
            "test_commands.json": '{"test_commands": []}\n',
        },
        "baseline",
    )
    history.commit(
        {
            "mimo_test_command.sh": "exit 0\n",
            "test_commands.json": "{}\n",
        },
        "changed verifier commands",
    )
    history.checkout(base)
    task = package(
        tmp_path,
        "harness-only",
        "Missing source behavior.",
        [
            "mimo_test_command.sh",
            "test_commands.json",
        ],
        "",
    )
    result = extract_readonly(history, task, tmp_path / "out")
    assert result["status"] == "no-identifiable-fix"
    assert result["task_test_files"] == []
    assert result["n_candidates"] == 0


def test_invalid_cli_evidence_target_does_not_leave_success_receipt(tmp_path, capsys):
    history = History(tmp_path / "git")
    model, test = "package/core.py", "tests/test_core.py"
    base = history.commit({model: "VALUE = 1\n", test: "# baseline\n"}, "baseline")
    history.commit({model: "VALUE = 2\n", test: "# baseline\n# fix\n"}, "correct value")
    history.checkout(base)
    task = package(tmp_path, "invalid-evidence", "Correct value in package/core.py.", [test])
    before = snapshot(task)
    out = tmp_path / "out"
    assert (
        oracle.main(
            [
                "--task",
                str(task),
                "--git-dir",
                history.git_dir,
                "--out",
                str(out),
                "--evidence",
                str(task / "instruction.md"),
            ]
        )
        == 1
    )
    assert snapshot(task) == before
    assert not (out / "solution.patch").exists()
    assert json.loads((out / "evidence.json").read_text())["status"] == "input-error"
    assert '"status": "input-error"' in capsys.readouterr().out
