"""HAR-177 leak scan: behavioural tests on synthetic fixtures (no network)."""

from __future__ import annotations

import csv
import gzip
import importlib.util
import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_SCAN = REPO_ROOT / "research" / "experiments" / "har177-leak-scan" / "scan.py"


def load_scan():
    spec = importlib.util.spec_from_file_location("har177_leak_scan", _SCAN)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scan = load_scan()

DOCKERFILE = (
    "# The task's image, pinned by digest.\n"
    "FROM docker.io/xiaomimimo/mimo-v2.6-rl-oss"
    "@sha256:e4feae817a2d6660ebb8ea03f3487af0acaa2431fd2cf3763738395d67a76709\n"
)
SETUP = "#!/bin/bash\nCWD=/testbed\ngit rev-parse HEAD\n"


def test_parse_image_digest() -> None:
    """The Dockerfile digest is the scan's image identity."""
    assert scan.parse_image_digest(DOCKERFILE) == (
        "sha256:e4feae817a2d6660ebb8ea03f3487af0acaa2431fd2cf3763738395d67a76709"
    )
    assert scan.parse_image_digest("FROM ubuntu:22.04\n") is None


def test_parse_setup_cwd() -> None:
    """The setup working directory locates the in-image repo; default /testbed."""
    assert scan.parse_setup_cwd(SETUP) == "/testbed"
    assert scan.parse_setup_cwd("#!/bin/bash\necho hi\n") == "/testbed"


def test_parse_manifest_single_layer() -> None:
    """Manifest parsing keeps the layer list the streamer walks."""
    body = (
        b'{"schemaVersion":2,"config":{"digest":"sha256:aaaa","size":1},'
        b'"layers":[{"digest":"sha256:bbbb","size":2}]}'
    )
    assert scan.parse_manifest(body) == {"config": "sha256:aaaa", "layers": ["sha256:bbbb"]}
    try:
        scan.parse_manifest(b'{"schemaVersion":2,"layers":[]}')
    except ValueError:
        pass
    else:
        raise AssertionError("layerless manifest must raise")


def test_sanitize_subject_keeps_metadata_only() -> None:
    """Subjects are one line and never break the ; separated column."""
    assert scan.sanitize_subject("fix\nthe\nthing") == "fix the thing"
    assert scan.sanitize_subject("a;b|c") == "a,b/c"
    assert len(scan.sanitize_subject("x" * 500)) == 200


def test_verdict_values() -> None:
    """The exact CSV vocabulary the verdict consumer reads."""
    assert scan.verdict(True) == ("yes", "true")
    assert scan.verdict(False) == ("no", "false")
    assert scan.verdict(None) == ("unscanned", "false")


def test_build_row_leak_on_ref() -> None:
    """Beyond-base commits mean yes/true with the ref flag set."""
    row = scan.build_row(
        task_id="format-code-task-1",
        run="original",
        ledger_digest="sha256:x",
        image_digest="sha256:y",
        base_commit="a" * 40,
        beyond=3,
        unreachable=0,
        samples=[("b" * 40, "fix the leak")],
        repo_path="testbed",
        method="mirror.gcr.io-stream",
    )
    assert (row["has_future_history"], row["needs_repair"], row["on_ref"]) == (
        "yes",
        "true",
        "yes",
    )
    assert row["sample_shas"] == "b" * 40
    assert row["sample_subjects"] == "fix the leak"


def test_build_row_clean_and_error() -> None:
    """No post-base history is no/false; failures are unscanned, never silent."""
    clean = scan.build_row(
        task_id="t",
        run="original",
        ledger_digest="d",
        image_digest="i",
        beyond=0,
        unreachable=0,
    )
    assert (clean["has_future_history"], clean["needs_repair"], clean["on_ref"]) == (
        "no",
        "false",
        "no",
    )
    missing_git = scan.build_row(
        task_id="t",
        run="original",
        ledger_digest="d",
        image_digest="i",
        git_present=False,
    )
    assert missing_git["has_future_history"] == "no"
    broken = scan.build_row(
        task_id="t",
        run="original",
        ledger_digest="d",
        image_digest="i",
        error="RuntimeError: boom",
    )
    assert (broken["has_future_history"], broken["needs_repair"], broken["on_ref"]) == (
        "unscanned",
        "false",
        "unknown",
    )


def test_build_row_caps_samples() -> None:
    """At most five sample SHAs/subjects land in the CSV."""
    samples = [(f"{n:040x}", f"subject {n}") for n in range(9)]
    row = scan.build_row(
        task_id="t",
        run="original",
        ledger_digest="d",
        image_digest="i",
        beyond=9,
        samples=samples,
    )
    assert len(row["sample_shas"].split(";")) == 5
    assert len(row["sample_subjects"].split(";")) == 5


def _layer_bytes(members: list[tuple[str, bytes | None]]) -> io.BytesIO:
    """In-memory gzip layer; ``None`` body means a directory entry."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        for name, body in members:
            info = tarfile.TarInfo(name)
            if body is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
    buf.seek(0)
    return buf


def test_tar_git_root_routing() -> None:
    """Only <root>/.git members route to the extractor."""
    assert scan.tar_git_root("testbed/.git/HEAD") == ("testbed", "HEAD")
    assert scan.tar_git_root("./testbed/.git/packed-refs") == ("testbed", "packed-refs")
    assert scan.tar_git_root("/testbed/.git/HEAD") == ("testbed", "HEAD")
    assert scan.tar_git_root(".git/HEAD") == ("", "HEAD")
    assert scan.tar_git_root("testbed/work.py") is None
    assert scan.tar_git_root("testbed/.github/workflows/x.yml") is None


def test_extract_git_subset_blocks_traversal(tmp_path: Path) -> None:
    """A ../ member never escapes the scratch dir; the good files still land."""
    layer = _layer_bytes(
        [
            ("testbed/.git/HEAD", b"abc\n"),
            ("testbed/.git/../../evil.txt", b"evil\n"),
        ]
    )
    dest = tmp_path / "git"
    dest.mkdir()
    roots, _ = scan.extract_git_subset(layer, str(dest))
    assert roots == {"testbed": (1, len(b"abc\n"))}
    assert not (tmp_path / "evil.txt").exists()


def test_extract_git_subset_skips_worktree(tmp_path: Path) -> None:
    """The streamer reads only what it needs: .git files land, worktree does not."""
    layer = _layer_bytes(
        [
            ("testbed/.git/", None),
            ("testbed/.git/HEAD", b"ref: refs/heads/master\n"),
            ("testbed/.git/packed-refs", b"# pack-refs\n"),
            ("testbed/work.py", b"print('solution')\n"),
            ("testbed/tests/test_x.py", b"def test_x(): pass\n"),
        ]
    )
    dest = tmp_path / "git"
    dest.mkdir()
    roots, nbytes = scan.extract_git_subset(layer, str(dest))
    assert roots == {"testbed": (2, nbytes)}
    assert nbytes > 0
    assert (dest / "testbed" / "HEAD").is_file()
    assert not (tmp_path / "git" / "testbed" / "work.py").exists()
    assert list(dest.rglob("*.py")) == []


def test_extract_git_subset_empty_layer(tmp_path: Path) -> None:
    """A layer without .git reports no roots instead of failing."""
    layer = _layer_bytes([("testbed/work.py", b"x\n")])
    assert scan.extract_git_subset(layer, str(tmp_path)) == ({}, 0)


def test_extract_git_subset_reports_all_roots(tmp_path: Path) -> None:
    """Independent repos elsewhere in the layer are reported, not merged."""
    layer = _layer_bytes(
        [
            ("testbed/.git/HEAD", b"a\n"),
            ("opt/other/.git/HEAD", b"b\n"),
            ("opt/other/.git/index", b"c\n"),
        ]
    )
    dest = tmp_path / "git"
    dest.mkdir()
    roots, _ = scan.extract_git_subset(layer, str(dest))
    assert roots == {"testbed": (1, 2), "opt/other": (2, 4)}
    assert (dest / "opt_other" / "HEAD").is_file()


def _git_repo(path: Path, history: list[str]) -> dict[str, str]:
    """Tiny git fixture; returns {label: sha} for each committed label."""
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}
    path.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> str:
        proc = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=path,
            env=env,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    git("init", "-q", "-b", "master")
    git("config", "user.email", "test@localhost")
    git("config", "user.name", "test")
    shas: dict[str, str] = {}
    for label in history:
        (path / "file.txt").write_text(label + "\n")
        git("add", "-A")
        git(
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "-q",
            "-m",
            f"subject {label}",
        )
        shas[label] = git("rev-parse", "HEAD")
    return shas


def test_analyze_git_dir_history_on_ref(tmp_path: Path) -> None:
    """A branch past HEAD counts as beyond-base (the setup.sh question)."""
    shas = _git_repo(tmp_path / "repo", ["base", "fix"])
    repo = tmp_path / "repo"
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(
        ["git", "branch", "future"], cwd=repo, env=env, check=True, capture_output=True, timeout=60
    )
    subprocess.run(
        ["git", "checkout", "-q", shas["base"]],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        timeout=60,
    )
    analysis = scan.analyze_git_dir(str(repo / ".git"))
    assert analysis["base"] == shas["base"]
    assert analysis["beyond"] == 1
    assert analysis["samples"][0][0] == shas["fix"]
    assert analysis["samples"][0][1] == "subject fix"


def test_analyze_git_dir_unreachable_only(tmp_path: Path) -> None:
    """A reset-away fix is unreachable but still future history."""
    shas = _git_repo(tmp_path / "repo", ["base", "fix"])
    repo = tmp_path / "repo"
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(
        ["git", "reset", "-q", "--hard", shas["base"]],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        timeout=60,
    )
    analysis = scan.analyze_git_dir(str(repo / ".git"))
    assert analysis["beyond"] == 0
    assert analysis["unreachable"] >= 1
    assert shas["fix"] in analysis["unreachable_shas"]
    row = scan.build_row(
        task_id="t",
        run="original",
        ledger_digest="d",
        image_digest="i",
        base_commit=analysis["base"],
        beyond=analysis["beyond"],
        unreachable=analysis["unreachable"],
        samples=analysis["samples"],
    )
    assert (row["has_future_history"], row["needs_repair"], row["on_ref"]) == (
        "yes",
        "true",
        "no",
    )


def test_analyze_git_dir_clean(tmp_path: Path) -> None:
    """History cut at the base scans clean."""
    _git_repo(tmp_path / "repo", ["base"])
    analysis = scan.analyze_git_dir(str(tmp_path / "repo" / ".git"))
    assert analysis["beyond"] == 0
    assert analysis["unreachable"] == 0
    row = scan.build_row(
        task_id="t",
        run="original",
        ledger_digest="d",
        image_digest="i",
        beyond=analysis["beyond"],
        unreachable=analysis["unreachable"],
    )
    assert row["has_future_history"] == "no"


def test_git_allowlist_refuses_content_commands(tmp_path: Path) -> None:
    """The extractor can never be pointed at blobs, diffs, or file reads."""
    _git_repo(tmp_path / "repo", ["base"])
    git_dir = str(tmp_path / "repo" / ".git")
    for forbidden in (["status"], ["show", "HEAD:file.txt"], ["diff", "HEAD"]):
        try:
            scan._git(git_dir, *forbidden)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{forbidden} must be refused")


def _result(short: str, **overrides) -> dict:
    base = {
        "task_id": f"format-code-task-{short}",
        "has_future_history": "yes",
        "beyond_base_refs": "3",
        "unreachable_commits": "0",
        "error": "",
        "_unreachable_shas": [],
    }
    return base | overrides


def test_validate_result_agrees() -> None:
    """Scan rows matching HAR-161 raise no disagreement."""
    assert scan.validate_result("002391", _result("002391", beyond_base_refs="185")) == []
    row = _result(
        "002402",
        beyond_base_refs="0",
        unreachable_commits="696",
        _unreachable_shas=["56f63eb6" + "0" * 32],
    )
    assert scan.validate_result("002402", row) == []


def test_validate_result_disagrees() -> None:
    """Any mismatch with the known facts is a stop-and-report problem."""
    assert scan.validate_result("001269", _result("001269", has_future_history="no", error="boom"))
    assert scan.validate_result("002391", _result("002391", beyond_base_refs="0"))
    assert scan.validate_result(
        "001269", _result("001269", beyond_base_refs="0", unreachable_commits="7")
    )
    assert scan.validate_result(
        "002552",
        _result(
            "002552",
            beyond_base_refs="0",
            unreachable_commits="12",
            _unreachable_shas=["000000" + "0" * 34],
        ),
    )


def _package(root: Path, task_id: str, *, digest: str, cwd: str = "/testbed") -> None:
    env = root / task_id / "environment"
    (env / "setup").mkdir(parents=True)
    (env / "Dockerfile").write_text(f"FROM docker.io/xiaomimimo/mimo-v2.6-rl-oss@{digest}\n")
    (env / "setup" / "setup.sh").write_text(f"#!/bin/bash\nCWD={cwd}\n")


def test_resolve_image_original_and_variant(tmp_path: Path) -> None:
    """Digest resolution follows the run package: snapshot or variant."""
    snap = tmp_path / "tasks"
    snap.mkdir()
    digest = "sha256:" + "a" * 64
    _package(snap, "format-code-task-1", digest=digest)
    image, root, error = scan.resolve_image(
        {"task_id": "format-code-task-1", "run": "original", "run_digest": "sha256:x"},
        snapshot_dir=str(snap),
        variants_dir=str(tmp_path / "variants"),
    )
    assert (image, root, error) == (digest, "testbed", "")
    variants = tmp_path / "variants"
    vpkg = variants / "mimo-v2.6-rl__format-code-task-2" / ("b" * 12) / "environment"
    (vpkg / "setup").mkdir(parents=True)
    (vpkg / "Dockerfile").write_text(
        "FROM docker.io/xiaomimimo/mimo-v2.6-rl-oss@sha256:" + "c" * 64 + "\n"
    )
    (vpkg / "setup" / "setup.sh").write_text("#!/bin/bash\nCWD=/workspace/repo\n")
    image, root, error = scan.resolve_image(
        {"task_id": "format-code-task-2", "run": "repair", "run_digest": "sha256:" + "b" * 64},
        snapshot_dir=str(snap),
        variants_dir=str(variants),
    )
    assert image == "sha256:" + "c" * 64
    assert error == ""
    missing = scan.resolve_image(
        {"task_id": "format-code-task-9", "run": "original", "run_digest": "sha256:x"},
        snapshot_dir=str(snap),
        variants_dir=str(variants),
    )
    assert missing[0] is None and missing[2]


def test_write_csv_schema_and_roundtrip(tmp_path: Path) -> None:
    """The on-disk contract the verdict slice reads: columns and values."""
    rows = [
        scan.build_row(
            task_id="format-code-task-1",
            run="original",
            ledger_digest="sha256:x",
            image_digest="sha256:y",
            beyond=2,
            samples=[("d" * 40, "fix it")],
            method="mirror.gcr.io-stream",
        ),
        scan.build_row(
            task_id="format-code-task-2",
            run="repair",
            ledger_digest="sha256:z",
            image_digest="",
            method="",
            error="no package Dockerfile: gone",
        ),
    ]
    out = tmp_path / "leak_scan.csv"
    scan.write_csv(str(out), rows)
    with open(out, newline="") as handle:
        reader = csv.DictReader(handle)
        assert "needs_repair" in reader.fieldnames
        assert "has_future_history" in reader.fieldnames
        assert set(reader.fieldnames) == set(scan.CSV_COLUMNS)
        back = list(reader)
    assert [row["task_id"] for row in back] == ["format-code-task-1", "format-code-task-2"]
    assert [(row["has_future_history"], row["needs_repair"]) for row in back] == [
        ("yes", "true"),
        ("unscanned", "false"),
    ]
    assert all("_elapsed_s" not in row for row in back)


def test_drop_packed_refs() -> None:
    """Packed-only bad refs are filtered with their peel lines; good refs stay."""
    text = (
        "# pack-refs with: peeled fully-peeled sorted \n"
        + "a" * 40
        + " refs/heads/master\n"
        + "b" * 40
        + " refs/remotes/origin/HEAD\n"
        + "c" * 40
        + " refs/tags/v1\n"
        + "^"
        + "d" * 40
        + "\n"
    )
    out = scan.drop_packed_refs(text, {"refs/tags/v1"})
    assert "refs/heads/master" in out
    assert "refs/tags/v1" not in out
    assert "^" + "d" * 40 not in out
    assert "refs/remotes/origin/HEAD" in out


def test_quarantine_dangling_symref_keeps_unreachable(tmp_path: Path) -> None:
    """A dangling origin/HEAD no longer fails the scan; unreachable commits count."""
    shas = _git_repo(tmp_path / "repo", ["base", "fix"])
    repo = tmp_path / "repo"
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(
        ["git", "reset", "-q", "--hard", shas["base"]],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        timeout=60,
    )
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master"],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        timeout=60,
    )
    git_dir = str(repo / ".git")
    try:
        scan._git(git_dir, "fsck", "--unreachable", "--no-reflogs")
    except RuntimeError:
        dangling_breaks_fsck = True
    else:
        dangling_breaks_fsck = False
    analysis = scan.analyze_git_dir(git_dir)
    assert analysis["quarantined"] == ["refs/remotes/origin/HEAD"]
    assert analysis["beyond"] == 0
    assert shas["fix"] in analysis["unreachable_shas"]
    assert dangling_breaks_fsck, "fixture must reproduce the fsck failure mode"
    row = scan.build_row(
        task_id="t",
        run="original",
        ledger_digest="d",
        image_digest="i",
        base_commit=analysis["base"],
        beyond=analysis["beyond"],
        unreachable=analysis["unreachable"],
        samples=analysis["samples"],
    )
    assert (row["has_future_history"], row["needs_repair"]) == ("yes", "true")


def test_analyze_git_dir_flags_alternates_and_worktrees(tmp_path: Path) -> None:
    """Linked checkouts the strip repair cannot handle are flagged, not hidden."""
    _git_repo(tmp_path / "repo", ["base"])
    git_dir = tmp_path / "repo" / ".git"
    assert scan.analyze_git_dir(str(git_dir))["has_alternates"] is False
    assert scan.analyze_git_dir(str(git_dir))["has_worktrees"] is False
    alternates = git_dir / "objects" / "info" / "alternates"
    alternates.write_text("/elsewhere/objects\n")
    (git_dir / "worktrees").mkdir()
    (git_dir / "worktrees" / "linked").mkdir()
    analysis = scan.analyze_git_dir(str(git_dir))
    assert analysis["has_alternates"] is True
    assert analysis["has_worktrees"] is True


class _FakeRegistry:
    """Serves one synthetic layer; no network."""

    def __init__(self, layer: bytes) -> None:
        self._layer = layer

    def manifest(self, digest: str) -> tuple[dict, str]:
        return {"config": "", "layers": ["sha256:" + "f" * 64]}, "fake-stream"

    def open_blob(self, digest: str) -> tuple:
        return io.BytesIO(self._layer), "fake-stream"


def _git_layer(git_dir: Path, root: str = "testbed") -> bytes:
    """A gzip layer carrying a real fixture ``.git`` under ``<root>/.git``."""
    members: list[tuple[str, bytes | None]] = []
    for dirpath, _dirnames, filenames in os.walk(git_dir):
        rel_dir = os.path.relpath(dirpath, git_dir)
        members.append((f"{root}/.git/{rel_dir}" if rel_dir != "." else f"{root}/.git/", None))
        for name in filenames:
            full = Path(dirpath) / name
            members.append(
                (
                    f"{root}/.git/{rel_dir}/{name}" if rel_dir != "." else f"{root}/.git/{name}",
                    full.read_bytes(),
                )
            )
    return _layer_bytes(members).getvalue()


def _scan_kwargs(tmp_path: Path, **overrides) -> dict:
    params = {
        "task_id": "format-code-task-1",
        "run": "original",
        "ledger_digest": "sha256:" + "d" * 64,
        "image_digest": "sha256:" + "e" * 64,
        "registry": _FakeRegistry(b""),
        "work_root": str(tmp_path),
    }
    return params | overrides


def test_scan_image_pipeline_returns_row(tmp_path: Path) -> None:
    """End to end on a synthetic image: a row comes back, never None."""
    shas = _git_repo(tmp_path / "repo", ["base", "fix"])
    repo = tmp_path / "repo"
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(
        ["git", "reset", "-q", "--hard", shas["base"]],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        timeout=60,
    )
    layer = _git_layer(repo / ".git")
    row = scan.scan_image(**_scan_kwargs(tmp_path, registry=_FakeRegistry(layer)))
    assert row is not None
    assert row["task_id"] == "format-code-task-1"
    assert row["base_commit"] == shas["base"]
    assert (row["has_future_history"], row["needs_repair"], row["on_ref"]) == ("yes", "true", "no")
    assert row["repo_path"] == "testbed"
    assert row["method"] == "fake-stream"


def test_scan_image_registry_failure_is_unscanned(tmp_path: Path) -> None:
    """A dead registry is a recorded blocker, never an exception or None."""

    class _DeadRegistry:
        def manifest(self, digest: str) -> tuple[dict, str]:
            raise RuntimeError("mirror down")

        def open_blob(self, digest: str) -> tuple:
            raise AssertionError("unreached")

    row = scan.scan_image(**_scan_kwargs(tmp_path, registry=_DeadRegistry()))
    assert row is not None
    assert (row["has_future_history"], row["needs_repair"]) == ("unscanned", "false")
    assert "mirror down" in row["error"]


def test_draw_sample_is_seeded_and_bounded() -> None:
    """The prevalence sample is reproducible from the recorded seed."""
    ids = [f"format-code-task-{n:06d}" for n in range(50)]
    first = scan.draw_sample(ids, 10, seed=177100)
    assert first == scan.draw_sample(ids, 10, seed=177100)
    assert first == sorted(first) and len(set(first)) == 10
    assert all(tid in ids for tid in first)
    try:
        scan.draw_sample(ids, 51, seed=1)
    except ValueError:
        pass
    else:
        raise AssertionError("oversized sample must raise")


def test_wilson_interval() -> None:
    """The reported bound is a real 95% Wilson interval."""
    lo, hi = scan.wilson(95, 100)
    assert abs(lo - 0.888) < 0.002 and abs(hi - 0.978) < 0.002
    assert scan.wilson(0, 10) == (0.0, scan.wilson(0, 10)[1])
    assert scan.wilson(10, 10)[1] == 1.0
    assert scan.wilson(0, 0) == (0.0, 0.0)


def test_load_reuse_skips_unscanned(tmp_path: Path) -> None:
    """Only decided rows are reused; unscanned rows are rescanned."""
    path = tmp_path / "prior.csv"
    scan.write_csv(
        str(path),
        [
            scan.build_row(
                task_id="a", run="original", ledger_digest="d", image_digest="i", beyond=1
            ),
            scan.build_row(
                task_id="b", run="original", ledger_digest="d", image_digest="i", error="boom"
            ),
        ],
    )
    reused = scan.load_reuse([str(path)])
    assert set(reused) == {"a"}


def test_prevalence_counts_and_interval() -> None:
    """Prevalence covers scanned rows only; unscanned rows are counted apart."""
    rows = [
        scan.build_row(task_id="a", run="original", ledger_digest="d", image_digest="i", beyond=2),
        scan.build_row(task_id="b", run="original", ledger_digest="d", image_digest="i"),
        scan.build_row(task_id="c", run="original", ledger_digest="d", image_digest="i", error="x"),
    ]
    report = scan.prevalence(rows)
    assert report["counts"] == {"yes": 1, "no": 1, "unscanned": 1}
    assert (report["yes"], report["scanned"]) == (1, 2)
    lo, hi = scan.wilson(1, 2)
    assert (report["lo"], report["hi"]) == (lo, hi)
