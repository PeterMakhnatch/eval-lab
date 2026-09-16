"""Data-view regressions; executable adversarial controls run only in native containers."""

import importlib.util
from pathlib import Path

import pytest

TASK = Path(__file__).resolve().parents[1] / "library/tasks/release-branch-rescue"


@pytest.fixture(scope="module")
def verifier():
    spec = importlib.util.spec_from_file_location("release_verifier", TASK / "tests/test_rescue.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def submitted(tmp_path, verifier):
    source = tmp_path / "submitted"
    source.mkdir()
    git = verifier.trusted_git
    assert git(source, ["init", "--initial-branch=main"]).returncode == 0
    assert git(source, ["config", "user.name", "Fixture"]).returncode == 0
    assert git(source, ["config", "user.email", "fixture@example.invalid"]).returncode == 0
    payload = source / "payload.txt"
    payload.write_text("expected\n")
    payload.chmod(0o755)
    assert git(source, ["add", "payload.txt"]).returncode == 0
    assert git(source, ["commit", "-m", "fixture"]).returncode == 0
    assert git(source, ["remote", "add", "origin", "/srv/origin.git"]).returncode == 0
    assert git(source, ["update-ref", "refs/remotes/origin/main", "HEAD"]).returncode == 0
    assert git(source, ["branch", "--set-upstream-to=origin/main", "main"]).returncode == 0
    return source


def test_clean_view_preserves_content_modes_tracking_and_packed_refs(tmp_path, submitted, verifier):
    assert verifier.trusted_git(submitted, ["pack-refs", "--all"]).returncode == 0
    view = verifier.repository_view(submitted, tmp_path / "view")
    status = verifier.trusted_git(view, ["status", "--porcelain", "--ignored"])
    assert status.returncode == 0 and status.stdout == ""
    tracking = verifier.trusted_git(view, ["rev-parse", "--symbolic-full-name", "main@{upstream}"])
    assert tracking.returncode == 0 and tracking.stdout.strip() == "refs/remotes/origin/main"
    assert (view / "payload.txt").read_bytes() == b"expected\n"


@pytest.mark.parametrize("driver", ["clean", "process"])
def test_filter_configuration_cannot_mask_dirty_data(tmp_path, submitted, verifier, driver):
    # Never execute a payload on the host. This nonexistent executable is inert
    # configuration; only native container controls execute a harmless marker.
    config = submitted / ".git/config"
    with config.open("a") as stream:
        stream.write(
            f'\n[filter "har51"]\n\t{driver} = /nonexistent-har51-filter\n\trequired = true\n'
        )
    (submitted / ".git/info/attributes").write_text("payload.txt filter=har51\n")
    (submitted / "payload.txt").write_text("expected\nUNCOMMITTED\n")
    view = verifier.repository_view(submitted, tmp_path / "view")
    status = verifier.trusted_git(view, ["status", "--porcelain"])
    assert status.returncode == 0 and " M payload.txt" in status.stdout
    assert verifier.trusted_git(view, ["diff-files", "--quiet"]).returncode == 1


def test_submitted_include_and_worktree_redirect_have_no_authority(tmp_path, submitted, verifier):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload.txt").write_text("outside\n")
    (submitted / ".git/config").write_text(
        f"[core]\n\tworktree = {outside}\n[include]\n\tpath = /nonexistent-har51-config\n"
    )
    view = verifier.repository_view(submitted, tmp_path / "view")
    assert verifier.trusted_git(view, ["status", "--porcelain"]).stdout == ""
    assert (view / "payload.txt").read_text() == "expected\n"
    assert (outside / "payload.txt").read_text() == "outside\n"


@pytest.mark.parametrize("relative", ["payload.txt", ".git/objects", ".git/config"])
def test_symlinks_cannot_redirect_repository_view(tmp_path, submitted, verifier, relative):
    target = submitted / relative
    saved = tmp_path / "saved"
    target.rename(saved)
    target.symlink_to(saved, target_is_directory=saved.is_dir())
    with pytest.raises(ValueError):
        verifier.repository_view(submitted, tmp_path / "view")
