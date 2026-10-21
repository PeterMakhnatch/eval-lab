"""Reference-vs-test coverage: which blocks of the reference fix does no test name?

A reference patch splits into *change blocks* (contiguous runs of edited lines per file, the
unit ``evallab ref-subset`` drops one at a time). A block is **unnamed** when the test text names
neither its file nor any identifier its lines touch. Most such blocks are behaviour the hidden
tests never reach: on 1,239 DEV blocks of the executed MiMo reference-subset probe, 80% of unnamed
blocks could be deleted with every hidden test still passing, against 33% of all blocks. Blocks the
tests do name are droppable 16% of the time.

This is a static *locator*, not a verdict: a test can reach code without naming it (an older test
in an unchanged file), and a droppable block can be a refactor the instruction never asks for. It
needs the reference patch, so it only fires where one is supplied (``match --reference`` or
``solution/*.patch``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

_DOC_SUFFIXES = frozenset({".md", ".rst", ".txt", ".adoc", ".csv"})
_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|specs?)(/|$)"
    r"|(^|/)(test_[^/]*|[^/]*_test\.[a-z]+|[^/]*\.(test|spec)\.[a-z]+|conftest\.py)$",
    re.IGNORECASE,
)
_IMPORT = re.compile(
    r"^\s*(import\s|from\s+\S+\s+import\s|#include\b|use\s+[\w:]+;|using\s"
    r"|(const|let|var)\s+\w+\s*=\s*require\(|package\s)"
)
_COMMENT = re.compile(r"^\s*(#|//|/\*|\*|\*/|\"\"\"|'''|--)")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
_KEYWORD_TEXT = (
    "self this cls true false none null return import from def class function const let var new "
    "for while with else elif and not the str int float bool list dict set tuple len range print "
    "raise except try finally lambda yield async await"
)
_KEYWORDS = frozenset(_KEYWORD_TEXT.split())
_HUNK = re.compile(r"@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")
_NO_BODY = ("---", "+++", "index ", "new file", "deleted file", "similarity", "rename", "Binary")


@dataclass(frozen=True)
class ChangeBlock:
    """One contiguous run of edited lines of a reference patch."""

    path: str
    line: int  # first base-file line the run touches
    plus: tuple[str, ...]
    minus: tuple[str, ...]

    @property
    def lines(self) -> tuple[str, ...]:
        return self.plus + self.minus

    @property
    def identifiers(self) -> frozenset[str]:
        found = _IDENT.findall("\n".join(self.lines))
        return frozenset(w for w in found if w.lower() not in _KEYWORDS)


class _Run:
    """Edited lines seen so far in the block being read."""

    def __init__(self, start: int) -> None:
        self.start = start
        self.plus: list[str] = []
        self.minus: list[str] = []


def _flush(blocks: list[ChangeBlock], path: str, run: _Run | None) -> None:
    if run is not None:
        blocks.append(ChangeBlock(path, run.start, tuple(run.plus), tuple(run.minus)))


def change_blocks(patch: str) -> list[ChangeBlock]:
    """Split a unified diff into its change blocks, in patch order."""
    blocks: list[ChangeBlock] = []
    path = ""
    old_line = 0
    in_hunk = False
    run: _Run | None = None
    for line in patch.splitlines():
        if not in_hunk and line.startswith("diff --git "):
            _flush(blocks, path, run)
            run = None
            path = line.rsplit(" b/", 1)[-1]
        elif hunk := _HUNK.match(line):
            _flush(blocks, path, run)
            run = None
            old_line = int(hunk.group(1))
            in_hunk = True
        elif not path or line.startswith("\\") or (not in_hunk and line.startswith(_NO_BODY)):
            continue
        elif line.startswith(("-", "+")):
            run = run or _Run(old_line)
            (run.minus if line[0] == "-" else run.plus).append(line[1:])
            old_line += line[0] == "-"
        elif line.startswith("diff --git "):
            _flush(blocks, path, run)
            run = None
            path = line.rsplit(" b/", 1)[-1]
            in_hunk = False
        else:
            _flush(blocks, path, run)
            run = None
            old_line += 1
    _flush(blocks, path, run)
    return blocks


def _is_behaviour(block: ChangeBlock) -> bool:
    """Code a test could reach: not a test file, a doc, or a block of only imports/comments."""
    if _TEST_PATH.search(block.path) or PurePosixPath(block.path).suffix in _DOC_SUFFIXES:
        return False
    body = [ln for ln in block.lines if ln.strip()]
    return bool(body) and not all(_COMMENT.match(ln) or _IMPORT.match(ln) for ln in body)


def unnamed_blocks(patch: str, test_text: str) -> list[ChangeBlock]:
    """Behaviour blocks of ``patch`` whose file and identifiers no part of ``test_text`` names."""
    named = set(_IDENT.findall(test_text))
    out = []
    for block in change_blocks(patch):
        ids = block.identifiers
        stem = PurePosixPath(block.path).stem
        if _is_behaviour(block) and ids and stem not in test_text and not ids & named:
            out.append(block)
    return out


def describe(block: ChangeBlock) -> str:
    """Evidence line: ``path:line`` and why the block counts as unnamed."""
    stem = PurePosixPath(block.path).stem
    return (
        f"{block.path}:{block.line} ({len(block.lines)} changed lines; no test names "
        f"`{stem}` or any of its {len(block.identifiers)} identifiers)"
    )
