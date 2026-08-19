"""HARVEST: front-matter conformance for `research/inbox/` notes.

Contract from `docs/prompts/context-supply-program.md` (LOOP-HARVEST, "Note
format (enforced by the conformance test)"). The inbox is the single doorway for
external source material, so provenance is the whole point: a note whose origin,
licence, or downstream target is unrecorded is worse than no note, because a
later corpus entry citing it inherits an unverifiable claim.

Enforced per note:

- `source_url`, `source_type`, `retrieved`, `license_note`, `status`, `feeds`
  all present and non-empty.
- `source_type` in {paper, repo, thread, drive, blog}.
- `status` in {raw, distilled, superseded}.
- `retrieved` is an ISO date (YYYY-MM-DD), not free text.
- `feeds` is a non-empty list, and every entry either points at a standards
  corpus target under `library/curated/standards/` (or the `_proposed_templates/`
  staging area) or is exactly `parked`. This is the acceptance clause "every
  note's `feeds` field names at least one STANDARDS target or explicitly
  `parked`" made executable.

`QUEUE.md` is the queue itself, not a note, and is exempt by name.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from evallab.contextpack import parse_front_matter

REPO_ROOT = Path(__file__).resolve().parents[1]
INBOX = REPO_ROOT / "research" / "inbox"

EXEMPT_NAMES = frozenset({"QUEUE.md", "README.md"})
REQUIRED_FIELDS = ("source_url", "source_type", "retrieved", "license_note", "status", "feeds")
VALID_SOURCE_TYPES = frozenset({"paper", "repo", "thread", "drive", "blog"})
VALID_STATUSES = frozenset({"raw", "distilled", "superseded"})
STANDARDS_PREFIXES = ("library/curated/standards/", "_proposed_templates/")


def inbox_notes() -> list[Path]:
    if not INBOX.is_dir():
        return []
    return sorted(p for p in INBOX.glob("*.md") if p.name not in EXEMPT_NAMES)


def note_ids() -> list[str]:
    return [p.name for p in inbox_notes()]


def test_inbox_directory_exists_and_holds_notes() -> None:
    """The doorway exists and is not empty — a silently missing inbox is a failure."""
    assert INBOX.is_dir(), f"missing inbox directory: {INBOX}"
    assert inbox_notes(), "inbox holds no notes; HARVEST has landed nothing"


@pytest.mark.parametrize("name", note_ids())
def test_note_front_matter_is_conformant(name: str) -> None:
    note = INBOX / name
    front_matter, body = parse_front_matter(note.read_text(encoding="utf-8"))

    assert front_matter is not None, f"{name}: no parseable YAML front-matter"

    missing = [field for field in REQUIRED_FIELDS if not front_matter.get(field)]
    assert not missing, f"{name}: missing or empty front-matter fields: {missing}"

    source_type = front_matter["source_type"]
    assert source_type in VALID_SOURCE_TYPES, (
        f"{name}: source_type {source_type!r} not in {sorted(VALID_SOURCE_TYPES)}"
    )

    status = front_matter["status"]
    assert status in VALID_STATUSES, (
        f"{name}: status {status!r} not in {sorted(VALID_STATUSES)}"
    )

    retrieved = str(front_matter["retrieved"])
    try:
        datetime.date.fromisoformat(retrieved)
    except ValueError:
        pytest.fail(f"{name}: retrieved {retrieved!r} is not an ISO date (YYYY-MM-DD)")

    assert body.strip(), f"{name}: front-matter present but body is empty"


@pytest.mark.parametrize("name", note_ids())
def test_note_feeds_names_a_standards_target_or_parked(name: str) -> None:
    """`feeds` must be actionable: a corpus target STANDARDS can pick up, or `parked`."""
    front_matter, _ = parse_front_matter((INBOX / name).read_text(encoding="utf-8"))
    assert front_matter is not None, f"{name}: no parseable YAML front-matter"

    feeds = front_matter["feeds"]
    assert isinstance(feeds, list) and feeds, f"{name}: feeds must be a non-empty list"

    for entry in feeds:
        target = str(entry).strip()
        parked = target == "parked"
        points_at_corpus = target.startswith(STANDARDS_PREFIXES)
        assert parked or points_at_corpus, (
            f"{name}: feeds entry {target!r} is neither 'parked' nor a standards "
            f"target under {STANDARDS_PREFIXES}"
        )
