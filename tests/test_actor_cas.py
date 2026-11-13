"""Synthetic directory publication fixtures; no cloud I/O or stored bodies."""

from __future__ import annotations

from pathlib import Path

import pytest

from evallab.production import actor_cas
from evallab.production.actor_cas import (
    publish_staged_directory,
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    # pytest's own trusted temporary root may use /var or /tmp aliases on macOS.
    return tmp_path.resolve()


def test_discard_refuses_alias_entries_and_removes_only_owned_trees(root: Path) -> None:
    owned, foreign = root / "owned", root / "foreign"
    for directory in (owned, foreign):
        directory.mkdir()
        (directory / "payload").write_bytes(b"inert bytes")
    owned_status = owned.stat()
    alias = root / "owned-alias"
    alias.symlink_to(owned)

    assert actor_cas.discard_owned_directory(alias, owned_status) is False
    assert alias.is_symlink()
    assert (owned / "payload").exists()

    foreign_status = foreign.stat()
    substitute = root / "renamed-owned"
    owned.rename(substitute)
    foreign_empty = root / "owned"
    foreign_empty.mkdir()
    assert actor_cas.discard_owned_directory(foreign_empty, owned_status) is False
    assert foreign_empty.exists()
    assert actor_cas.discard_owned_directory(substitute, owned_status) is True
    assert not substitute.exists()
    assert actor_cas.discard_owned_directory(foreign, foreign_status) is True
    assert not foreign.exists()


def test_publication_refuses_a_staging_tree_that_was_swapped(root: Path) -> None:
    staging, foreign, destination = root / "private-staging", root / "foreign", root / "published"
    for directory in (staging, foreign):
        directory.mkdir(mode=0o700)
    (staging / "staged").write_bytes(b"validated bytes")
    (foreign / "unrelated").write_bytes(b"unrelated bytes")
    expected = actor_cas.directory_status(staging)
    staging.rename(root / "moved-staging")
    foreign.rename(staging)

    with pytest.raises(ValueError, match="no longer the one this run created"):
        publish_staged_directory(staging, destination, expected=expected)
    assert not destination.exists()
    assert (staging / "unrelated").read_bytes() == b"unrelated bytes"


def test_publication_of_a_substituted_tree_is_reported_not_accepted(
    root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging, substitute, destination = (
        root / "private-staging", root / "substituted", root / "published",
    )
    for directory in (staging, substitute):
        directory.mkdir(mode=0o700)
    (staging / "staged").write_bytes(b"validated bytes")
    (substitute / "unrelated").write_bytes(b"unrelated bytes")

    original_publish = actor_cas._publish_no_replace

    def publish_substitute(parent: int, staging_name: str, destination_name: str) -> None:
        original_publish(parent, substitute.name, destination_name)

    monkeypatch.setattr(actor_cas, "_publish_no_replace", publish_substitute)
    with pytest.raises(actor_cas.PublicationUncertainError, match="not the staged tree"):
        publish_staged_directory(staging, destination)

    assert (destination / "unrelated").read_bytes() == b"unrelated bytes"
    assert (staging / "staged").read_bytes() == b"validated bytes"


def test_publish_staged_directory_moves_complete_sibling_tree(root: Path) -> None:
    staging, destination = root / "private-staging", root / "published"
    staging.mkdir(mode=0o700)
    (staging / "inert-data").write_bytes(b"complete staged bundle")
    publish_staged_directory(staging, destination)
    assert not staging.exists()
    assert (destination / "inert-data").read_bytes() == b"complete staged bundle"


@pytest.mark.parametrize("invalid", ["different-parent", "same-path", "symlink-staging", "symlink-parent"])
def test_publish_staged_directory_rejects_aliases(root: Path, invalid: str) -> None:
    staging, destination = root / "private-staging", root / "published"
    staging.mkdir(mode=0o700)
    (staging / "inert-data").write_bytes(b"retain staged bytes")
    if invalid == "different-parent":
        other = root / "other"
        other.mkdir()
        destination = other / destination.name
    elif invalid == "same-path":
        destination = staging
    elif invalid == "symlink-staging":
        alias = root / "alias"
        alias.symlink_to(staging, target_is_directory=True)
        staging = alias
    else:
        alias = root / "alias"
        alias.symlink_to(root, target_is_directory=True)
        staging, destination = alias / staging.name, alias / destination.name
    with pytest.raises((ValueError, OSError)):
        publish_staged_directory(staging, destination)
    assert (root / "private-staging" / "inert-data").read_bytes() == b"retain staged bytes"
    assert not (root / "published").exists()


def test_unsupported_atomic_publication_fails_closed(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    staging, destination = root / "staging", root / "published"
    staging.mkdir()
    (staging / "payload").write_bytes(b"retain staged bytes")
    monkeypatch.setattr(actor_cas.sys, "platform", "unsupported-fixture-platform")
    with pytest.raises(RuntimeError, match="unsupported"):
        publish_staged_directory(staging, destination)
    assert not destination.exists()
    assert (staging / "payload").read_bytes() == b"retain staged bytes"
