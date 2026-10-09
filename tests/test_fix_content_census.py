"""Behavioural tests for the fix-content census (no Docker)."""

from __future__ import annotations

from pathlib import Path

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
    assert non_test_files(["pkg/core.py", "tests/test_core.py", "pkg/core.py"]) == [
        "pkg/core.py"
    ]


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
        "rev-list --all",
        "fsck --unreachable",
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
    (out / "caches.txt").write_text("present /root/.cache/pip\nabsent /root/.npm\n", encoding="utf-8")
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
    (out / "fix_present_pre").write_text("yes", encoding="utf-8")
    (out / "fix_present_post").write_text("no", encoding="utf-8")
    (out / "rev_count").write_text("1", encoding="utf-8")
    (out / "fsck_unreachable").write_text("0", encoding="utf-8")
    (out / "pattern_count").write_text("3", encoding="utf-8")
    (out / "blobs.txt").write_text("", encoding="utf-8")
    (out / "hit_detail.txt").write_text("", encoding="utf-8")
    (out / "fix_mtimes.txt").write_text(
        "1700000000\ta.py\n1700000000\tb.py\n", encoding="utf-8"
    )
    (out / "worktree_mtimes").write_text("1", encoding="utf-8")
    (out / "caches.txt").write_text("", encoding="utf-8")
    row = collect_result("format-code-task-000001", "Go", "abc123def456", "clean", out)
    assert row["open_leak"] == "no"
    assert row["mtime_signal"] == "no"  # normalized: single worktree stamp


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
