"""Hackbook catalog models and queries (``evallab.hackbook.entry/v0``).

The Hackbook is a versioned catalog of known ways agents cheat and tasks
break. Entries live in ``research/hackbook/entries.jsonl``; this module
validates them and answers list/show/search queries over them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from evallab.qa.models import Category

ENTRY_SCHEMA = "evallab.hackbook.entry/v0"
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_HACKBOOK_DIR = Path("research/hackbook")
TB4_AUDIT_DIR = Path("research/answer-keys/tb4-audit-v1")

Side = Literal["task", "run"]
Direction = Literal["false_pass", "false_fail", "either"]
# executed: a witness or differential replay was run against the grader; observed: seen in an
# archived real agent run by a deterministic check of the submission, actions or verifier output,
# not replayed; claimed: the source says so.
EvidenceStatus = Literal["executed", "observed", "claimed"]
Source = Literal[
    "preference_model_tb4",
    "benchjack",
    "cheatbench",
    "terminal_wrench",
    "hacktrace",
    "envcheck",
    "mimo_atlas",
    "eval3",
    "tb2_to_tb21",
    "bespoke_tb4",
    "automationbench_verified",
    "rea_environments",
    "wandr",
    "reward_seeker",
    "tb4_run_census",
]
ExampleSource = Literal[
    Source,
    Literal["taxonomy_v1", "delphik_open_defect"],
]
RecipeKind = Literal["static_check", "tripwire_rule", "probe_test"]
MethodId = Literal[
    "reference_mutation",
    "differential_tests",
    "reward_seeker",
    "static_review",
    "failure_clustering",
    "trace_judge",
    "tripwires",
    "fixed_task_rerun",
    "controls",
    "equivalent_rewrite",
    "grader_fuzz",
]

#: The 25 canonical structural signals shared with ``entries.jsonl``.
STRUCTURAL_SIGNALS: tuple[str, ...] = (
    "unread_output_mentions",
    "single_seed_or_no_repeat",
    "surface_only_workbook_check",
    "unpinned_or_untimed_warmup",
    "boolean_flag_or_status_trusted",
    "missing_negative_or_clean_preservation_check",
    "untested_reference_subblock",
    "validator_prefix_or_allowlist_gap",
    "nan_or_null_comparison_bug",
    "comment_or_first_block_extractor_bug",
    "oracle_or_answer_file_in_image",
    "git_history_or_remote_leak",
    "in_process_grader_import",
    "conftest_or_sitecustomize_writable",
    "verifier_binary_or_script_writable",
    "self_reported_timer_or_success_marker",
    "root_or_excessive_container_privileges",
    "test_only_string_literals",
    "test_only_numeric_constants",
    "instruction_grader_contradiction_signal",
    "overstrict_private_symbol_or_form",
    "wrong_or_contradictory_gold_constant",
    "env_python_or_runtime_mismatch",
    "infeasible_broken_reference",
    "unsanitized_llm_judge_rubric",
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class FileRegexRule(_Model):
    surface: str = "any"
    pattern: str = Field(min_length=1)
    description: str = ""
    weight: float = Field(default=0.5, ge=0.0)


class TraceRegexRule(_Model):
    pattern: str = Field(min_length=1)
    description: str = ""
    weight: float = Field(default=0.5, ge=0.0)


class HackbookSignatures(_Model):
    file_globs: list[str] = Field(default_factory=list)
    file_regexes: list[FileRegexRule] = Field(default_factory=list)
    trace_command_regexes: list[TraceRegexRule] = Field(default_factory=list)
    trace_diff_regexes: list[TraceRegexRule] = Field(default_factory=list)
    structural_signals: list[str] = Field(default_factory=list)


class DetectionRecipe(_Model):
    kind: RecipeKind
    method_id: MethodId
    summary: str
    steps: list[str] = Field(default_factory=list)
    rule_or_probe: str = ""


class ExploitTemplate(_Model):
    applicable: bool
    mode: str = ""
    goal: str = ""
    preconditions: list[str] = Field(default_factory=list)
    template_steps: list[str] = Field(default_factory=list)
    snippet: str | None = None
    differential_verdict: dict[str, float | None] = Field(default_factory=dict)


class FixPattern(_Model):
    fix_location: str
    summary: str
    checklist: list[str] = Field(default_factory=list)
    example_diff_or_rule: str = ""


class HackbookExample(_Model):
    source: ExampleSource
    item_or_task_id: str = Field(min_length=1)
    summary: str
    source_url: str | None = None
    evidence_status: EvidenceStatus


class HackbookEntry(_Model):
    schema_: Literal["evallab.hackbook.entry/v0"] = Field(default=ENTRY_SCHEMA, alias="schema")
    version: str = "v0"
    id: str = Field(pattern=r"^HB-(TS|RS)\d{2}-\d{3}$")
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    title: str
    taxonomy_class_id: str = Field(pattern=r"^(TS|RS)\d{2}$")
    taxonomy_class: str
    side: Side
    direction: Direction
    qa_category: Category
    enabled_by: list[str] = Field(default_factory=list)
    sources: list[Source] = Field(min_length=1)
    mechanism: str
    signatures: HackbookSignatures
    detection_recipe: DetectionRecipe
    exploit_template: ExploitTemplate
    fix_pattern: FixPattern
    examples: list[HackbookExample] = Field(default_factory=list)

    def dump(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")


def _root(root: Path | None) -> Path:
    return REPO_ROOT if root is None else Path(root)


def hackbook_dir(root: Path | None = None) -> Path:
    return _root(root) / DEFAULT_HACKBOOK_DIR


def load_hackbook(root: Path | None = None) -> list[HackbookEntry]:
    """Load and validate every entry in ``research/hackbook/entries.jsonl``."""
    path = hackbook_dir(root) / "entries.jsonl"
    entries: list[HackbookEntry] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entries.append(HackbookEntry.model_validate(json.loads(line)))
        except ValueError as exc:
            raise ValueError(f"{path}:{number}: invalid hackbook entry: {exc}") from exc
    ids = [e.id for e in entries]
    slugs = [e.slug for e in entries]
    if len(set(ids)) != len(ids) or len(set(slugs)) != len(slugs):
        raise ValueError(f"{path}: duplicate entry id or slug")
    return entries


def load_taxonomy(root: Path | None = None) -> dict[str, Any]:
    path = hackbook_dir(root) / "taxonomy.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _entries(entries: Sequence[HackbookEntry] | None, root: Path | None) -> list[HackbookEntry]:
    return list(entries) if entries is not None else load_hackbook(root)


def get_entry(
    entry_id_or_slug: str,
    entries: Sequence[HackbookEntry] | None = None,
    *,
    root: Path | None = None,
) -> HackbookEntry:
    """Resolve an id or slug (case-insensitive), else a unique prefix/substring."""
    pool = _entries(entries, root)
    needle = entry_id_or_slug.strip().lower()
    for entry in pool:
        if needle in (entry.id.lower(), entry.slug.lower()):
            return entry
    for test in (str.startswith, str.__contains__):
        found = [e for e in pool if test(e.id.lower(), needle) or test(e.slug.lower(), needle)]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            names = ", ".join(e.id for e in found[:8])
            raise KeyError(f"ambiguous hackbook entry {entry_id_or_slug!r}: {names}")
    raise KeyError(f"no hackbook entry {entry_id_or_slug!r}")


_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORD_TEXT = (
    "a an the of to in on for and or is are was were be been it its this that these those with "
    "without as at by from into than then so such not no can could may might will would should "
    "shall do does did done has have had having which who whom whose what when where why how if "
    "else also only any all each every both either neither more most less least very just over "
    "under again further once here there their they them we you your i he she his her our ours "
    "via per"
)
_STOPWORDS = frozenset(_STOPWORD_TEXT.split())
# BM25F field weights: plain defaults, not fitted. A 648-point grid (k1, b, field weights) tuned on
# the preference_model_tb4 examples moved the other sources by +1 top-1 hit, and tuned on the other
# sources moved preference_model_tb4 by -1 top-1 and -3 top-5, so tuning was not worth keeping.


@lru_cache(maxsize=4)
def _tb4_test_terms(root: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    base = Path(root) / TB4_AUDIT_DIR
    tasks: set[str] = set()
    for line in (base / "tasks.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get("split") == "test":
                tasks.add(str(row["package"]["name"]))
    items: set[str] = set()
    items_path = base / "items.jsonl"
    if items_path.exists():
        for line in items_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("split") == "test" and row.get("item_id"):
                    items.add(str(row["item_id"]))
    return tuple(sorted(tasks)), tuple(sorted(items))


def tb4_test_split_terms(root: Path | None = None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(test-split task names, test-split item ids) of ``tb4-audit-v1``."""
    return _tb4_test_terms(str(_root(root).resolve()))


def find_tb4_test_leaks(text: str, *, root: Path | None = None) -> list[str]:
    """Forbidden test-split task names / item ids appearing in ``text``."""
    tasks, items = tb4_test_split_terms(root)
    hits = [item for item in items if item in text]
    for task in tasks:
        if re.search(rf"(?<![a-z0-9-]){re.escape(task)}(?![a-z0-9-])", text):
            hits.append(task)
    return sorted(set(hits))


def validate_no_tb4_test_leak(
    entries: Sequence[HackbookEntry], *, root: Path | None = None
) -> list[str]:
    """Return ``"<entry id>: <term>"`` for every test-split leak; empty means clean."""
    problems: list[str] = []
    for entry in entries:
        text = json.dumps(entry.dump(), sort_keys=True)
        problems += [f"{entry.id}: {term}" for term in find_tb4_test_leaks(text, root=root)]
    return problems
