"""Context Pack Compiler (WS-B).

Purpose: context engineering as code. Agents get a compiled, deterministic
context bundle per mission type instead of an unbounded docs/ crawl.

Flow:
1. Select docs where `status == "living"` and `mission_type in audience`.
2. (Optional) If `--task <task_ref>` is provided: query `craft.parquet` for
   task facets and append relevant craft patterns/facets.
3. Append mission brief / instructions template.
4. Compute deterministic content SHA-256 hash.
5. Emit a clean, compiled markdown document with header `generated-by: contextpack v1`.

Entry point: `python -m evallab.contextpack build <mission_type> [--task REF] [-o out.md]`
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from evallab.storage.paths import derived_root_from_environment

CONTEXTPACK_VERSION = "contextpack v1"
HEADER_PREFIX = "<!-- generated-by: contextpack v1 -->"
VALID_MISSION_TYPES = ("builder", "analyst", "runner", "operator")
VALID_STATUSES = ("living", "historical")
VALID_AUDIENCES = ("builder", "analyst", "runner", "operator")
DEFAULT_TOKEN_BUDGET: int = 12_000
CHARS_PER_TOKEN: int = 4
FRONT_MATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def repo_root() -> Path:
    """Return the repository root for this checkout."""
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class DocMetadata:
    """Metadata and content for a repository documentation file."""

    path: str
    title: str
    status: str
    audience: tuple[str, ...]
    body: str
    raw_content: str
    content_digest: str

    def matches_mission(self, mission_type: str) -> bool:
        """Check if this document is living and intended for the given mission type."""
        return self.status == "living" and mission_type.lower() in [
            a.lower() for a in self.audience
        ]


@dataclass(frozen=True)
class TaskFacetSummary:
    """Facet summary extracted from craft.parquet for a specific task."""

    task_ref: str
    source_repo: str | None = None
    task_digest: str | None = None
    verifier_type: str | None = None
    verifier_signals: tuple[str, ...] = ()
    anti_cheat: tuple[str, ...] = ()
    answer_hiding: str | None = None
    env_languages: tuple[str, ...] = ()
    env_services_n: int | None = None
    env_multi_container: bool | None = None
    pinned_deps: bool | None = None
    base_image_pin: str | None = None
    human_minutes: int | None = None
    instruction_chars: int | None = None
    difficulty_mechanism: str | None = None
    instruction_style: str | None = None
    unresolved_facets: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Convert facet summary to a clean dictionary."""
        return {
            "task_ref": self.task_ref,
            "source_repo": self.source_repo,
            "task_digest": self.task_digest,
            "verifier_type": self.verifier_type,
            "verifier_signals": list(self.verifier_signals),
            "anti_cheat": list(self.anti_cheat),
            "answer_hiding": self.answer_hiding,
            "env_languages": list(self.env_languages),
            "env_services_n": self.env_services_n,
            "env_multi_container": self.env_multi_container,
            "pinned_deps": self.pinned_deps,
            "base_image_pin": self.base_image_pin,
            "human_minutes": self.human_minutes,
            "instruction_chars": self.instruction_chars,
            "difficulty_mechanism": self.difficulty_mechanism,
            "instruction_style": self.instruction_style,
            "unresolved_facets": list(self.unresolved_facets),
        }


@dataclass(frozen=True)
class ContextPackResult:
    """The compiled context pack output and associated metadata."""

    mission_type: str
    task_ref: str | None
    docs: tuple[DocMetadata, ...]
    task_facets: TaskFacetSummary | None
    content_hash: str
    markdown: str
    token_budget: int | None = None
    estimated_tokens: int = 0
    truncated: bool = False
    dropped_items: tuple[str, ...] = ()
    tokens_shed: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert result to a JSON-serializable dictionary."""
        return {
            "generator": CONTEXTPACK_VERSION,
            "mission_type": self.mission_type,
            "task_ref": self.task_ref,
            "content_hash": self.content_hash,
            "doc_count": len(self.docs),
            "docs": [
                {
                    "path": d.path,
                    "title": d.title,
                    "status": d.status,
                    "audience": list(d.audience),
                    "digest": d.content_digest,
                }
                for d in self.docs
            ],
            "task_facets": self.task_facets.to_dict() if self.task_facets else None,
            "token_budget": self.token_budget,
            "estimated_tokens": self.estimated_tokens,
            "truncated": self.truncated,
            "dropped_items": list(self.dropped_items),
            "tokens_shed": self.tokens_shed,
        }


def parse_front_matter(content: str) -> tuple[dict[str, Any] | None, str]:
    """Extract YAML front-matter and body from markdown content."""
    match = FRONT_MATTER_PATTERN.match(content)
    if not match:
        return None, content.strip()

    front_matter_raw = match.group(1)
    body = content[match.end() :].strip()

    try:
        parsed = yaml.safe_load(front_matter_raw)
        if isinstance(parsed, dict):
            return parsed, body
    except Exception:
        pass

    return None, content.strip()


def extract_title(body: str, fallback: str) -> str:
    """Extract first markdown H1 header title or use fallback."""
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def parse_doc(file_path: Path, root: Path | None = None) -> DocMetadata:
    """Parse a single markdown documentation file into DocMetadata."""
    resolved_root = root if root is not None else repo_root()
    content = file_path.read_text(encoding="utf-8")
    content_digest = f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"

    fm, body = parse_front_matter(content)
    rel_path = file_path.resolve().relative_to(resolved_root.resolve()).as_posix()
    title = extract_title(body, file_path.stem.replace("-", " ").capitalize())

    status = "unclassified"
    audience: list[str] = []

    if fm:
        raw_status = str(fm.get("status", "")).strip().lower()
        if raw_status in VALID_STATUSES:
            status = raw_status

        raw_audience = fm.get("audience", [])
        if isinstance(raw_audience, str):
            audience = [raw_audience.strip().lower()]
        elif isinstance(raw_audience, (list, tuple)):
            audience = [str(a).strip().lower() for a in raw_audience]

    return DocMetadata(
        path=rel_path,
        title=title,
        status=status,
        audience=tuple(audience),
        body=body,
        raw_content=content,
        content_digest=content_digest,
    )


def select_docs(
    docs_dir: Path,
    mission_type: str,
    root: Path | None = None,
) -> list[DocMetadata]:
    """Discover and filter documentation files for a given mission type."""
    resolved_root = root if root is not None else repo_root()
    if not docs_dir.is_dir():
        return []

    candidates: list[Path] = []
    # Collect markdown files in docs/ and living subdirectories (e.g. research/)
    for path in sorted(docs_dir.glob("*.md")):
        if path.is_file() and not path.name.startswith("."):
            candidates.append(path)

    research_dir = docs_dir / "research"
    if research_dir.is_dir():
        for path in sorted(research_dir.glob("*.md")):
            if path.is_file() and not path.name.startswith("."):
                candidates.append(path)

    selected: list[DocMetadata] = []
    for path in sorted(candidates, key=lambda p: p.as_posix()):
        doc = parse_doc(path, root=resolved_root)
        if doc.matches_mission(mission_type):
            selected.append(doc)

    return selected


discover_docs = select_docs


def query_task_facets(
    task_ref: str,
    parquet_path: Path | None = None,
    root: Path | None = None,
) -> TaskFacetSummary | None:
    """Query craft.parquet for task facets for the given task reference."""
    resolved_root = root if root is not None else repo_root()

    if parquet_path is None:
        try:
            droot = derived_root_from_environment(resolved_root)
            candidate_path = droot / "craft" / "craft.parquet"
        except Exception:
            candidate_path = resolved_root / "derived" / "parquet" / "craft" / "craft.parquet"
    else:
        candidate_path = parquet_path

    if not candidate_path.is_file():
        # Fallback check directly in derived/parquet
        alt_path = resolved_root / "derived" / "parquet" / "craft" / "craft.parquet"
        if alt_path.is_file():
            candidate_path = alt_path
        else:
            return None

    try:
        import duckdb

        con = duckdb.connect()
        query = (
            f"SELECT * FROM read_parquet('{candidate_path.as_posix()}') "
            "WHERE task_ref = ? OR task_ref LIKE ?"
        )
        cursor = con.execute(query, [task_ref, f"%/{task_ref}"])
        cols = [d[0] for d in cursor.description]
        row = cursor.fetchone()
        if not row:
            return None

        record = dict(zip(cols, row, strict=False))

        def _to_tuple(val: Any) -> tuple[str, ...]:
            if val is None:
                return ()
            if isinstance(val, (list, tuple)):
                return tuple(str(x) for x in val)
            return (str(val),)

        return TaskFacetSummary(
            task_ref=str(record.get("task_ref", task_ref)),
            source_repo=record.get("source_repo"),
            task_digest=record.get("task_digest"),
            verifier_type=record.get("verifier_type"),
            verifier_signals=_to_tuple(record.get("verifier_signals")),
            anti_cheat=_to_tuple(record.get("anti_cheat")),
            answer_hiding=record.get("answer_hiding"),
            env_languages=_to_tuple(record.get("env_languages")),
            env_services_n=record.get("env_services_n"),
            env_multi_container=record.get("env_multi_container"),
            pinned_deps=record.get("pinned_deps"),
            base_image_pin=record.get("base_image_pin"),
            human_minutes=record.get("human_minutes"),
            instruction_chars=record.get("instruction_chars"),
            difficulty_mechanism=record.get("difficulty_mechanism"),
            instruction_style=record.get("instruction_style"),
            unresolved_facets=_to_tuple(record.get("unresolved_facets")),
        )
    except Exception as exc:
        print(f"warning: failed to query craft.parquet: {exc}", file=sys.stderr)
        return None


def render_task_facets_section(facets: TaskFacetSummary) -> str:
    """Render a structured markdown section of task facets and CRAFT patterns."""
    lines: list[str] = []
    lines.append(f"## Task Design Facets & CRAFT Patterns: `{facets.task_ref}`")
    lines.append("")
    lines.append(
        f"Corpus task `{facets.task_ref}` facets resolved from CRAFT task-corpus analysis:"
    )
    lines.append("")
    lines.append("| Facet | Value | Meaning / Implication |")
    lines.append("|---|---|---|")
    lines.append(
        f"| **Source Repo** | `{facets.source_repo or 'unknown'}` | Origin dataset / corpus |"
    )
    lines.append(
        f"| **Task Digest** | `{facets.task_digest or 'uncomputed'}` | Pinned content hash |"
    )
    lines.append(
        f"| **Verifier Type** | `{facets.verifier_type or 'unclassified'}` | "
        "Verification mechanism class |"
    )
    v_sigs = ", ".join(facets.verifier_signals) or "none"
    lines.append(f"| **Verifier Signals** | `{v_sigs}` | Structural evidence detected |")
    a_cheat = ", ".join(facets.anti_cheat) or "none_observed"
    lines.append(f"| **Anti-Cheat** | `{a_cheat}` | Cheating mitigation techniques |")
    lines.append(
        f"| **Answer Hiding** | `{facets.answer_hiding or 'none_observed'}` | "
        "Test code isolation strategy |"
    )
    e_langs = ", ".join(facets.env_languages) or "unspecified"
    lines.append(f"| **Languages** | `{e_langs}` | Primary environment runtimes |")
    lines.append(
        f"| **Multi-Container** | `{facets.env_multi_container}` "
        f"(services: {facets.env_services_n or 1}) | Compose vs lone Dockerfile |"
    )
    lines.append(
        f"| **Pinned Dependencies** | `{facets.pinned_deps}` | Package version repeatability |"
    )
    b_pin = facets.base_image_pin or "none"
    lines.append(f"| **Base Image Pin** | `{b_pin}` | Docker base reference style |")
    if facets.human_minutes is not None:
        hours = round(facets.human_minutes / 60, 1)
        lines.append(
            f"| **Human Time Anchor** | `{facets.human_minutes}m` (~{hours}h) | "
            "Stated baseline solution time |"
        )
    lines.append("")

    lines.append("### Applicable Eval Design Patterns")
    lines.append("")

    # Verifier pattern recommendations
    vtype = facets.verifier_type or ""
    vsignals = facets.verifier_signals or ()
    if vtype == "pytest" or "pytest" in vsignals:
        lines.append("#### 1. Pytest Verifier Pattern")
        lines.append("- Use discrete assertions with descriptive error messages.")
        lines.append("- Parse structured return values or files, never raw stdout strings.")
        lines.append("- Keep test files strictly immutable during trial execution.")
    elif vtype == "golden_file" or "golden_file" in vsignals:
        lines.append("#### 1. Golden File / Output Diff Pattern")
        lines.append(
            "- Normalize line endings (`\\r\\n` -> `\\n`), trailing whitespace, and float rounding."
        )
        lines.append(
            "- Never fail on cosmetic formatting differences unless formatting is the objective."
        )
    elif vtype == "hybrid" or ("pytest" in vsignals and "golden_file" in vsignals):
        lines.append("#### 1. Hybrid Verifier Pattern")
        lines.append("- Unit tests verify internal code state and function contracts.")
        lines.append("- Artifact diffs verify exported file shapes and side effects.")
    else:
        lines.append("#### 1. Deterministic Verifier Pattern")
        lines.append(
            "- Verifier must return binary pass/fail without ambient network or model dependencies."
        )

    lines.append("")

    # Anti-cheat pattern recommendations
    if "hidden_tests" in facets.anti_cheat or "answer_outside_image" in facets.anti_cheat:
        lines.append("#### 2. Clean-Room Anti-Cheat Pattern")
        lines.append(
            "- Run evaluation in a separate verifier container or post-trial mounting step."
        )
        lines.append(
            "- Prohibit solution strings and test answers from being baked into candidate image."
        )
    else:
        lines.append("#### 2. Isolation & Anti-Tampering Pattern")
        lines.append(
            "- Ensure verifier scripts are owned by root and non-writable by the agent user."
        )

    lines.append("")

    # Reproducibility pattern recommendations
    if facets.pinned_deps is False or facets.base_image_pin != "digest":
        lines.append("#### 3. Strict Environment Pinning Pattern")
        lines.append(
            "- Lock all package dependencies (uv.lock, requirements.txt with hashes, package-lock)."
        )
        lines.append("- Pin base Docker images to sha256 digests rather than mutable tags.")
    else:
        lines.append("#### 3. Deterministic Build Pattern")
        lines.append(
            "- Build context contains all required files locally without remote downloads."
        )

    lines.append("")
    return "\n".join(lines)


def render_mission_brief_template(mission_type: str, task_ref: str | None = None) -> str:
    """Render mission-specific execution instructions and contract."""
    lines: list[str] = []
    lines.append(f"## Mission Brief & Execution Guide: `{mission_type}`")
    lines.append("")

    if mission_type == "builder":
        lines.append("### Objective: Authoring & Quality Certification")
        lines.append(
            "You are operating as a **Builder** agent. "
            "Your goal is to author, calibrate, or qualify Harbor evaluation tasks."
        )
        lines.append("")
        lines.append("### Standard Workflow")
        lines.append(
            "1. **Task Package Layout**: Maintain standard Harbor task layout "
            "(`task.toml`, `instruction.md`, `environment/`, `tests/`, `solution/`)."
        )
        lines.append(
            "2. **Local Free Controls**: Always provide an `oracle` "
            "(clean ground-truth solution) and test against `nop`."
        )
        lines.append(
            "3. **Adversarial Probes**: Provide at least 3 invalid/adversarial "
            "solutions that the verifier correctly rejects."
        )
        lines.append(
            "4. **Workbench Certification**: Run "
            "`uv run python -m evallab.task_workbench review <candidate_dir>` "
            "to verify isolation and safety."
        )
        lines.append(
            "5. **Handoff Contract**: Update `agents/handoffs/<role>.md` "
            "with `Status: review-wanted` when ready for human gate."
        )
    elif mission_type == "analyst":
        lines.append("### Objective: Evidence & Trajectory Analysis")
        lines.append(
            "You are operating as an **Analyst** agent. "
            "Your goal is to extract facts, classify failure modes, and synthesize findings."
        )
        lines.append("")
        lines.append("### Standard Workflow")
        lines.append(
            "1. **Trace & Evidence Inspection**: Read canonical trial artifacts under "
            "`runs/<job_id>/` and Parquet facts under `derived/parquet/`."
        )
        lines.append(
            "2. **ATIF Citation Grounding**: Every model classification must resolve "
            "to exact step and tool call indices."
        )
        lines.append(
            "3. **Sidecar Validation**: Ensure analysis records conform to "
            "`schemas.AnalysisSidecar` before writing."
        )
        lines.append(
            "4. **Statistical Rigor**: Use clustered standard errors and exact "
            "confidence intervals; never report unclustered pass@k means."
        )
    elif mission_type == "runner":
        lines.append("### Objective: Experiment Execution & Queue Management")
        lines.append(
            "You are operating as a **Runner** agent. "
            "Your goal is to safely execute Harbor trials through the queue."
        )
        lines.append("")
        lines.append("### Standard Workflow")
        lines.append(
            "1. **Preflight Check**: Run `uv run evallab preflight` before dispatch "
            "to ensure quota headroom and profile health."
        )
        lines.append(
            "2. **Required Purpose**: Every `ExperimentSpec` must declare a valid `purpose` "
            "(`baseline|comparison|elicitation|drift|calibration|craft|practice`)."
        )
        lines.append(
            "3. **Subscription Authentication**: Use `oracle` / `nop` for testing; "
            "use subscription keys strictly under policy limits."
        )
        lines.append(
            "4. **Evidence Preservation**: Never alter raw job directories once written."
        )
    elif mission_type == "operator":
        lines.append("### Objective: Platform Operations & Fleet Health")
        lines.append(
            "You are operating as an **Operator** agent. "
            "Your goal is to maintain lab infrastructure, database hygiene, and fleet health."
        )
        lines.append("")
        lines.append("### Standard Workflow")
        lines.append(
            "1. **System Health Check**: Run `uv run evallab doctor` to verify local Docker, "
            "PostgreSQL, and storage headroom."
        )
        lines.append(
            "2. **Daily Digest & Status**: Monitor `digests/YYYY-MM-DD.md` and fleet handoffs "
            "under `agents/handoffs/`."
        )
        lines.append(
            "3. **Database Maintenance**: Manage PostgreSQL catalog and Parquet views; "
            "perform routine backups."
        )
        lines.append(
            "4. **Isolated Worktrees**: Coordinate writers across `.worktrees/<role>` "
            "leases to prevent file collisions."
        )

    lines.append("")
    if task_ref:
        lines.append(f"### Target Task Reference: `{task_ref}`")
        lines.append(
            f"All operations in this mission should benchmark against task `{task_ref}`."
        )
        lines.append("")

    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    """Estimate token count from text length using standard 4 chars/token heuristic.

    Uses ceiling integer arithmetic: `(len(text) + (CHARS_PER_TOKEN - 1)) // CHARS_PER_TOKEN`.
    Empty string returns 0.
    """
    if not text:
        return 0
    return (len(text) + (CHARS_PER_TOKEN - 1)) // CHARS_PER_TOKEN


def doc_priority_key(doc: DocMetadata, mission_type: str) -> tuple[int, int, int, str]:
    """Calculate truncation priority sort key for living docs (lowest priority first).

    Priority hierarchy (most expendable to most essential):
    - Category 0: Supplemental research notes (`docs/research/*`) and
      catalog indexes (`docs/INDEX.md`, `docs/repo-map.md`).
    - Category 1: Broad multi-audience platform references (3 or 4 audiences).
    - Category 2: Dual-audience technical specifications (2 audiences).
    - Category 3: Single-audience primary mission workbenches/guides (1 audience).
    Tie-breakers:
    - Audience count ascending (fewer audiences = more specialized = higher priority).
    - Document length descending (larger docs shed more tokens earlier within the same category).
    - Lexicographical file path (deterministic tie-breaker).
    """
    is_research = doc.path.startswith("docs/research/")
    is_index = doc.path in ("docs/INDEX.md", "docs/repo-map.md")
    if is_research or is_index:
        cat = 0
    elif len(doc.audience) >= 3:
        cat = 1
    elif len(doc.audience) == 2:
        cat = 2
    else:
        cat = 3
    return (cat, -len(doc.audience), -len(doc.body), doc.path)


def _render_pack_body(
    mission_type: str,
    task_ref: str | None,
    docs: Sequence[DocMetadata],
    task_facets: TaskFacetSummary | None,
    dropped_items: Sequence[tuple[str, int]],
    token_budget: int | None,
    untruncated_tokens: int,
    untruncated_chars: int,
    include_task_facets: bool = True,
) -> str:
    """Render canonical markdown body of the context pack."""
    body_sections: list[str] = []

    # Title & Metadata
    body_sections.append(f"# Context Pack: {mission_type.capitalize()} Mission")
    body_sections.append("")
    body_sections.append(
        f"> Mission Type: `{mission_type}` | Target Task: `{task_ref or 'none'}` | "
        f"Living Docs Included: {len(docs)}"
    )
    body_sections.append("")

    # Truncation Notice (if items were dropped due to budget constraints)
    if dropped_items and token_budget is not None:
        total_tokens_shed = sum(tok for _, tok in dropped_items)
        body_sections.append("### ⚠️ Context Pack Truncation Notice")
        body_sections.append("")
        budget_chars = token_budget * CHARS_PER_TOKEN
        body_sections.append(
            f"This context pack exceeded the configured token budget of {token_budget:,} tokens "
            f"(~{budget_chars:,} chars) and was truncated per v2 §6 priority policy "
            "(most-expendable content shed first; mission brief and instructions protected)."
        )
        body_sections.append("")
        body_sections.append(
            f"- **Configured Token Budget**: {token_budget:,} tokens (~{budget_chars:,} chars)"
        )
        body_sections.append(
            f"- **Estimated Untruncated Size**: ~{untruncated_tokens:,} tokens "
            f"(~{untruncated_chars:,} chars)"
        )
        body_sections.append(
            f"- **Tokens Shed**: ~{total_tokens_shed:,} tokens across "
            f"{len(dropped_items)} dropped section(s)/doc(s)"
        )
        body_sections.append(f"- **Retained Living Docs**: {len(docs)}")
        body_sections.append("- **Dropped Items (in order shed)**:")
        for item_name, tok_count in dropped_items:
            body_sections.append(f"  - `{item_name}` (~{tok_count:,} tokens shed)")
        body_sections.append("")
        body_sections.append("---")
        body_sections.append("")

    # Table of Contents
    body_sections.append("## Index of Living Documentation")
    body_sections.append("")
    for i, doc in enumerate(docs, start=1):
        anchor = doc.path.replace("/", "-").replace(".", "-")
        aud_str = ", ".join(doc.audience)
        body_sections.append(f"{i}. [{doc.title}](#{anchor}) — `{doc.path}` ({aud_str})")
    body_sections.append("")

    # Compiled Living Documentation
    body_sections.append("---")
    body_sections.append("")
    for doc in docs:
        anchor = doc.path.replace("/", "-").replace(".", "-")
        body_sections.append(f'<a id="{anchor}"></a>')
        body_sections.append(f"## {doc.title}")
        aud_list = ", ".join(doc.audience)
        body_sections.append(
            f"*Source: `{doc.path}` | Status: `{doc.status}` | Audience: `[{aud_list}]`*"
        )
        body_sections.append("")
        body_sections.append(doc.body)
        body_sections.append("")
        body_sections.append("---")
        body_sections.append("")

    # Task Facets and CRAFT Patterns (if task ref provided and retained)
    if include_task_facets:
        if task_facets:
            body_sections.append(render_task_facets_section(task_facets))
            body_sections.append("---")
            body_sections.append("")
        elif task_ref:
            body_sections.append(f"## Target Task: `{task_ref}`")
            body_sections.append("")
            body_sections.append(
                f"Task `{task_ref}` was requested, but facets were not found in `craft.parquet`."
            )
            body_sections.append(
                "Author or evaluate this task following standard Harbor and CRAFT principles."
            )
            body_sections.append("")
            body_sections.append("---")
            body_sections.append("")

    # Mission Brief & Instructions (INVIOLABLE - NEVER DROPPED)
    body_sections.append(render_mission_brief_template(mission_type, task_ref=task_ref))

    return "\n".join(body_sections).strip() + "\n"


def _format_full_markdown(
    mission_type: str,
    task_ref: str | None,
    doc_count: int,
    canonical_body: str,
    content_hash: str,
    truncated: bool,
    token_budget: int | None,
    tokens_shed: int,
) -> str:
    """Format final compiled markdown with header comments and content digest."""
    header_lines = [
        HEADER_PREFIX,
        f"<!-- mission-type: {mission_type} -->",
        f"<!-- target-task: {task_ref or 'none'} -->",
        f"<!-- doc-count: {doc_count} -->",
    ]
    if truncated:
        header_lines.append("<!-- truncated: true -->")
        if token_budget is not None:
            header_lines.append(f"<!-- token-budget: {token_budget} -->")
        header_lines.append(f"<!-- tokens-shed: {tokens_shed} -->")
    header_lines.append(f"<!-- content-sha256: {content_hash} -->")
    header_lines.append("")
    return "\n".join(header_lines) + canonical_body


def build_context_pack(
    mission_type: str,
    *,
    task_ref: str | None = None,
    docs_dir: Path | None = None,
    parquet_path: Path | None = None,
    root: Path | None = None,
    token_budget: int | None = DEFAULT_TOKEN_BUDGET,
) -> ContextPackResult:
    """Compile a deterministic context pack for the requested mission type.

    Enforces a token budget (default 12k tokens, overridable) with deterministic
    priority-ordered truncation when the budget binds.
    """
    resolved_root = root if root is not None else repo_root()
    resolved_docs_dir = docs_dir if docs_dir is not None else resolved_root / "docs"

    if mission_type not in VALID_MISSION_TYPES:
        valid_types_str = ", ".join(VALID_MISSION_TYPES)
        raise ValueError(
            f"Invalid mission type '{mission_type}'. Must be one of: {valid_types_str}"
        )

    # 1. Select living docs matching the mission type
    docs = select_docs(resolved_docs_dir, mission_type, root=resolved_root)

    # 2. Query task facets if task reference is provided
    task_facets: TaskFacetSummary | None = None
    if task_ref:
        task_facets = query_task_facets(task_ref, parquet_path=parquet_path, root=resolved_root)

    # 3. Assemble full untruncated canonical body
    untruncated_body = _render_pack_body(
        mission_type,
        task_ref,
        docs,
        task_facets,
        dropped_items=(),
        token_budget=None,
        untruncated_tokens=0,
        untruncated_chars=0,
        include_task_facets=True,
    )
    untruncated_hash = f"sha256:{hashlib.sha256(untruncated_body.encode('utf-8')).hexdigest()}"
    untruncated_md = _format_full_markdown(
        mission_type,
        task_ref,
        len(docs),
        untruncated_body,
        untruncated_hash,
        truncated=False,
        token_budget=None,
        tokens_shed=0,
    )
    untruncated_tokens = estimate_tokens(untruncated_md)
    untruncated_chars = len(untruncated_md)

    # 4. Check if token budget binds
    if token_budget is None or token_budget <= 0 or untruncated_tokens <= token_budget:
        return ContextPackResult(
            mission_type=mission_type,
            task_ref=task_ref,
            docs=tuple(docs),
            task_facets=task_facets,
            content_hash=untruncated_hash,
            markdown=untruncated_md,
            token_budget=token_budget,
            estimated_tokens=untruncated_tokens,
            truncated=False,
            dropped_items=(),
            tokens_shed=0,
        )

    # 5. Priority-ordered truncation
    drop_order = sorted(docs, key=lambda d: doc_priority_key(d, mission_type))
    retained_docs = list(docs)
    dropped_items: list[tuple[str, int]] = []
    include_task_facets = True

    curr_body = untruncated_body
    curr_hash = untruncated_hash
    curr_md = untruncated_md

    # Phase 1: Drop living docs from most expendable to most essential
    for doc_to_drop in drop_order:
        if estimate_tokens(curr_md) <= token_budget:
            break
        retained_docs.remove(doc_to_drop)
        # Keep retained docs sorted alphabetically by path
        retained_docs = sorted(retained_docs, key=lambda d: d.path)
        doc_tok = estimate_tokens(doc_to_drop.body)
        dropped_items.append((doc_to_drop.path, doc_tok))
        curr_body = _render_pack_body(
            mission_type,
            task_ref,
            retained_docs,
            task_facets,
            dropped_items=dropped_items,
            token_budget=token_budget,
            untruncated_tokens=untruncated_tokens,
            untruncated_chars=untruncated_chars,
            include_task_facets=include_task_facets,
        )
        curr_hash = f"sha256:{hashlib.sha256(curr_body.encode('utf-8')).hexdigest()}"
        tokens_shed = sum(tok for _, tok in dropped_items)
        curr_md = _format_full_markdown(
            mission_type,
            task_ref,
            len(retained_docs),
            curr_body,
            curr_hash,
            truncated=True,
            token_budget=token_budget,
            tokens_shed=tokens_shed,
        )

    # Phase 2: If all docs dropped and still over budget, drop task facets if present
    has_task = task_facets is not None or task_ref is not None
    if estimate_tokens(curr_md) > token_budget and has_task:
        facets_name = f"task-facets:{task_ref or 'target'}"
        dropped_items.append((facets_name, 500))
        curr_body = _render_pack_body(
            mission_type,
            task_ref,
            retained_docs,
            task_facets,
            dropped_items=dropped_items,
            token_budget=token_budget,
            untruncated_tokens=untruncated_tokens,
            untruncated_chars=untruncated_chars,
            include_task_facets=False,
        )
        curr_hash = f"sha256:{hashlib.sha256(curr_body.encode('utf-8')).hexdigest()}"
        tokens_shed = sum(tok for _, tok in dropped_items)
        curr_md = _format_full_markdown(
            mission_type,
            task_ref,
            len(retained_docs),
            curr_body,
            curr_hash,
            truncated=True,
            token_budget=token_budget,
            tokens_shed=tokens_shed,
        )

    # Final result
    total_tokens_shed = sum(tok for _, tok in dropped_items)
    final_tokens = estimate_tokens(curr_md)

    return ContextPackResult(
        mission_type=mission_type,
        task_ref=task_ref,
        docs=tuple(retained_docs),
        task_facets=task_facets if include_task_facets else None,
        content_hash=curr_hash,
        markdown=curr_md,
        token_budget=token_budget,
        estimated_tokens=final_tokens,
        truncated=True,
        dropped_items=tuple(name for name, _ in dropped_items),
        tokens_shed=total_tokens_shed,
    )


def build_parser() -> argparse.ArgumentParser:
    """Construct command-line argument parser for context pack compiler."""
    parser = argparse.ArgumentParser(
        prog="contextpack",
        description="Compile deterministic, audience-filtered context packs for eval-lab missions.",
    )
    subparsers = parser.add_subparsers(dest="command", help="Subcommand to execute")

    # `build` subcommand
    build_cmd = subparsers.add_parser("build", help="Build a context pack for a mission type")
    build_cmd.add_argument(
        "mission_type",
        choices=VALID_MISSION_TYPES,
        help="Target mission type (builder, analyst, runner, operator)",
    )
    build_cmd.add_argument(
        "--task",
        metavar="REF",
        default=None,
        help="Target task reference (e.g. terminal-bench/atrx-vep-crispr)",
    )
    build_cmd.add_argument(
        "-o",
        "--out",
        type=Path,
        metavar="FILE",
        default=None,
        help="Path to write compiled markdown context pack",
    )
    build_cmd.add_argument(
        "--docs-dir",
        type=Path,
        metavar="DIR",
        default=None,
        help="Directory containing living docs (defaults to docs/)",
    )
    build_cmd.add_argument(
        "--parquet",
        type=Path,
        metavar="FILE",
        default=None,
        help="Path to craft.parquet (defaults to derived/parquet/craft/craft.parquet)",
    )
    build_cmd.add_argument(
        "--budget",
        "--token-budget",
        type=int,
        metavar="TOKENS",
        default=DEFAULT_TOKEN_BUDGET,
        help=f"Token budget ceiling (default {DEFAULT_TOKEN_BUDGET} per v2 §6; 0 for unlimited)",
    )
    build_cmd.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON summary instead of raw markdown",
    )

    # `list-docs` subcommand
    list_cmd = subparsers.add_parser(
        "list-docs", help="List docs with their status and audience tags"
    )
    list_cmd.add_argument(
        "--docs-dir",
        type=Path,
        metavar="DIR",
        default=None,
        help="Directory containing living docs (defaults to docs/)",
    )
    list_cmd.add_argument(
        "--status",
        choices=("all", *VALID_STATUSES),
        default="all",
        help="Filter by status (all, living, historical)",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for context pack compiler."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    root = repo_root()

    if args.command == "list-docs":
        docs_dir = args.docs_dir if args.docs_dir else root / "docs"
        if not docs_dir.is_dir():
            print(f"error: docs directory not found: {docs_dir}", file=sys.stderr)
            return 1

        all_mds = sorted(docs_dir.glob("*.md"))
        research_dir = docs_dir / "research"
        if research_dir.is_dir():
            all_mds.extend(sorted(research_dir.glob("*.md")))

        print("| Path | Status | Audience | Title |")
        print("|---|---|---|---|")
        for path in sorted(all_mds, key=lambda p: p.as_posix()):
            doc = parse_doc(path, root=root)
            if args.status != "all" and doc.status != args.status:
                continue
            audience_str = ", ".join(doc.audience) if doc.audience else "(none)"
            print(f"| `{doc.path}` | `{doc.status}` | `{audience_str}` | {doc.title} |")
        return 0

    if args.command == "build":
        budget_val = args.budget if (args.budget is not None and args.budget > 0) else None
        try:
            result = build_context_pack(
                args.mission_type,
                task_ref=args.task,
                docs_dir=args.docs_dir,
                parquet_path=args.parquet,
                root=root,
                token_budget=budget_val,
            )
        except Exception as exc:
            print(f"error: failed to build context pack: {exc}", file=sys.stderr)
            return 1

        if args.out:
            out_path = args.out.expanduser().resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(result.markdown, encoding="utf-8")
            if args.json:
                payload = result.to_dict()
                payload["output_file"] = out_path.as_posix()
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(
                    f"Compiled context pack for {args.mission_type} "
                    f"({len(result.docs)} docs, {result.content_hash}) -> {out_path}"
                )
        else:
            if args.json:
                print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            else:
                print(result.markdown)

        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
