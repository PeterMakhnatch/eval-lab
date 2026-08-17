"""Documentation index generator and archive sweep (WS-E item 7).

Emits a deterministic `docs/INDEX.md` from front-matter already parsed by
`evallab.contextpack.parse_doc`. Does not reimplement front-matter parsing.

Entry point: `python -m evallab.docindex generate|check`
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from evallab.contextpack import (
    VALID_AUDIENCES,
    VALID_STATUSES,
    DocMetadata,
    parse_doc,
    parse_front_matter,
    repo_root,
)

DOCINDEX_VERSION = "docindex v1"
GENERATED_BY_MARKER = "<!-- generated-by: docindex v1 -->"
DEFAULT_INDEX_RELATIVE = "docs/INDEX.md"
INDEX_TITLE = "Documentation index"
AUDIENCE_ORDER = VALID_AUDIENCES
STATUS_ORDER = VALID_STATUSES


def default_docs_dir(root: Path | None = None) -> Path:
    """Return the repository `docs/` directory."""
    return (root if root is not None else repo_root()) / "docs"


def default_index_path(root: Path | None = None) -> Path:
    """Return the committed index path (`docs/INDEX.md`)."""
    return (root if root is not None else repo_root()) / DEFAULT_INDEX_RELATIVE


def root_for_docs_dir(docs_dir: Path, root: Path | None = None) -> Path:
    """Use an explicit root, else the parent of `docs/` when that is the leaf."""
    if root is not None:
        return root
    resolved = docs_dir.resolve()
    if resolved.name == "docs":
        return resolved.parent
    return repo_root()


def discover_doc_paths(docs_dir: Path) -> list[Path]:
    """Discover markdown docs that participate in the index.

    Matches the context-pack corpus: top-level `docs/*.md` plus
    `docs/research/*.md`. The generated index is excluded so a write does
    not change the next generation. Nested work-order trees (`prompts/`,
    `checkpoints/`) are not part of the front-matter contract.
    """
    if not docs_dir.is_dir():
        return []

    candidates: list[Path] = []
    for path in sorted(docs_dir.glob("*.md")):
        if path.is_file() and not path.name.startswith(".") and path.name != "INDEX.md":
            candidates.append(path)

    research_dir = docs_dir / "research"
    if research_dir.is_dir():
        for path in sorted(research_dir.glob("*.md")):
            if path.is_file() and not path.name.startswith("."):
                candidates.append(path)

    return sorted(candidates, key=lambda p: p.as_posix())


def load_docs(docs_dir: Path, root: Path | None = None) -> list[DocMetadata]:
    """Parse every discovered documentation file via `parse_doc`."""
    resolved_root = root if root is not None else repo_root()
    return [parse_doc(path, root=resolved_root) for path in discover_doc_paths(docs_dir)]


def _audience_cell(audience: Sequence[str]) -> str:
    return ", ".join(audience) if audience else "(none)"


def _table_row(doc: DocMetadata) -> str:
    return (
        f"| `{doc.path}` | {doc.title} | `{doc.status}` | `{_audience_cell(doc.audience)}` |"
    )


def _table(docs: Sequence[DocMetadata]) -> list[str]:
    lines = [
        "| Path | Title | Status | Audience |",
        "|---|---|---|---|",
    ]
    if docs:
        lines.extend(_table_row(doc) for doc in docs)
    else:
        lines.append("| — | _None._ | — | — |")
    return lines


def _docs_for_audience_status(
    docs: Sequence[DocMetadata], audience: str, status: str
) -> list[DocMetadata]:
    return [
        doc
        for doc in docs
        if status == doc.status and audience in doc.audience
    ]


def _historical_docs(docs: Sequence[DocMetadata]) -> list[DocMetadata]:
    return [doc for doc in docs if doc.status == "historical"]


def render_index(docs: Sequence[DocMetadata]) -> str:
    """Render the committed index markdown. No timestamp; byte-stable."""
    ordered = sorted(docs, key=lambda d: (d.path, d.title))
    lines = [
        "---",
        "status: living",
        "audience:",
        "  - builder",
        "  - analyst",
        "  - runner",
        "  - operator",
    ]
    if ordered:
        lines.append("inputs:")
        for doc in ordered:
            lines.append(f"  - path: {doc.path}")
            lines.append(f"    digest: {doc.content_digest}")
    else:
        lines.append("inputs: []")
    lines.extend(
        [
            "---",
            "",
            GENERATED_BY_MARKER,
            "",
            f"# {INDEX_TITLE}",
            "",
            "Front-matter driven index of `docs/`. Grouped by audience, then by",
            "status. Historical documents are repeated in the Archive section so",
            "an operator can see what is archived.",
            "",
        ]
    )

    for audience in AUDIENCE_ORDER:
        lines.append(f"## {audience}")
        lines.append("")
        for status in STATUS_ORDER:
            lines.append(f"### {status}")
            lines.append("")
            group = _docs_for_audience_status(ordered, audience, status)
            lines.extend(_table(group))
            lines.append("")

    lines.append("## Archive")
    lines.append("")
    lines.append(
        "Historical documents. These are archived records, not living contracts."
    )
    lines.append("")
    lines.extend(_table(_historical_docs(ordered)))
    lines.append("")
    return "\n".join(lines)


def generate_index(
    docs_dir: Path | None = None,
    root: Path | None = None,
) -> str:
    """Generate the index text for the given docs tree."""
    resolved_root = root if root is not None else repo_root()
    resolved_docs = docs_dir if docs_dir is not None else default_docs_dir(resolved_root)
    return render_index(load_docs(resolved_docs, root=resolved_root))


def write_index(
    output: Path,
    docs_dir: Path | None = None,
    root: Path | None = None,
) -> str:
    """Generate and write the index. Returns the written text."""
    text = generate_index(docs_dir=docs_dir, root=root)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    return text


@dataclass(frozen=True)
class CheckIssue:
    """One fail-closed validation finding."""

    path: str
    message: str


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def collect_check_issues(
    docs_dir: Path,
    index_path: Path,
    root: Path | None = None,
) -> list[CheckIssue]:
    """Return every fail-closed problem in the docs tree and committed index."""
    resolved_root = root if root is not None else repo_root()
    issues: list[CheckIssue] = []

    for path in discover_doc_paths(docs_dir):
        rel = _relative_path(path, resolved_root)
        content = path.read_text(encoding="utf-8")
        front_matter, _body = parse_front_matter(content)
        if front_matter is None:
            issues.append(CheckIssue(rel, "missing YAML front-matter"))
            continue

        raw_status = str(front_matter.get("status", "")).strip().lower()
        if raw_status not in VALID_STATUSES:
            issues.append(
                CheckIssue(
                    rel,
                    f"status {raw_status!r} is outside {VALID_STATUSES}",
                )
            )

        raw_audience = front_matter.get("audience", [])
        if isinstance(raw_audience, str):
            audience_values = [raw_audience.strip().lower()]
        elif isinstance(raw_audience, (list, tuple)):
            audience_values = [str(item).strip().lower() for item in raw_audience]
        else:
            audience_values = [str(raw_audience).strip().lower()]

        for value in audience_values:
            if value not in VALID_AUDIENCES:
                issues.append(
                    CheckIssue(
                        rel,
                        f"audience {value!r} is outside {VALID_AUDIENCES}",
                    )
                )

    expected = generate_index(docs_dir=docs_dir, root=resolved_root)
    index_rel = _relative_path(index_path, resolved_root)
    if not index_path.is_file():
        issues.append(CheckIssue(index_rel, "committed index is missing"))
        return issues

    actual = index_path.read_text(encoding="utf-8")
    index_fm, _index_body = parse_front_matter(actual)
    if index_fm is None:
        issues.append(CheckIssue(index_rel, "missing YAML front-matter"))
    else:
        index_status = str(index_fm.get("status", "")).strip().lower()
        if index_status != "living":
            issues.append(
                CheckIssue(index_rel, f"status must be 'living', got {index_status!r}")
            )
        raw_audience = index_fm.get("audience", [])
        if isinstance(raw_audience, str):
            index_audiences = {raw_audience.strip().lower()}
        elif isinstance(raw_audience, (list, tuple)):
            index_audiences = {str(item).strip().lower() for item in raw_audience}
        else:
            index_audiences = set()
        missing_roles = [role for role in VALID_AUDIENCES if role not in index_audiences]
        if missing_roles:
            issues.append(
                CheckIssue(
                    index_rel,
                    f"audience must cover all four roles; missing {missing_roles}",
                )
            )
        if "inputs" not in index_fm or not isinstance(index_fm["inputs"], list):
            issues.append(
                CheckIssue(
                    index_rel,
                    "inputs field in front-matter must be a list",
                )
            )

    if actual != expected:
        issues.append(
            CheckIssue(index_rel, "committed index is stale relative to a fresh generation")
        )

    return issues


def check_index(
    docs_dir: Path | None = None,
    index_path: Path | None = None,
    root: Path | None = None,
) -> list[CheckIssue]:
    """Validate front-matter and index freshness. Empty list means pass."""
    resolved_root = root if root is not None else repo_root()
    resolved_docs = docs_dir if docs_dir is not None else default_docs_dir(resolved_root)
    resolved_index = index_path if index_path is not None else default_index_path(resolved_root)
    return collect_check_issues(resolved_docs, resolved_index, root=resolved_root)


def build_parser() -> argparse.ArgumentParser:
    """Construct the `python -m evallab.docindex` argument parser."""
    parser = argparse.ArgumentParser(
        prog="docindex",
        description="Generate and validate the deterministic docs/INDEX.md archive sweep.",
    )
    subparsers = parser.add_subparsers(dest="command", help="Subcommand to execute")

    generate_cmd = subparsers.add_parser(
        "generate", help="Write a deterministic documentation index"
    )
    generate_cmd.add_argument(
        "-o",
        "--out",
        type=Path,
        metavar="FILE",
        default=None,
        help="Path to write the index (defaults to docs/INDEX.md)",
    )
    generate_cmd.add_argument(
        "--docs-dir",
        type=Path,
        metavar="DIR",
        default=None,
        help="Directory containing documentation (defaults to docs/)",
    )

    check_cmd = subparsers.add_parser(
        "check",
        help="Fail-closed validation of front-matter and index freshness",
    )
    check_cmd.add_argument(
        "--docs-dir",
        type=Path,
        metavar="DIR",
        default=None,
        help="Directory containing documentation (defaults to docs/)",
    )
    check_cmd.add_argument(
        "--index",
        type=Path,
        metavar="FILE",
        default=None,
        help="Committed index path (defaults to docs/INDEX.md)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the documentation index generator."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    root = repo_root()

    if args.command == "generate":
        docs_dir = args.docs_dir if args.docs_dir is not None else default_docs_dir(root)
        if not docs_dir.is_dir():
            print(f"error: docs directory not found: {docs_dir}", file=sys.stderr)
            return 1
        resolved_root = root_for_docs_dir(docs_dir, None if args.docs_dir else root)
        output = args.out if args.out is not None else default_index_path(resolved_root)
        write_index(output, docs_dir=docs_dir, root=resolved_root)
        print(f"Wrote documentation index -> {output}")
        return 0

    if args.command == "check":
        docs_dir = args.docs_dir if args.docs_dir is not None else default_docs_dir(root)
        if not docs_dir.is_dir():
            print(f"error: docs directory not found: {docs_dir}", file=sys.stderr)
            return 1
        resolved_root = root_for_docs_dir(docs_dir, None if args.docs_dir else root)
        index_path = args.index if args.index is not None else default_index_path(resolved_root)
        issues = check_index(docs_dir=docs_dir, index_path=index_path, root=resolved_root)
        if issues:
            print("docindex check failed:", file=sys.stderr)
            for issue in issues:
                print(f"  {issue.path}: {issue.message}", file=sys.stderr)
            return 1
        print("docindex check passed")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
