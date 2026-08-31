#!/usr/bin/env python3
"""Dated document naming migration tool for eval-lab and research-context.

Standardizes dated markdown documents to canonical YYYY-MM-DD-<kebab-slug>.md format,
infers dates for undated documents, checks safety/collision preconditions, builds an
inbound link rewrite plan, and generates a comprehensive migration report.

Defaults to --dry-run. Requires --apply to mutate files.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

# Standard regexes
CONFORMANT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-([a-z0-9-]+)\.md$")
SUFFIX_RE = re.compile(r"^(.*?)[-_](\d{4}-\d{2}-\d{2})\.md$", re.IGNORECASE)
FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
WIKI_LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")

EXCLUDED_SCAN_DIRS = frozenset(
    {
        ".git",
        ".worktrees",
        ".venv",
        "venv",
        "node_modules",
        "runs",
        "queue",
        "derived",
        "backups",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        ".omp",
    }
)

INBOX_EXEMPT_NAMES = frozenset({"QUEUE.md", "README.md"})
INBOX_REQUIRED_FIELDS = (
    "source_url",
    "source_type",
    "retrieved",
    "license_note",
    "status",
    "feeds",
)
INBOX_VALID_SOURCE_TYPES = frozenset({"paper", "repo", "thread", "drive", "blog"})
INBOX_VALID_STATUSES = frozenset({"raw", "distilled", "superseded"})
INBOX_STANDARDS_PREFIXES = ("library/curated/standards/", "_proposed_templates/")


@dataclasses.dataclass
class DocItem:
    repo_id: str
    repo_root: Path
    rel_path: Path
    abs_path: Path
    filename: str
    classification: str  # 'already-conformant', 'date-suffixed', 'undated'
    inferred_date: str
    date_source: str
    proposed_filename: str
    proposed_rel_path: Path
    refused: bool = False
    refusal_reasons: list[str] = dataclasses.field(default_factory=list)
    front_matter: dict[str, Any] | None = None
    body: str = ""

    @property
    def needs_rename(self) -> bool:
        return self.filename != self.proposed_filename and not self.refused


@dataclasses.dataclass
class LinkRewrite:
    source_repo_id: str
    source_repo_root: Path
    source_doc_rel: Path
    line_number: int
    raw_link: str
    old_target: str
    new_target: str
    link_type: str  # 'markdown' or 'wikilink'


def _normalize_yaml_values(data: Any) -> Any:
    """Normalize date/datetime objects to ISO string representation in parsed yaml."""
    if isinstance(data, dict):
        return {k: _normalize_yaml_values(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_normalize_yaml_values(v) for v in data]
    if isinstance(data, (datetime.date, datetime.datetime)):
        return data.isoformat()
    return data


def parse_front_matter(content: str) -> tuple[dict[str, Any] | None, str]:
    """Extract YAML front-matter and body from markdown content."""
    match = FRONT_MATTER_RE.match(content)
    if not match:
        return None, content.strip()
    front_matter_raw = match.group(1)
    body = content[match.end() :].strip()
    try:
        parsed = yaml.safe_load(front_matter_raw)
        if isinstance(parsed, dict):
            normalized = _normalize_yaml_values(parsed)
            return normalized, body
    except Exception:
        pass
    return None, content.strip()


def to_kebab_slug(stem: str) -> str:
    """Normalize arbitrary title / filename stem to lower-case kebab-slug."""
    s = stem.lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def is_valid_iso_date(dt_str: str) -> bool:
    """Validate ISO date YYYY-MM-DD."""
    if not isinstance(dt_str, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", dt_str):
        return False
    try:
        datetime.date.fromisoformat(dt_str)
        return True
    except ValueError:
        return False


def infer_date_for_undated_doc(
    abs_path: Path,
    rel_path: Path,
    repo_root: Path,
    front_matter: dict[str, Any] | None,
) -> tuple[str, str, list[str]]:
    """Infer date for an undated document with source tracking and ambiguity checks.

    Hierarchy:
    1. Front-matter `date`, `reviewed`, `retrieved`
    2. Git history: `git log --diff-filter=A --format=%cs -- <path>`
    3. Fallback: filesystem mtime
    """
    reasons: list[str] = []
    # 1. Front-matter
    if front_matter:
        for key in ("date", "reviewed", "retrieved"):
            raw_val = front_matter.get(key)
            if raw_val:
                val_str = str(raw_val).strip()
                if is_valid_iso_date(val_str):
                    return val_str, f"front-matter ({key})", reasons
                else:
                    reasons.append(f"Invalid date format in front-matter {key}: {raw_val!r}")

    # 2. Git log add commit date
    try:
        res = subprocess.run(
            ["git", "log", "--diff-filter=A", "--format=%cs", "--", str(rel_path)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
            if lines and is_valid_iso_date(lines[0]):
                return lines[0], "git-add", reasons
    except Exception as e:
        reasons.append(f"Git history lookup failed: {e}")

    # 3. Mtime fallback
    try:
        mtime_ts = abs_path.stat().st_mtime
        mtime_dt = datetime.date.fromtimestamp(mtime_ts).isoformat()
        return mtime_dt, "mtime", reasons
    except Exception as e:
        reasons.append(f"mtime lookup failed: {e}")
        return "1970-01-01", "unknown", reasons


def find_all_markdown_files(root: Path) -> list[Path]:
    """Find all markdown files under root excluding ignored directories."""
    md_files: list[Path] = []
    if not root.exists():
        return md_files
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_SCAN_DIRS and not d.startswith(".")]
        for f in filenames:
            if f.endswith(".md"):
                md_files.append(Path(dirpath) / f)
    return md_files


def get_open_pr_modified_files(repo_root: Path) -> set[str]:
    """Get relative file paths modified in open PRs or active branches."""
    modified_files: set[str] = set()
    try:
        res = subprocess.run(
            ["gh", "pr", "list", "--json", "number,headRefName"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if res.returncode == 0:
            prs = json.loads(res.stdout)
            for pr in prs:
                branch = pr.get("headRefName")
                if branch:
                    diff_res = subprocess.run(
                        ["git", "diff", "--name-only", f"origin/main...{branch}"],
                        cwd=repo_root,
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if diff_res.returncode == 0:
                        for line in diff_res.stdout.splitlines():
                            if line.strip():
                                modified_files.add(line.strip())
    except Exception:
        pass
    return modified_files


def get_other_worktree_modified_files(repo_root: Path) -> set[str]:
    """Get relative file paths dirty in other worktrees."""
    worktree_dirty: set[str] = set()
    try:
        res = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if res.returncode == 0:
            wt_paths: list[Path] = []
            for line in res.stdout.splitlines():
                if line.startswith("worktree "):
                    wt_paths.append(Path(line.split(" ", 1)[1]))
            # For worktrees that are not this repo_root, check their status
            for wt in wt_paths:
                if wt != repo_root and wt.exists():
                    st_res = subprocess.run(
                        ["git", "status", "--porcelain"],
                        cwd=wt,
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if st_res.returncode == 0:
                        for st_line in st_res.stdout.splitlines():
                            if len(st_line) > 3:
                                worktree_dirty.add(st_line[3:].strip())
    except Exception:
        pass
    return worktree_dirty


def inventory_repo_documents(
    repo_id: str,
    repo_root: Path,
    target_dirs: list[str],
    pr_modified_files: set[str] | None = None,
    worktree_dirty_files: set[str] | None = None,
) -> list[DocItem]:
    """Scan and classify target markdown files in a repository."""
    items: list[DocItem] = []
    if not repo_root.exists():
        return items

    pr_modified = pr_modified_files or set()
    wt_dirty = worktree_dirty_files or set()

    for target_dir in target_dirs:
        dir_path = repo_root / target_dir
        if not dir_path.exists():
            continue
        for file_path in sorted(dir_path.rglob("*.md")):
            rel_path = file_path.relative_to(repo_root)
            filename = file_path.name

            # Check inbox exemptions
            if "research/inbox" in str(rel_path.parent) and filename in INBOX_EXEMPT_NAMES:
                continue

            content = file_path.read_text(encoding="utf-8", errors="replace")
            fm, body = parse_front_matter(content)

            # Classify
            conformant_match = CONFORMANT_RE.match(filename)
            suffix_match = SUFFIX_RE.match(filename)

            refusal_reasons: list[str] = []

            if conformant_match and is_valid_iso_date(conformant_match.group(1)):
                classification = "already-conformant"
                inferred_date = conformant_match.group(1)
                date_source = "filename-prefix"
                proposed_filename = filename
            elif suffix_match and is_valid_iso_date(suffix_match.group(2)):
                classification = "date-suffixed"
                inferred_date = suffix_match.group(2)
                date_source = "filename-suffix"
                slug = to_kebab_slug(suffix_match.group(1))
                proposed_filename = f"{inferred_date}-{slug}.md"
            else:
                classification = "undated"
                inferred_date, date_source, reasons = infer_date_for_undated_doc(
                    file_path, rel_path, repo_root, fm
                )
                if reasons:
                    refusal_reasons.extend(reasons)
                if not is_valid_iso_date(inferred_date):
                    refusal_reasons.append(
                        f"Ambiguous date: inferred {inferred_date!r} is not a valid ISO date"
                    )
                slug = to_kebab_slug(file_path.stem)
                proposed_filename = f"{inferred_date}-{slug}.md"

            proposed_rel_path = rel_path.parent / proposed_filename

            # Check open PR reference
            if str(rel_path) in pr_modified:
                refusal_reasons.append("Referenced in open PR diff or active branch")

            # Check worktree conflict
            if str(rel_path) in wt_dirty:
                refusal_reasons.append("File has uncommitted changes in another active worktree")

            # Check inbox conformance safety
            if "research/inbox" in str(rel_path.parent) and fm:
                st = fm.get("source_type")
                if st and st not in INBOX_VALID_SOURCE_TYPES:
                    refusal_reasons.append(
                        f"Inbox note has invalid source_type {st!r} not in {sorted(INBOX_VALID_SOURCE_TYPES)}"
                    )

            item = DocItem(
                repo_id=repo_id,
                repo_root=repo_root,
                rel_path=rel_path,
                abs_path=file_path,
                filename=filename,
                classification=classification,
                inferred_date=inferred_date,
                date_source=date_source,
                proposed_filename=proposed_filename,
                proposed_rel_path=proposed_rel_path,
                refused=bool(refusal_reasons),
                refusal_reasons=refusal_reasons,
                front_matter=fm,
                body=body,
            )
            items.append(item)

    return items


def audit_collisions(items: list[DocItem]) -> None:
    """Detect target filename collisions across proposed renames and mark as refused."""
    targets: dict[tuple[str, str], list[DocItem]] = defaultdict(list)
    for item in items:
        target_key = (item.repo_id, str(item.proposed_rel_path))
        targets[target_key].append(item)

    for target_key, doc_group in targets.items():
        if len(doc_group) > 1:
            sources = ", ".join(str(d.rel_path) for d in doc_group)
            for d in doc_group:
                d.refused = True
                d.refusal_reasons.append(
                    f"Target collision: multiple files map to {target_key[1]} ({sources})"
                )
        else:
            doc = doc_group[0]
            # Check if target already exists on disk and is a different file
            target_abs = doc.repo_root / doc.proposed_rel_path
            if (
                target_abs.exists()
                and doc.needs_rename
                and target_abs.resolve() != doc.abs_path.resolve()
            ):
                doc.refused = True
                doc.refusal_reasons.append(
                    f"Target already exists on disk at {doc.proposed_rel_path}"
                )


def build_link_rewrite_plan(
    all_items: list[DocItem],
    repos: list[tuple[str, Path]],
) -> list[LinkRewrite]:
    """Scan all markdown documents in both repos to build inbound link rewrite plan."""
    # Build rename lookup map: old_filename -> new_filename and old_stem -> new_stem
    # Only include items that need rename and are not refused
    rename_lookup: dict[str, str] = {}
    for item in all_items:
        if item.needs_rename and not item.refused:
            rename_lookup[item.filename] = item.proposed_filename

    rewrites: list[LinkRewrite] = []
    if not rename_lookup:
        return rewrites

    for repo_id, repo_root in repos:
        for md_path in find_all_markdown_files(repo_root):
            rel_path = md_path.relative_to(repo_root)
            try:
                content = md_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            lines = content.splitlines()
            for lineno, line in enumerate(lines, 1):
                # Match standard markdown links [text](url)
                for m in MD_LINK_RE.finditer(line):
                    raw_target = m.group(2).strip()
                    # Strip anchor and query
                    path_part = raw_target.split("#")[0].split("?")[0]
                    target_fname = Path(path_part).name
                    if target_fname in rename_lookup:
                        new_fname = rename_lookup[target_fname]
                        # Replace only the target filename in the link URL
                        new_target = raw_target.replace(target_fname, new_fname)
                        rewrites.append(
                            LinkRewrite(
                                source_repo_id=repo_id,
                                source_repo_root=repo_root,
                                source_doc_rel=rel_path,
                                line_number=lineno,
                                raw_link=m.group(0),
                                old_target=raw_target,
                                new_target=new_target,
                                link_type="markdown",
                            )
                        )

                # Match Obsidian / Wiki links [[target]]
                for m in WIKI_LINK_RE.finditer(line):
                    raw_wiki = m.group(1).strip()
                    wiki_fname = raw_wiki if raw_wiki.endswith(".md") else f"{raw_wiki}.md"
                    if wiki_fname in rename_lookup:
                        new_fname = rename_lookup[wiki_fname]
                        new_wiki = new_fname if raw_wiki.endswith(".md") else new_fname[:-3]
                        rewrites.append(
                            LinkRewrite(
                                source_repo_id=repo_id,
                                source_repo_root=repo_root,
                                source_doc_rel=rel_path,
                                line_number=lineno,
                                raw_link=m.group(0),
                                old_target=raw_wiki,
                                new_target=new_wiki,
                                link_type="wikilink",
                            )
                        )

    return rewrites


def enrich_front_matter_for_apply(
    item: DocItem,
    default_author: str = "lane/naming-migration",
) -> str:
    """Enrich front-matter with missing required keys and return full document text."""
    fm = dict(item.front_matter or {})
    body = item.body or ""

    # Ensure standard keys
    if "date" not in fm or not fm["date"]:
        fm["date"] = item.inferred_date or datetime.date.today().isoformat()
    else:
        fm["date"] = str(fm["date"]).strip()

    if "author" not in fm or not fm["author"]:
        fm["author"] = default_author

    if "summary" not in fm or not fm["summary"]:
        # Extract first non-heading sentence or fallback
        summary_val = ""
        for line in body.splitlines():
            clean = line.strip().lstrip("#").strip()
            if clean and not clean.startswith(("-", "*", "|", "`", ">")):
                summary_val = clean
                break
        fm["summary"] = (
            summary_val
            if summary_val
            else f"Documentation and analysis for {item.proposed_filename}."
        )

    if "status" not in fm or fm["status"] not in INBOX_VALID_STATUSES:
        fm["status"] = "raw"

    # Enforce inbox conformance if inside research/inbox
    is_inbox = "research/inbox" in str(item.rel_path.parent)
    if is_inbox:
        if "source_url" not in fm or not fm["source_url"]:
            fm["source_url"] = "https://github.com/eval-lab/eval-lab"
        if "source_type" not in fm or fm["source_type"] not in INBOX_VALID_SOURCE_TYPES:
            fm["source_type"] = "repo"
        if "retrieved" not in fm or not fm["retrieved"]:
            fm["retrieved"] = str(fm["date"])
        else:
            ret_str = str(fm["retrieved"]).strip()
            if not is_valid_iso_date(ret_str):
                fm["retrieved"] = str(fm["date"])
        if "license_note" not in fm or not fm["license_note"]:
            fm["license_note"] = "Internal research and evaluation note."
        if "feeds" not in fm or not isinstance(fm["feeds"], list) or not fm["feeds"]:
            fm["feeds"] = ["parked"]

    fm_yaml = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
    clean_body = body.strip()
    return f"---\n{fm_yaml}\n---\n\n{clean_body}\n"


def apply_migration(
    items: list[DocItem],
    link_rewrites: list[LinkRewrite],
) -> tuple[int, int]:
    """Execute renames via git mv and apply link rewrites."""
    renamed_count = 0
    rewritten_links_count = 0

    # 1. Apply document renames and front-matter enrichment
    for item in items:
        if not item.needs_rename or item.refused:
            continue

        target_abs = item.repo_root / item.proposed_rel_path
        target_abs.parent.mkdir(parents=True, exist_ok=True)

        # Update file content with enriched front matter
        new_content = enrich_front_matter_for_apply(item)
        item.abs_path.write_text(new_content, encoding="utf-8")

        # Execute git mv
        cmd = ["git", "mv", str(item.rel_path), str(item.proposed_rel_path)]
        res = subprocess.run(cmd, cwd=item.repo_root, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(
                f"git mv failed for {item.rel_path} -> {item.proposed_rel_path}: {res.stderr}"
            )

        renamed_count += 1

    # 2. Apply inbound link rewrites
    # Group link rewrites by file
    rewrites_by_file: dict[tuple[Path, Path], list[LinkRewrite]] = defaultdict(list)
    for rw in link_rewrites:
        rewrites_by_file[(rw.source_repo_root, rw.source_doc_rel)].append(rw)

    for (repo_root, doc_rel), rws in rewrites_by_file.items():
        doc_abs = repo_root / doc_rel
        if not doc_abs.exists():
            continue
        content = doc_abs.read_text(encoding="utf-8", errors="replace")
        for rw in rws:
            content = content.replace(rw.old_target, rw.new_target)
            rewritten_links_count += 1
        doc_abs.write_text(content, encoding="utf-8")

    return renamed_count, rewritten_links_count


def generate_migration_report(
    items: list[DocItem],
    link_rewrites: list[LinkRewrite],
    report_path: Path,
) -> str:
    """Generate comprehensive markdown migration report."""
    by_repo_class: dict[tuple[str, str], int] = defaultdict(int)
    for it in items:
        cls_key = "refused" if it.refused else it.classification
        by_repo_class[(it.repo_id, cls_key)] += 1

    refused_items = [it for it in items if it.refused]
    ready_renames = [it for it in items if it.needs_rename and not it.refused]

    report_lines: list[str] = [
        "---",
        "date: 2026-08-31",
        "author: lane/naming-migration",
        'summary: "Dated document naming migration dry-run inventory, classification breakdown, proposed renames, safety refusal audit, and inbound link rewrite plan across eval-lab and research-context."',
        "status: raw",
        "---",
        "",
        "# Document Naming Migration Plan (2026-08-31)",
        "",
        "## Executive Summary & Critical Preconditions",
        "",
        "> **CRITICAL EXECUTION PRECONDITION**  ",
        "> **The PR queue on `eval-lab` (currently 9 open PRs) must be completely drained before executing `--apply`.**  ",
        "> Main merges frequently and multiple worktrees are active. Executing mass document renames while PRs are open will cause widespread merge conflicts. Execution of this migration is strictly gated on a quiet main branch with zero open PRs touching research directories.",
        "",
        "This dry-run migration audits all markdown documents across:",
        "- **Repo A (`eval-lab`)**: `research/inbox`, `research/analysis`, `research/explorations`",
        "- **Repo B (`research-context`)**: `trajectory-analysis` (including subdirectories)",
        "",
        "## File Inventory and Classification Breakdown",
        "",
        "| Repository | Already Conformant | Date-Suffixed | Undated | Refused (Unsafe) | Total Scanned |",
        "|---|---|---|---|---|---|",
    ]

    repos = sorted({it.repo_id for it in items})
    total_conformant = sum(
        1 for it in items if it.classification == "already-conformant" and not it.refused
    )
    total_suffixed = sum(
        1 for it in items if it.classification == "date-suffixed" and not it.refused
    )
    total_undated = sum(1 for it in items if it.classification == "undated" and not it.refused)
    total_refused = len(refused_items)
    total_all = len(items)

    for r in repos:
        r_conf = sum(
            1
            for it in items
            if it.repo_id == r and it.classification == "already-conformant" and not it.refused
        )
        r_suf = sum(
            1
            for it in items
            if it.repo_id == r and it.classification == "date-suffixed" and not it.refused
        )
        r_und = sum(
            1
            for it in items
            if it.repo_id == r and it.classification == "undated" and not it.refused
        )
        r_ref = sum(1 for it in items if it.repo_id == r and it.refused)
        r_tot = sum(1 for it in items if it.repo_id == r)
        report_lines.append(f"| `{r}` | {r_conf} | {r_suf} | {r_und} | {r_ref} | {r_tot} |")

    report_lines.append(
        f"| **Total** | **{total_conformant}** | **{total_suffixed}** | **{total_undated}** | **{total_refused}** | **{total_all}** |"
    )
    report_lines.extend(
        [
            "",
            "### Key Statistics",
            f"- **Total Documents Scanned:** {total_all}",
            f"- **Already Conformant:** {total_conformant} (no rename needed)",
            f"- **Proposed Renames (Ready):** {len(ready_renames)}",
            f"- **Refused Unsafe Cases:** {total_refused}",
            f"- **Inbound Link Rewrites Planned:** {len(link_rewrites)}",
            "",
            "## Refused Files and Safety Audit",
            "",
        ]
    )

    if refused_items:
        report_lines.extend(
            [
                "The following documents were refused from automated renaming to prevent collisions, broken links, or merge conflicts:",
                "",
                "| Repository | Original Path | Inferred Date | Date Source | Refusal Reason |",
                "|---|---|---|---|---|",
            ]
        )
        for it in sorted(refused_items, key=lambda x: (x.repo_id, str(x.rel_path))):
            reasons = "; ".join(it.refusal_reasons)
            report_lines.append(
                f"| `{it.repo_id}` | `{it.rel_path}` | {it.inferred_date} | {it.date_source} | {reasons} |"
            )
    else:
        report_lines.append("No files were refused. All documents passed safety audits cleanly.")

    report_lines.extend(
        [
            "",
            "## Proposed Rename Plan",
            "",
            "| Repository | Original Path | Classification | Inferred Date | Date Source | Proposed Path | Status |",
            "|---|---|---|---|---|---|---|",
        ]
    )

    for it in sorted(items, key=lambda x: (x.repo_id, str(x.rel_path))):
        status_label = (
            "REFUSED" if it.refused else ("ALREADY CONFORMANT" if not it.needs_rename else "READY")
        )
        report_lines.append(
            f"| `{it.repo_id}` | `{it.rel_path}` | {it.classification} | {it.inferred_date} | {it.date_source} | `{it.proposed_rel_path}` | {status_label} |"
        )

    report_lines.extend(
        [
            "",
            "## Inbound Link Rewrite Plan",
            "",
            f"A total of **{len(link_rewrites)}** inbound Markdown links and wikilinks across both repositories reference files scheduled for renaming.",
            "",
        ]
    )

    if link_rewrites:
        report_lines.extend(
            [
                "| Source Repo | Source Document | Line | Link Type | Original Target | Proposed Target |",
                "|---|---|---|---|---|---|",
            ]
        )
        for rw in sorted(
            link_rewrites, key=lambda x: (x.source_repo_id, str(x.source_doc_rel), x.line_number)
        ):
            report_lines.append(
                f"| `{rw.source_repo_id}` | `{rw.source_doc_rel}` | {rw.line_number} | {rw.link_type} | `{rw.old_target}` | `{rw.new_target}` |"
            )
    else:
        report_lines.append("No inbound link rewrites required.")

    report_lines.append("")
    report_content = "\n".join(report_lines)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_content, encoding="utf-8")
    return report_content


def run_migration(
    repo_a_path: Path,
    repo_b_path: Path,
    report_path: Path,
    apply: bool = False,
    verbose: bool = False,
) -> tuple[list[DocItem], list[LinkRewrite], str]:
    """Execute dry-run or apply migration across both repositories."""
    pr_modified_a = get_open_pr_modified_files(repo_a_path)
    wt_dirty_a = get_other_worktree_modified_files(repo_a_path)

    items_a = inventory_repo_documents(
        repo_id="eval-lab",
        repo_root=repo_a_path,
        target_dirs=["research/inbox", "research/analysis", "research/explorations"],
        pr_modified_files=pr_modified_a,
        worktree_dirty_files=wt_dirty_a,
    )

    items_b = inventory_repo_documents(
        repo_id="research-context",
        repo_root=repo_b_path,
        target_dirs=["trajectory-analysis"],
    )

    all_items = items_a + items_b
    audit_collisions(all_items)

    repos = [("eval-lab", repo_a_path), ("research-context", repo_b_path)]
    link_rewrites = build_link_rewrite_plan(all_items, repos)

    report_content = generate_migration_report(all_items, link_rewrites, report_path)

    if apply:
        renamed, rewritten = apply_migration(all_items, link_rewrites)
        if verbose:
            print(f"[APPLIED] Renamed {renamed} files and updated {rewritten} links.")
    else:
        if verbose:
            print(f"[DRY-RUN] Audited {len(all_items)} files. Report written to {report_path}")

    return all_items, link_rewrites, report_content


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dated document naming migration tool (defaults to dry-run)."
    )
    workspace_root = Path(__file__).resolve().parents[1]
    default_repo_a = (
        workspace_root
        if (workspace_root / "research").exists()
        else Path("/Users/petermakhnatch/Developer/eval-lab")
    )
    default_repo_b = Path("/Users/petermakhnatch/Developer/research-context")
    default_report = default_repo_a / "research/analysis/document-naming-migration-2026-08-31.md"

    parser.add_argument(
        "--repo-a", type=Path, default=default_repo_a, help="Path to eval-lab repository root"
    )
    parser.add_argument(
        "--repo-b",
        type=Path,
        default=default_repo_b,
        help="Path to research-context repository root",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=default_report,
        help="Path to output migration report markdown",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Perform dry-run without modifying files (default)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Explicitly apply renames and front-matter updates",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", default=False, help="Verbose output"
    )

    args = parser.parse_args()

    apply_mode = args.apply

    print(f"Starting Document Naming Migration (mode: {'APPLY' if apply_mode else 'DRY-RUN'})...")
    print(f"Repo A (eval-lab): {args.repo_a}")
    print(f"Repo B (research-context): {args.repo_b}")
    print(f"Report Output: {args.report}")

    items, rewrites, report_content = run_migration(
        repo_a_path=args.repo_a.resolve(),
        repo_b_path=args.repo_b.resolve(),
        report_path=args.report.resolve(),
        apply=apply_mode,
        verbose=True,
    )

    total_conformant = sum(
        1 for it in items if it.classification == "already-conformant" and not it.refused
    )
    total_suffixed = sum(
        1 for it in items if it.classification == "date-suffixed" and not it.refused
    )
    total_undated = sum(1 for it in items if it.classification == "undated" and not it.refused)
    total_refused = sum(1 for it in items if it.refused)

    print("\n=== CLASSIFICATION SUMMARY ===")
    print(f"Already Conformant: {total_conformant}")
    print(f"Date-Suffixed:      {total_suffixed}")
    print(f"Undated:            {total_undated}")
    print(f"Refused (Unsafe):   {total_refused}")
    print(f"Total Scanned:      {len(items)}")
    print(f"Link Rewrites:      {len(rewrites)}")
    print(f"\nMigration report generated at: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
