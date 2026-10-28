"""Read-only Run Atlas view model: validated run facets and clusters on admitted DEV runs.

Producers write two retained files under ``<data_root>/run-atlas/``:

* ``facets-v1.jsonl`` -- one ``run-facets/v1`` record per line, append-only; the latest
  line for a ``run_id`` wins. ``run_id`` is ``agent_traces.trace_id`` (the database's
  ``run_id`` column is shared by many traces and is never used to look a run up).
* ``clusters-v1.jsonl`` -- producer ``run-clusters/v1`` rows; the most recent
  complete build supplies the tree, with label rates filled only after scoring.

This module validates both strictly and joins them to the *admitted DEV* trace identities
the caller supplies. A facet whose run is not admitted, or whose evidence step is not a
step of that run, is never shown. Nothing here opens a database, a model or the network,
and nothing is recomputed: counts are the producer's, derived rates are the browser's.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import stat
from collections import Counter
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

FACETS_FILE = "facets-v1.jsonl"
CLUSTERS_FILE = "clusters-v1.jsonl"
MAX_FACET_BYTES = 128 * 1024 * 1024
MAX_CLUSTER_BYTES = 64 * 1024 * 1024
MAX_REJECTION_NOTES = 20
MAX_PAGE = 200
MAX_FLAG_STEPS = 20_000

_Count = Annotated[int, Field(ge=0)]
_Label = Annotated[str, Field(min_length=1, pattern=r"\S")]
_Text = Annotated[str, Field(min_length=1, max_length=600, pattern=r"\S")]
_ModelId = Annotated[str, Field(pattern=r"^[^\s/]+/[^\s]+$")]


class _Record(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


class RunFacets(_Record):
    """One blinded whole-run form; ``run_id``, ``source``, ``split`` and ``outcome`` are the database's."""

    run_id: _Label
    source: _Label
    split: Literal["DEV"]
    facet_version: Literal["run-facets/v1"]
    facet_model: _ModelId
    input_tokens: _Count
    output_tokens: _Count
    goal: _Text
    approach: _Text
    outcome: Literal["pass", "fail", "unknown"]
    tests: Literal["untouched", "read", "modified", "deleted", "replaced"]
    grader_or_harness_touched: bool
    answer_material_accessed: bool
    success_claim_without_evidence: bool
    eval_awareness: Literal["none", "mentions_tests", "reasons_about_grader", "plans_to_game"]
    shortcut: _Text
    suspicion: Annotated[int, Field(ge=0, le=3)]
    evidence_steps: Annotated[list[Annotated[int, Field(ge=0)]], Field(max_length=5)]

    @field_validator("evidence_steps")
    @classmethod
    def validate_distinct_steps(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("evidence steps must be distinct")
        return value


class ClusterLabels(_Record):
    """The producer's binary-label tally, not a model prediction or accuracy."""

    labelled: _Count
    positives: _Count
    rate: Annotated[float, Field(ge=0, le=1)] | None

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.positives > self.labelled:
            raise ValueError("label hits must not exceed labelled runs")
        if not self.labelled:
            if self.rate is not None:
                raise ValueError("an unlabelled cluster has no rate")
        elif self.rate is None or not math.isclose(
            self.rate, self.positives / self.labelled, rel_tol=1e-9, abs_tol=1e-12
        ):
            raise ValueError("label rate disagrees with its recorded counts")
        return self


class RunCluster(_Record):
    # Unused embedder/naming diagnostics and sample texts stay producer-owned.
    model_config = ConfigDict(strict=True, extra="ignore", allow_inf_nan=False)

    cluster_version: Literal["run-clusters/v1"]
    cluster_id: _Label
    name: _Text
    level: Annotated[int, Field(ge=1, le=3)]
    parent: _Label | None
    run_ids: list[_Label]
    centroid_text: str
    size: _Count
    label_rates: ClusterLabels | None
    build_id: _Label
    facet_count: _Count
    created_at: str

    @field_validator("created_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        if datetime.fromisoformat(value).tzinfo is None or "T" not in value:
            raise ValueError("timestamps must be ISO-8601 with a timezone")
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if (self.level == 1) != (self.parent is None):
            raise ValueError("only level-1 clusters have no parent")
        if len(set(self.run_ids)) != len(self.run_ids) or self.size != len(self.run_ids):
            raise ValueError("size must equal the number of distinct run ids")
        if self.label_rates is not None and self.label_rates.labelled > self.size:
            raise ValueError("a label rate cannot cover more runs than the cluster holds")
        return self


class RunClusters(_Record):
    """One complete producer build selected from the append-only cluster stream."""

    clusters: list[RunCluster]

    @model_validator(mode="after")
    def validate_tree(self) -> Self:
        if not self.clusters:
            return self
        by_id = {cluster.cluster_id: cluster for cluster in self.clusters}
        if len(by_id) != len(self.clusters):
            raise ValueError("cluster ids must be unique within a build")
        first = self.clusters[0]
        members = {key: set(cluster.run_ids) for key, cluster in by_id.items()}
        levels: dict[int, set[str]] = {level: set() for level in (1, 2, 3)}
        for cluster in self.clusters:
            if (cluster.build_id, cluster.facet_count) != (first.build_id, first.facet_count):
                raise ValueError("cluster build metadata disagrees")
            own = members[cluster.cluster_id]
            if own & levels[cluster.level]:
                raise ValueError("a run belongs to only one cluster at each level")
            levels[cluster.level].update(own)
            if cluster.parent is not None:
                parent = by_id.get(cluster.parent)
                if parent is None or parent.level != cluster.level - 1:
                    raise ValueError("a parent must exist one level above its child")
                if not own <= members[parent.cluster_id]:
                    raise ValueError("a child cluster's runs must all belong to its parent")
        if len(levels[1]) != first.facet_count or not levels[1] == levels[2] == levels[3]:
            raise ValueError(
                "incomplete cluster build: each of three levels must cover facet_count"
            )
        return self


def _cluster_snapshot(raw: bytes) -> dict[str, Any]:
    """Match the producer: final contiguous build in file order, not timestamp order."""
    latest: list[dict[str, Any]] = []
    build_id: str | None = None
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        row = strict_loads(line)
        if not isinstance(row, dict) or row.get("cluster_version") != "run-clusters/v1":
            raise ValueError("cluster stream contains an unknown record")
        if row.get("build_id") != build_id:
            latest = []
            build_id = row.get("build_id")
        latest.append(row)
    validated = RunClusters.model_validate({"clusters": latest})
    nodes = []
    for cluster in validated.clusters:
        node = cluster.model_dump()
        labels = cluster.label_rates
        node["label_rates"] = (
            []
            if labels is None
            else [
                {
                    "label": "positive source label",
                    "hits_n": labels.positives,
                    "of_n": labels.labelled,
                }
            ]
        )
        nodes.append(node)
    # Clustering time is not scoring time. The producer never records scored_at.
    return {"clusters": nodes, "scored_at": None, "build_id": build_id}


@dataclass(frozen=True)
class AtlasStore:
    """Validated retained atlas files and what was refused while reading them."""

    facets: dict[str, dict[str, Any]] = field(default_factory=dict)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    rejected_n: int = 0
    superseded_n: int = 0
    clusters: dict[str, Any] | None = None
    cluster_error: str | None = None
    sha256: dict[str, str] = field(default_factory=dict)


def read_regular(directory: Path, name: str, limit: int) -> bytes | None:
    """Read a bounded regular file without following the directory or file link."""
    try:
        parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError(f"{name} is not a regular file")
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            raw = stream.read(limit + 1)
    finally:
        os.close(parent)
    if len(raw) > limit:
        raise ValueError(f"{name} exceeds {limit} bytes")
    return raw


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON number {value}")


def strict_loads(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_json_object, parse_constant=_reject_constant)


def json_reason(error: Exception) -> str:
    """One bounded, readable line: the first failing field and why."""
    if isinstance(error, ValidationError):
        first = error.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "document"
        return f"{where}: {first['msg']}"[:300]
    return (str(error).splitlines() or [type(error).__name__])[0][:300]


def load_atlas_store(directory: Path) -> AtlasStore:
    """Validate facet JSONL and select the latest complete cluster JSONL build."""
    digests: dict[str, str] = {}
    facets: dict[str, dict[str, Any]] = {}
    rejected: list[dict[str, Any]] = []
    rejected_n = superseded_n = 0
    raw = read_regular(directory, FACETS_FILE, MAX_FACET_BYTES)
    if raw is not None:
        digests[FACETS_FILE] = hashlib.sha256(raw).hexdigest()
        for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = RunFacets.model_validate(strict_loads(line)).model_dump()
            except (ValueError, ValidationError) as error:
                rejected_n += 1
                if len(rejected) < MAX_REJECTION_NOTES:
                    reason = json_reason(error)
                    rejected.append({"line": number, "reason": reason})
                continue
            superseded_n += record["run_id"] in facets
            facets[record["run_id"]] = record
    clusters: dict[str, Any] | None = None
    cluster_error: str | None = None
    raw = read_regular(directory, CLUSTERS_FILE, MAX_CLUSTER_BYTES)
    if raw is not None:
        digests[CLUSTERS_FILE] = hashlib.sha256(raw).hexdigest()
        try:
            clusters = _cluster_snapshot(raw)
        except (ValueError, ValidationError) as error:
            cluster_error = json_reason(error)
    return AtlasStore(facets, rejected, rejected_n, superseded_n, clusters, cluster_error, digests)


@dataclass(frozen=True)
class AtlasFilters:
    q: str = ""
    source: str | None = None
    outcome: str | None = None
    tests: str | None = None
    eval_awareness: str | None = None
    facet_model: str | None = None
    min_suspicion: int = 0
    grader_or_harness_touched: bool | None = None
    answer_material_accessed: bool | None = None
    success_claim_without_evidence: bool | None = None
    cluster_id: str | None = None
    alarmed_by: str | None = None
    sort: Literal["suspicion", "alarms"] = "suspicion"


_CATEGORIES = ("source", "outcome", "tests", "eval_awareness", "suspicion", "facet_model")
_FLAGS = (
    "grader_or_harness_touched",
    "answer_material_accessed",
    "success_claim_without_evidence",
)


def visible_facets(
    store: AtlasStore, traces: Mapping[str, Mapping[str, Any]]
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Facets on admitted DEV runs whose evidence steps exist, plus what was withheld."""
    shown: dict[str, dict[str, Any]] = {}
    unadmitted = outside = 0
    for run_id, facet in store.facets.items():
        identity = traces.get(run_id)
        if identity is None:
            unadmitted += 1
        elif not set(facet["evidence_steps"]).issubset(identity["step_indices"]):
            outside += 1
        else:
            shown[run_id] = {
                **facet,
                "source": identity["dataset_id"],
                "outcome": identity["outcome"],
                "split": "DEV",
            }
    return shown, {"unadmitted_n": unadmitted, "evidence_outside_run_n": outside}


def _matches(facet: Mapping[str, Any], identity: Mapping[str, Any], filters: AtlasFilters) -> bool:
    if any(
        wanted is not None and facet[name] != wanted
        for name, wanted in (
            ("source", filters.source),
            ("outcome", filters.outcome),
            ("tests", filters.tests),
            ("facet_model", filters.facet_model),
            ("eval_awareness", filters.eval_awareness),
            *((flag, getattr(filters, flag)) for flag in _FLAGS),
        )
    ):
        return False
    if facet["suspicion"] < filters.min_suspicion:
        return False
    needle = filters.q.strip().casefold()
    if needle:
        haystack = " ".join(
            (
                facet["goal"],
                facet["approach"],
                facet["shortcut"],
                facet["run_id"],
                identity["task_id"],
            )
        ).casefold()
        return needle in haystack
    return True


def atlas_listing(
    store: AtlasStore,
    traces: Mapping[str, Mapping[str, Any]],
    filters: AtlasFilters,
    *,
    limit: int = 50,
    offset: int = 0,
    blinding: Mapping[str, str] | None = None,
    alarms: Mapping[str, Collection[str]] | None = None,
    read: Mapping[str, int] | None = None,
    alarms_available: bool = False,
) -> dict[str, Any]:
    """Cluster tree, filter counts and one page of matching runs for the admitted DEV set."""
    if not 1 <= limit <= MAX_PAGE or offset < 0:
        raise ValueError(f"limit must be 1..{MAX_PAGE} and offset non-negative")
    shown, withheld = visible_facets(store, traces)
    alarmed: Mapping[str, Collection[str]] = alarms or {}
    members: set[str] | None = None
    tree = list((store.clusters or {}).get("clusters", []))
    if filters.cluster_id is not None:
        chosen = next((item for item in tree if item["cluster_id"] == filters.cluster_id), None)
        if chosen is None:
            raise ValueError("unknown cluster")
        members = set(chosen["run_ids"])
    matching = [
        run_id
        for run_id, facet in shown.items()
        if (members is None or run_id in members)
        and (filters.alarmed_by is None or filters.alarmed_by in alarmed.get(run_id, ()))
        and _matches(facet, traces[run_id], filters)
    ]
    matching_set = set(matching)
    if filters.sort == "alarms":
        matching.sort(key=lambda run_id: (-len(alarmed.get(run_id, ())), -shown[run_id]["suspicion"], run_id))
    else:
        matching.sort(key=lambda run_id: (-shown[run_id]["suspicion"], run_id))
    page = matching[offset : offset + limit]
    by_run: dict[str, list[str]] = {run_id: [] for run_id in page}
    for item in tree:
        for run_id in item["run_ids"]:
            if run_id in by_run:
                by_run[run_id].append(item["cluster_id"])
    options = {
        name: dict(sorted(Counter(str(facet[name]) for facet in shown.values()).items()))
        for name in _CATEGORIES
    } | {name: sum(1 for facet in shown.values() if facet[name]) for name in _FLAGS}
    options["alarmed_by"] = dict(
        sorted(Counter(name for run_id in shown for name in alarmed.get(run_id, ())).items())
    )
    return {
        "store": {
            "facets_n": len(store.facets),
            "visible_n": len(shown),
            "rejected_n": store.rejected_n,
            "rejected": store.rejected,
            "superseded_n": store.superseded_n,
            "sha256": store.sha256,
            "cluster_error": store.cluster_error,
            **withheld,
        },
        "scored_at": (store.clusters or {}).get("scored_at"),
        "options": options,
        "alarms_available": alarms_available,
        "clusters": [
            {
                "cluster_id": item["cluster_id"],
                "name": item["name"],
                "level": item["level"],
                "parent": item["parent"],
                "centroid_text": item["centroid_text"],
                "size": item["size"],
                "admitted_n": sum(1 for run_id in item["run_ids"] if run_id in shown),
                "matching_n": sum(1 for run_id in item["run_ids"] if run_id in matching_set),
                "label_rates": item["label_rates"],
            }
            for item in tree
        ],
        "total": len(matching),
        "runs": [
            {
                **shown[run_id],
                "task_id": traces[run_id]["task_id"],
                "dataset_id": traces[run_id]["dataset_id"],
                "trust_tier": traces[run_id]["trust_tier"],
                "step_count": traces[run_id]["step_count"],
                "cluster_ids": by_run[run_id],
                "alarms": sorted(alarmed.get(run_id, ())),
                "detector_results_n": (read or {}).get(run_id, 0),
                "blinding": None if blinding is None else blinding.get(run_id),
            }
            for run_id in page
        ],
        "notice": (
            "Facets are model-read summaries of blinded runs and are leads, not verdicts. "
            "Only admitted DEV runs appear; label rates are recorded at scoring time."
        ),
    }


def cluster_paths(store: AtlasStore, run_id: str) -> list[list[dict[str, str]]]:
    """Root-to-leaf chains of the clusters holding one run (one chain per leaf)."""
    tree = {item["cluster_id"]: item for item in (store.clusters or {}).get("clusters", [])}
    holding = {cid for cid, item in tree.items() if run_id in item["run_ids"]}
    parents = {tree[cid]["parent"] for cid in holding}
    chains = []
    for cid in sorted(holding - parents):
        chain, cursor = [], cid
        while cursor is not None:
            item = tree[cursor]
            chain.append({"cluster_id": cursor, "name": item["name"]})
            cursor = item["parent"]
        chains.append(chain[::-1])
    return chains
