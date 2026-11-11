"""Relative Markdown links on the route pages and ladder READMEs must resolve."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Repository-relative page globs whose relative links must resolve. Add pages here.
LINK_CHECKED_PAGES: tuple[str, ...] = (
    "README.md",
    "docs/NOW.md",
    "docs/mimo/*.md",
    "research/experiments/mimo-clean-v6/README.md",
    "research/experiments/mimo-v6-full-fleet/README.md",
    "research/experiments/library-ladder-coverage/README.md",
)

_TARGET = r"(<[^>\n]*>|[^\s)]+)"
_INLINE_LINK = re.compile(
    r"!?\[(?:[^\[\]\n]|\[[^\[\]\n]*\])*\]\(\s*"
    + _TARGET
    + r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)"
)
_REFERENCE_DEFINITION = re.compile(r"^ {0,3}\[[^\]\n]+\]:\s*" + _TARGET, re.MULTILINE)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _prose(text: str) -> str:
    """Return ``text`` with fenced code blocks blanked out."""
    kept: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        match = _FENCE.match(line)
        if fence is None and match:
            fence = match.group(1)
            continue
        if fence is not None:
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
                fence = None
            continue
        kept.append(line)
    return "\n".join(kept)


def _relative_targets(text: str) -> list[str]:
    """Return link and image targets written as relative paths, fragment and query stripped."""
    prose = _prose(text)
    raw = [m.group(1) for m in _INLINE_LINK.finditer(prose)]
    raw += [m.group(1) for m in _REFERENCE_DEFINITION.finditer(prose)]
    targets: list[str] = []
    for target in raw:
        target = target.removeprefix("<").removesuffix(">")
        if _SCHEME.match(target) or target.startswith(("~", "/")):
            continue
        path = target.split("#", 1)[0].split("?", 1)[0]
        if path:
            targets.append(path)
    return targets


def dead_links(root: Path, pages: tuple[str, ...]) -> list[str]:
    """Return ``page -> target`` for each relative target that does not exist."""
    dead: list[str] = []
    for pattern in pages:
        page_paths = sorted(root.glob(pattern))
        if not page_paths:
            dead.append(f"{pattern}: no page matches")
        for page_path in page_paths:
            page = page_path.relative_to(root).as_posix()
            for target in _relative_targets(page_path.read_text(encoding="utf-8")):
                if not (page_path.parent / unquote(target)).exists():
                    dead.append(f"{page} -> {target}")
    return dead


def assert_no_dead_links(root: Path, pages: tuple[str, ...]) -> None:
    dead = dead_links(root, pages)
    assert not dead, f"{len(dead)} dead relative link(s):\n" + "\n".join(dead)


def test_committed_route_pages_have_no_dead_relative_links() -> None:
    assert_no_dead_links(REPO_ROOT, LINK_CHECKED_PAGES)


def test_missing_target_fails_naming_page_and_target(tmp_path: Path) -> None:
    (tmp_path / "docs" / "sub").mkdir(parents=True)
    (tmp_path / "docs" / "present.md").write_text("ok\n", encoding="utf-8")
    (tmp_path / "docs" / "fig.png").write_bytes(b"")
    page = tmp_path / "docs" / "page.md"
    page.write_text(
        "\n".join(
            [
                "[file](present.md#section) [dir](sub/) [up](../docs/present.md)",
                "![image](fig.png 'title') [angle](<present.md>) [anchor](#only-here)",
                "[web](https://example.com/missing.md) [mail](mailto:x@example.com)",
                "[home](~/Developer/missing.md) [abs](/missing.md)",
                "```",
                "[fenced](fenced-missing.md)",
                "```",
                "[ref]: present.md",
                "See [gone](gone/missing.md#part) here.",
            ]
        ),
        encoding="utf-8",
    )

    assert dead_links(tmp_path, ("docs/page.md",)) == ["docs/page.md -> gone/missing.md"]
    with pytest.raises(AssertionError, match=r"docs/page\.md -> gone/missing\.md"):
        assert_no_dead_links(tmp_path, ("docs/page.md",))


def test_dead_reference_definition_and_missing_page_are_reported(tmp_path: Path) -> None:
    (tmp_path / "page.md").write_text("[ref]: absent.png\n", encoding="utf-8")

    assert dead_links(tmp_path, ("page.md", "nope.md")) == [
        "page.md -> absent.png",
        "nope.md: no page matches",
    ]
