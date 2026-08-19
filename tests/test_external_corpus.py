"""GYM-DATA: discipline for external corpora under `research/external/`.

These tests guard the two rules that make imported data safe to keep, and they are
written to hold *before* any corpus lands so that the first import cannot skip them:

1. **Every external corpus states its contamination class.** Public rollouts on
   public tasks are behaviour-study material; a corpus directory without that
   caveat is how an imported number ends up in a capability claim.
2. **`fetch ≠ register`, and pinned refs only.** `fetch.py` already refuses
   unpinned refs (`latest`/`head`/`main`/`master`); this pins that behaviour so a
   future external-corpus fetcher cannot quietly accept a moving target.

A corpus that has not been acquired must say so rather than presenting an empty
directory as data — `harbor-index` is currently in exactly that state.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from evallab.fetch import UNPINNED_VERSIONS, FetchError, parse_pin

REPO_ROOT = Path(__file__).resolve().parents[1]
EXTERNAL_ROOT = REPO_ROOT / "research" / "external"

CONTAMINATION_TERMS = (
    "contamination",
    "exposure",
    "leakage",
    "behaviour-study",
    "behavior-study",
)


def corpus_dirs() -> list[Path]:
    if not EXTERNAL_ROOT.is_dir():
        return []
    return sorted(p for p in EXTERNAL_ROOT.iterdir() if p.is_dir())


def corpus_ids() -> list[str]:
    return [p.name for p in corpus_dirs()]


def test_external_root_exists() -> None:
    assert EXTERNAL_ROOT.is_dir(), f"missing external corpus root: {EXTERNAL_ROOT}"


@pytest.mark.parametrize("name", corpus_ids())
def test_corpus_has_a_readme(name: str) -> None:
    assert (EXTERNAL_ROOT / name / "README.md").is_file(), (
        f"{name}: an external corpus must document its provenance in README.md"
    )


@pytest.mark.parametrize("name", corpus_ids())
def test_corpus_states_its_contamination_class(name: str) -> None:
    """The caveat must live with the data, not in a doc someone may not open."""
    text = (EXTERNAL_ROOT / name / "README.md").read_text(encoding="utf-8").lower()
    assert any(term in text for term in CONTAMINATION_TERMS), (
        f"{name}: README must state the contamination class "
        f"(one of {CONTAMINATION_TERMS})"
    )


@pytest.mark.parametrize("name", corpus_ids())
def test_corpus_declares_fetch_not_register(name: str) -> None:
    """Acquiring data must never read as registering a task."""
    text = (EXTERNAL_ROOT / name / "README.md").read_text(encoding="utf-8")
    assert re.search(r"fetch\s*(≠|!=|is not|never)\s*register", text, re.IGNORECASE), (
        f"{name}: README must spell out the fetch != register discipline"
    )


@pytest.mark.parametrize("name", corpus_ids())
def test_unacquired_corpus_says_so_instead_of_looking_empty(name: str) -> None:
    """A directory with no data files must declare itself pending.

    Otherwise an empty directory is indistinguishable from a corpus whose files were
    lost, and a later reader cannot tell which.
    """
    corpus = EXTERNAL_ROOT / name
    data_files = [
        p
        for p in corpus.rglob("*")
        if p.is_file() and p.name not in {"README.md", ".gitkeep"}
    ]
    if not data_files:
        text = (corpus / "README.md").read_text(encoding="utf-8").lower()
        assert "pending" in text, (
            f"{name}: no data files present, so the README must mark the corpus pending"
        )


@pytest.mark.parametrize("unpinned", sorted(UNPINNED_VERSIONS))
def test_unpinned_refs_are_refused(unpinned: str) -> None:
    """External acquisition is pinned-only; a moving ref is not reproducible."""
    with pytest.raises(FetchError):
        parse_pin(f"harbor-index@{unpinned}")


def test_a_ref_without_a_version_is_refused() -> None:
    with pytest.raises(FetchError):
        parse_pin("harbor-index")


def test_a_pinned_ref_parses() -> None:
    pin = parse_pin("harbor-index@1.0")
    assert (pin.name, pin.version) == ("harbor-index", "1.0")
