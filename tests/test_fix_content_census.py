"""Behavioural tests for the fix-content census (no Docker)."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from evallab.fix_content_census import (
    CSV_FIELDS,
    PROBE_SH,
    classify_location,
    collect_result,
    compose_clean_setup,
    distinctive_added_lines,
    is_test_path,
    non_test_files,
    parse_diff_added_lines,
    run_probe,
    stage_probe,
    summarize_hits,
    write_csv,
)

PATCH = """\
diff --git a/pkg/core.py b/pkg/core.py
index 1111111..2222222 100644
--- a/pkg/core.py
+++ b/pkg/core.py
@@ -1,3 +1,4 @@
+from pkg.newdep import promote_batch_shape
 def run(x):
-    return x
+    return promote_batch_shape(x)
diff --git a/tests/test_core.py b/tests/test_core.py
index 3333333..4444444 100644
--- a/tests/test_core.py
+++ b/tests/test_core.py
@@ -1 +1,2 @@
 def test_x(): pass
+def test_promote_batch_shape(): pass
"""


def test_test_paths_never_carry_fix_content() -> None:
    assert is_test_path("tests/test_core.py")
    assert is_test_path("pkg/tests/helpers.py")
    assert is_test_path("web/src/app.spec.ts")
    assert is_test_path("conftest.py")
    assert is_test_path("mimo_test_command.sh")
    assert not is_test_path("pkg/core.py")
    assert not is_test_path("src/main.go")
    assert non_test_files(["pkg/core.py", "tests/test_core.py", "pkg/core.py"]) == ["pkg/core.py"]


def test_added_lines_come_from_non_test_files_only() -> None:
    per_file = parse_diff_added_lines(PATCH)
    assert per_file["pkg/core.py"] == [
        "from pkg.newdep import promote_batch_shape",
        "    return promote_batch_shape(x)",
    ]
    lines = distinctive_added_lines(PATCH)
    assert "from pkg.newdep import promote_batch_shape" in lines
    assert "return promote_batch_shape(x)" in lines
    assert not any("test_promote_batch_shape" in line for line in lines)
    assert distinctive_added_lines(PATCH, max_lines=1) == [
        "from pkg.newdep import promote_batch_shape"
    ]


def test_short_generic_lines_are_not_distinctive() -> None:
    patch = PATCH + "+\n+    return x\n"
    lines = distinctive_added_lines(patch)
    assert "return x" not in lines


def test_locations_classify_to_routes() -> None:
    assert classify_location("/testbed/.git/objects/ab/cd", "/testbed") == "git-objects"
    assert (
        classify_location("/usr/local/lib/python3.8/site-packages/miio/x.py", "/testbed")
        == "site-packages"
    )
    assert classify_location("/testbed/build/lib/miio/x.py", "/testbed") == "installed-copies"
    assert classify_location("/testbed/pkg.egg-info/PKG-INFO", "/testbed") == "installed-copies"
    assert classify_location("/root/.cache/pip/http-v2/ab", "/testbed") == "pip-cache"
    assert classify_location("/root/.npm/_cacache/content/x", "/testbed") == "npm-cache"
    assert classify_location("/root/.cache/yarn/v6/x", "/testbed") == "yarn-cache"
    assert classify_location("/go/pkg/mod/example.com/x@v1/x.go", "/testbed") == "go-mod-cache"
    assert classify_location("/root/.cache/go-build/ab/cd", "/testbed") == "go-build-cache"
    assert classify_location("/root/.cargo/registry/src/i/x-1.0/x.rs", "/repo") == "cargo-registry"
    assert classify_location("/root/.m2/repository/a/b/x.jar", "/repo") == "maven-repo"
    assert classify_location("/tmp/work/x.py", "/testbed") == "tmp"
    assert classify_location("/opt/stage/x.py", "/testbed") == "system-dirs"
    assert classify_location("/testbed/pkg/core.py", "/testbed") == "worktree"
    assert classify_location("/workspace/repo/target/debug/x", "/workspace/repo") == "target-dir"


def test_probe_covers_all_stages() -> None:
    for token in (
        "cat-file -t",
        "fix_present_pre",
        "setup.sh",
        "fix_present_post",
        "git_hidden",
        "rev-list --all",
        "fsck --unreachable",
        "patterns_raw",
        "grep -ohF",
        "grep -rlF",
        "hit_detail",
        "fix_mtimes",
        "worktree_mtimes",
        "caches.txt",
        "setup_rc",
    ):
        assert token in PROBE_SH
    assert "census-stage" in PROBE_SH
    assert ".git" in PROBE_SH  # the content scan excludes the object store


def test_stage_probe_lays_out_setup_and_probe(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "files").mkdir(parents=True)
    (source / "setup.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (source / "files" / "blocklist").write_text("x\n", encoding="utf-8")
    stage = tmp_path / "stage"
    stage_probe(stage, source, b"#!/bin/bash\n# clean\n")
    assert (stage / "probe.sh").read_text(encoding="utf-8").startswith("#!/bin/bash")
    assert (stage / "setup" / "setup.sh").read_bytes() == b"#!/bin/bash\n# clean\n"
    assert (stage / "setup" / "files" / "blocklist").is_file()
    assert (stage / "patterns.awk").read_text(encoding="utf-8")


def test_clean_chain_composes_in_order() -> None:
    # mimo_harbor-shaped Code setup (literal anchor lines, as in
    # test_strip_future_history) so the composition pins the real contract.
    root = (
        "#!/bin/bash\nfail() { exit 1; }\n"
        "write_blocklist() { :; }\n"
        'echo "$BASE" > "$M/base"\n'
        "# Images are built with history truncated at the base. If one is not, the fix could be read out of git log,\n"
        "# so .git is hidden while the agent works and put back for grading.\n"
        'LATER=$(git rev-list --all --not "$BASE" 2>/dev/null | head -n 5 | grep -c . || true)\n'
        "write_blocklist\n"
        'if [ "$LATER" -gt 0 ]; then\n'
        '  mv "$CWD/.git" "$M/git-hidden"\n'
        '  echo "history not truncated at ${BASE:0:12}: .git hidden while the agent works"\n'
        "fi\n"
        'touch "$M/ready"\n'
    )
    text, applied = compose_clean_setup(root, with_purge=True)
    assert applied == [
        "strip-future-history@1",
        "purge-installed-copies@1",
        "purge-build-caches@2",
        "mtime-normalize@1",
    ]
    order = [
        text.index("strip-future-history@1"),
        text.index("purge-installed-copies@1"),
        text.index("purge-build-caches@2"),
        text.index("mtime-normalize@1"),
    ]
    assert order == sorted(order)
    _, applied_plain = compose_clean_setup(root, with_purge=False)
    assert "purge-installed-copies@1" not in applied_plain


def test_collect_result_flags_open_leaks(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "workdir").write_text("/testbed", encoding="utf-8")
    (out / "fix").write_text("abc123", encoding="utf-8")
    (out / "setup_rc").write_text("0", encoding="utf-8")
    (out / "ready").write_text("yes", encoding="utf-8")
    (out / "docker_rc").write_text("0", encoding="utf-8")
    (out / "fix_present_pre").write_text("yes", encoding="utf-8")
    (out / "fix_present_post").write_text("no", encoding="utf-8")
    (out / "rev_count").write_text("1", encoding="utf-8")
    (out / "fsck_unreachable").write_text("0", encoding="utf-8")
    (out / "pattern_count").write_text("3", encoding="utf-8")
    (out / "blobs.txt").write_text("deadbeef\tpkg/core.py\n", encoding="utf-8")
    (out / "hit_detail.txt").write_text(
        "/usr/local/lib/python3.8/site-packages/pkg/core.py\t2\tdeadbeef  /x\n",
        encoding="utf-8",
    )
    (out / "fix_mtimes.txt").write_text("1700000000\tpkg/core.py\n", encoding="utf-8")
    (out / "worktree_mtimes").write_text("5", encoding="utf-8")
    (out / "caches.txt").write_text(
        "present /root/.cache/pip\nabsent /root/.npm\n", encoding="utf-8"
    )
    row = collect_result("format-code-task-000001", "Go", "abc123def456", "clean", out)
    assert row["open_leak"] == "yes"
    assert row["hits_total"] == 1
    assert row["hits_by_route"] == '{"site-packages": 1}'
    assert row["blob_matches"] == '["/usr/local/lib/python3.8/site-packages/pkg/core.py"]'
    assert row["mtime_signal"] == "no"  # single fix file: no cluster verdict
    assert row["fix_sha"] == "abc123"


def test_collect_result_clean_row_has_no_leak(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "workdir").write_text("/testbed", encoding="utf-8")
    (out / "fix").write_text("abc123", encoding="utf-8")
    (out / "setup_rc").write_text("0", encoding="utf-8")
    (out / "ready").write_text("yes", encoding="utf-8")
    (out / "docker_rc").write_text("0", encoding="utf-8")
    (out / "scan_rc").write_text("1", encoding="utf-8")
    (out / "fix_present_pre").write_text("yes", encoding="utf-8")
    (out / "fix_present_post").write_text("no", encoding="utf-8")
    (out / "rev_count").write_text("1", encoding="utf-8")
    (out / "fsck_unreachable").write_text("0", encoding="utf-8")
    (out / "pattern_count").write_text("3", encoding="utf-8")
    (out / "blobs.txt").write_text("", encoding="utf-8")
    (out / "hit_detail.txt").write_text("", encoding="utf-8")
    (out / "fix_mtimes.txt").write_text("1700000000\ta.py\n1700000000\tb.py\n", encoding="utf-8")
    (out / "worktree_mtimes").write_text("1", encoding="utf-8")
    (out / "caches.txt").write_text("", encoding="utf-8")
    row = collect_result("format-code-task-000001", "Go", "abc123def456", "clean", out)
    assert row["open_leak"] == "no"
    assert row["mtime_signal"] == "no"  # normalized: single worktree stamp
    assert row["scan_complete"] is True
    assert row["probe_rc"] == "0"


@pytest.mark.parametrize(
    ("scan_rc", "complete"),
    [
        (None, False),
        ("0", True),
        ("1", True),
        ("no-patterns", True),
        ("2", False),
        ("124", False),
        ("137", False),
    ],
)
def test_collect_result_preserves_scan_completion_evidence(
    tmp_path: Path, scan_rc: str | None, complete: bool
) -> None:
    if scan_rc is not None:
        (tmp_path / "scan_rc").write_text(scan_rc, encoding="utf-8")
    (tmp_path / "modal_rc").write_text("0", encoding="utf-8")
    row = collect_result("task", "python", "image", "clean", tmp_path)
    assert row["hits_total"] == 0
    assert row["scan_complete"] is complete
    assert row["probe_rc"] == "0"
    assert ("content scan incomplete" in row["notes"]) is not complete


def test_summarize_counts_routes() -> None:
    hits = [{"path": "/tmp/x.py"}, {"path": "/tmp/y.py"}]
    counts, annotated = summarize_hits(hits, "/testbed")
    assert counts == {"tmp": 2}
    assert all(h["route"] == "tmp" for h in annotated)


def test_write_csv_drops_nested_hits(tmp_path: Path) -> None:
    dest = tmp_path / "census.csv"
    write_csv(
        [
            {
                "task_id": "t",
                "mode": "clean",
                "open_leak": "no",
                "hits": [{"path": "/x"}],
            }
        ],
        dest,
    )
    text = dest.read_text(encoding="utf-8")
    assert text.splitlines()[0].split(",") == list(CSV_FIELDS)
    assert "/x" not in text


def test_patterns_awk_matches_host_classifier(tmp_path: Path) -> None:
    """The staged awk selects the same lines as distinctive_added_lines."""
    import subprocess

    from evallab.fix_content_census import PATTERNS_AWK

    diff = tmp_path / "fix.diff"
    diff.write_text(PATCH, encoding="utf-8")
    awk = tmp_path / "patterns.awk"
    awk.write_text(PATTERNS_AWK, encoding="utf-8")
    proc = subprocess.run(
        ["awk", "-f", str(awk), str(diff)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert sorted(proc.stdout.splitlines()) == sorted(distinctive_added_lines(PATCH))


def test_recover_fix_lite_finds_continuous_toucher(tmp_path: Path) -> None:
    """Lite S1 on a fixture repo: first continuous test+source toucher wins."""
    import shutil
    import subprocess

    from evallab.fix_content_census import recover_fix_lite

    if shutil.which("git") is None:
        pytest.skip("needs git")
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (repo / "tests" / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env, timeout=60)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env, timeout=60)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "base"], check=True, env=env, timeout=60
    )
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
        timeout=60,
    ).stdout.strip()
    (repo / "src" / "a.py").write_text(
        "def a():\n    return promote_batch_shape(1)\n", encoding="utf-8"
    )
    (repo / "tests" / "test_a.py").write_text(
        "def test_a(): pass\ndef test_promote(): pass\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env, timeout=60)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "fix"],
        check=True,
        env=env,
        timeout=60,
    )
    fix = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
        timeout=60,
    ).stdout.strip()
    found = recover_fix_lite(repo / ".git", base, ["tests/test_a.py"])
    assert found.get("sha") == fix
    assert recover_fix_lite(repo / ".git", base, ["tests/missing.py"])["status"] == "no-test-blobs"
    assert recover_fix_lite(repo / ".git", fix, ["tests/test_a.py"])["status"] == "no-candidate"


def test_rendered_probe_parses_as_shell(tmp_path: Path) -> None:
    """The staged probe must parse: a dropped loop closer kills every run."""
    import shutil
    import subprocess

    from evallab.fix_content_census import PROBE_SH

    if shutil.which("bash") is None:
        pytest.skip("needs bash")
    script = tmp_path / "probe.sh"
    script.write_text(PROBE_SH, encoding="utf-8")
    proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_stage_probe_precomputed_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source).mkdir(parents=True)
    (source / "setup.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    stage = tmp_path / "stage"
    stage_probe(
        stage,
        source,
        None,
        precomputed={
            "patterns": ["from pkg.newdep import promote_batch_shape"],
            "files": ["pkg/core.py"],
            "fix_source": "known-patch:oracle/task-1",
        },
    )
    assert (stage / "patterns.pre").read_text(encoding="utf-8").splitlines() == [
        "from pkg.newdep import promote_batch_shape"
    ]
    assert (stage / "files.pre").read_text(encoding="utf-8").splitlines() == ["pkg/core.py"]
    assert (stage / "fix_source").read_text(encoding="utf-8") == "known-patch:oracle/task-1"
    assert "patterns.pre" in PROBE_SH


def test_collect_result_precomputed_mode(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "workdir").write_text("/testbed", encoding="utf-8")
    (out / "fix").write_text("", encoding="utf-8")
    (out / "fix_source").write_text("known-patch:oracle/task-1", encoding="utf-8")
    (out / "fix_present_pre").write_text("precomputed", encoding="utf-8")
    (out / "fix_present_post").write_text("no", encoding="utf-8")
    (out / "setup_rc").write_text("0", encoding="utf-8")
    (out / "ready").write_text("yes", encoding="utf-8")
    (out / "docker_rc").write_text("0", encoding="utf-8")
    (out / "pattern_count").write_text("2", encoding="utf-8")
    (out / "pattern_raw_count").write_text("2", encoding="utf-8")
    (out / "blobs.txt").write_text("", encoding="utf-8")
    (out / "hit_detail.txt").write_text(
        "/testbed/pkg/installed.py\t1\tabc123  /testbed/pkg/installed.py\n", encoding="utf-8"
    )
    (out / "fix_mtimes.txt").write_text("", encoding="utf-8")
    (out / "worktree_mtimes").write_text("1", encoding="utf-8")
    (out / "caches.txt").write_text("", encoding="utf-8")
    row = collect_result("task-1", "python", "abc123def456", "clean", out)
    assert row["open_leak"] == "yes"
    assert row["hits_total"] == 1
    assert row["fix_source"] == "known-patch:oracle/task-1"
    assert any("known patch" in note for note in row["notes"].split("; "))


class _FakeStream:
    """Modal exec stream stub: ``read()`` returns the whole chunk."""

    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


class _FakeProc:
    """Modal container-process stub with a fixed exit code and streams."""

    def __init__(self, rc: int, stdout: str = "", stderr: str = "") -> None:
        self._rc = rc
        self._stdout = stdout
        self._stderr = stderr

    @property
    def stdout(self) -> _FakeStream:
        return _FakeStream(self._stdout)

    @property
    def stderr(self) -> _FakeStream:
        return _FakeStream(self._stderr)

    def wait(self) -> int:
        return self._rc


class _FakeFilesystem:
    """Modal ``sandbox.filesystem`` stub (write_bytes/read_bytes)."""

    def __init__(self, sandbox: _FakeSandbox) -> None:
        self._sandbox = sandbox

    def write_bytes(self, data: bytes, remote_path: str) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._sandbox.remote[remote_path] = bytes(data)

    def read_bytes(self, remote_path: str) -> bytes:
        return self._sandbox.remote[remote_path]

    def make_directory(self, remote_path: str, create_parents: bool = True) -> None:
        self._sandbox.remote.setdefault(remote_path + "/.dir", b"")


class _FakeSandbox:
    """Modal sandbox stub: records execs, serves a fake remote filesystem."""

    def __init__(
        self,
        probe_outputs: dict | None = None,
        probe_rc: int = 0,
        fail_on: tuple = (),
        terminate_error: Exception | None = None,
    ) -> None:
        self.remote: dict = {}
        self.exec_calls: list = []
        self.terminated = False
        self.object_id = "sb-fake123"
        self.terminate_error = terminate_error
        self.probe_argv: list | None = None
        self.probe_outputs = dict(probe_outputs or {})
        self.probe_rc = probe_rc
        self.fail_on = tuple(fail_on)
        self.filesystem = _FakeFilesystem(self)

    def exec(self, *argv: str, workdir: str | None = None, timeout: int | None = None):
        self.exec_calls.append({"argv": list(argv), "workdir": workdir, "timeout": timeout})
        if argv[0] in self.fail_on:
            raise RuntimeError(f"fake {argv[0]} failure")
        if argv[0] == "mkdir":
            return _FakeProc(0)
        if list(argv[:2]) == ["bash", "/census-stage/probe.sh"]:
            self.probe_argv = list(argv)
            self.remote.update(self.probe_outputs)
            return _FakeProc(self.probe_rc, stdout="probe stdout\n")
        if argv[0] == "find":
            names = sorted(p for p in self.remote if p.startswith("/census-out/"))
            return _FakeProc(0, stdout="".join(f"{name}\n" for name in names))
        raise AssertionError(f"unexpected fake exec: {argv!r}")

    def terminate(self) -> None:
        self.terminated = True
        if self.terminate_error is not None:
            raise self.terminate_error


def _install_fake_modal(monkeypatch: pytest.MonkeyPatch, sandbox: _FakeSandbox) -> dict:
    """Inject a fake ``modal`` SDK module recording App/Image/Sandbox calls."""
    record: dict = {}
    fake = types.ModuleType("modal")

    class App:
        @staticmethod
        def lookup(name: str, create_if_missing: bool = False, **kwargs: object):
            record["lookup_name"] = name
            record["create_if_missing"] = create_if_missing
            return ("fake-app", name)

    class Image:
        @staticmethod
        def from_registry(image: str, **kwargs: object):
            # Real SDK takes no platform kwarg; Modal requires linux/amd64.
            record["image"] = image
            record["kwargs"] = dict(kwargs)
            return ("fake-image", image)

    class SandboxNS:
        @staticmethod
        def create(*args: object, **kwargs: object) -> _FakeSandbox:
            record["create_args"] = args
            record["create_kwargs"] = kwargs
            return sandbox

    fake.App = App  # type: ignore[attr-defined]
    fake.Image = Image  # type: ignore[attr-defined]
    fake.Sandbox = SandboxNS  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "modal", fake)
    return record


def _write_probe_outputs() -> dict:
    """Canned ``/census-out`` bytes: positive control with one open leak."""
    return {
        "/census-out/workdir": b"/testbed",
        "/census-out/fix": b"",
        "/census-out/fix_source": b"known-patch:oracle/task-m",
        "/census-out/fix_present_pre": b"precomputed",
        "/census-out/fix_present_post": b"no",
        "/census-out/base": b"abc123",
        "/census-out/setup_rc": b"0",
        "/census-out/ready": b"yes",
        "/census-out/scan_rc": b"0",
        "/census-out/pattern_count": b"1",
        "/census-out/pattern_raw_count": b"1",
        "/census-out/blobs.txt": b"",
        "/census-out/hit_detail.txt": (
            b"/testbed/pkg/installed.py\t1\tabc123  /testbed/pkg/installed.py\n"
        ),
        "/census-out/fix_mtimes.txt": b"",
        "/census-out/worktree_mtimes": b"1",
        "/census-out/caches.txt": b"",
    }


def test_run_probe_rejects_unknown_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def _no_docker(*args: object, **kwargs: object):
        raise AssertionError("no runner may launch on an unknown backend")

    monkeypatch.setattr(subprocess, "run", _no_docker)
    with pytest.raises(ValueError, match="backend"):
        run_probe("img", "/testbed", "abc", tmp_path / "stage", tmp_path / "out", backend="nope")


def test_run_probe_docker_default_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess
    import types as std_types

    seen: dict = {}

    def _fake_run(cmd: object, **kwargs: object):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return std_types.SimpleNamespace(returncode=0, stdout="o" * 10, stderr="e" * 5)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    out = tmp_path / "out"
    run_probe("img:tag", "/testbed", "abc123", tmp_path / "stage", out)
    assert seen["cmd"] == [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "--network",
        "none",
        "--user",
        "root",
        "-v",
        f"{tmp_path / 'stage'}:/census-stage:ro",
        "-v",
        f"{out}:/census-out",
        "img:tag",
        "bash",
        "/census-stage/probe.sh",
        "/testbed",
        "abc123",
    ]
    assert (out / "docker_rc").read_text(encoding="utf-8") == "0"
    assert (out / "docker_stdout.txt").read_text(encoding="utf-8") == "o" * 10
    assert (out / "docker_stderr.txt").read_text(encoding="utf-8") == "e" * 5
    assert not (out / "modal_rc").exists()


def test_run_probe_modal_replays_same_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "setup.sh").write_text("#!/bin/bash\necho setup\n", encoding="utf-8")
    stage = tmp_path / "stage"
    stage_probe(
        stage,
        source,
        None,
        precomputed={
            "patterns": ["from pkg.newdep import promote_batch_shape"],
            "files": ["pkg/core.py"],
            "fix_source": "known-patch:oracle/task-m",
        },
    )
    sandbox = _FakeSandbox(probe_outputs=_write_probe_outputs())
    record = _install_fake_modal(monkeypatch, sandbox)
    out = tmp_path / "out"
    run_probe(
        "docker.io/x/mimo@sha256:deadbeef",
        "/testbed",
        "",
        stage,
        out,
        backend="modal",
    )
    # Same pinned image, same workdir, root sandbox, no network.
    # The named billed app is looked up (never an ephemeral App) for exact
    # slice attribution. Modal requires linux/amd64 registry images and
    # takes no platform kwarg (modal>=1.5).
    assert record["lookup_name"] == "mimo-clean-census-fix"
    assert record["create_if_missing"] is True
    assert record["image"] == "docker.io/x/mimo@sha256:deadbeef"
    assert record.get("kwargs", {}) == {}
    assert record["create_kwargs"]["workdir"] == "/testbed"
    assert record["create_kwargs"]["block_network"] is True
    assert record["create_kwargs"]["app"] == ("fake-app", "mimo-clean-census-fix")
    assert record["create_kwargs"]["image"] == (
        "fake-image",
        "docker.io/x/mimo@sha256:deadbeef",
    )
    # The actual staged payload (not a re-derived approximation) is uploaded.
    assert sandbox.remote["/census-stage/probe.sh"] == PROBE_SH.encode("utf-8")
    assert sandbox.remote["/census-stage/setup/setup.sh"] == b"#!/bin/bash\necho setup\n"
    assert b"promote_batch_shape" in sandbox.remote["/census-stage/patterns.pre"]
    # The genuine probe runs with the same workdir/fix arguments.
    assert sandbox.probe_argv == ["bash", "/census-stage/probe.sh", "/testbed", ""]
    # Stdout/stderr evidence and rc are retained under modal_* names.
    assert (out / "modal_rc").read_text(encoding="utf-8") == "0"
    assert "probe stdout" in (out / "modal_stdout.txt").read_text(encoding="utf-8")
    # Every probe output file is downloaded byte-for-byte.
    assert (out / "fix_present_pre").read_text(encoding="utf-8") == "precomputed"
    assert (out / "hit_detail.txt").read_bytes() == (
        b"/testbed/pkg/installed.py\t1\tabc123  /testbed/pkg/installed.py\n"
    )
    assert not (out / "docker_rc").exists()
    # Lifecycle evidence attributes the exact billed app and sandbox.
    runtime = json.loads((out / "probe-runtime.json").read_text(encoding="utf-8"))
    assert runtime["backend"] == "modal"
    assert runtime["app_name"] == "mimo-clean-census-fix"
    assert runtime["sandbox_id"] == "sb-fake123"
    assert runtime["image"] == "docker.io/x/mimo@sha256:deadbeef"
    assert runtime["workdir"] == "/testbed"
    assert runtime["started_at"] <= runtime["ended_at"]
    assert not (out / "modal_terminate_error.txt").exists()
    # The published positive control is consumed exactly like a Docker run.
    row = collect_result("task-m", "python", "deadbeef", "clean", out)
    assert row["hits_total"] == 1
    assert row["open_leak"] == "yes"
    assert any("known patch" in note for note in row["notes"].split("; "))
    assert "docker rc" not in row["notes"]
    assert sandbox.terminated is True


def test_run_probe_modal_terminates_sandbox_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "probe.sh").write_text(PROBE_SH, encoding="utf-8")
    sandbox = _FakeSandbox(probe_outputs=_write_probe_outputs(), fail_on=("bash",))
    _install_fake_modal(monkeypatch, sandbox)
    out = tmp_path / "out"
    with pytest.raises(RuntimeError, match="fake bash failure"):
        run_probe("img", "/testbed", "", stage, out, backend="modal")
    assert sandbox.terminated is True
    # No fabricated probe evidence on orchestration failure, but the billed
    # sandbox lifecycle is still recorded.
    assert not (out / "modal_rc").exists()
    assert not (out / "hit_detail.txt").exists()
    runtime = json.loads((out / "probe-runtime.json").read_text(encoding="utf-8"))
    assert runtime["sandbox_id"] == "sb-fake123"
    assert runtime["app_name"] == "mimo-clean-census-fix"


def test_run_probe_egress_lock_false_opens_both_backends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess
    import types as std_types

    seen: dict = {}

    def _fake_run(cmd: object, **kwargs: object):
        seen["cmd"] = cmd
        return std_types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    out = tmp_path / "out-docker"
    run_probe("img:tag", "/testbed", "abc", tmp_path / "stage", out, egress_lock=False)
    assert "--network" not in seen["cmd"]
    assert "none" not in seen["cmd"]

    stage = tmp_path / "stage-modal"
    stage.mkdir()
    (stage / "probe.sh").write_text(PROBE_SH, encoding="utf-8")
    sandbox = _FakeSandbox(probe_outputs=_write_probe_outputs())
    record = _install_fake_modal(monkeypatch, sandbox)
    out = tmp_path / "out-modal"
    run_probe("img", "/testbed", "", stage, out, backend="modal", egress_lock=False)
    assert record["create_kwargs"]["block_network"] is False
    assert (out / "modal_rc").read_text(encoding="utf-8") == "0"
    assert sandbox.terminated is True
    runtime = json.loads((out / "probe-runtime.json").read_text(encoding="utf-8"))
    assert runtime["sandbox_id"] == "sb-fake123"
    assert runtime["app_name"] == "mimo-clean-census-fix"


def test_run_probe_modal_terminate_failure_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "probe.sh").write_text(PROBE_SH, encoding="utf-8")
    sandbox = _FakeSandbox(
        probe_outputs=_write_probe_outputs(),
        terminate_error=RuntimeError("fake terminate denied"),
    )
    _install_fake_modal(monkeypatch, sandbox)
    out = tmp_path / "out"
    # A failed termination may mean continued billing: it must surface, with
    # the evidence retained, not be suppressed.
    with pytest.raises(RuntimeError, match="fake terminate denied"):
        run_probe("img", "/testbed", "", stage, out, backend="modal")
    assert "fake terminate denied" in (out / "modal_terminate_error.txt").read_text(
        encoding="utf-8"
    )
    # The genuine probe results still landed before the termination failure.
    assert (out / "modal_rc").read_text(encoding="utf-8") == "0"
    assert (out / "probe-runtime.json").is_file()


def test_run_probe_modal_terminate_failure_keeps_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "probe.sh").write_text(PROBE_SH, encoding="utf-8")
    sandbox = _FakeSandbox(
        probe_outputs=_write_probe_outputs(),
        fail_on=("bash",),
        terminate_error=RuntimeError("fake terminate denied"),
    )
    _install_fake_modal(monkeypatch, sandbox)
    out = tmp_path / "out"
    with pytest.raises(RuntimeError, match="fake bash failure"):
        run_probe("img", "/testbed", "", stage, out, backend="modal")
    assert "fake terminate denied" in (out / "modal_terminate_error.txt").read_text(
        encoding="utf-8"
    )


def test_run_probe_modal_requires_sdk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "modal", None)
    with pytest.raises(RuntimeError, match="Modal SDK"):
        run_probe("img", "/testbed", "", tmp_path / "stage", tmp_path / "out", backend="modal")


def test_collect_result_runner_rc_notes(tmp_path: Path) -> None:
    def _out_with(rc_name: str, rc: str) -> Path:
        out = tmp_path / rc_name
        out.mkdir(exist_ok=True)
        (out / "workdir").write_text("/testbed", encoding="utf-8")
        (out / "fix_present_pre").write_text("yes", encoding="utf-8")
        (out / "fix_present_post").write_text("no", encoding="utf-8")
        (out / "setup_rc").write_text("0", encoding="utf-8")
        (out / "ready").write_text("yes", encoding="utf-8")
        (out / rc_name).write_text(rc, encoding="utf-8")
        return out

    assert (
        "modal_rc rc=3"
        in collect_result("t", "python", "img", "clean", _out_with("modal_rc", "3"))["notes"]
    )
    assert (
        "docker rc"
        not in collect_result("t", "python", "img", "clean", _out_with("modal_rc", "0"))["notes"]
    )
    assert (
        "docker_rc rc=1"
        in collect_result("t", "python", "img", "clean", _out_with("docker_rc", "1"))["notes"]
    )
