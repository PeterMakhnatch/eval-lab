"""BUILDER authoring pipeline (WS-C).

Agent-authored evals as a measured pipeline with a human-only promotion
gate. Proposals live in quarantine under `library/tasks/_proposed/`; the
qualification ledger is Parquet at `derived/parquet/qualification/`.

State machine per proposal:

    proposed → battery_passed → craft_reviewed → registered | rejected

`registered` is reachable only through the existing human-only
`evallab registry` path. This module refuses to register.

The default battery is a local structural control over the proposal
package (oracle / nop / fair-oracle / adversarial). It never calls a
paid model and never starts Harbor; a Harbor-backed runner can be
injected. Determinism stops at ULID allocation, wall-clock timestamps,
and any injected Harbor runner.

Entry point: `python -m evallab.authoring`. `evallab author …` is not
wired here because `cli.py` is leased elsewhere.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from typing import Any, Literal, Protocol, overload

import pyarrow as pa
import pyarrow.parquet as pq
import yaml
from pydantic import Field

from evallab.evidence.facts import AnalyzerCallable, AnalyzerCallResult
from evallab.modeladapter import ModelAdapter, ModelAdapterError
from evallab.queue import DirectoryQueue, PolicyGate, load_policy, new_ulid
from evallab.schemas import (
    AuthoringSeedClass,
    ContractModel,
    ExperimentSpec,
    InversionAnalysis,
    InversionSpec,
    PolicyDecision,
    ProposalSpec,
)
from evallab.storage.paths import derived_root_from_environment

MODEL_PROVENANCE_SCHEMA_VERSION = "authoring-model/1"
MODEL_TRANSPORTS: tuple[str, ...] = ("cursor-agent", "agy")
MODEL_SPEC_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "name",
        "category",
        "scenario",
        "difficulty",
        "summary",
        "seed_class",
    ],
    "properties": {
        "schema_version": {"const": "spec/1"},
        "name": {"type": "string", "minLength": 1},
        "category": {"type": "string", "minLength": 1},
        "scenario": {"type": "string", "minLength": 1},
        "difficulty": {"type": "string", "minLength": 1},
        "summary": {"type": "string", "minLength": 1},
        "seed_class": {"const": "scenario"},
        "target_facets": {"type": ["object", "null"]},
        "scenario_path": {"type": ["string", "null"]},
        "ref_task": {"type": ["string", "null"]},
        "provenance": {"type": ["string", "null"]},
        "axes": {"type": ["object", "null"]},
    },
}

SCHEMA_VERSION = "authoring/1"
PROPOSED_RELATIVE = Path("library/tasks/_proposed")
LEDGER_RELATIVE = Path("qualification/ledger.parquet")
CRAFT_PARQUET_RELATIVE = Path("craft/craft.parquet")
RESEARCH_RELATIVE = Path("research")
LIBRARY_TASKS_RELATIVE = Path("library/tasks")
REGISTRY_RELATIVE = Path("library/registry")
META_TASK_RELATIVE = Path("library/meta/synthesize-task@1")
TEMPLATES_RELATIVE = Path("authoring/templates")
AXES_NAMES: tuple[str, ...] = ("category", "scenario", "difficulty")
SeedClass = AuthoringSeedClass
Outcome = Literal["proposed", "battery_passed", "craft_reviewed", "registered", "rejected"]
BatteryCheck = Literal["oracle", "nop", "fair_oracle", "adversarial"]

SEED_CLASSES: tuple[SeedClass, ...] = ("mutation", "scenario", "craft-gap", "inversion")
OUTCOMES: tuple[Outcome, ...] = (
    "proposed",
    "battery_passed",
    "craft_reviewed",
    "registered",
    "rejected",
)
BATTERY_CHECKS: tuple[BatteryCheck, ...] = (
    "oracle",
    "nop",
    "fair_oracle",
    "adversarial",
)
VERIFIER_TYPES: tuple[str, ...] = ("pytest", "diff", "golden_file", "judge", "hybrid")
GAP_AXES: tuple[str, ...] = ("verifier_type", "env_multi_container", "pinned_deps")

REGISTER_REFUSAL = (
    "authoring cannot register a proposal: registration is human-only via "
    "`evallab registry`. A human must promote the qualified package."
)

#: One DuckDB query answers pass-rate per seed_class. `$ledger` is the
#: parquet path. The four battery columns are the qualification predicate.
SEED_CLASS_PASS_RATE_SQL = """
SELECT
    seed_class,
    avg(
        CAST(
            coalesce(battery_oracle, false)
            AND coalesce(battery_nop, false)
            AND coalesce(battery_fair_oracle, false)
            AND coalesce(battery_adversarial, false)
            AS INTEGER
        )
    ) AS pass_rate,
    count(*) AS n
FROM read_parquet($ledger)
GROUP BY 1
ORDER BY 1
""".strip()

_IGNORE_COPY = frozenset({"__pycache__", ".git", ".pytest_cache", ".DS_Store"})
_VERSION_LINE = re.compile(r'(?m)^version\s*=\s*"[^"]*"')
_CLOCK = Callable[[], datetime]
_IDS = Callable[[], str]


class AuthoringError(RuntimeError):
    """Safe refusal for a broken authoring request."""


class ProposalNotFoundError(AuthoringError):
    """Raised when a proposal id is not in quarantine."""


class RegisterRefusal(AuthoringError):
    """Automation asked to register; the human gate refused."""

    def __init__(self, proposal_id: str, outcome: Outcome) -> None:
        self.proposal_id = proposal_id
        self.outcome = outcome
        super().__init__(REGISTER_REFUSAL)


class QualificationRecord(ContractModel):
    """One proposal's row in the qualification ledger."""

    proposal_id: str
    seed_class: SeedClass
    ref_task: str | None = None
    battery_oracle: bool | None = None
    battery_nop: bool | None = None
    battery_fair_oracle: bool | None = None
    battery_adversarial: bool | None = None
    evidence_paths: list[str] = Field(default_factory=list)
    review_score: float | None = None
    outcome: Outcome
    created_at: str
    updated_at: str


LEDGER_SCHEMA = pa.schema(
    [
        pa.field("proposal_id", pa.string(), nullable=False),
        pa.field("seed_class", pa.string(), nullable=False),
        pa.field("ref_task", pa.string()),
        pa.field("battery_oracle", pa.bool_()),
        pa.field("battery_nop", pa.bool_()),
        pa.field("battery_fair_oracle", pa.bool_()),
        pa.field("battery_adversarial", pa.bool_()),
        pa.field("evidence_paths", pa.list_(pa.string())),
        pa.field("review_score", pa.float64()),
        pa.field("outcome", pa.string(), nullable=False),
        pa.field("created_at", pa.string(), nullable=False),
        pa.field("updated_at", pa.string(), nullable=False),
    ]
)


@dataclass(frozen=True)
class ControlResult:
    """One battery check: a bool, a reward, and the evidence path."""

    check: BatteryCheck
    passed: bool
    reward: float
    agent: str
    evidence_path: str
    notes: str
    attempts: int = 1


class ControlRunner(Protocol):
    """Local free control. Implementations must not call a paid model."""

    def run(
        self,
        proposal_dir: Path,
        check: BatteryCheck,
        *,
        evidence_dir: Path,
    ) -> ControlResult: ...


@dataclass(frozen=True)
class Proposal:
    """In-memory view of a quarantined proposal."""

    proposal_id: str
    seed_class: SeedClass
    ref_task: str | None
    path: Path
    outcome: Outcome
    version: str | None = None
    source_path: str | None = None
    source_digest: str | None = None
    scenario_path: str | None = None
    target_facets: dict[str, Any] | None = None
    created_at: str | None = None
    job_id: str | None = None
    inputs: list[dict[str, Any]] | None = None
    injected_spec: dict[str, Any] | None = None
    exemplar: str | None = None
    category: str | None = None
    scenario: str | None = None
    difficulty: str | None = None
    provenance: dict[str, Any] | str | None = None
    axes: dict[str, Any] | None = None
    inversion_analysis: dict[str, Any] | None = None

    def manifest(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "proposal_id": self.proposal_id,
            "seed_class": self.seed_class,
            "ref_task": self.ref_task,
            "outcome": self.outcome,
            "version": self.version,
            "source_path": self.source_path,
            "source_digest": self.source_digest,
            "scenario_path": self.scenario_path,
            "target_facets": self.target_facets,
            "created_at": self.created_at,
        }
        if self.job_id is not None:
            data["job_id"] = self.job_id
        if self.inputs is not None:
            data["inputs"] = self.inputs
        if self.injected_spec is not None:
            data["injected_spec"] = self.injected_spec
        if self.exemplar is not None:
            data["exemplar"] = self.exemplar
        if self.category is not None:
            data["category"] = self.category
        if self.scenario is not None:
            data["scenario"] = self.scenario
        if self.difficulty is not None:
            data["difficulty"] = self.difficulty
        if self.provenance is not None:
            data["provenance"] = self.provenance
        if self.axes is not None:
            data["axes"] = self.axes
        if self.inversion_analysis is not None:
            data["inversion_analysis"] = self.inversion_analysis
        return data


@dataclass(frozen=True)
class BatteryReport:
    proposal_id: str
    outcome: Outcome
    checks: tuple[ControlResult, ...]
    all_passed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "outcome": self.outcome,
            "all_passed": self.all_passed,
            "checks": [
                {
                    "check": item.check,
                    "passed": item.passed,
                    "reward": item.reward,
                    "agent": item.agent,
                    "evidence_path": item.evidence_path,
                    "notes": item.notes,
                    "attempts": item.attempts,
                }
                for item in self.checks
            ],
        }


@dataclass(frozen=True)
class ReviewReport:
    proposal_id: str
    outcome: Outcome
    score: float
    reasons: tuple[str, ...]
    evidence_path: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "outcome": self.outcome,
            "score": self.score,
            "reasons": list(self.reasons),
            "evidence_path": self.evidence_path,
        }


@dataclass(frozen=True)
class BatchItem:
    proposal_id: str
    seed_class: SeedClass
    outcome: Outcome
    battery: BatteryReport
    review: ReviewReport


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def now_utc() -> datetime:
    return datetime.now(UTC)


def isoformat(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def proposed_root(repo_root: Path) -> Path:
    return repo_root / PROPOSED_RELATIVE


def ledger_path(derived_root: Path) -> Path:
    return derived_root / LEDGER_RELATIVE


def craft_parquet_path(derived_root: Path) -> Path:
    return derived_root / CRAFT_PARQUET_RELATIVE


def _ignore_copy(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in _IGNORE_COPY}


def _sha256_tree(root: Path) -> str:
    digest = __import__("hashlib").sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            digest.update(b"dir\0")
            digest.update(relative.encode())
            continue
        if path.is_symlink() or not path.is_file():
            continue
        digest.update(b"file\0")
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()}"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text)
    temporary.replace(path)


def _atomic_write_json(path: Path, payload: Any) -> None:
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _repo_relative(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def bump_version(version: str | None) -> str:
    """New version for a mutation copy. Never returns the source version."""
    if not version or not version.strip():
        return "0.1.0"
    cleaned = version.strip()
    core, _, _suffix = cleaned.partition("-")
    parts = core.split(".")
    if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit():
        return f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        return f"{parts[0]}.{int(parts[1]) + 1}.0"
    return f"{cleaned}-proposed"


def rewrite_task_version(text: str, new_version: str) -> str:
    if _VERSION_LINE.search(text):
        return _VERSION_LINE.sub(f'version = "{new_version}"', text, count=1)
    if "[task]" in text:
        return text.replace("[task]", f'[task]\nversion = "{new_version}"', 1)
    return f'version = "{new_version}"\n' + text


def read_task_version(task_toml: Path) -> str | None:
    if not task_toml.is_file():
        return None
    try:
        document = tomllib.loads(task_toml.read_text())
    except tomllib.TOMLDecodeError:
        return None
    task = document.get("task")
    if not isinstance(task, dict):
        return None
    version = task.get("version")
    return version if isinstance(version, str) and version.strip() else None


def load_ledger(path: Path) -> list[QualificationRecord]:
    if not path.is_file():
        return []
    rows = pq.read_table(path, schema=LEDGER_SCHEMA).to_pylist()
    return [QualificationRecord.model_validate(_normalize_row(row)) for row in rows]


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    paths = payload.get("evidence_paths")
    if paths is None:
        payload["evidence_paths"] = []
    elif not isinstance(paths, list):
        payload["evidence_paths"] = list(paths)
    return payload


def write_ledger(path: Path, records: Sequence[QualificationRecord]) -> Path:
    """Replace the ledger atomically. Rows stay sorted by proposal_id."""
    ordered = sorted(records, key=lambda record: record.proposal_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(
        [record.model_dump(mode="json") for record in ordered],
        schema=LEDGER_SCHEMA,
    )
    temporary = path.with_suffix(".parquet.tmp")
    pq.write_table(
        table, temporary, compression="zstd", use_dictionary=False, write_statistics=True
    )
    temporary.replace(path)
    return path


def upsert_ledger(path: Path, record: QualificationRecord) -> QualificationRecord:
    if record.outcome == "registered":
        raise RegisterRefusal(record.proposal_id, record.outcome)
    existing = {item.proposal_id: item for item in load_ledger(path)}
    existing[record.proposal_id] = record
    write_ledger(path, list(existing.values()))
    return record


def seed_class_pass_rates(path: Path) -> list[tuple[str, float, int]]:
    """Pass-rate per seed_class. Same predicate as `SEED_CLASS_PASS_RATE_SQL`."""
    import duckdb

    with duckdb.connect(":memory:") as connection:
        return list(
            connection.execute(SEED_CLASS_PASS_RATE_SQL, {"ledger": path.as_posix()}).fetchall()
        )


def templates_root(repo_root: Path) -> Path:
    return repo_root / TEMPLATES_RELATIVE


def load_axis(
    axis_name: str,
    repo_root: Path | None = None,
    *,
    template_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Load and validate an axis definition YAML file (SG-2).

    Refuses malformed or missing axis files with an AuthoringError naming the file.
    """
    if template_dir is not None:
        root = template_dir
    elif repo_root is not None and (repo_root / TEMPLATES_RELATIVE).is_dir():
        root = repo_root / TEMPLATES_RELATIVE
    else:
        root = templates_root(repo_root or repository_root())
    filename = f"{axis_name}.yaml" if not axis_name.endswith(".yaml") else axis_name
    path = root / filename
    if not path.is_file():
        raise AuthoringError(f"axis file not found: {path.name}")
    try:
        content = path.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
    except Exception as exc:
        raise AuthoringError(f"malformed axis file {path.name}: {exc}") from exc

    if not isinstance(data, list) or not data:
        raise AuthoringError(
            f"malformed axis file {path.name}: expected non-empty list of axis items"
        )

    base_axis = axis_name.replace(".yaml", "")
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise AuthoringError(f"malformed axis file {path.name}: item {i} is not a dictionary")
        slug = item.get("slug")
        if not slug or not isinstance(slug, str):
            raise AuthoringError(f"malformed axis file {path.name}: item {i} missing valid 'slug'")
        if not item.get("description"):
            raise AuthoringError(
                f"malformed axis file {path.name}: item {slug!r} missing 'description'"
            )
        if base_axis == "category" and not item.get("title"):
            raise AuthoringError(
                f"malformed axis file {path.name}: category {slug!r} missing 'title'"
            )
        if base_axis == "scenario":
            if not item.get("title") or not item.get("register") or not item.get("length"):
                raise AuthoringError(
                    f"malformed axis file {path.name}: "
                    f"scenario {slug!r} missing title/register/length"
                )
        elif base_axis == "difficulty" and (
            "anti_patterns" not in item or not isinstance(item["anti_patterns"], list)
        ):
            raise AuthoringError(
                f"malformed axis file {path.name}: difficulty {slug!r} missing 'anti_patterns' list"
            )

    return data


def load_all_axes(
    repo_root: Path | None = None,
    *,
    template_dir: Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Load all three canonical axes: category, scenario, difficulty."""
    return {axis: load_axis(axis, repo_root, template_dir=template_dir) for axis in AXES_NAMES}


def find_all_craft_gaps(parquet_path: Path) -> list[dict[str, Any]]:
    """All facet triples with zero coverage in the CRAFT parquet.

    Axes are `verifier_type × env_multi_container × pinned_deps`. Order is
    stable so the same parquet yields the same gap ordering.
    """
    if not parquet_path.is_file():
        raise AuthoringError(
            f"craft parquet not found at {parquet_path}; "
            "run `python -m evallab.craft scan` before querying craft gaps"
        )
    table = pq.read_table(parquet_path)
    missing = [name for name in GAP_AXES if name not in table.column_names]
    if missing:
        raise AuthoringError(f"craft parquet {parquet_path} is missing gap columns {missing}")
    covered: set[tuple[Any, ...]] = set()
    for row in table.select(list(GAP_AXES)).to_pylist():
        if row.get("verifier_type") is not None:
            covered.add(tuple(row[name] for name in GAP_AXES))
    gaps: list[dict[str, Any]] = []
    for candidate in product(VERIFIER_TYPES, (False, True), (False, True)):
        if candidate not in covered:
            gaps.append(
                {
                    "verifier_type": candidate[0],
                    "env_multi_container": candidate[1],
                    "pinned_deps": candidate[2],
                }
            )
    return gaps


def find_craft_gap(parquet_path: Path) -> dict[str, Any]:
    """First facet triple with zero coverage in the CRAFT parquet.

    Axes are `verifier_type × env_multi_container × pinned_deps`. Order is
    stable so the same parquet yields the same gap.
    """
    gaps = find_all_craft_gaps(parquet_path)
    if not gaps:
        raise AuthoringError(
            f"craft parquet {parquet_path} covers every "
            "verifier_type × env_multi_container × pinned_deps combination"
        )
    return gaps[0]


def spec_coordinate_key(spec: dict[str, Any] | ProposalSpec | Proposal) -> tuple[Any, ...]:
    """Canonical coordinate key for deduplication against the ledger and within batches."""
    if isinstance(spec, Proposal):
        inj = spec.injected_spec or {}
        cat = spec.category or inj.get("category") or ""
        scen = spec.scenario or inj.get("scenario") or ""
        diff = spec.difficulty or inj.get("difficulty") or ""
        facets = spec.target_facets or inj.get("target_facets") or {}
    elif isinstance(spec, ProposalSpec):
        data = spec.model_dump()
        cat = str(data.get("category") or "")
        scen = str(data.get("scenario") or "")
        diff = str(data.get("difficulty") or "")
        facets = data.get("target_facets") or {}
    else:
        data = dict(spec)
        cat = str(data.get("category") or "")
        scen = str(data.get("scenario") or "")
        diff = str(data.get("difficulty") or "")
        facets = data.get("target_facets") or {}
    facets_items = ((k, str(v)) for k, v in facets.items()) if isinstance(facets, dict) else ()
    facets_tuple = tuple(sorted(facets_items))
    return (cat.lower(), scen.lower(), diff.lower(), facets_tuple)


def extract_ledger_coordinates(
    ledger_path: Path,
    proposed_root: Path | None = None,
) -> set[tuple[Any, ...]]:
    """Extract all existing spec coordinate keys from the qualification ledger and quarantine."""
    records = load_ledger(ledger_path)
    coords: set[tuple[Any, ...]] = set()
    for record in records:
        if proposed_root is not None:
            prop_json = proposed_root / record.proposal_id / "proposal.json"
            if prop_json.is_file():
                try:
                    raw = json.loads(prop_json.read_text(encoding="utf-8"))
                    coords.add(spec_coordinate_key(raw))
                    if "injected_spec" in raw and isinstance(raw["injected_spec"], dict):
                        coords.add(spec_coordinate_key(raw["injected_spec"]))
                    if "axes" in raw and isinstance(raw["axes"], dict):
                        coords.add(spec_coordinate_key(raw["axes"]))
                except Exception:
                    pass
        if record.ref_task:
            coords.add((record.ref_task.lower(), "", "", ()))
    return coords


def local_test_designer(topic_seed: str, style_constraint: str) -> dict[str, Any]:
    """Deterministic designer reserved for tests and offline controls."""
    clean_topic = topic_seed.strip().replace(" ", "-").lower()
    clean_style = style_constraint.strip().replace(" ", "-").lower()
    cat_name = f"novel-{clean_topic}"
    scen_name = f"novel-{clean_style}"
    return {
        "schema_version": "spec/1",
        "name": f"novel-{clean_topic}-{clean_style}",
        "category": cat_name,
        "scenario": scen_name,
        "difficulty": "intermediate",
        "summary": f"Novel designed task for topic '{topic_seed}' under style '{style_constraint}'",
        "seed_class": "scenario",
        "provenance": "novel-spec",
        "axes": {
            "category": cat_name,
            "scenario": scen_name,
            "difficulty": "intermediate",
            "topic_seed": topic_seed,
            "style_constraint": style_constraint,
        },
    }


def build_novel_spec_prompt(topic_seed: str, style_constraint: str) -> str:
    """Build a JSON-only prompt; only its digest is retained as provenance."""
    schema = json.dumps(MODEL_SPEC_RESPONSE_SCHEMA, sort_keys=True)
    return (
        "Design one novel evaluation task specification. Return exactly one JSON object, "
        "with no markdown fences, commentary, chain-of-thought, or secret values. "
        "The object must validate against this strict JSON Schema: "
        f"{schema}\n"
        f"Topic seed: {topic_seed}\n"
        f"Style constraint: {style_constraint}\n"
        'Set "schema_version" to "spec/1" and "seed_class" to "scenario".'
    )


@dataclass(frozen=True)
class ModelDesign:
    spec: dict[str, Any]
    provenance: dict[str, str]


class ModelBackedDesigner:
    """Strict model-backed designer using an injected AnalyzerCallable."""

    def __init__(self, adapter: AnalyzerCallable) -> None:
        self.adapter = adapter

    def design(self, topic_seed: str, style_constraint: str) -> ModelDesign:
        prompt = build_novel_spec_prompt(topic_seed, style_constraint)
        result: AnalyzerCallResult = self.adapter(prompt, MODEL_SPEC_RESPONSE_SCHEMA)
        raw_output = result.raw_output
        try:
            parsed = json.loads(raw_output)
        except (TypeError, json.JSONDecodeError) as exc:
            raise AuthoringError("model designer returned malformed JSON") from exc
        if not isinstance(parsed, dict):
            raise AuthoringError("model designer returned a JSON value, not an object")
        required = MODEL_SPEC_RESPONSE_SCHEMA["required"]
        missing = [name for name in required if name not in parsed]
        if missing or not isinstance(parsed.get("summary"), str) or not parsed["summary"].strip():
            raise AuthoringError("model designer returned an incomplete proposal spec")
        try:
            validated = ProposalSpec.model_validate(parsed)
        except Exception as exc:
            raise AuthoringError("model designer returned an invalid proposal spec") from exc
        spec = validated.model_dump(mode="json")
        if spec["seed_class"] != "scenario":
            raise AuthoringError("model designer returned unsupported seed_class")
        provenance = {
            "schema_version": MODEL_PROVENANCE_SCHEMA_VERSION,
            "spec_schema_version": spec["schema_version"],
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "raw_output_sha256": hashlib.sha256(raw_output.encode("utf-8")).hexdigest(),
            "model": str(
                getattr(result, "model", None)
                or getattr(self.adapter, "model", None)
                or "injected"
            ),
            "transport": str(
                getattr(result, "transport", None)
                or getattr(self.adapter, "transport", None)
                or "injected"
            ),
        }
        spec["provenance"] = "model-backed"
        return ModelDesign(spec=spec, provenance=provenance)

    def __call__(self, topic_seed: str, style_constraint: str) -> dict[str, Any]:
        return self.design(topic_seed, style_constraint).spec


def design_novel_spec(
    topic_seed: str,
    style_constraint: str,
    *,
    designer: Callable[[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Design a spec with an injected designer; local fallback is test-only."""
    fn = designer or local_test_designer
    return fn(topic_seed, style_constraint)


def sample_spec_batch(
    repo_root: Path,
    count: int = 20,
    *,
    derived_root: Path | None = None,
    seed: int = 42,
    template_dir: Path | None = None,
    novel_designer: Callable[[str, str], dict[str, Any]] | None = None,
    novel_count: int = 0,
) -> list[dict[str, Any]]:
    """Dimension-decoupled spec sampling for authoring pipeline, coverage-first (SG-2).

    Sampling order:
    1. Primary: Craft-gap query (facet combinations with zero registered coverage).
    2. Secondary: Random axis product (used to fill remainder after gaps exhausted).
    3. Multi-phase novel-spec mode: lightweight designer from topic seeds (when requested).

    Deduplicates against the qualification ledger and within the emitted batch.
    """
    derived = derived_root or derived_root_from_environment(repo_root)
    axes = load_all_axes(repo_root, template_dir=template_dir)
    categories = axes["category"]
    scenarios = axes["scenario"]
    difficulties = axes["difficulty"]

    ledger_coords = extract_ledger_coordinates(ledger_path(derived), proposed_root(repo_root))
    seen_coords: set[tuple[Any, ...]] = set(ledger_coords)
    emitted: list[dict[str, Any]] = []

    # 1. Primary: CRAFT gap queries (coverage-first)
    try:
        gaps = find_all_craft_gaps(craft_parquet_path(derived))
    except AuthoringError:
        gaps = []

    gap_limit = max(0, count - novel_count) if novel_count > 0 else count
    for idx, gap in enumerate(gaps):
        if len(emitted) >= gap_limit:
            break
        v_type = gap.get("verifier_type", "")
        matching_cats = [c for c in categories if v_type in c.get("typical_verifier_types", [])]
        cat = (
            matching_cats[idx % len(matching_cats)]
            if matching_cats
            else categories[idx % len(categories)]
        )
        scen = scenarios[idx % len(scenarios)]
        diff = difficulties[idx % len(difficulties)]

        cat_slug = cat["slug"]
        scen_slug = scen["slug"]
        diff_slug = diff["slug"]

        spec_dict = {
            "schema_version": "spec/1",
            "name": f"gap-{gap['verifier_type']}-{cat_slug}-{scen_slug}",
            "category": cat_slug,
            "scenario": scen_slug,
            "difficulty": diff_slug,
            "summary": (
                f"Task targeting CRAFT gap {gap['verifier_type']} in {cat_slug} ({scen_slug})"
            ),
            "seed_class": "craft-gap",
            "target_facets": gap,
            "provenance": "craft-gap",
            "axes": {
                "category": cat_slug,
                "scenario": scen_slug,
                "difficulty": diff_slug,
                "target_facets": gap,
            },
        }
        coord = spec_coordinate_key(spec_dict)
        if coord not in seen_coords:
            seen_coords.add(coord)
            emitted.append(spec_dict)

    # 2. Novel spec mode (if requested)
    if novel_count > 0 and len(emitted) < count:
        topic_seeds: list[str] = []
        for c in categories:
            topic_seeds.extend(c.get("topic_seeds", []))
        if not topic_seeds:
            topic_seeds = [c["slug"] for c in categories]

        for i in range(novel_count):
            if len(emitted) >= count:
                break
            t_seed = topic_seeds[i % len(topic_seeds)]
            s_style = scenarios[i % len(scenarios)]["slug"]
            novel_spec = design_novel_spec(t_seed, s_style, designer=novel_designer)
            coord = spec_coordinate_key(novel_spec)
            if coord not in seen_coords:
                seen_coords.add(coord)
                emitted.append(novel_spec)

    # 3. Secondary: Random axis product (fill remainder)
    if len(emitted) < count:
        import random

        rng = random.Random(seed)
        all_product = list(product(categories, scenarios, difficulties))
        rng.shuffle(all_product)

        for cat, scen, diff in all_product:
            if len(emitted) >= count:
                break
            cat_slug = cat["slug"]
            scen_slug = scen["slug"]
            diff_slug = diff["slug"]

            spec_dict = {
                "schema_version": "spec/1",
                "name": f"axis-{cat_slug}-{scen_slug}-{diff_slug}",
                "category": cat_slug,
                "scenario": scen_slug,
                "difficulty": diff_slug,
                "summary": (
                    f"Task sampled from axis product: {cat_slug} x {scen_slug} x {diff_slug}"
                ),
                "seed_class": "scenario",
                "provenance": "random-product",
                "axes": {
                    "category": cat_slug,
                    "scenario": scen_slug,
                    "difficulty": diff_slug,
                },
            }
            coord = spec_coordinate_key(spec_dict)
            if coord not in seen_coords:
                seen_coords.add(coord)
                emitted.append(spec_dict)
    return emitted


def discover_scenario_paths(repo_root: Path) -> list[Path]:
    """Research-tree markdown that can seed a scenario proposal."""
    research = repo_root / RESEARCH_RELATIVE
    if not research.is_dir():
        return []
    preferred = research / "scenarios"
    roots = (
        [preferred]
        if preferred.is_dir()
        else [
            research / "explorations",
            research / "inspections",
            research,
        ]
    )
    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            resolved = path.resolve()
            if resolved in seen or path.name.upper() == "README.md":
                continue
            seen.add(resolved)
            found.append(path)
    return found


def resolve_scenario(repo_root: Path, ref: str | None) -> Path:
    candidates = discover_scenario_paths(repo_root)
    if not candidates:
        raise AuthoringError(f"no research scenario material under {repo_root / RESEARCH_RELATIVE}")
    if ref is None:
        return candidates[0]
    needle = ref.strip()
    for path in candidates:
        relative = _repo_relative(path, repo_root)
        if path.stem == needle or relative == needle or relative.endswith(needle):
            return path
    explicit = (repo_root / needle).resolve() if not Path(needle).is_absolute() else Path(needle)
    if explicit.is_file():
        try:
            explicit.relative_to((repo_root / RESEARCH_RELATIVE).resolve())
        except ValueError as exc:
            raise AuthoringError(
                f"scenario --ref {ref!r} is not under {RESEARCH_RELATIVE}"
            ) from exc
        return explicit
    raise AuthoringError(f"scenario --ref {ref!r} not found under {RESEARCH_RELATIVE}")


def list_library_tasks(repo_root: Path) -> list[Path]:
    root = repo_root / LIBRARY_TASKS_RELATIVE
    if not root.is_dir():
        return []
    tasks: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        if (child / "task.toml").is_file() and (child / "instruction.md").is_file():
            tasks.append(child)
    return tasks


def resolve_registered_task(repo_root: Path, ref: str | None) -> Path:
    """A mutation source: registry path if present, else `library/tasks/<ref>`.

    The registry is the preferred binding. This checkout may have no records
    yet, so a well-formed library task is accepted as a source — the copy
    still never edits it in place.
    """
    registry_dir = repo_root / REGISTRY_RELATIVE
    records: dict[str, str] = {}
    if registry_dir.is_dir():
        for record_file in sorted(registry_dir.glob("*.json")):
            try:
                raw = json.loads(record_file.read_text())
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            task_id = raw.get("task_id")
            task_path = raw.get("task_path")
            state = raw.get("state")
            if isinstance(task_id, str) and isinstance(task_path, str) and state == "registered":
                records[task_id] = task_path

    if ref is not None:
        needle = ref.strip().removeprefix("registered/")
        if needle in records:
            path = repo_root / records[needle]
            if path.is_dir():
                return path
        library = repo_root / LIBRARY_TASKS_RELATIVE / needle
        if library.is_dir() and (library / "task.toml").is_file():
            return library
        explicit = Path(needle)
        if explicit.is_absolute() and explicit.is_dir():
            return explicit
        raise AuthoringError(f"mutation --ref {ref!r} is not a registered or library task")

    for task_id, relative in sorted(records.items()):
        path = repo_root / relative
        if path.is_dir():
            return path
        del task_id
    tasks = list_library_tasks(repo_root)
    if tasks:
        return tasks[0]
    raise AuthoringError("no registered or library task available for --seed mutation")


def load_proposal(proposal_dir: Path) -> Proposal:
    manifest_path = proposal_dir / "proposal.json"
    if not manifest_path.is_file():
        raise ProposalNotFoundError(f"proposal manifest missing: {manifest_path}")
    raw = json.loads(manifest_path.read_text())
    inv_analysis = raw.get("inversion_analysis")
    if inv_analysis is None and (proposal_dir / "inversion.json").is_file():
        with contextlib.suppress(Exception):
            inv_analysis = json.loads((proposal_dir / "inversion.json").read_text())
    return Proposal(
        proposal_id=str(raw["proposal_id"]),
        seed_class=raw["seed_class"],
        ref_task=raw.get("ref_task"),
        path=proposal_dir,
        outcome=raw["outcome"],
        version=raw.get("version"),
        source_path=raw.get("source_path"),
        source_digest=raw.get("source_digest"),
        scenario_path=raw.get("scenario_path"),
        target_facets=raw.get("target_facets"),
        created_at=raw.get("created_at"),
        job_id=raw.get("job_id"),
        inputs=raw.get("inputs"),
        injected_spec=raw.get("injected_spec"),
        exemplar=raw.get("exemplar"),
        category=raw.get("category"),
        scenario=raw.get("scenario"),
        difficulty=raw.get("difficulty"),
        provenance=raw.get("provenance"),
        axes=raw.get("axes"),
        inversion_analysis=inv_analysis,
    )


class StructuralControlRunner:
    """Free local control that never starts Harbor and never calls a model.

    oracle    — solution/ is present (the oracle package exists) → reward 1.0
    nop       — tests/ is present; empty work cannot pass → reward 0.0, n=2
    fair-oracle — instruction + environment only; solution/tests stay outside
    adversarial — cheat-visible answers are absent; a cheater scores 0
    """

    def run(
        self,
        proposal_dir: Path,
        check: BatteryCheck,
        *,
        evidence_dir: Path,
    ) -> ControlResult:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        if check == "oracle":
            return self._oracle(proposal_dir, evidence_dir)
        if check == "nop":
            return self._nop(proposal_dir, evidence_dir)
        if check == "fair_oracle":
            return self._fair_oracle(proposal_dir, evidence_dir)
        return self._adversarial(proposal_dir, evidence_dir)

    def _oracle(self, proposal_dir: Path, evidence_dir: Path) -> ControlResult:
        solution = proposal_dir / "solution"
        files = _existing_files(solution)
        passed = bool(files)
        payload = {
            "check": "oracle",
            "agent": "oracle",
            "reward": 1.0 if passed else 0.0,
            "passed": passed,
            "solution_files": files,
        }
        path = evidence_dir / "oracle.json"
        _atomic_write_json(path, payload)
        return ControlResult(
            check="oracle",
            passed=passed,
            reward=1.0 if passed else 0.0,
            agent="oracle",
            evidence_path=path.as_posix(),
            notes="structural oracle: solution/ present" if passed else "solution/ missing",
        )

    def _nop(self, proposal_dir: Path, evidence_dir: Path) -> ControlResult:
        tests = proposal_dir / "tests"
        attempts: list[dict[str, Any]] = []
        for index in (1, 2):
            reward = 0.0 if tests.is_dir() and _existing_files(tests) else 1.0
            attempts.append({"attempt": index, "agent": "nop", "reward": reward})
        passed = bool(attempts) and all(item["reward"] == 0.0 for item in attempts)
        payload = {"check": "nop", "agent": "nop", "passed": passed, "attempts": attempts}
        path = evidence_dir / "nop.json"
        _atomic_write_json(path, payload)
        return ControlResult(
            check="nop",
            passed=passed,
            reward=0.0 if passed else 1.0,
            agent="nop",
            evidence_path=path.as_posix(),
            notes="structural nop n=2: verifier present" if passed else "tests/ missing",
            attempts=len(attempts),
        )

    def _fair_oracle(self, proposal_dir: Path, evidence_dir: Path) -> ControlResult:
        instruction = proposal_dir / "instruction.md"
        environment = proposal_dir / "environment"
        visible = _existing_files(environment)
        leaked = [
            relative
            for relative in visible
            if relative.startswith("solution/")
            or relative.startswith("tests/")
            or "/solution/" in relative
            or "/tests/" in relative
        ]
        has_instruction = instruction.is_file() and instruction.stat().st_size > 0
        passed = has_instruction and environment.is_dir() and not leaked
        payload = {
            "check": "fair_oracle",
            "agent": "fair-oracle",
            "passed": passed,
            "visible_environment_files": visible,
            "leaked": leaked,
            "instruction_present": instruction.is_file(),
        }
        path = evidence_dir / "fair-oracle.json"
        _atomic_write_json(path, payload)
        return ControlResult(
            check="fair_oracle",
            passed=passed,
            reward=1.0 if passed else 0.0,
            agent="fair-oracle",
            evidence_path=path.as_posix(),
            notes=(
                "fresh agent sees instruction+environment only"
                if passed
                else "fair-oracle surface incomplete or leaked hidden inputs"
            ),
        )

    def _adversarial(self, proposal_dir: Path, evidence_dir: Path) -> ControlResult:
        instruction = (
            (proposal_dir / "instruction.md").read_text()
            if (proposal_dir / "instruction.md").is_file()
            else ""
        )
        solution_blobs = _file_blobs(proposal_dir / "solution")
        leaked = [name for name, blob in solution_blobs if blob and blob in instruction]
        tests_present = bool(_existing_files(proposal_dir / "tests"))
        passed = tests_present and not leaked
        payload = {
            "check": "adversarial",
            "agent": "adversarial",
            "reward": 0.0 if passed else 1.0,
            "passed": passed,
            "leaked_solution_files": leaked,
            "tests_present": tests_present,
        }
        path = evidence_dir / "adversarial.json"
        _atomic_write_json(path, payload)
        return ControlResult(
            check="adversarial",
            passed=passed,
            reward=0.0 if passed else 1.0,
            agent="adversarial",
            evidence_path=path.as_posix(),
            notes=(
                "cheat-instructed agent scores 0: answer not in instruction"
                if passed
                else "adversarial would succeed: leaked answer or missing verifier"
            ),
        )


def _existing_files(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    files: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            files.append(path.relative_to(root).as_posix())
    return files


def _file_blobs(root: Path) -> list[tuple[str, str]]:
    blobs: list[tuple[str, str]] = []
    if not root.is_dir():
        return blobs
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        if text.strip():
            blobs.append((path.relative_to(root).as_posix(), text))
    return blobs


def score_review(proposal: Proposal) -> tuple[float, list[str]]:
    """Deterministic CRAFT-derived rubric. Same bytes ⇒ same score."""
    reasons: list[str] = []
    score = 0.0
    directory = proposal.path
    layout = {
        "task.toml": (directory / "task.toml").is_file(),
        "instruction.md": (directory / "instruction.md").is_file(),
        "environment": (directory / "environment").is_dir(),
        "tests": (directory / "tests").is_dir() and bool(_existing_files(directory / "tests")),
    }
    if all(layout.values()):
        score += 0.25
        reasons.append("harbor layout complete (task.toml, instruction, environment, tests)")
    else:
        missing = [name for name, present in layout.items() if not present]
        reasons.append(f"incomplete harbor layout: missing {missing}")

    isolated = False
    task_toml = directory / "task.toml"
    if task_toml.is_file():
        text = task_toml.read_text()
        isolated = (
            'environment_mode = "separate"' in text or "environment_mode = 'separate'" in text
        )
    if isolated:
        score += 0.25
        reasons.append("CRAFT anti-cheat: separate verifier image (hidden_tests)")
    else:
        reasons.append("CRAFT anti-cheat: verifier is not declared separate")

    env_files = _existing_files(directory / "environment")
    hidden_ok = not any(
        relative.startswith("solution/") or relative.startswith("tests/") for relative in env_files
    )
    instruction = (
        (directory / "instruction.md").read_text()
        if (directory / "instruction.md").is_file()
        else ""
    )
    leaked = [
        name for name, blob in _file_blobs(directory / "solution") if blob and blob in instruction
    ]
    if hidden_ok and not leaked:
        score += 0.25
        reasons.append("CRAFT answer-hiding: solution/tests stay outside instruction+environment")
    else:
        reasons.append(
            "CRAFT answer-hiding failed: "
            + (
                "solution leaked into instruction"
                if leaked
                else "hidden inputs visible in environment"
            )
        )

    if proposal.seed_class == "mutation":
        if proposal.version and proposal.source_digest:
            score += 0.25
            reasons.append(f"mutation is a new version {proposal.version!r}, source digest bound")
        else:
            reasons.append("mutation missing version or source digest")
    elif proposal.seed_class == "scenario":
        if proposal.scenario_path:
            score += 0.25
            reasons.append(f"scenario cites research material {proposal.scenario_path}")
        else:
            reasons.append("scenario missing research citation")
    elif proposal.seed_class == "inversion":
        inv = proposal.inversion_analysis
        if inv and inv.get("computed_value") is not None and proposal.source_digest:
            score += 0.25
            reasons.append(
                f"inversion answer key verified by execution against {proposal.source_path} "
                f"(digest {proposal.source_digest})"
            )
        else:
            reasons.append("inversion missing verified execution key or source data digest")
    else:
        facets = proposal.target_facets or {}
        if all(name in facets for name in GAP_AXES):
            score += 0.25
            reasons.append(
                "craft-gap targets "
                f"verifier_type={facets['verifier_type']} "
                f"env_multi_container={facets['env_multi_container']} "
                f"pinned_deps={facets['pinned_deps']}"
            )
        else:
            reasons.append("craft-gap missing target facet triple")
    return round(score, 4), reasons


def compute_file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def sample_meta_spec(
    repo_root: Path,
    *,
    seed: SeedClass = "craft-gap",
    ref: str | None = None,
    derived_root: Path | None = None,
) -> dict[str, Any]:
    derived = derived_root or derived_root_from_environment(repo_root)
    if seed == "craft-gap":
        facets = find_craft_gap(craft_parquet_path(derived))
        return {
            "schema_version": "spec/1",
            "name": f"gap-{facets['verifier_type']}",
            "category": "data-processing",
            "scenario": "structured-pipeline",
            "difficulty": "medium",
            "summary": f"Task targeting CRAFT gap {facets['verifier_type']}",
            "seed_class": "craft-gap",
            "target_facets": {name: facets[name] for name in GAP_AXES},
            "provenance": "craft-gap",
            "axes": {
                "category": "data-processing",
                "scenario": "structured-pipeline",
                "difficulty": "medium",
                "target_facets": {name: facets[name] for name in GAP_AXES},
            },
        }
    if seed == "scenario":
        scenario = resolve_scenario(repo_root, ref)
        return {
            "schema_version": "spec/1",
            "name": f"scenario-{scenario.stem}",
            "category": "data-processing",
            "scenario": scenario.stem,
            "difficulty": "medium",
            "summary": f"Task seeded from {scenario.name}",
            "seed_class": "scenario",
            "scenario_path": _repo_relative(scenario, repo_root),
            "provenance": "scenario",
            "axes": {
                "category": "data-processing",
                "scenario": scenario.stem,
                "difficulty": "medium",
            },
        }
    if seed == "inversion":
        asset_path, asset_rel = resolve_inversion_asset(repo_root, ref)
        asset_digest = compute_file_digest(asset_path)
        slug = asset_path.stem.replace("_", "-")
        inv_spec = InversionSpec(
            name=f"inversion-{slug}",
            data_asset_path=asset_rel,
            data_asset_digest=asset_digest,
            summary=f"Inversion task seeded from {asset_rel}",
        )
        return inv_spec.model_dump()
    source = resolve_registered_task(repo_root, ref)
    return {
        "schema_version": "spec/1",
        "name": f"mutation-{source.name}",
        "category": "data-processing",
        "scenario": "mutation",
        "difficulty": "medium",
        "summary": f"Mutation of {source.name}",
        "seed_class": "mutation",
        "ref_task": source.name,
        "provenance": "mutation",
        "axes": {
            "category": "data-processing",
            "scenario": "mutation",
            "difficulty": "medium",
        },
    }


def find_library_data_assets(repo_root: Path) -> list[Path]:
    """Find available data assets within library/ environments and tasks."""
    candidates: list[Path] = []
    lib_dir = repo_root / "library"
    if not lib_dir.is_dir():
        return candidates

    data_exts = {".json", ".jsonl", ".csv", ".tsv", ".parquet", ".sql", ".txt", ".sqlite", ".db"}
    ignore_names = {
        "Dockerfile",
        "task.toml",
        "spec.json",
        "package.json",
        "tsconfig.json",
        "guidelines.md",
        "README.md",
        "CARD.md",
        "REJECTED.md",
    }

    for p in sorted(lib_dir.rglob("*")):
        if not p.is_file():
            continue
        if p.name in ignore_names or p.name.startswith("."):
            continue
        parts = p.parts
        if any(part in {"solution", "tests", "_proposed", "_staged"} for part in parts):
            continue
        if p.suffix.lower() in data_exts and p.stat().st_size > 0:
            candidates.append(p)
    return candidates


def resolve_inversion_asset(repo_root: Path, ref: str | None = None) -> tuple[Path, str]:
    """Resolve a data asset for inversion from library/ environments.

    Returns (absolute_path, repo_relative_path).
    """
    if ref:
        cand = repo_root / ref
        if cand.is_file():
            return cand, _repo_relative(cand, repo_root)
        task_env = repo_root / "library/tasks" / ref / "environment"
        if task_env.is_dir():
            for child in sorted(task_env.iterdir()):
                if child.is_file() and child.name != "Dockerfile" and child.stat().st_size > 0:
                    return child, _repo_relative(child, repo_root)
        for p in find_library_data_assets(repo_root):
            if p.name == ref or p.stem == ref or ref in p.as_posix():
                return p, _repo_relative(p, repo_root)
        raise AuthoringError(f"no data asset found for inversion ref {ref!r}")

    assets = find_library_data_assets(repo_root)
    if not assets:
        raise AuthoringError("no data assets found in library/ for inversion")
    chosen = assets[0]
    return chosen, _repo_relative(chosen, repo_root)


def generate_inversion_analysis_code(asset_path: Path) -> tuple[str, str, dict[str, Any]]:
    """Probe a data asset and produce reference analysis Python code and instruction."""
    asset_name = asset_path.name
    suffix = asset_path.suffix.lower()

    if suffix == ".jsonl":
        code = f"""import json
import math
from collections import Counter
from pathlib import Path

input_path = Path("/app/input/{asset_name}")
if not input_path.is_file():
    input_path = Path("environment/{asset_name}")
if not input_path.is_file():
    input_path = Path("input/{asset_name}")
if not input_path.is_file():
    input_path = Path("{asset_name}")

lines = [
    line.strip()
    for line in input_path.read_text(encoding="utf-8").splitlines()
    if line.strip()
]
records = [json.loads(line) for line in lines]

cat_keys = ("kind", "type", "category", "event", "status")
cat_key = next((k for k in cat_keys if records and k in records[0]), None)
num_keys = ("duration_ms", "value", "val", "amount", "score", "time")
num_key = next(
    (
        k
        for k in num_keys
        if records and k in records[0] and isinstance(records[0][k], (int, float))
    ),
    None,
)

summary = {{
    "schema_version": 1,
    "total_records": len(records),
    "status": "ok",
}}

if cat_key:
    counts = Counter(r.get(cat_key) for r in records if cat_key in r)
    summary["counts"] = {{str(k): counts[k] for k in sorted(counts)}}

if num_key:
    nums = sorted(
        r[num_key]
        for r in records
        if num_key in r and isinstance(r[num_key], (int, float))
    )
    if nums:
        summary[f"total_{{num_key}}"] = sum(nums)
        p95_idx = math.ceil(0.95 * len(nums)) - 1
        summary[f"p95_{{num_key}}"] = nums[p95_idx]

output_dir = Path("/app/output")
if not output_dir.exists():
    output_dir = Path("output")
output_dir.mkdir(parents=True, exist_ok=True)
out_text = json.dumps(summary, indent=2, sort_keys=True) + "\\n"
(output_dir / "summary.json").write_text(out_text, encoding="utf-8")
"""
        instruction = f"""# Process {asset_name}

Read records in `/app/input/{asset_name}` and create `/app/output/summary.json`.
The output must be a valid JSON object with the following fields:
- `schema_version`: integer `1`
- `total_records`: total number of records processed
- `status`: string `"ok"`

Write valid UTF-8 JSON with a trailing newline. Do not modify or replace the input file.
"""
        return code, instruction, {"output_file": "output/summary.json"}

    if suffix == ".json":
        code = f"""import json
from collections import Counter
from pathlib import Path

input_path = Path("/app/input/{asset_name}")
if not input_path.is_file():
    input_path = Path("environment/{asset_name}")
if not input_path.is_file():
    input_path = Path("input/{asset_name}")
if not input_path.is_file():
    input_path = Path("{asset_name}")

data = json.loads(input_path.read_text(encoding="utf-8"))

if isinstance(data, list):
    cat_keys = ("type", "kind", "category", "status")
    cat_key = next(
        (
            k
            for k in cat_keys
            if data and isinstance(data[0], dict) and k in data[0]
        ),
        None,
    )
    num_keys = ("val", "value", "amount", "duration_ms", "score")
    num_key = next(
        (
            k
            for k in num_keys
            if data
            and isinstance(data[0], dict)
            and k in data[0]
            and isinstance(data[0][k], (int, float))
        ),
        None,
    )
    summary = {{
        "schema_version": 1,
        "total_records": len(data),
        "status": "ok",
    }}
    if cat_key:
        counts = Counter(r.get(cat_key) for r in data if isinstance(r, dict) and cat_key in r)
        summary["type_counts"] = {{str(k): counts[k] for k in sorted(counts)}}
    if num_key:
        nums = [
            r[num_key]
            for r in data
            if isinstance(r, dict) and num_key in r and isinstance(r[num_key], (int, float))
        ]
        if nums:
            summary[f"total_{{num_key}}"] = sum(nums)
            summary[f"max_{{num_key}}"] = max(nums)
else:
    summary = {{
        "schema_version": 1,
        "total_keys": len(data.keys()) if isinstance(data, dict) else 1,
        "status": "ok",
    }}

output_dir = Path("/app/output")
if not output_dir.exists():
    output_dir = Path("output")
output_dir.mkdir(parents=True, exist_ok=True)
out_text = json.dumps(summary, indent=2, sort_keys=True) + "\\n"
(output_dir / "summary.json").write_text(out_text, encoding="utf-8")
"""
        instruction = f"""# Process {asset_name}

Read the records in `/app/input/{asset_name}` and create `/app/output/summary.json`.

The output must be a valid JSON object with the following fields:
- `schema_version`: integer `1`
- `total_records`: count of records processed
- `status`: string `"ok"`

Write valid UTF-8 JSON with a trailing newline. Do not modify or replace the input file.
"""
        return code, instruction, {"output_file": "output/summary.json"}

    code = f"""import hashlib
import json
from pathlib import Path

input_path = Path("/app/input/{asset_name}")
if not input_path.is_file():
    input_path = Path("environment/{asset_name}")
if not input_path.is_file():
    input_path = Path("input/{asset_name}")
if not input_path.is_file():
    input_path = Path("{asset_name}")

raw_bytes = input_path.read_bytes()
text = raw_bytes.decode("utf-8", errors="replace")
lines = text.splitlines()

summary = {{
    "schema_version": 1,
    "total_lines": len(lines),
    "non_empty_lines": sum(1 for line in lines if line.strip()),
    "total_characters": len(text),
    "sha256": hashlib.sha256(raw_bytes).hexdigest(),
    "status": "ok",
}}

output_dir = Path("/app/output")
if not output_dir.exists():
    output_dir = Path("output")
output_dir.mkdir(parents=True, exist_ok=True)
out_text = json.dumps(summary, indent=2, sort_keys=True) + "\\n"
(output_dir / "summary.json").write_text(out_text, encoding="utf-8")
"""
    instruction = f"""# Process {asset_name}

Read the data file in `/app/input/{asset_name}` and create `/app/output/summary.json`.

The output must be a valid JSON object with line counts and file statistics.
Write valid UTF-8 JSON with a trailing newline. Do not modify or replace the input file.
"""
    return code, instruction, {"output_file": "output/summary.json"}


def execute_reference_analysis(
    analysis_code: str,
    data_asset_path: Path,
    *,
    output_file_rel: str = "output/summary.json",
    timeout_sec: float = 30.0,
) -> dict[str, Any]:
    """Execute Python reference analysis code against the data asset in a scratch sandbox.

    Raises AuthoringError if execution fails, times out, or produces invalid output.
    Never returns a guessed or default key.
    """
    if not data_asset_path.is_file():
        raise AuthoringError(f"reference analysis failed: data asset not found: {data_asset_path}")

    with tempfile.TemporaryDirectory(prefix="evallab_inversion_") as tmp:
        sandbox = Path(tmp)
        env_dir = sandbox / "environment"
        env_dir.mkdir(parents=True, exist_ok=True)
        input_dir = sandbox / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        app_input_dir = sandbox / "app" / "input"
        app_input_dir.mkdir(parents=True, exist_ok=True)

        asset_name = data_asset_path.name
        shutil.copy2(data_asset_path, env_dir / asset_name)
        shutil.copy2(data_asset_path, input_dir / asset_name)
        shutil.copy2(data_asset_path, app_input_dir / asset_name)
        shutil.copy2(data_asset_path, sandbox / asset_name)

        script_path = sandbox / "solve.py"
        script_path.write_text(analysis_code, encoding="utf-8")

        try:
            res = subprocess.run(
                [sys.executable, str(script_path)],
                cwd=sandbox,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            raise AuthoringError(
                f"reference analysis timed out after {timeout_sec}s on {data_asset_path.name}"
            ) from exc
        except Exception as exc:
            raise AuthoringError(f"reference analysis execution failed: {exc}") from exc

        if res.returncode != 0:
            err_msg = res.stderr.strip() or res.stdout.strip() or f"exit code {res.returncode}"
            raise AuthoringError(f"reference analysis failed on {data_asset_path.name}: {err_msg}")

        candidate_outputs = [
            sandbox / output_file_rel,
            sandbox / "app" / output_file_rel,
            sandbox / "summary.json",
            sandbox / "output" / "summary.json",
            sandbox / "app" / "output" / "summary.json",
        ]
        out_file = None
        for cand in candidate_outputs:
            if cand.is_file() and cand.stat().st_size > 0:
                out_file = cand
                break

        if out_file is None:
            msg = (
                f"reference analysis on {data_asset_path.name} "
                f"produced no output file at {output_file_rel}"
            )
            raise AuthoringError(msg)

        try:
            computed_value = json.loads(out_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise AuthoringError(
                f"reference analysis on {data_asset_path.name} produced invalid JSON: {exc}"
            ) from exc

        return computed_value


def reexecute_inversion_analysis(proposal_path: Path) -> dict[str, Any]:
    """Re-execute the recorded reference analysis for an inversion proposal.

    Locates the data asset and reference analysis code, re-runs execution,
    and returns the computed answer key.
    """
    proposal_path = Path(proposal_path)
    inv_file = proposal_path / "inversion.json"
    analysis_code: str | None = None
    data_file: Path | None = None

    if inv_file.is_file():
        with contextlib.suppress(Exception):
            inv_data = json.loads(inv_file.read_text(encoding="utf-8"))
            if isinstance(inv_data, dict):
                analysis_code = inv_data.get("analysis_code")

    if analysis_code is None:
        prop_file = proposal_path / "proposal.json"
        if prop_file.is_file():
            with contextlib.suppress(Exception):
                p_data = json.loads(prop_file.read_text(encoding="utf-8"))
                inv_block = p_data.get("inversion_analysis")
                if isinstance(inv_block, dict):
                    analysis_code = inv_block.get("analysis_code")

    if analysis_code is None:
        solve_py = proposal_path / "solution" / "solve.py"
        if solve_py.is_file():
            analysis_code = solve_py.read_text(encoding="utf-8")

    if analysis_code is None:
        raise AuthoringError(
            f"cannot re-execute inversion: no analysis code found in {proposal_path}"
        )

    env_dir = proposal_path / "environment"
    if env_dir.is_dir():
        for child in env_dir.iterdir():
            if child.is_file() and child.name != "Dockerfile" and child.stat().st_size > 0:
                data_file = child
                break

    if data_file is None:
        fix_dir = proposal_path / "tests" / "fixtures"
        if fix_dir.is_dir():
            for child in fix_dir.iterdir():
                if child.is_file() and child.stat().st_size > 0:
                    data_file = child
                    break

    if data_file is None:
        raise AuthoringError(
            f"cannot re-execute inversion: no data asset found in {proposal_path}/environment"
        )

    return execute_reference_analysis(analysis_code, data_file)


def verify_inversion_reproducibility(proposal_or_path: Proposal | Path) -> bool:
    """Verify that an inversion proposal's answer key is reproduced by re-execution.

    Returns True if re-computed answer matches recorded computed_value exactly.
    Raises AuthoringError if data or analysis missing.
    """
    path = (
        proposal_or_path.path if isinstance(proposal_or_path, Proposal) else Path(proposal_or_path)
    )
    inv_file = path / "inversion.json"
    recorded_val = None
    if inv_file.is_file():
        with contextlib.suppress(Exception):
            inv_data = json.loads(inv_file.read_text(encoding="utf-8"))
            recorded_val = inv_data.get("computed_value")

    if recorded_val is None:
        prop_file = path / "proposal.json"
        if prop_file.is_file():
            with contextlib.suppress(Exception):
                p_data = json.loads(prop_file.read_text(encoding="utf-8"))
                inv_block = p_data.get("inversion_analysis")
                if isinstance(inv_block, dict):
                    recorded_val = inv_block.get("computed_value")

    if recorded_val is None:
        raise AuthoringError(f"no recorded computed_value in {path}")

    recomputed = reexecute_inversion_analysis(path)
    return recomputed == recorded_val


def assemble_meta_task(
    repo_root: Path,
    *,
    spec: dict[str, Any] | None = None,
    exemplar: str | None = None,
    destination: Path | None = None,
) -> Path:
    meta_template = repo_root / META_TASK_RELATIVE
    spec_name = str(spec.get("name", "default")) if spec and spec.get("name") else "default"
    dest = destination or (repo_root / "library/meta/_staged" / spec_name)
    dest.mkdir(parents=True, exist_ok=True)
    if meta_template.is_dir():
        shutil.copytree(meta_template, dest, dirs_exist_ok=True, ignore=_ignore_copy)
    if spec is not None:
        spec_dest = dest / "environment/spec.json"
        _atomic_write_json(spec_dest, spec)
    if exemplar is not None:
        ex_src = resolve_registered_task(repo_root, exemplar)
        ex_dest = dest / "environment/exemplar"
        if ex_dest.exists():
            shutil.rmtree(ex_dest)
        shutil.copytree(ex_src, ex_dest, dirs_exist_ok=True, ignore=_ignore_copy)
    return dest


def check_package_structure(task_dir: Path) -> dict[str, Any]:
    task_dir = Path(task_dir)
    errors: list[str] = []

    required_files = [
        "task.toml",
        "instruction.md",
        "environment/Dockerfile",
        "solution/solve.sh",
        "tests/Dockerfile",
        "tests/test.sh",
    ]

    for rel in required_files:
        p = task_dir / rel
        if not p.is_file():
            errors.append(f"required file missing: {rel}")
        elif p.stat().st_size == 0:
            errors.append(f"required file is empty: {rel}")

    task_toml_path = task_dir / "task.toml"
    if task_toml_path.is_file() and task_toml_path.stat().st_size > 0:
        try:
            config = tomllib.loads(task_toml_path.read_text(encoding="utf-8"))
            if "task" not in config or not isinstance(config["task"], dict):
                errors.append("task.toml: missing [task] section")
            else:
                for field in ("name", "version", "description"):
                    if not config["task"].get(field):
                        errors.append(f"task.toml: [task].{field} is missing or empty")

            for sec in ("environment", "agent", "verifier"):
                if sec not in config or not isinstance(config[sec], dict):
                    errors.append(f"task.toml: missing [{sec}] section")
        except Exception as exc:
            errors.append(f"task.toml: parse error: {exc}")

    for script_rel in ("solution/solve.sh", "tests/test.sh"):
        p = task_dir / script_rel
        if p.is_file():
            content = p.read_text(encoding="utf-8", errors="replace")
            if not content.startswith("#!"):
                errors.append(f"{script_rel}: missing shebang (e.g. #!/bin/sh)")

    passed = len(errors) == 0
    msg = "package structure valid" if passed else f"structure errors: {'; '.join(errors)}"
    return {
        "check": "package_structure",
        "passed": passed,
        "errors": errors,
        "message": msg,
    }


def _extract_sensitive_spans(task_dir: Path) -> list[tuple[str, str]]:
    spans: list[tuple[str, str]] = []
    roots = [task_dir / "solution", task_dir / "tests"]

    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            if p.name in {"Dockerfile", "test.sh", "solve.sh"}:
                continue
            rel = p.relative_to(task_dir).as_posix()
            text = p.read_text(encoding="utf-8", errors="replace")

            for line in text.splitlines():
                normalized = " ".join(line.strip().split())
                if (
                    len(normalized) >= 24
                    and not normalized.startswith(("#", "//", "/*", "*", "import ", "from "))
                    and re.search(r"[A-Za-z0-9]", normalized)
                    and not normalized.startswith("def main():")
                    and not normalized.startswith('if __name__ == "__main__":')
                ):
                    spans.append((rel, normalized))

    return spans


def check_no_answer_leakage(task_dir: Path) -> dict[str, Any]:
    task_dir = Path(task_dir)
    leaks: list[str] = []

    env_dir = task_dir / "environment"
    if env_dir.is_dir():
        for p in env_dir.rglob("*"):
            rel = p.relative_to(env_dir).as_posix()
            parts = p.relative_to(env_dir).parts
            if any(part in {"solution", "tests", "verifier"} for part in parts):
                leaks.append(f"environment/ contains hidden directory: {rel}")
            answer_keys = ("golden", "solution", "expected_summary", "answer_key")
            if p.is_file() and any(k in p.name.lower() for k in answer_keys):
                leaks.append(f"environment/ contains answer file: {rel}")

        df = env_dir / "Dockerfile"
        if df.is_file():
            df_text = df.read_text(encoding="utf-8", errors="replace")
            for line in df_text.splitlines():
                line_clean = line.strip()
                if line_clean.startswith(("COPY", "ADD")) and any(
                    bad in line_clean for bad in ("solution", "tests", "verifier")
                ):
                    leaks.append(f"environment/Dockerfile copies hidden files: {line_clean}")

    visible_files: list[Path] = []
    instr = task_dir / "instruction.md"
    if instr.is_file():
        visible_files.append(instr)
    if env_dir.is_dir():
        for p in env_dir.rglob("*"):
            if p.is_file() and p.name != "Dockerfile":
                visible_files.append(p)

    visible_texts: list[str] = []
    for vf in visible_files:
        content = vf.read_text(encoding="utf-8", errors="replace")
        visible_texts.append(" ".join(content.split()))

    combined_visible = "\n".join(visible_texts)

    sensitive_spans = _extract_sensitive_spans(task_dir)
    for src, span in sensitive_spans:
        if span in combined_visible:
            leaks.append(f"sensitive span from {src} leaked in visible surface: {span[:40]}...")

    passed = len(leaks) == 0
    msg = "no answer leakage detected" if passed else f"leakage detected: {'; '.join(leaks)}"
    return {
        "check": "no_answer_leakage",
        "passed": passed,
        "leaks": leaks,
        "message": msg,
    }


def _prepare_workspace(task_dir: Path, workspace: Path) -> None:
    env_dir = task_dir / "environment"
    if env_dir.is_dir():
        for item in env_dir.iterdir():
            if item.name == "Dockerfile":
                continue
            dest = workspace / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)

    input_dir = workspace / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    if env_dir.is_dir():
        for item in env_dir.iterdir():
            if item.name != "Dockerfile" and not item.is_dir():
                shutil.copy2(item, input_dir / item.name)

    (workspace / "output").mkdir(parents=True, exist_ok=True)


def check_oracle_solution_runs(
    task_dir: Path,
    timeout: float = 60.0,
    workspace: Path | None = None,
) -> dict[str, Any]:
    task_dir = Path(task_dir).resolve()
    sol_sh = (task_dir / "solution/solve.sh").resolve()
    sol_py = (task_dir / "solution/solve.py").resolve()

    if not sol_sh.is_file() and not sol_py.is_file():
        return {
            "check": "oracle_solution_runs",
            "passed": False,
            "message": "solution/solve.sh or solution/solve.py is missing",
        }

    use_temp = workspace is None
    target_workspace = Path(tempfile.mkdtemp(prefix="task_oracle_")) if use_temp else workspace
    assert target_workspace is not None

    try:
        if use_temp:
            _prepare_workspace(task_dir, target_workspace)

        cmd = ["bash", str(sol_sh)] if sol_sh.is_file() else [sys.executable, str(sol_py)]
        env = dict(os.environ)
        env["APP_DIR"] = str(target_workspace)

        proc = subprocess.run(
            cmd,
            cwd=str(target_workspace),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        passed = proc.returncode == 0
        err_tail = proc.stderr[-200:]
        msg = (
            "oracle solution executed successfully"
            if passed
            else f"oracle failed (rc={proc.returncode}): {err_tail}"
        )
        return {
            "check": "oracle_solution_runs",
            "passed": passed,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-1000:],
            "stderr": proc.stderr[-1000:],
            "message": msg,
        }
    except Exception as exc:
        return {
            "check": "oracle_solution_runs",
            "passed": False,
            "message": f"oracle execution raised exception: {exc}",
        }
    finally:
        if use_temp and target_workspace.exists():
            shutil.rmtree(target_workspace, ignore_errors=True)


def check_task_tests_pass(
    task_dir: Path,
    timeout: float = 60.0,
    workspace: Path | None = None,
) -> dict[str, Any]:
    task_dir = Path(task_dir).resolve()
    test_sh = (task_dir / "tests/test.sh").resolve()
    verify_py = (task_dir / "tests/verify.py").resolve()

    if not test_sh.is_file() and not verify_py.is_file():
        return {
            "check": "task_tests_pass",
            "passed": False,
            "message": "tests/test.sh or tests/verify.py is missing",
        }

    use_temp = workspace is None
    target_workspace = Path(tempfile.mkdtemp(prefix="task_verify_")) if use_temp else workspace
    assert target_workspace is not None

    try:
        if use_temp:
            _prepare_workspace(task_dir, target_workspace)
            sol_sh = (task_dir / "solution/solve.sh").resolve()
            sol_py = (task_dir / "solution/solve.py").resolve()
            cmd = ["bash", str(sol_sh)] if sol_sh.is_file() else [sys.executable, str(sol_py)]
            subprocess.run(cmd, cwd=str(target_workspace), capture_output=True, timeout=timeout)

        cmd = ["bash", str(test_sh)] if test_sh.is_file() else [sys.executable, str(verify_py)]
        logs_dir = target_workspace / "logs/verifier"
        logs_dir.mkdir(parents=True, exist_ok=True)

        proc = subprocess.run(
            cmd,
            cwd=str(target_workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        passed = proc.returncode == 0
        reward_file = logs_dir / "reward.json"
        if reward_file.is_file():
            try:
                reward_data = json.loads(reward_file.read_text(encoding="utf-8"))
                if isinstance(reward_data, dict) and reward_data.get("reward") != 1.0:
                    passed = False
            except Exception:
                pass

        err_tail = proc.stderr[-200:]
        msg = "task tests passed" if passed else f"tests failed (rc={proc.returncode}): {err_tail}"
        return {
            "check": "task_tests_pass",
            "passed": passed,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-1000:],
            "stderr": proc.stderr[-1000:],
            "message": msg,
        }
    except Exception as exc:
        return {
            "check": "task_tests_pass",
            "passed": False,
            "message": f"verifier raised exception: {exc}",
        }
    finally:
        if use_temp and target_workspace.exists():
            shutil.rmtree(target_workspace, ignore_errors=True)


def check_task_completeness(task_dir: Path, timeout: float = 60.0) -> dict[str, Any]:
    task_dir = Path(task_dir)

    structure_res = check_package_structure(task_dir)
    leakage_res = check_no_answer_leakage(task_dir)

    workspace = Path(tempfile.mkdtemp(prefix="task_completeness_"))
    try:
        _prepare_workspace(task_dir, workspace)
        oracle_res = check_oracle_solution_runs(task_dir, timeout=timeout, workspace=workspace)
        tests_res = check_task_tests_pass(task_dir, timeout=timeout, workspace=workspace)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    checks = {
        "package_structure": structure_res,
        "no_answer_leakage": leakage_res,
        "oracle_solution_runs": oracle_res,
        "task_tests_pass": tests_res,
    }

    all_passed = all(c["passed"] for c in checks.values())
    rewards = {"reward": 1.0 if all_passed else 0.0}

    return {
        "passed": all_passed,
        "checks": checks,
        "rewards": rewards,
    }


def generate_stub_task(
    destination: Path,
    spec: dict[str, Any],
    *,
    exemplar_dir: Path | None = None,
) -> Path:
    _ = exemplar_dir
    destination.mkdir(parents=True, exist_ok=True)
    task_name = re.sub(
        r"[^a-z0-9-]+", "-", str(spec.get("name", "synthesized-task")).lower()
    ).strip("-") or "synthesized-task"
    category = str(spec.get("category", "data-processing"))
    difficulty = str(spec.get("difficulty", "medium"))
    summary = str(spec.get("summary", "Process input data and generate summary report"))
    toml_category = json.dumps(category)
    toml_difficulty = json.dumps(difficulty)
    toml_summary = json.dumps(summary)

    task_toml = f"""schema_version = "1.4"
artifacts = [
    "/app/output/summary.json",
]

[task]
name = "local-lab/{task_name}"
version = "0.1.0"
description = {toml_summary}
keywords = ["python", {toml_category}, "separate-verifier"]

[[task.authors]]
name = "Eval Lab Synthesizer"
email = "p.makhnatch@gmail.com"

[metadata]
difficulty = {toml_difficulty}
category = {toml_category}
tags = ["synthetic", "authoring"]

[verifier]
timeout_sec = 60.0
environment_mode = "separate"
collect = []

[agent]
timeout_sec = 120.0

[environment]
network_mode = "public"
build_timeout_sec = 300.0
os = "linux"
cpus = 1
memory_mb = 512
storage_mb = 2048
mcp_servers = []
"""
    (destination / "task.toml").write_text(task_toml, encoding="utf-8")

    instruction = f"""# {task_name.replace("-", " ").title()}

Read the records in `/app/input/data.json` and create `/app/output/summary.json`.

The output must be a valid JSON object with the following fields:
- `schema_version`: integer `1`
- `total_records`: total count of records processed
- `status`: string `"ok"`

Write valid UTF-8 JSON with a trailing newline. Do not modify or delete the input file.
"""
    (destination / "instruction.md").write_text(instruction, encoding="utf-8")

    env_dir = destination / "environment"
    env_dir.mkdir(parents=True, exist_ok=True)
    env_dockerfile = (
        "FROM python:3.13-slim-bookworm\n\n"
        "WORKDIR /app\n\n"
        "COPY data.json /app/input/data.json\n\n"
        "RUN mkdir -p /app/output\n"
    )
    (env_dir / "Dockerfile").write_text(env_dockerfile, encoding="utf-8")
    sample_data = [
        {"id": 1, "type": "event_a", "val": 10},
        {"id": 2, "type": "event_b", "val": 20},
        {"id": 3, "type": "event_a", "val": 30},
    ]
    (env_dir / "data.json").write_text(json.dumps(sample_data, indent=2) + "\n", encoding="utf-8")

    sol_dir = destination / "solution"
    sol_dir.mkdir(parents=True, exist_ok=True)
    sol_sh = """#!/bin/sh
set -eu

if [ -f /solution/solve.py ]; then
    exec python /solution/solve.py
else
    SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
    exec python "$SCRIPT_DIR/solve.py"
fi
"""
    (sol_dir / "solve.sh").write_text(sol_sh, encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(sol_dir / "solve.sh", 0o755)

    sol_py = """import json
from pathlib import Path

input_file = Path("/app/input/data.json")
if not input_file.is_file():
    input_file = Path("environment/data.json")
if not input_file.is_file():
    input_file = Path("input/data.json")

data = json.loads(input_file.read_text(encoding="utf-8"))
summary = {
    "schema_version": 1,
    "total_records": len(data),
    "status": "ok",
}
output_dir = Path("/app/output")
if not output_dir.exists():
    output_dir = Path("output")
output_dir.mkdir(parents=True, exist_ok=True)
(output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\\n", encoding="utf-8")
"""
    (sol_dir / "solve.py").write_text(sol_py, encoding="utf-8")

    tests_dir = destination / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    test_dockerfile = (
        "FROM python:3.13-slim-bookworm\n\n"
        "WORKDIR /app\n\n"
        "COPY test.sh /tests/test.sh\n"
        "COPY verify.py /tests/verify.py\n\n"
        "RUN chmod +x /tests/test.sh\n"
    )
    (tests_dir / "Dockerfile").write_text(test_dockerfile, encoding="utf-8")
    test_sh = """#!/bin/sh
set -eu

if [ -f /tests/verify.py ]; then
    exec python /tests/verify.py
else
    SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
    exec python "$SCRIPT_DIR/verify.py"
fi
"""
    (tests_dir / "test.sh").write_text(test_sh, encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(tests_dir / "test.sh", 0o755)

    verify_py = """import json
from pathlib import Path

AGENT_OUTPUT = Path("/app/output/summary.json")
if not AGENT_OUTPUT.is_file():
    AGENT_OUTPUT = Path("output/summary.json")

LOG_DIR = Path("/logs/verifier")
if not LOG_DIR.exists():
    LOG_DIR = Path("logs/verifier")

def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not AGENT_OUTPUT.is_file():
        passed = False
        message = "summary.json is missing"
    else:
        try:
            data = json.loads(AGENT_OUTPUT.read_text(encoding="utf-8"))
            passed = (
                isinstance(data, dict)
                and data.get("schema_version") == 1
                and data.get("total_records") == 3
                and data.get("status") == "ok"
            )
            message = "summary.json is valid" if passed else "summary.json content mismatch"
        except Exception as exc:
            passed = False
            message = f"error parsing json: {exc}"

    checks = {"correctness": {"passed": passed, "message": message}}
    rewards = {"reward": 1.0 if passed else 0.0}
    ctrf = {
        "report": {
            "summary": {
                "tests": 1,
                "passed": 1 if passed else 0,
                "failed": 0 if passed else 1,
            }
        }
    }
    (LOG_DIR / "checks.json").write_text(json.dumps(checks, indent=2) + "\\n", encoding="utf-8")
    (LOG_DIR / "reward.json").write_text(json.dumps(rewards, indent=2) + "\\n", encoding="utf-8")
    (LOG_DIR / "ctrf.json").write_text(json.dumps(ctrf, indent=2) + "\\n", encoding="utf-8")
    print(json.dumps({"passed": passed, "checks": checks}))

if __name__ == "__main__":
    main()
"""
    (tests_dir / "verify.py").write_text(verify_py, encoding="utf-8")
    return destination


class AuthoringPipeline:
    """Propose → battery → review. Registration is a hard refusal."""

    def __init__(
        self,
        repo_root: Path,
        *,
        derived_root: Path | None = None,
        runner: ControlRunner | None = None,
        adapter: AnalyzerCallable | None = None,
        now: _CLOCK | None = None,
        new_id: _IDS | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.derived_root = (
            derived_root.resolve()
            if derived_root is not None
            else derived_root_from_environment(self.repo_root)
        )
        self.runner = runner or StructuralControlRunner()
        self.adapter = adapter
        self._now = now or now_utc
        self._new_id = new_id or new_ulid

    @property
    def quarantine(self) -> Path:
        return proposed_root(self.repo_root)

    @property
    def ledger(self) -> Path:
        return ledger_path(self.derived_root)

    def records(self) -> list[QualificationRecord]:
        return load_ledger(self.ledger)

    def get(self, proposal_id: str) -> Proposal:
        path = self.quarantine / proposal_id
        if not path.is_dir():
            raise ProposalNotFoundError(f"proposal {proposal_id!r} is not in {self.quarantine}")
        return load_proposal(path)

    def sample_specs(
        self,
        count: int = 20,
        *,
        seed: int = 42,
        novel_designer: Callable[[str, str], dict[str, Any]] | None = None,
        novel_count: int = 0,
    ) -> list[dict[str, Any]]:
        """Sample specifications coverage-first via sample_spec_batch (SG-2)."""
        return sample_spec_batch(
            self.repo_root,
            count=count,
            derived_root=self.derived_root,
            seed=seed,
            novel_designer=novel_designer,
            novel_count=novel_count,
        )

    def propose_model(
        self,
        topic_seed: str,
        style_constraint: str,
        *,
        adapter: AnalyzerCallable | None = None,
    ) -> Proposal:
        """Generate, validate, and quarantine one model-backed proposal."""
        active_adapter = adapter or self.adapter
        if active_adapter is None:
            raise AuthoringError(
                "model proposal requires an injected adapter with an explicit pinned model"
            )
        design = ModelBackedDesigner(active_adapter).design(topic_seed, style_constraint)
        spec = design.spec
        coordinate = spec_coordinate_key(spec)
        existing = extract_ledger_coordinates(self.ledger, self.quarantine)
        if coordinate in existing:
            raise AuthoringError("model designer returned a duplicate proposal coordinate")

        proposal_id = self._new_id()
        destination = self.quarantine / proposal_id
        if destination.exists():
            raise AuthoringError(f"proposal id {proposal_id!r} already exists")
        created = isoformat(self._now())
        try:
            generate_stub_task(destination, spec)
            axes = spec.get("axes")
            if not isinstance(axes, dict):
                axes = {
                    "category": spec["category"],
                    "scenario": spec["scenario"],
                    "difficulty": spec["difficulty"],
                }
            proposal = Proposal(
                proposal_id=proposal_id,
                seed_class="scenario",
                ref_task=None,
                path=destination,
                outcome="proposed",
                version="0.1.0",
                category=spec["category"],
                scenario=spec["scenario"],
                difficulty=spec["difficulty"],
                injected_spec=spec,
                provenance=design.provenance,
                axes=axes,
                created_at=created,
            )
            _atomic_write_json(destination / "proposal.json", proposal.manifest())
            upsert_ledger(
                self.ledger,
                QualificationRecord(
                    proposal_id=proposal_id,
                    seed_class="scenario",
                    outcome="proposed",
                    created_at=created,
                    updated_at=created,
                ),
            )
        except Exception:
            if destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            raise
        return proposal

    @overload
    def propose(
        self,
        seed: SeedClass = "craft-gap",
        *,
        ref: str | None = None,
        via_harbor: Literal[False] = False,
        agent: str = "oracle",
        model: str | None = None,
        spec: dict[str, Any] | None = None,
        exemplar: str | None = None,
        analysis_code: str | None = None,
    ) -> Proposal: ...

    @overload
    def propose(
        self,
        seed: SeedClass = "craft-gap",
        *,
        ref: str | None = None,
        via_harbor: Literal[True],
        agent: str = "oracle",
        model: str | None = None,
        spec: dict[str, Any] | None = None,
        exemplar: str | None = None,
        analysis_code: str | None = None,
    ) -> tuple[ExperimentSpec, Path, PolicyDecision]: ...

    def propose(
        self,
        seed: SeedClass = "craft-gap",
        *,
        ref: str | None = None,
        via_harbor: bool = False,
        agent: str = "oracle",
        model: str | None = None,
        spec: dict[str, Any] | None = None,
        exemplar: str | None = None,
        analysis_code: str | None = None,
    ) -> Proposal | tuple[ExperimentSpec, Path, PolicyDecision]:
        if via_harbor:
            return self.propose_via_harbor(
                seed,
                ref=ref,
                agent=agent,
                model=model,
                spec=spec,
                exemplar=exemplar,
            )
        if seed not in SEED_CLASSES:
            raise AuthoringError(f"unknown seed class {seed!r}; expected one of {SEED_CLASSES}")
        proposal_id = self._new_id()
        destination = self.quarantine / proposal_id
        if destination.exists():
            raise AuthoringError(f"proposal id {proposal_id!r} already exists")
        destination.mkdir(parents=True, exist_ok=False)
        created = isoformat(self._now())
        try:
            if seed == "mutation":
                proposal = self._propose_mutation(proposal_id, destination, ref, created)
            elif seed == "scenario":
                proposal = self._propose_scenario(proposal_id, destination, ref, created)
            elif seed == "inversion":
                proposal = self._propose_inversion(
                    proposal_id, destination, ref, created, analysis_code=analysis_code
                )
            else:
                proposal = self._propose_craft_gap(proposal_id, destination, ref, created)
            _atomic_write_json(destination / "proposal.json", proposal.manifest())
        except Exception:
            if destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            raise
        upsert_ledger(
            self.ledger,
            QualificationRecord(
                proposal_id=proposal_id,
                seed_class=seed,
                ref_task=proposal.ref_task,
                outcome="proposed",
                created_at=created,
                updated_at=created,
            ),
        )
        return proposal

    def propose_via_harbor(
        self,
        seed: SeedClass = "craft-gap",
        *,
        ref: str | None = None,
        agent: str = "oracle",
        model: str | None = None,
        spec: dict[str, Any] | None = None,
        exemplar: str | None = None,
        submitted_by: str | None = None,
    ) -> tuple[ExperimentSpec, Path, PolicyDecision]:
        spec_dict = spec or sample_meta_spec(
            self.repo_root,
            seed=seed,
            ref=ref,
            derived_root=self.derived_root,
        )
        exemplar_name = exemplar or (
            ref if ref and (self.repo_root / "library/tasks" / ref).is_dir() else "event-summary"
        )
        meta_task_dir = assemble_meta_task(
            self.repo_root,
            spec=spec_dict,
            exemplar=exemplar_name,
        )
        task_ref = _repo_relative(meta_task_dir, self.repo_root)

        spec_id = self._new_id()
        task_name_slug = (
            re.sub(r"[^a-z0-9-]+", "-", str(spec_dict.get("name", "task")).lower()).strip("-")
            or "task"
        )
        exp_name = f"synth-{seed[:8]}-{task_name_slug[:20]}-{spec_id[-8:].lower()}"
        exp_name = re.sub(r"[^a-z0-9-]+", "-", exp_name).strip("-")
        if len(exp_name) < 3:
            exp_name = f"synth-{exp_name}"

        experiment_spec = ExperimentSpec(
            spec_id=spec_id,
            name=exp_name,
            purpose="craft",
            hypothesis="Synthesize task via meta-task scaffold and completeness verifier",
            task=task_ref,
            agent=agent,
            model=model,
            submitted_by=submitted_by or "authoring-metaloop",
        )

        queue = DirectoryQueue(self.repo_root / "queue")
        policy_path = self.repo_root / "policy/standing-approvals.yaml"
        gate = PolicyGate(load_policy(policy_path), repo_root=self.repo_root)

        dest_path, decision = queue.submit(
            experiment_spec,
            gate=gate,
            spent_today_usd=0.0,
        )
        return experiment_spec, dest_path, decision

    def harvest(self, job_dir_or_id: str | Path) -> Proposal:
        if isinstance(job_dir_or_id, Path):
            job_dir = job_dir_or_id
        else:
            job_dir = self.repo_root / "runs" / job_dir_or_id

        if not job_dir.exists():
            alt = self.repo_root / str(job_dir_or_id)
            if alt.exists():
                job_dir = alt
            else:
                alt_ev = self.repo_root / "research/evidence/runs" / str(job_dir_or_id)
                if alt_ev.exists():
                    job_dir = alt_ev
                else:
                    raise AuthoringError(f"job directory not found: {job_dir_or_id}")

        candidates = [
            job_dir / "artifacts/output/task",
            job_dir / "artifacts/task",
            job_dir / "output/task",
            job_dir / "task",
        ]
        task_pkg = None
        for cand in candidates:
            if cand.is_dir() and (cand / "task.toml").is_file():
                task_pkg = cand
                break

        if task_pkg is None:
            raise AuthoringError(f"no generated task artifact found in {job_dir}")

        verifier_passed = False
        verifier_dir = job_dir / "verifier"
        reward_file = verifier_dir / "reward.json"
        checks_file = verifier_dir / "checks.json"
        if reward_file.is_file():
            try:
                rdata = json.loads(reward_file.read_text(encoding="utf-8"))
                if isinstance(rdata, dict) and rdata.get("reward") == 1.0:
                    verifier_passed = True
            except Exception:
                pass
        elif checks_file.is_file():
            try:
                cdata = json.loads(checks_file.read_text(encoding="utf-8"))
                if isinstance(cdata, dict) and all(
                    v.get("passed") for v in cdata.values() if isinstance(v, dict)
                ):
                    verifier_passed = True
            except Exception:
                pass
        else:
            report = check_task_completeness(task_pkg)
            if report.get("passed"):
                verifier_passed = True

        if not verifier_passed:
            raise AuthoringError(f"harvest refused: completeness checker did not pass in {job_dir}")

        job_id = job_dir.name
        job_manifest = job_dir / "manifest.json"
        job_result = job_dir / "result.json"
        spec_data: dict[str, Any] | None = None
        exemplar_name: str | None = None

        if job_manifest.is_file():
            try:
                m_raw = json.loads(job_manifest.read_text(encoding="utf-8"))
                if isinstance(m_raw, dict):
                    job_id = m_raw.get("job_id", job_id)
                    spec_data = m_raw.get("injected_spec") or m_raw.get("spec")
                    exemplar_name = m_raw.get("exemplar")
            except Exception:
                pass

        proposal_id = self._new_id()
        destination = self.quarantine / proposal_id
        destination.mkdir(parents=True, exist_ok=False)
        shutil.copytree(task_pkg, destination, dirs_exist_ok=True, ignore=_ignore_copy)

        created = isoformat(self._now())
        seed_class: SeedClass = "craft-gap"
        if spec_data and spec_data.get("seed_class") in SEED_CLASSES:
            seed_class = spec_data["seed_class"]

        inputs: list[dict[str, Any]] = []
        target_file = (
            job_result
            if job_result.is_file()
            else (job_manifest if job_manifest.is_file() else None)
        )
        if target_file is not None:
            inputs.append(
                {
                    "path": _repo_relative(target_file, self.repo_root),
                    "id": job_id,
                    "digest": compute_file_digest(target_file),
                }
            )
        else:
            inputs.append(
                {
                    "path": _repo_relative(job_dir, self.repo_root),
                    "id": job_id,
                    "digest": None,
                }
            )
        category = spec_data.get("category") if spec_data else None
        scenario = spec_data.get("scenario") if spec_data else None
        difficulty = spec_data.get("difficulty") if spec_data else None
        axes = spec_data.get("axes") if spec_data else None
        provenance = spec_data.get("provenance") if spec_data else None

        proposal = Proposal(
            proposal_id=proposal_id,
            seed_class=seed_class,
            ref_task=exemplar_name,
            path=destination,
            outcome="proposed",
            version=read_task_version(destination / "task.toml") or "0.1.0",
            created_at=created,
            job_id=job_id,
            inputs=inputs,
            injected_spec=spec_data,
            exemplar=exemplar_name,
            category=category,
            scenario=scenario,
            difficulty=difficulty,
            axes=axes,
            provenance=provenance,
        )
        _atomic_write_json(destination / "proposal.json", proposal.manifest())
        upsert_ledger(
            self.ledger,
            QualificationRecord(
                proposal_id=proposal_id,
                seed_class=seed_class,
                ref_task=exemplar_name,
                outcome="proposed",
                created_at=created,
                updated_at=created,
            ),
        )
        return proposal

    def run_battery(self, proposal_id: str) -> BatteryReport:
        proposal = self.get(proposal_id)
        evidence_dir = proposal.path / "battery"
        checks: list[ControlResult] = []
        for check in BATTERY_CHECKS:
            result = self.runner.run(proposal.path, check, evidence_dir=evidence_dir)
            if result.check != check:
                raise AuthoringError(f"control runner returned {result.check!r} for {check!r}")
            checks.append(result)
        all_passed = all(item.passed for item in checks)
        expected = {
            "oracle": 1.0,
            "nop": 0.0,
            "fair_oracle": 1.0,
            "adversarial": 0.0,
        }
        for item in checks:
            if item.passed and item.reward != expected[item.check]:
                all_passed = False
        outcome: Outcome = "battery_passed" if all_passed else proposal.outcome
        if outcome == "registered":
            raise RegisterRefusal(proposal_id, outcome)
        if proposal.outcome == "craft_reviewed" and all_passed:
            outcome = "craft_reviewed"
        self._write_outcome(proposal, outcome)
        record = self._record_for(proposal_id)
        updated = record.model_copy(
            update={
                "battery_oracle": checks[0].passed,
                "battery_nop": checks[1].passed,
                "battery_fair_oracle": checks[2].passed,
                "battery_adversarial": checks[3].passed,
                "evidence_paths": [item.evidence_path for item in checks],
                "outcome": outcome,
                "updated_at": isoformat(self._now()),
            }
        )
        upsert_ledger(self.ledger, updated)
        return BatteryReport(
            proposal_id=proposal_id,
            outcome=outcome,
            checks=tuple(checks),
            all_passed=all_passed,
        )

    def review(self, proposal_id: str) -> ReviewReport:
        proposal = self.get(proposal_id)
        record = self._record_for(proposal_id)
        if record.outcome not in {"battery_passed", "craft_reviewed"}:
            raise AuthoringError(
                f"proposal {proposal_id!r} is {record.outcome!r}; review requires battery_passed"
            )
        score, reasons = score_review(proposal)
        evidence_path = proposal.path / "review.json"
        payload = {
            "proposal_id": proposal_id,
            "score": score,
            "reasons": reasons,
            "rubric": "craft-patterns/v1",
            "seed_class": proposal.seed_class,
            "target_facets": proposal.target_facets,
        }
        _atomic_write_json(evidence_path, payload)
        evidence = evidence_path.as_posix()
        paths = list(record.evidence_paths)
        if evidence not in paths:
            paths.append(evidence)
        updated = record.model_copy(
            update={
                "review_score": score,
                "evidence_paths": paths,
                "outcome": "craft_reviewed",
                "updated_at": isoformat(self._now()),
            }
        )
        upsert_ledger(self.ledger, updated)
        self._write_outcome(proposal, "craft_reviewed")
        return ReviewReport(
            proposal_id=proposal_id,
            outcome="craft_reviewed",
            score=score,
            reasons=tuple(reasons),
            evidence_path=evidence,
        )

    def register(self, proposal_id: str) -> None:
        """Fail-closed human gate. Never writes `registered`."""
        record = self._record_for(proposal_id)
        raise RegisterRefusal(proposal_id, record.outcome)

    def run_batch(
        self, count: int = 5, *, seeds: Sequence[SeedClass] | None = None
    ) -> list[BatchItem]:
        if count < 1:
            raise AuthoringError("batch count must be >= 1")
        cycle: Sequence[SeedClass] = seeds or ("mutation", "scenario", "craft-gap", "inversion")
        items: list[BatchItem] = []
        for index in range(count):
            seed = cycle[index % len(cycle)]
            proposal = self.propose(seed)
            battery = self.run_battery(proposal.proposal_id)
            if not battery.all_passed:
                raise AuthoringError(
                    f"batch halted: {proposal.proposal_id} failed battery ({seed})"
                )
            review = self.review(proposal.proposal_id)
            items.append(
                BatchItem(
                    proposal_id=proposal.proposal_id,
                    seed_class=seed,
                    outcome=review.outcome,
                    battery=battery,
                    review=review,
                )
            )
        return items

    def _propose_mutation(
        self, proposal_id: str, destination: Path, ref: str | None, created: str
    ) -> Proposal:
        source = resolve_registered_task(self.repo_root, ref)
        source_digest = _sha256_tree(source)
        shutil.copytree(source, destination, dirs_exist_ok=True, ignore=_ignore_copy)
        task_toml = destination / "task.toml"
        if not task_toml.is_file():
            raise AuthoringError(f"mutation source {source} has no task.toml")
        new_version = bump_version(read_task_version(task_toml))
        task_toml.write_text(rewrite_task_version(task_toml.read_text(), new_version))
        instruction = destination / "instruction.md"
        if instruction.is_file():
            instruction.write_text(
                instruction.read_text()
                + "\n\n## Mutation\n\n"
                + f"Versioned variant of `{_repo_relative(source, self.repo_root)}` "
                + f"at {new_version}. Source digest `{source_digest}`.\n"
            )
        after = _sha256_tree(source)
        if after != source_digest:
            raise AuthoringError(f"mutation edited source in place: {source}")
        return Proposal(
            proposal_id=proposal_id,
            seed_class="mutation",
            ref_task=source.name,
            path=destination,
            outcome="proposed",
            version=new_version,
            source_path=_repo_relative(source, self.repo_root),
            source_digest=source_digest,
            category="data-processing",
            scenario="mutation",
            difficulty="intermediate",
            provenance="mutation",
            axes={
                "category": "data-processing",
                "scenario": "mutation",
                "difficulty": "intermediate",
            },
            created_at=created,
        )

    def _propose_scenario(
        self, proposal_id: str, destination: Path, ref: str | None, created: str
    ) -> Proposal:
        scenario = resolve_scenario(self.repo_root, ref)
        relative = _repo_relative(scenario, self.repo_root)
        body = scenario.read_text()
        title = next(
            (line[2:].strip() for line in body.splitlines() if line.startswith("# ")),
            scenario.stem,
        )
        excerpt = body.strip()
        if len(excerpt) > 4000:
            excerpt = excerpt[:4000].rstrip() + "\n"
        _write_stub_task(
            destination,
            name=f"proposed-{scenario.stem}",
            version="0.1.0",
            instruction=(
                f"# {title}\n\nSeeded from research scenario `{relative}`.\n\n{excerpt}\n"
            ),
            separate=True,
            verifier_hint="pytest",
        )
        return Proposal(
            proposal_id=proposal_id,
            seed_class="scenario",
            ref_task=relative,
            path=destination,
            outcome="proposed",
            version="0.1.0",
            scenario_path=relative,
            category="data-processing",
            scenario=scenario.stem,
            difficulty="intermediate",
            provenance="scenario",
            axes={
                "category": "data-processing",
                "scenario": scenario.stem,
                "difficulty": "intermediate",
            },
            created_at=created,
        )

    def _propose_craft_gap(
        self, proposal_id: str, destination: Path, ref: str | None, created: str
    ) -> Proposal:
        facets = find_craft_gap(craft_parquet_path(self.derived_root))
        if ref is not None:
            facets = {**facets, "requested_ref": ref}
        _write_stub_task(
            destination,
            name=f"proposed-gap-{facets['verifier_type']}",
            version="0.1.0",
            instruction=(
                "# CRAFT coverage gap\n\n"
                "This proposal targets a facet combination with zero coverage "
                "in `derived/parquet/craft/craft.parquet`:\n\n"
                f"- verifier_type: `{facets['verifier_type']}`\n"
                f"- env_multi_container: `{facets['env_multi_container']}`\n"
                f"- pinned_deps: `{facets['pinned_deps']}`\n"
            ),
            separate=True,
            verifier_hint=str(facets["verifier_type"]),
            multi_container=bool(facets["env_multi_container"]),
            pinned=bool(facets["pinned_deps"]),
        )
        return Proposal(
            proposal_id=proposal_id,
            seed_class="craft-gap",
            ref_task=ref,
            path=destination,
            outcome="proposed",
            version="0.1.0",
            target_facets={name: facets[name] for name in GAP_AXES},
            category="data-processing",
            scenario="structured-pipeline",
            difficulty="intermediate",
            provenance="craft-gap",
            axes={
                "category": "data-processing",
                "scenario": "structured-pipeline",
                "difficulty": "intermediate",
                "target_facets": {name: facets[name] for name in GAP_AXES},
            },
            created_at=created,
        )

    def _propose_inversion(
        self,
        proposal_id: str,
        destination: Path,
        ref: str | None,
        created: str,
        *,
        analysis_code: str | None = None,
    ) -> Proposal:
        asset_file, asset_rel = resolve_inversion_asset(self.repo_root, ref)
        asset_digest = compute_file_digest(asset_file)
        asset_name = asset_file.name

        if analysis_code is not None:
            code = analysis_code
            instruction_text = (
                f"# Process {asset_name}\n\n"
                f"Read `/app/input/{asset_name}` and create `/app/output/summary.json`.\n\n"
                "Write valid UTF-8 JSON with a trailing newline. Do not modify the input file.\n"
            )
        else:
            code, instruction_text, _ = generate_inversion_analysis_code(asset_file)

        # Execute reference analysis to compute answer key by construction
        computed_value = execute_reference_analysis(code, asset_file)
        analysis_digest = f"sha256:{hashlib.sha256(code.encode('utf-8')).hexdigest()}"

        inversion_metadata = {
            "schema_version": "inversion/1",
            "data_asset_path": asset_rel,
            "data_asset_digest": asset_digest,
            "analysis_code": code,
            "analysis_digest": analysis_digest,
            "computed_value": computed_value,
            "executed_at": created,
            "output_path": "output/summary.json",
        }
        InversionAnalysis.model_validate(inversion_metadata)

        destination.mkdir(parents=True, exist_ok=True)
        task_name = f"proposed-inversion-{asset_file.stem.replace('_', '-')}"

        task_toml = f"""schema_version = "1.0"

[task]
name = "{task_name}"
version = "0.1.0"
description = "Inversion task computed from {asset_name}"

[metadata]
category = "data-processing"
seed_class = "inversion"

[verifier]
timeout_sec = 60.0
environment_mode = "separate"

[agent]
timeout_sec = 120.0

[environment]
network_mode = "public"
build_timeout_sec = 300.0
os = "linux"
cpus = 1
memory_mb = 512
storage_mb = 2048
mcp_servers = []
"""
        (destination / "task.toml").write_text(task_toml, encoding="utf-8")
        (destination / "instruction.md").write_text(instruction_text, encoding="utf-8")

        env_dir = destination / "environment"
        env_dir.mkdir(parents=True, exist_ok=True)
        env_dockerfile = (
            "FROM python:3.12-slim\n\n"
            "WORKDIR /app\n\n"
            f"COPY {asset_name} /app/input/{asset_name}\n\n"
            "RUN mkdir -p /app/output\n"
        )
        (env_dir / "Dockerfile").write_text(env_dockerfile, encoding="utf-8")
        shutil.copy2(asset_file, env_dir / asset_name)

        sol_dir = destination / "solution"
        sol_dir.mkdir(parents=True, exist_ok=True)
        sol_sh = """#!/bin/sh
set -eu

if [ -f /solution/solve.py ]; then
    exec python /solution/solve.py
else
    SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
    exec python "$SCRIPT_DIR/solve.py"
 fi
"""
        (sol_dir / "solve.sh").write_text(sol_sh, encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(sol_dir / "solve.sh", 0o755)
        (sol_dir / "solve.py").write_text(code, encoding="utf-8")

        tests_dir = destination / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        fixtures_dir = tests_dir / "fixtures"
        fixtures_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(asset_file, fixtures_dir / asset_name)

        test_dockerfile = (
            "FROM python:3.12-slim\n\n"
            "WORKDIR /app\n\n"
            "COPY test.sh /tests/test.sh\n"
            "COPY verify.py /tests/verify.py\n"
            "COPY fixtures/ /tests/fixtures/\n\n"
            "RUN chmod +x /tests/test.sh\n"
        )
        (tests_dir / "Dockerfile").write_text(test_dockerfile, encoding="utf-8")

        test_sh = """#!/bin/sh
set -eu

if [ -f /tests/verify.py ]; then
    exec python /tests/verify.py
else
    SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
    exec python "$SCRIPT_DIR/verify.py"
fi
"""
        (tests_dir / "test.sh").write_text(test_sh, encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tests_dir / "test.sh", 0o755)

        verify_py = f"""import json
from pathlib import Path

AGENT_OUTPUT = Path("/app/output/summary.json")
if not AGENT_OUTPUT.is_file():
    AGENT_OUTPUT = Path("output/summary.json")

EXPECTED_OUTPUT = {json.dumps(computed_value, indent=2)}

LOG_DIR = Path("/logs/verifier")
if not LOG_DIR.exists():
    LOG_DIR = Path("logs/verifier")


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not AGENT_OUTPUT.is_file():
        passed = False
        message = "summary.json is missing"
    else:
        try:
            candidate = json.loads(AGENT_OUTPUT.read_text(encoding="utf-8"))
            passed = candidate == EXPECTED_OUTPUT
            message = (
                "summary.json verified against computed answer key"
                if passed
                else "summary.json content mismatch"
            )
        except Exception as exc:
            passed = False
            message = f"error parsing json: {{exc}}"

    checks = {{"correctness": {{"passed": passed, "message": message}}}}
    rewards = {{"reward": 1.0 if passed else 0.0}}
    ctrf = {{
        "report": {{
            "summary": {{
                "tests": 1,
                "passed": 1 if passed else 0,
                "failed": 0 if passed else 1,
            }}
        }}
    }}
    (LOG_DIR / "checks.json").write_text(
        json.dumps(checks, indent=2) + "\\n", encoding="utf-8"
    )
    (LOG_DIR / "reward.json").write_text(
        json.dumps(rewards, indent=2) + "\\n", encoding="utf-8"
    )
    (LOG_DIR / "ctrf.json").write_text(
        json.dumps(ctrf, indent=2) + "\\n", encoding="utf-8"
    )
    print(json.dumps({{"passed": passed, "checks": checks}}))


if __name__ == "__main__":
    main()
"""
        (tests_dir / "verify.py").write_text(verify_py, encoding="utf-8")
        _atomic_write_json(destination / "inversion.json", inversion_metadata)

        inputs = [
            {
                "path": asset_rel,
                "id": asset_name,
                "digest": asset_digest,
            }
        ]

        return Proposal(
            proposal_id=proposal_id,
            seed_class="inversion",
            ref_task=asset_rel,
            path=destination,
            outcome="proposed",
            version="0.1.0",
            source_path=asset_rel,
            source_digest=asset_digest,
            category="data-processing",
            scenario="inversion",
            difficulty="intermediate",
            provenance="inversion",
            axes={
                "category": "data-processing",
                "scenario": "inversion",
                "difficulty": "intermediate",
            },
            created_at=created,
            inputs=inputs,
            inversion_analysis=inversion_metadata,
        )

    def _write_outcome(self, proposal: Proposal, outcome: Outcome) -> None:
        if outcome == "registered":
            raise RegisterRefusal(proposal.proposal_id, outcome)
        payload = proposal.manifest()
        payload["outcome"] = outcome
        _atomic_write_json(proposal.path / "proposal.json", payload)

    def _record_for(self, proposal_id: str) -> QualificationRecord:
        for record in self.records():
            if record.proposal_id == proposal_id:
                return record
        raise ProposalNotFoundError(f"proposal {proposal_id!r} has no ledger row")


def _write_stub_task(
    destination: Path,
    *,
    name: str,
    version: str,
    instruction: str,
    separate: bool,
    verifier_hint: str,
    multi_container: bool = False,
    pinned: bool = True,
) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    mode = 'environment_mode = "separate"' if separate else ""
    (destination / "task.toml").write_text(
        f"""schema_version = "1.0"

[task]
name = "{name}"
version = "{version}"

[metadata]
category = "proposed"

[verifier]
timeout_sec = 60.0
{mode}
"""
    )
    text = instruction if instruction.endswith("\n") else instruction + "\n"
    (destination / "instruction.md").write_text(text)
    environment = destination / "environment"
    environment.mkdir(exist_ok=True)
    pin = "@sha256:" + ("0" * 64) if pinned else ":3.12-slim"
    (environment / "Dockerfile").write_text(f"FROM python{pin}\n")
    if pinned:
        (environment / "requirements.txt").write_text("pytest==8.4.1\n")
    if multi_container:
        (environment / "docker-compose.yaml").write_text(
            "services:\n  main:\n    build: .\n  sidecar:\n    image: python:3.12-slim\n"
        )
    tests = destination / "tests"
    tests.mkdir(exist_ok=True)
    if verifier_hint == "pytest":
        (tests / "test_proposed.py").write_text(
            "def test_proposed_placeholder() -> None:\n    assert False, 'proposed task'\n"
        )
    elif verifier_hint == "golden_file":
        (tests / "golden.txt").write_text("expected\n")
        (tests / "test.sh").write_text("#!/bin/bash\ndiff -u golden.txt /app/output/answer.txt\n")
    else:
        (tests / "test.sh").write_text("#!/bin/bash\nexit 1\n")
    solution = destination / "solution"
    solution.mkdir(exist_ok=True)
    (solution / "solve.sh").write_text("#!/bin/bash\necho proposed\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evallab.authoring",
        description="BUILDER authoring pipeline (WS-C). Halts at the human registry gate.",
    )
    parser.add_argument("--root", type=Path, default=None, help="repository root")
    parser.add_argument("--out", type=Path, default=None, help="derived parquet root")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    subparsers = parser.add_subparsers(dest="command", required=True)

    propose = subparsers.add_parser("propose", help="seed a quarantined proposal")
    propose.add_argument(
        "--seed",
        default="craft-gap",
        choices=SEED_CLASSES,
        help="mutation | scenario | craft-gap | inversion",
    )
    propose.add_argument("--ref", default=None, help="source task or research scenario")
    propose.add_argument(
        "--via-harbor",
        action="store_true",
        help="assemble meta-task and submit through Harbor queue (purpose=craft)",
    )
    propose.add_argument("--agent", default="oracle", help="agent for Harbor execution")
    propose.add_argument("--model", default=None, help="model for Harbor execution")
    propose.add_argument("--spec", type=Path, default=None, help="custom spec JSON path")
    propose.add_argument("--exemplar", default=None, help="exemplar task name")
    propose.add_argument(
        "--analysis-code", default=None, help="custom Python analysis code for inversion"
    )

    model_propose = subparsers.add_parser(
        "model-propose",
        help="model-design one quarantined proposal (spends subscription quota)",
    )
    model_propose.add_argument("--topic", required=True, help="topic seed for the designer")
    model_propose.add_argument("--style", required=True, help="style constraint for the designer")
    model_propose.add_argument(
        "--model",
        required=True,
        help="explicit pinned model selector; this spends subscription quota",
    )
    model_propose.add_argument(
        "--transport",
        required=True,
        choices=MODEL_TRANSPORTS,
        help="model transport; explicit selection required and spends subscription quota",
    )
    model_propose.add_argument("--timeout", type=float, default=120.0)


    harvest = subparsers.add_parser(
        "harvest",
        help="harvest a verified task from completed Harbor job into _proposed/",
    )
    harvest.add_argument("job", help="job directory or job ID to harvest")
    battery = subparsers.add_parser("battery", help="run the four local control checks")
    battery.add_argument("proposal_id")

    review = subparsers.add_parser("review", help="score a battery-passed proposal")
    review.add_argument("proposal_id")

    register = subparsers.add_parser(
        "register",
        help="refused: registration is human-only via evallab registry",
    )
    register.add_argument("proposal_id")

    sample = subparsers.add_parser(
        "sample",
        help="sample task specs coverage-first (model novel mode spends subscription quota)",
    )
    sample.add_argument("--count", type=int, default=20, help="number of specs to sample")
    sample.add_argument("--seed", type=int, default=42, help="random seed for axis product")
    sample.add_argument("--novel", type=int, default=0, help="number of novel specs to design")
    sample.add_argument("--model", default=None, help="explicit pinned model for --novel")
    sample.add_argument(
        "--transport",
        default=None,
        choices=MODEL_TRANSPORTS,
        help="explicit transport for --novel; model calls spend subscription quota",
    )
    sample.add_argument("--timeout", type=float, default=120.0)
    batch = subparsers.add_parser("batch", help="propose → battery → review; halt at the gate")
    batch.add_argument("--count", type=int, default=5)
    return parser


def _pipeline_from_args(args: argparse.Namespace) -> AuthoringPipeline:
    root = args.root.resolve() if args.root is not None else repository_root()
    derived = args.out.resolve() if args.out is not None else None
    adapter: AnalyzerCallable | None = None
    needs_model = args.command == "model-propose" or (
        args.command == "sample" and getattr(args, "novel", 0) > 0
    )
    if needs_model:
        model = getattr(args, "model", None)
        transport = getattr(args, "transport", None)
        if not model or not transport:
            raise AuthoringError(
                "model-backed authoring requires explicit --model and --transport; "
                "model calls spend subscription quota"
            )
        adapter = ModelAdapter(
            model=model,
            transport=transport,
            timeout_seconds=float(getattr(args, "timeout", 120.0)),
        )
    return AuthoringPipeline(root, derived_root=derived, adapter=adapter)

def _emit(payload: Any, *, as_json: bool, stream: Any = None) -> None:
    target = sys.stdout if stream is None else stream
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True), file=target)
        return
    if isinstance(payload, str):
        print(payload, file=target)
        return
    print(json.dumps(payload, indent=2, sort_keys=True), file=target)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        pipeline = _pipeline_from_args(args)
        if args.command == "sample":
            designer = ModelBackedDesigner(pipeline.adapter) if pipeline.adapter else None
            specs = pipeline.sample_specs(
                count=args.count,
                seed=args.seed,
                novel_designer=designer,
                novel_count=args.novel,
            )
            gap_count = sum(1 for s in specs if s.get("provenance") == "craft-gap")
            rand_count = sum(1 for s in specs if s.get("provenance") == "random-product")
            nov_count = sum(1 for s in specs if s.get("provenance") == "novel-spec")
            payload = {
                "count": len(specs),
                "craft_gap_count": gap_count,
                "random_product_count": rand_count,
                "novel_count": nov_count,
                "specs": specs,
            }
            if args.json:
                _emit(payload, as_json=True)
            else:
                pct = (gap_count / len(specs) * 100) if specs else 0.0
                print(f"Sampled {len(specs)} specs (Coverage-First):")
                print(f"  Craft-gap queries: {gap_count} ({pct:.1f}%)")
                print(f"  Random axis product: {rand_count}")
                if nov_count:
                    print(f"  Novel specs: {nov_count}")
                print("\nSpecs:")
                for s in specs:
                    print(
                        f"  [{s.get('provenance')}] {s['name']} -> "
                        f"({s['category']}, {s['scenario']}, {s['difficulty']})"
                    )
            return 0
        if args.command == "model-propose":
            proposal = pipeline.propose_model(args.topic, args.style)
            _emit(
                {
                    "proposal_id": proposal.proposal_id,
                    "seed_class": proposal.seed_class,
                    "outcome": proposal.outcome,
                    "path": _repo_relative(proposal.path, pipeline.repo_root),
                    "provenance": proposal.provenance,
                },
                as_json=args.json,
            )
            return 0
        if args.command == "propose":
            if args.via_harbor:
                custom_spec = (
                    json.loads(args.spec.read_text(encoding="utf-8"))
                    if args.spec and args.spec.is_file()
                    else None
                )
                res = pipeline.propose(
                    args.seed,
                    ref=args.ref,
                    via_harbor=True,
                    agent=args.agent,
                    model=args.model,
                    spec=custom_spec,
                    exemplar=args.exemplar,
                )
                assert isinstance(res, tuple)
                exp_spec, queue_path, decision = res
                _emit(
                    {
                        "spec_id": exp_spec.spec_id,
                        "name": exp_spec.name,
                        "purpose": exp_spec.purpose,
                        "task": exp_spec.task,
                        "agent": exp_spec.agent,
                        "queue_path": _repo_relative(queue_path, pipeline.repo_root),
                        "destination": queue_path.parent.name,
                        "admitted": decision.admitted,
                        "reason_code": decision.reason_code,
                    },
                    as_json=args.json,
                )
                return 0
            proposal = pipeline.propose(
                args.seed, ref=args.ref, analysis_code=getattr(args, "analysis_code", None)
            )
            assert isinstance(proposal, Proposal)
            _emit(
                {
                    "proposal_id": proposal.proposal_id,
                    "seed_class": proposal.seed_class,
                    "ref_task": proposal.ref_task,
                    "outcome": proposal.outcome,
                    "path": _repo_relative(proposal.path, pipeline.repo_root),
                },
                as_json=args.json,
            )
            return 0
        if args.command == "harvest":
            harvested = pipeline.harvest(args.job)
            _emit(
                {
                    "proposal_id": harvested.proposal_id,
                    "seed_class": harvested.seed_class,
                    "ref_task": harvested.ref_task,
                    "outcome": harvested.outcome,
                    "path": _repo_relative(harvested.path, pipeline.repo_root),
                    "job_id": harvested.job_id,
                },
                as_json=args.json,
            )
            return 0
        if args.command == "battery":
            report = pipeline.run_battery(args.proposal_id)
            _emit(report.as_dict(), as_json=args.json)
            return 0 if report.all_passed else 1
        if args.command == "review":
            report = pipeline.review(args.proposal_id)
            _emit(report.as_dict(), as_json=args.json)
            return 0
        if args.command == "register":
            pipeline.register(args.proposal_id)
            raise AuthoringError("register returned without refusing")
        items = pipeline.run_batch(args.count)
        payload = {
            "count": len(items),
            "outcome": "craft_reviewed",
            "halt": REGISTER_REFUSAL,
            "proposals": [
                {
                    "proposal_id": item.proposal_id,
                    "seed_class": item.seed_class,
                    "outcome": item.outcome,
                    "review_score": item.review.score,
                }
                for item in items
            ],
        }
        _emit(payload, as_json=args.json)
        print(REGISTER_REFUSAL, file=sys.stderr)
        return 0
    except RegisterRefusal as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except AuthoringError as exc:
        print(f"authoring: {exc}", file=sys.stderr)
        return 2
    except ModelAdapterError as exc:
        print(f"authoring: model adapter failed closed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
