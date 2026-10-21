"""Hackbook catalog models and queries (``evallab.hackbook.entry/v0``).

The Hackbook is a versioned catalog of known ways agents cheat and tasks
break. Entries live in ``research/hackbook/entries.jsonl``; this module
validates them and answers list/show/search queries over them.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from evallab.qa.models import Category

ENTRY_SCHEMA = "evallab.hackbook.entry/v0"
MATCH_SCHEMA = "evallab.hackbook.match/v0"
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_HACKBOOK_DIR = Path("research/hackbook")
TB4_AUDIT_DIR = Path("research/answer-keys/tb4-audit-v1")

Side = Literal["task", "run"]
Direction = Literal["false_pass", "false_fail", "either"]
Outcome = Literal["pass", "fail"]
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


class HackbookMatchHit(_Model):
    """One ranked Hackbook entry matched against a task or trace."""

    entry_id: str
    slug: str
    title: str
    taxonomy_class_id: str
    taxonomy_class: str
    side: Side
    direction: Direction
    qa_category: Category
    score: float
    matched_signals: list[str] = Field(default_factory=list)
    regex_hits: list[dict[str, Any]] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    detection_recipe: DetectionRecipe
    # True when the trace was matched together with its task package and this task-side entry
    # also fires on that package (the weakness exists and the run touched it); None otherwise.
    task_confirmed: bool | None = None
    # Share of a declared fleet this entry fires on (``--fleet-rates``); ``non_discriminating``
    # marks entries above the prevalence gate: they measure the harness, not the task or run.
    fleet_rate: float | None = None
    non_discriminating: bool = False
    exploit_template: ExploitTemplate
    fix_pattern: FixPattern


class HackbookMatchResult(_Model):
    """Match output for one task, one trace, or one exported view."""

    schema_: Literal["evallab.hackbook.match/v0"] = Field(default=MATCH_SCHEMA, alias="schema")
    kind: Literal["task", "trace", "view"]
    target: str
    task_id: str | None = None
    trial_id: str | None = None
    outcome: Outcome | None = None
    dropped_by_outcome: list[str] = Field(default_factory=list)
    task_gate: list[str] | None = None
    signals: dict[str, list[str]] = Field(default_factory=dict)
    matches: list[HackbookMatchHit] = Field(default_factory=list)
    tasks: list[HackbookMatchResult] = Field(default_factory=list)
    aggregate: list[dict[str, Any]] = Field(default_factory=list)


HackbookMatchResult.model_rebuild()


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


CROSSWALK_SCHEMA = "evallab.hackbook.crosswalk/v0"
CROSSWALK_CONFIDENCE = ("high", "medium", "low", "none")
CROSSWALK_AXES = ("task_defect", "run_mechanism", "run_verdict", "trace_label")


def load_crosswalk(root: Path | None = None) -> dict[str, Any]:
    """External label -> class mappings (``research/hackbook/crosswalk.yaml``)."""
    import yaml

    data = yaml.safe_load((hackbook_dir(root) / "crosswalk.yaml").read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != CROSSWALK_SCHEMA:
        raise ValueError(f"crosswalk.yaml: expected a mapping with schema {CROSSWALK_SCHEMA}")
    return data


def validate_crosswalk(
    crosswalk: dict[str, Any], taxonomy: dict[str, Any], entries: Sequence[HackbookEntry]
) -> list[str]:
    """Problems in a crosswalk: unknown classes, entries, subtypes or confidence levels."""
    classes = {row["id"] for side in ("task_side", "run_side") for row in taxonomy.get(side, [])}
    by_id = {entry.id: entry for entry in entries}
    subtypes = set(crosswalk.get("subtypes") or [])
    errors: list[str] = []
    sources = crosswalk.get("sources")
    if not isinstance(sources, dict) or not sources:
        return ["crosswalk.yaml: no sources"]
    for name, source in sources.items():
        if not isinstance(source, dict) or not source.get("title"):
            errors.append(f"{name}: needs a title")
            continue
        if not (source.get("url") or source.get("card") or source.get("note")):
            errors.append(f"{name}: needs a url, a card or a note")
        if source.get("axis") not in CROSSWALK_AXES:
            errors.append(f"{name}: axis must be one of {CROSSWALK_AXES}")
        labels = source.get("labels")
        if not isinstance(labels, dict):
            errors.append(f"{name}: labels must be a mapping")
            continue
        if not labels and not source.get("status"):
            errors.append(f"{name}: no labels and no status saying why")
        for label, row in labels.items():
            where = f"{name}/{label}"
            keys = row.get("keys") if isinstance(row, dict) else None
            if not isinstance(keys, list):
                errors.append(f"{where}: keys must be a list")
                continue
            if row.get("confidence") not in CROSSWALK_CONFIDENCE:
                errors.append(f"{where}: confidence must be one of {CROSSWALK_CONFIDENCE}")
            if (row.get("confidence") == "none") != (not keys):
                errors.append(f"{where}: confidence is none exactly when keys is empty")
            errors += [f"{where}: unknown class {key}" for key in keys if key not in classes]
            if row.get("subtype") and row["subtype"] not in subtypes:
                errors.append(f"{where}: unknown subtype {row['subtype']}")
            for entry_id in row.get("entries") or []:
                entry = by_id.get(entry_id)
                if entry is None:
                    errors.append(f"{where}: unknown entry {entry_id}")
                elif entry.taxonomy_class_id not in keys:
                    errors.append(f"{where}: {entry_id} is {entry.taxonomy_class_id}, not in keys")
    return errors


def crosswalk_summary(crosswalk: dict[str, Any]) -> dict[str, Any]:
    """Per source: labels, how many map at each confidence, and the unmapped ones."""
    by_source: list[dict[str, Any]] = []
    per_class: Counter[str] = Counter()
    for name, source in sorted(crosswalk["sources"].items()):
        labels: dict[str, dict[str, Any]] = source.get("labels") or {}
        levels = Counter(str(row["confidence"]) for row in labels.values())
        for row in labels.values():
            per_class.update(row["keys"])
        by_source.append(
            {
                "source": name,
                "axis": source["axis"],
                "labels": len(labels),
                "mapped": len(labels) - levels["none"],
                **{level: levels[level] for level in CROSSWALK_CONFIDENCE},
                "unmapped": [label for label, row in labels.items() if row["confidence"] == "none"],
                "status": source.get("status", ""),
            }
        )
    return {
        "sources": by_source,
        "labels": sum(row["labels"] for row in by_source),
        "mapped": sum(row["mapped"] for row in by_source),
        "labels_per_class": dict(sorted(per_class.items())),
    }


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


def class_matches(entry: HackbookEntry, taxonomy_class: str | None) -> bool:
    if not taxonomy_class:
        return True
    want = taxonomy_class.strip().lower()
    return want in (entry.taxonomy_class_id.lower(), entry.taxonomy_class.lower())


def list_entries(
    entries: Sequence[HackbookEntry] | None = None,
    *,
    taxonomy_class: str | None = None,
    side: str | None = None,
    source: str | None = None,
    evidence_status: str | None = None,
    root: Path | None = None,
) -> list[HackbookEntry]:
    """Filter entries; ``taxonomy_class`` accepts ``TS01`` or ``weak_check``."""
    out = []
    for entry in _entries(entries, root):
        if not class_matches(entry, taxonomy_class):
            continue
        if side and entry.side != side:
            continue
        if (
            source
            and source not in entry.sources
            and all(ex.source != source for ex in entry.examples)
        ):
            continue
        if evidence_status and all(ex.evidence_status != evidence_status for ex in entry.examples):
            continue
        out.append(entry)
    return out


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
_SUFFIXES = ("ations", "ation", "ings", "ing", "edly", "ed", "ies", "es", "s")
# BM25F field weights: plain defaults, not fitted. A 648-point grid (k1, b, field weights) tuned on
# the preference_model_tb4 examples moved the other sources by +1 top-1 hit, and tuned on the other
# sources moved preference_model_tb4 by -1 top-1 and -3 top-5, so tuning was not worth keeping.
_FIELD_WEIGHTS = {
    "id": 3.0,
    "title": 3.0,
    "class": 1.0,
    "mechanism": 2.0,
    "recipe": 1.0,
    "exploit": 1.0,
    "fix": 1.0,
    "sigdesc": 1.0,
    "examples": 1.0,
}
_BM25_K1 = 1.2
_BM25_B = 0.5


def _stem(token: str) -> str:
    for suffix in _SUFFIXES:
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return token


def _tokens(text: str) -> list[str]:
    """Lower-cased, stopword-free, lightly stemmed terms of ``text``."""
    return [_stem(t) for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS]


def _search_fields(entry: HackbookEntry) -> dict[str, str]:
    """Prose a person would describe the entry with; regex patterns and snippets are left out."""
    data = entry.dump()
    recipe, template, fix = data["detection_recipe"], data["exploit_template"], data["fix_pattern"]
    rules = (
        rule
        for key in ("file_regexes", "trace_command_regexes", "trace_diff_regexes")
        for rule in data["signatures"].get(key, [])
    )
    return {
        "id": f"{data['id']} {data['slug'].replace('-', ' ')}",
        "title": data["title"],
        "class": f"{data['taxonomy_class'].replace('_', ' ')} {data['taxonomy_class_id']}",
        "mechanism": data["mechanism"],
        "recipe": " ".join(
            [recipe.get("summary", ""), *recipe.get("steps", []), recipe.get("rule_or_probe", "")]
        ),
        "exploit": " ".join(
            [
                template.get("goal", ""),
                *template.get("preconditions", []),
                *template.get("template_steps", []),
            ]
        ),
        "fix": " ".join([fix.get("summary", ""), *fix.get("checklist", [])]),
        "sigdesc": " ".join(rule.get("description", "") for rule in rules),
        "examples": " ".join(ex["summary"] for ex in data["examples"]),
    }


def search_entries(
    query: str,
    entries: Sequence[HackbookEntry] | None = None,
    *,
    taxonomy_class: str | None = None,
    side: str | None = None,
    source: str | None = None,
    limit: int = 20,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    """BM25F relevance search; returns ``{entry, score, fields}`` rows, best first.

    Query terms are stopword-filtered and stemmed, weighted by inverse document frequency over
    the whole catalog (not the filtered pool), so a sentence such as a judge's free-text mechanism
    proposal ranks by its rare words. ``fields`` lists where a query term matched.
    """
    terms = list(dict.fromkeys(_tokens(query)))
    if not terms:
        return []
    catalog = list(_entries(entries, root))
    docs = {e.id: {k: _tokens(v) for k, v in _search_fields(e).items()} for e in catalog}
    doc_freq: Counter[str] = Counter()
    for fields in docs.values():
        doc_freq.update({t for tokens in fields.values() for t in tokens})
    avg_len = {k: sum(len(f[k]) for f in docs.values()) / max(len(docs), 1) for k in _FIELD_WEIGHTS}
    wanted = {
        e.id
        for e in list_entries(
            catalog, taxonomy_class=taxonomy_class, side=side, source=source, root=root
        )
    }
    rows: list[dict[str, Any]] = []
    for entry in catalog:
        if entry.id not in wanted:
            continue
        fields = docs[entry.id]
        score = 0.0
        matched: set[str] = set()
        for term in terms:
            weighted_tf = 0.0
            for name, weight in _FIELD_WEIGHTS.items():
                tokens = fields[name]
                count = tokens.count(term) if tokens else 0
                if count:
                    matched.add(name)
                    norm = 1 - _BM25_B + _BM25_B * len(tokens) / avg_len[name]
                    weighted_tf += weight * count / norm
            if weighted_tf:
                idf = math.log(1 + (len(docs) - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5))
                score += idf * weighted_tf * (_BM25_K1 + 1) / (weighted_tf + _BM25_K1)
        if score:
            ordered = [name for name in _FIELD_WEIGHTS if name in matched]
            rows.append({"entry": entry, "score": round(score, 3), "fields": ordered})
    rows.sort(key=lambda r: (-r["score"], r["entry"].id))
    return rows[: max(limit, 0)]


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


def _status_counts(examples: Iterable[HackbookExample]) -> dict[str, int]:
    counts = {"executed": 0, "observed": 0, "claimed": 0}
    for ex in examples:
        counts[ex.evidence_status] += 1
    return counts


def coverage_summary(
    entries: Sequence[HackbookEntry] | None = None, *, root: Path | None = None
) -> dict[str, Any]:
    """Build coverage tables by taxonomy class, seed source, and executed evidence."""
    pool = list(_entries(entries, root))
    taxonomy = load_taxonomy(root)
    class_meta: dict[str, dict[str, str]] = {}
    for side_key, side_name in (("task_side", "task"), ("run_side", "run")):
        for row in taxonomy.get(side_key, []):
            class_meta[row["id"]] = {
                "taxonomy_class_id": row["id"],
                "taxonomy_class": row["key"],
                "name": row.get("name", row["key"]),
                "side": side_name,
                "direction": str(row.get("direction", "")),
            }
    by_class: list[dict[str, Any]] = []
    for cid in sorted(class_meta):
        c_entries = [e for e in pool if e.taxonomy_class_id == cid]
        exec_entries = [
            e for e in c_entries if any(ex.evidence_status == "executed" for ex in e.examples)
        ]
        examples = [ex for e in c_entries for ex in e.examples]
        by_status = _status_counts(examples)
        srcs = sorted({s for e in c_entries for s in e.sources} | {ex.source for ex in examples})
        by_class.append(
            {
                **class_meta[cid],
                "entries": len(c_entries),
                "executed_entries": len(exec_entries),
                "claimed_only_entries": len(c_entries) - len(exec_entries),
                "examples": len(examples),
                "executed_examples": by_status["executed"],
                "observed_examples": by_status["observed"],
                "claimed_examples": by_status["claimed"],
                "entry_ids": [e.id for e in c_entries],
                "sources": srcs,
            }
        )
    all_sources = sorted(
        {s for e in pool for s in e.sources} | {ex.source for e in pool for ex in e.examples}
    )
    by_source: list[dict[str, Any]] = []
    for src in all_sources:
        s_entries = [
            e for e in pool if src in e.sources or any(ex.source == src for ex in e.examples)
        ]
        s_exec_entries = [
            e
            for e in s_entries
            if any(
                ex.evidence_status == "executed" and (ex.source == src or src in e.sources)
                for ex in e.examples
            )
        ]
        s_examples = [ex for e in pool for ex in e.examples if ex.source == src]
        s_status = _status_counts(s_examples)
        by_source.append(
            {
                "source": src,
                "entries": len(s_entries),
                "executed_entries": len(s_exec_entries),
                "examples": len(s_examples),
                "executed_examples": s_status["executed"],
                "observed_examples": s_status["observed"],
                "claimed_examples": s_status["claimed"],
                "classes": sorted({e.taxonomy_class_id for e in s_entries}),
            }
        )
    exec_entry_ids = [
        e.id for e in pool if any(ex.evidence_status == "executed" for ex in e.examples)
    ]
    claimed_only_ids = [e.id for e in pool if e.id not in set(exec_entry_ids)]
    all_examples = [ex for e in pool for ex in e.examples]
    total_status = _status_counts(all_examples)
    return {
        "total_entries": len(pool),
        "task_side_entries": sum(1 for e in pool if e.side == "task"),
        "run_side_entries": sum(1 for e in pool if e.side == "run"),
        "entries_with_executed_evidence": len(exec_entry_ids),
        "entries_claimed_only": len(claimed_only_ids),
        "executed_entry_share": round(len(exec_entry_ids) / len(pool), 4) if pool else 0.0,
        "total_examples": len(all_examples),
        "executed_examples": total_status["executed"],
        "observed_examples": total_status["observed"],
        "claimed_examples": total_status["claimed"],
        "executed_entry_ids": exec_entry_ids,
        "claimed_only_entry_ids": claimed_only_ids,
        "by_class": by_class,
        "by_source": by_source,
        "crosswalk": crosswalk_summary(load_crosswalk(root)),
    }
