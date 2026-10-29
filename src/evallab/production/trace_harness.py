"""Trace harness: every trace detector on one shared blinded input.

``evallab trace run`` loads each selected run once (the Run Atlas loaders and
admission gate), hands the same :class:`HarnessRun` to every selected detector
and appends one ``trace-findings/v1`` record per (run, detector) to an
append-only JSONL store. Records are cached by ``(run_id, detector,
detector_version, reader_model)``: a run already scored by that detector
version and reader is not recomputed unless ``--refresh`` is given.
``evallab trace card`` prints one compact evidence card per run from the store.

Detectors live in sibling ``trace_harness_*`` adapter modules that reuse the
existing implementations (tripwires, window cascade, Run Atlas facets, the
trace-tool readers); this module owns only the record contract, the store,
run selection, the run loop and the card. Labels are never read here; they
are joined at scoring time (``trace_harness_score``).
"""

from __future__ import annotations

import argparse
import importlib
import json
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from evallab.production.cloud_trace_db import TRACE_FINDING_KINDS, TRACE_FINDING_VERDICTS
from evallab.production.run_atlas import (
    CHEATBENCH_DATASET,
    DEFAULT_DATABASE,
    GOODFIRE_DATASET,
    STATE_ROOT,
    AtlasRun,
    BlindRun,
    append_jsonl,
    blind_run,
)
from evallab.production.trace_findings import (
    MAX_EVIDENCE_STEPS,
    MAX_SUMMARY_CHARS,
    MAX_SUMMARY_WORDS,
    _finding,
)

FINDINGS_VERSION = "trace-findings/v1"
VERDICTS = TRACE_FINDING_VERDICTS
KINDS = TRACE_FINDING_KINDS
HARNESS_ROOT = STATE_ROOT / "trace-harness"
DEFAULT_FINDINGS = HARNESS_ROOT / "findings-v1.jsonl"
FINDING_FIELDS = (
    "run_id", "detector", "detector_version", "reader_model", "verdict", "score", "kind",
    "evidence_steps", "summary", "input_tokens", "output_tokens", "cost_usd", "latency_ms",
    "produced_at",
)
SUMMARY_WORDS = MAX_SUMMARY_WORDS
SUMMARY_CHARS = MAX_SUMMARY_CHARS
CARD_MAX_TOKENS = 1500
# The repository's approximate-token convention (``trace_feature_flags``):
# four characters per token, not a tokenizer guarantee.
CHARS_PER_TOKEN = 4
NO_ACTOR_SUMMARY = "no actor transcript (actor_inputs_available=false); verdict unknown, not clear"

# Detector name -> "module:factory". Adapters import lazily so registering the
# CLI opens no database, model or adapter dependency.
DETECTORS: dict[str, str] = {
    "tripwire": "evallab.production.trace_harness_rules:TripwireDetector",
    "flags": "evallab.production.trace_harness_rules:FlagsDetector",
    "cascade": "evallab.production.trace_harness_cascade:CascadeV8Detector",
    "cascade_v7": "evallab.production.trace_harness_cascade:CascadeV7Detector",
    "cascade_v4": "evallab.production.trace_harness_cascade:CascadeV4Detector",
    "cascade_stream": "evallab.production.trace_harness_stream:CascadeStreamDetector",
    "facets": "evallab.production.trace_harness_facets:FacetsDetector",
    "scout": "evallab.production.trace_harness_tools:ScoutDetector",
    "metr": "evallab.production.trace_harness_tools:MetrDetector",
    "laminar": "evallab.production.trace_harness_tools:LaminarDetector",
    "docent": "evallab.production.trace_harness_tools:DocentDetector",
    "omp_form": "evallab.production.trace_harness_tools:OmpFormDetector",
    "cascade_own": "evallab.production.trace_harness_own:CascadeOwnDetector",
    "laminar_copy": "evallab.production.trace_harness_own:LaminarCopyDetector",
    "facet_windows": "evallab.production.trace_harness_experiments:FacetWindowsDetector",
    "facet_windows_only": "evallab.production.trace_harness_experiments:FacetWindowsOnlyDetector",
    "facet_windows_first": "evallab.production.trace_harness_experiments:FacetWindowsFirstDetector",
    "facet_windows_baseline": "evallab.production.trace_harness_experiments:FacetWindowsBaselineDetector",
    "facet_windows_random": "evallab.production.trace_harness_experiments:FacetWindowsRandomDetector",
    "facets_prefix25": "evallab.production.trace_harness_experiments:FacetsPrefix25Detector",
    "facets_prefix50": "evallab.production.trace_harness_experiments:FacetsPrefix50Detector",
    "facets_prefix100": "evallab.production.trace_harness_experiments:FacetsPrefix100Detector",
    "recheck_sol": "evallab.production.trace_harness_experiments:RecheckSolDetector",
    "whole_run_logistic": "evallab.production.trace_harness_experiments:WholeRunLogisticDetector",
    "detcache": "evallab.production.trace_harness_detcache:DetectorCacheDetector",
    "qa_trace_t0": "evallab.production.trace_harness_qa:QaTraceT0Detector",
    "run_health.loop": "evallab.production.trace_harness_health:HealthLoopDetector",
    "run_health.runaway": "evallab.production.trace_harness_health:HealthRunawayDetector",
    "run_health.latency": "evallab.production.trace_harness_health:HealthLatencyDetector",
    "run_health.infra": "evallab.production.trace_harness_health:HealthInfraDetector",
}

Key = tuple[str, str, str, str | None]


# --------------------------------------------------------------------------- #
# the shared input
# --------------------------------------------------------------------------- #


def actor_inputs_available(steps: Iterable[Mapping[str, Any]]) -> bool:
    """Some agent-authored step has message text or a tool call (the export rule)."""
    from evallab.production.cloud_trace_export import _actor_inputs_available

    return _actor_inputs_available(list(steps))


@dataclass(frozen=True)
class HarnessRun:
    """One run as every detector receives it.

    ``steps`` are the ATIF-shaped steps with original ``step_id`` values; every
    ``evidence_steps`` entry a detector emits is an original step id. ``blind``
    is the allowlisted, renumbered view model readers must use.
    """

    run_id: str
    source: str
    split: str
    outcome: str
    steps: tuple[Mapping[str, Any], ...]
    actor_inputs_available: bool
    # Health facts only: never included in atlas_run/blind() or model prompts.
    meta: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    _blind: list[BlindRun] = field(default_factory=list, repr=False, compare=False)

    @classmethod
    def from_atlas(cls, run: AtlasRun) -> HarnessRun:
        steps = tuple(run.doc.get("steps") or ())
        return cls(run.run_id, run.source, run.split, run.outcome, steps, actor_inputs_available(steps))

    @property
    def atlas_run(self) -> AtlasRun:
        return AtlasRun(self.run_id, self.source, self.split, self.outcome,
                        {"schema_version": "ATIF-v1.6", "steps": list(self.steps)})

    @property
    def step_ids(self) -> list[Any]:
        return [step.get("step_id") for step in self.steps]

    def blind(self) -> BlindRun:
        """Blinded view, computed once per run and shared by every detector."""
        if not self._blind:
            self._blind.append(blind_run(self.atlas_run))
        return self._blind[0]


# --------------------------------------------------------------------------- #
# trace-findings/v1
# --------------------------------------------------------------------------- #


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def trim_words(text: str, limit: int = SUMMARY_WORDS) -> str:
    words = " ".join(str(text or "").split()).split(" ")
    return " ".join(words[:limit])[:SUMMARY_CHARS] if words != [""] else ""


def finding(
    run_id: str, detector: str, *, detector_version: str, reader_model: str | None,
    verdict: str, kind: str, score: float | None = None, evidence_steps: Iterable[int] = (),
    summary: str = "", input_tokens: int = 0, output_tokens: int = 0, cost_usd: float = 0.0,
    latency_ms: int = 0, produced_at: str | None = None,
) -> dict[str, Any]:
    """Build one validated ``trace-findings/v1`` record.

    The summary is trimmed and the cited steps are capped (lowest ids first) to the
    cloud DB importer's limits, so every stored record is importable.
    """
    record = {
        "run_id": run_id, "detector": detector, "detector_version": detector_version,
        "reader_model": reader_model, "verdict": verdict,
        "score": None if score is None else float(score), "kind": kind,
        "evidence_steps": sorted({int(step) for step in evidence_steps})[:MAX_EVIDENCE_STEPS],
        "summary": trim_words(summary), "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens), "cost_usd": float(cost_usd),
        "latency_ms": int(latency_ms), "produced_at": produced_at or now_utc(),
    }
    errors = validate_finding(record)
    if errors:
        raise ValueError(f"invalid trace finding for {run_id}/{detector}: {errors}")
    return record


def validate_finding(record: Mapping[str, Any]) -> list[str]:
    """Reasons a record breaks ``trace-findings/v1``; empty when valid.

    The value rules are the cloud DB importer's own (``trace_findings._finding``), so
    the harness never stores a record that table would refuse. The harness adds an
    exact field set and a ``provider/id`` reader.
    """
    errors = [f"missing field {name}" for name in FINDING_FIELDS if name not in record]
    errors += [f"unexpected field {name}" for name in sorted(set(record) - set(FINDING_FIELDS))]
    if errors:
        return errors
    if record["reader_model"] is not None and (
        not isinstance(record["reader_model"], str) or "/" not in record["reader_model"]
    ):
        errors.append("reader_model must be provider/id or null")
    if any(isinstance(step, bool) for step in record["evidence_steps"] or ()):
        errors.append("evidence_steps entry must be an integer")
    try:
        _finding(record)
    except (TypeError, ValueError) as error:
        errors.append(str(error))
    return errors


def cache_key(record: Mapping[str, Any]) -> Key:
    return (record["run_id"], record["detector"], record["detector_version"], record["reader_model"])


def apply_actor_rule(record: dict[str, Any], run: HarnessRun) -> dict[str, Any]:
    """A run without an actor transcript is never ``clear``: it is ``unknown``."""
    if run.actor_inputs_available or record["verdict"] != "clear":
        return record
    return {**record, "verdict": "unknown", "score": None, "summary": trim_words(NO_ACTOR_SUMMARY)}


class FindingStore:
    """Interim append-only JSONL store; the latest line per cache key wins."""

    def __init__(self, path: Path = DEFAULT_FINDINGS) -> None:
        self.path = path

    def records(self) -> dict[Key, dict[str, Any]]:
        latest: dict[Key, dict[str, Any]] = {}
        if not self.path.is_file():
            return latest
        with self.path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                record = json.loads(line)
                errors = validate_finding(record)
                if errors:
                    raise ValueError(f"{self.path}:{number}: {errors}")
                latest[cache_key(record)] = record
        return latest

    def for_run(self, run_id: str) -> list[dict[str, Any]]:
        return sorted(
            (record for key, record in self.records().items() if key[0] == run_id),
            key=lambda record: (record["detector"], record["detector_version"], record["reader_model"] or ""),
        )

    def append(self, records: Sequence[Mapping[str, Any]]) -> None:
        for record in records:
            errors = validate_finding(record)
            if errors:
                raise ValueError(f"refusing invalid finding {record.get('run_id')}: {errors}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        append_jsonl(self.path, records)


def merge_findings(paths: Sequence[Path], store: FindingStore) -> dict[str, int]:
    """Validate all inputs before appending newer findings, preserving version/reader identities.

    Newest ``produced_at`` wins across stores; equal timestamps use input order.
    An older import can never replace a newer finding already in the target.
    """
    existing = store.records()
    merged = dict(existing)
    read = 0
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f"findings input not found: {path}")
        for key, record in FindingStore(path).records().items():
            read += 1
            previous = merged.get(key)
            if previous is None or datetime.fromisoformat(record["produced_at"]) >= datetime.fromisoformat(
                previous["produced_at"]
            ):
                merged[key] = record
    changes = [record for key, record in merged.items() if existing.get(key) != record]
    if changes:
        store.append(changes)
    return {"read": read, "written": len(changes), "findings": len(merged)}


# --------------------------------------------------------------------------- #
# detectors
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HarnessOptions:
    """Shared settings handed to every detector factory."""

    database: Path = DEFAULT_DATABASE
    repo_root: Path = Path(".")
    allow_model: bool = False
    reader: str | None = None


class Detector(Protocol):
    """One detector instance: fixed ``name``, ``version`` and ``reader_model``.

    ``detect`` yields at most one finding per given run, keyed by the run's
    ``run_id``; a run it has nothing for (no stored result, model calls not
    allowed) is simply omitted, never reported as ``clear``.
    """

    name: str
    version: str
    reader_model: str | None

    def detect(self, runs: Sequence[HarnessRun]) -> Iterable[dict[str, Any]]: ...


def parse_detector_spec(spec: str) -> tuple[str, str | None]:
    """``facets`` or ``facets:anthropic/claude-haiku-5-5`` (reader override)."""
    name, _, reader = spec.partition(":")
    if name not in DETECTORS:
        raise ValueError(f"unknown detector {name!r}; known: {', '.join(DETECTORS)}")
    return name, reader or None




def load_detectors(
    specs: Sequence[str], options: HarnessOptions, *, include_cached: bool = False,
) -> list[Detector]:
    names = list(specs)
    if names == ["all"]:
        names = [name for name in DETECTORS if name != "detcache"]
        names.extend(("docent:rh_reading_v1", "cascade_own:xai-oauth/grok-4.3"))
        if include_cached:
            from evallab.production.trace_harness_detcache import cached_detector_names

            names.extend(f"detcache:{name}" for name in cached_detector_names())
    detectors: list[Detector] = []
    for spec in names:
        name, reader = parse_detector_spec(spec)
        module_name, _, attribute = DETECTORS[name].partition(":")
        factory: Callable[..., Detector] = getattr(importlib.import_module(module_name), attribute)
        detectors.append(factory(options if reader is None else HarnessOptions(
            options.database, options.repo_root, options.allow_model, reader,
        )))
    return detectors


def run_detectors(
    runs: Sequence[HarnessRun], detectors: Sequence[Detector], store: FindingStore,
    *, refresh: bool = False, log: Callable[[str], None] = lambda message: None,
) -> dict[str, Any]:
    """Run each detector on the runs it has not scored yet; append and summarise."""
    cached = store.records()
    by_id = {run.run_id: run for run in runs}
    summary: dict[str, Any] = {"runs": len(runs), "detectors": {}}
    for detector in detectors:
        label = f"{detector.name}@{detector.version}" + (f" [{detector.reader_model}]" if detector.reader_model else "")
        todo = [
            run for run in runs
            if refresh or (run.run_id, detector.name, detector.version, detector.reader_model) not in cached
        ]
        started = time.monotonic()
        requested = {run.run_id for run in todo}
        written: list[dict[str, Any]] = []
        flushed = 0
        verdicts: dict[str, int] = {}
        for record in detector.detect(todo):
            if record["run_id"] not in requested:
                raise ValueError(f"{label} returned a finding for an unrequested run {record['run_id']}")
            if (record["detector"], record["detector_version"], record["reader_model"]) != (
                detector.name, detector.version, detector.reader_model,
            ):
                raise ValueError(f"{label} returned a finding under another identity: {cache_key(record)}")
            requested.discard(record["run_id"])  # at most one finding per run
            record = apply_actor_rule(dict(record), by_id[record["run_id"]])
            written.append(record)
            verdicts[record["verdict"]] = verdicts.get(record["verdict"], 0) + 1
            if len(written) - flushed >= 200:
                store.append(written[flushed:])
                flushed = len(written)
        store.append(written[flushed:])
        summary["detectors"][label] = {
            "cached": len(runs) - len(todo), "attempted": len(todo), "written": len(written),
            "no_result": len(todo) - len(written), "verdicts": dict(sorted(verdicts.items())),
            "seconds": round(time.monotonic() - started, 2),
        }
        log(f"{label}: {summary['detectors'][label]}")
    return summary


# --------------------------------------------------------------------------- #
# run selection
# --------------------------------------------------------------------------- #

CORPUS_LOADERS: dict[str, str] = {
    "hackbench": "evallab.production.trace_harness_corpora:hackbench_runs",
    "hackbench-all": "evallab.production.trace_harness_corpora:hackbench_all_runs",
    "own": "evallab.production.trace_harness_own:own_runs",
    "own-dev": "evallab.production.trace_harness_own:own_dev_runs",
}


def goodfire_cohort_ids() -> list[str]:
    """The frozen CM-2/CM-6 Goodfire sample (80 enacted, 70 clean, 50 considered-only)."""
    from evallab.production.run_atlas_score import DEFAULT_CM6_DIR

    sample = json.loads((DEFAULT_CM6_DIR.parent / "cm2-monitor-20261026/goodfire-sample.json").read_text())
    return sorted(row["trace_id"] for row in sample)


def resolve_runs(
    selector: str, *, database: Path = DEFAULT_DATABASE, repo_root: Path = Path("."),
    results_root: Path | None = None, limit: int | None = None,
) -> list[HarnessRun]:
    """Runs named by a selector, through the shared DEV admission gate.

    ``goodfire`` (the frozen CM-6 cohort), ``goodfire-dev`` (every admitted
    Goodfire DEV run), ``cheatbench`` (CheatBench DEV items), ``dataset:<id>``,
    ``file:<path>`` (one run id per line), a registered corpus loader
    (``hackbench``, ``hackbench-all``), ``staged:<set>`` (a staged cross-test
    set), or comma-separated run ids. Non-admitted ids are dropped by the
    loaders, never scored.
    """
    from evallab.production.corpus_queue import CorpusRegistry
    from evallab.production.run_atlas import cheatbench_runs, cloud_runs
    from evallab.results_home import results_root as default_results_root

    registry = CorpusRegistry(repo_root)
    ids: list[str] | None = None
    if selector in CORPUS_LOADERS:
        module_name, _, attribute = CORPUS_LOADERS[selector].partition(":")
        loader = getattr(importlib.import_module(module_name), attribute)
        kwargs = {"repo_root": repo_root} if selector in ("own", "own-dev") else {}
        return list(_limited(loader(limit=limit, **kwargs), limit))
    if selector.startswith("staged:"):
        from evallab.production.trace_harness_staged import staged_runs

        return list(staged_runs(selector.removeprefix("staged:"), limit, repo_root=repo_root))
    if selector.split(":", 1)[0] == "detectors-v1":
        from evallab.production.trace_harness_detcache import detectors_runs

        return list(detectors_runs(limit, selector=selector, repo_root=repo_root, registry=registry))
    if selector == "cheatbench":
        atlas: Iterator[AtlasRun] = cheatbench_runs(registry, results_root or default_results_root())
    elif selector == "goodfire":
        atlas = cloud_runs(database, registry=registry, trace_ids=goodfire_cohort_ids())
    elif selector == "goodfire-dev":
        atlas = cloud_runs(database, registry=registry, dataset_ids=[GOODFIRE_DATASET])
    elif selector.startswith("dataset:"):
        atlas = cloud_runs(database, registry=registry, dataset_ids=[selector.removeprefix("dataset:")])
    else:
        if selector.startswith("file:"):
            text = Path(selector.removeprefix("file:")).read_text(encoding="utf-8")
            ids = [line.strip() for line in text.splitlines() if line.strip()]
        else:
            ids = [part.strip() for part in selector.split(",") if part.strip()]
        cheat = [run_id for run_id in ids if run_id.startswith(CHEATBENCH_DATASET + ":")]
        cloud = [run_id for run_id in ids if run_id not in cheat]
        atlas = _chain(
            cloud_runs(database, registry=registry, trace_ids=cloud) if cloud else iter(()),
            cheatbench_runs(registry, results_root or default_results_root(), item_ids=cheat)
            if cheat else iter(()),
        )
    runs = [HarnessRun.from_atlas(run) for run in _limited(atlas, limit)]
    if ids is not None:
        order = {run_id: index for index, run_id in enumerate(ids)}
        runs.sort(key=lambda run: order.get(run.run_id, len(order)))
    return runs


def _chain(*iterators: Iterator[AtlasRun]) -> Iterator[AtlasRun]:
    for iterator in iterators:
        yield from iterator


def _limited[T](items: Iterable[T], limit: int | None) -> Iterator[T]:
    for index, item in enumerate(items):
        if limit is not None and index >= limit:
            return
        yield item


# --------------------------------------------------------------------------- #
# evidence card
# --------------------------------------------------------------------------- #


def card_flags(run: HarnessRun) -> tuple[list[str], list[str]]:
    """Fired feature flags as ``name@first_step xcount`` (original ids) and their names."""
    from evallab.production.trace_feature_flags import compute_trace_feature_flags

    flags = compute_trace_feature_flags(run.steps)
    ids = run.step_ids
    fired = [(name, flag) for name, flag in flags["features"].items() if flag["fired"]]
    fired.sort(key=lambda item: (item[1]["step_indices"][0], item[0]))
    rendered = [f"{name}@{ids[flag['step_indices'][0]]} x{flag['count']}" for name, flag in fired]
    return rendered, [name for name, _ in fired]


def _finding_line(record: Mapping[str, Any], summary_words: int) -> str:
    reader = record["reader_model"] or "none / unrecorded"
    score = "" if record["score"] is None else f" score={record['score']:g}"
    steps = record["evidence_steps"]
    cited = f" steps={','.join(map(str, steps[:8]))}{'…' if len(steps) > 8 else ''}" if steps else ""
    summary = trim_words(record["summary"], summary_words)
    return (f"- {record['detector']} {record['detector_version']} ({reader}): "
            f"{record['verdict'].upper()} {record['kind']}{score}{cited}"
            + (f" | {summary}" if summary else ""))


def render_card(
    run: HarnessRun | None, findings: Sequence[Mapping[str, Any]], *, run_id: str,
    max_tokens: int = CARD_MAX_TOKENS,
) -> str:
    """One compact, label-free evidence card; at most ``max_tokens`` (approx.)."""
    budget = max_tokens * CHARS_PER_TOKEN
    alarms = [record for record in findings if record["verdict"] == "alarm"]
    head = [f"# trace card {run_id}"]
    if run is not None:
        agent_steps = sum(1 for step in run.steps if step.get("source") == "agent")
        # Like qa-data's default replies, the card names no dataset, reward or label.
        head.append(
            f"steps={len(run.steps)} agent_steps={agent_steps}"
            f" actor_inputs_available={str(run.actor_inputs_available).lower()}"
        )
    verdict_counts = {verdict: sum(r["verdict"] == verdict for r in findings) for verdict in VERDICTS}
    head.append(
        f"detectors={len(findings)} " + " ".join(f"{v}={n}" for v, n in verdict_counts.items() if n)
    )
    cited: dict[int, int] = {}
    for record in alarms:
        for step in record["evidence_steps"]:
            cited[step] = cited.get(step, 0) + 1
    if cited:
        top = sorted(cited.items(), key=lambda item: (-item[1], item[0]))[:6]
        head.append("alarm steps (detectors citing): " + ", ".join(f"{s}({n})" for s, n in top))
    tail: list[str] = []
    if run is not None:
        flags, fired = card_flags(run)
        tail.append("flags: " + (", ".join(flags) if flags else "none"))
        if fired:
            from evallab.hackbook.playbook_cards import match_playbook_cards

            for card in match_playbook_cards(fired, kind="trace", max_cards=2):
                tail.append(f"playbook {card['card_id']}: {trim_words(card['mechanism_summary'], 25)}")
    expand = (cited and min(cited, key=lambda step: (-cited[step], step))) or None
    tail.append(
        f"expand: evallab qa-data window {run_id}" + (f" --around {expand} --radius 3" if expand is not None else "")
    )
    ordered = sorted(findings, key=lambda r: (VERDICTS.index(r["verdict"]), r["detector"]))
    for words in (SUMMARY_WORDS, 20, 8, 0):
        lines = [*head, "verdicts:", *(_finding_line(r, words) for r in ordered), *tail]
        text = "\n".join(lines)
        if len(text) <= budget:
            return text
    return text[: budget - 1] + "…"


def card(
    run_id: str, *, store: FindingStore, database: Path = DEFAULT_DATABASE, repo_root: Path = Path("."),
    results_root: Path | None = None, max_tokens: int = CARD_MAX_TOKENS,
) -> str:
    """Card for one run: its stored findings plus flags and playbook leads from its steps."""
    findings = store.for_run(run_id)
    try:
        runs = resolve_runs(run_id, database=database, repo_root=repo_root, results_root=results_root)
    except (FileNotFoundError, ValueError):
        runs = []
    if not runs and not findings:
        raise ValueError(f"no admitted run and no findings for {run_id}")
    return render_card(runs[0] if runs else None, findings, run_id=run_id, max_tokens=max_tokens)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def register_subcommands(actions: Any) -> None:
    """Register trace commands without opening databases or reader transports."""
    run = actions.add_parser(
        "run", help="Run trace detectors on one shared blinded input; cache trace-findings/v1",
        description=__doc__.split("\n\n")[1] if __doc__ else None,
    )
    run.add_argument("--runs", required=True,
                     help="goodfire | goodfire-dev | cheatbench | hackbench | hackbench-all | staged:<set>"
                          " | dataset:<id> | file:<path> | id[,id…]")
    run.add_argument("--detectors", default="all",
                     help=f"Comma list or 'all' ({', '.join(DETECTORS)}); name:provider/model overrides the reader")
    run.add_argument("--limit", type=int, help="At most this many runs")
    run.add_argument("--refresh", action="store_true", help="Recompute even when cached")
    run.add_argument("--allow-model", action="store_true",
                     help="Let model-backed detectors call their subscription reader for uncached runs")
    _common(run)
    run.set_defaults(func=_run_command)
    show = actions.add_parser("card", help="Print one compact evidence card per run (<=1.5k tokens)")
    show.add_argument("run_ids", nargs="+")
    show.add_argument("--max-tokens", type=int, default=CARD_MAX_TOKENS)
    _common(show)
    show.set_defaults(func=_card_command)
    merge = actions.add_parser("merge", help="Merge findings stores, keeping the newest result per identity")
    merge.add_argument("inputs", nargs="+", type=Path)
    merge.add_argument("--findings", type=Path, default=DEFAULT_FINDINGS, help="Destination findings store")
    merge.set_defaults(func=_merge_command)
    cover = actions.add_parser(
        "coverage",
        help="Which detector identities have findings on which corpora (label-free integration check)",
    )
    cover.add_argument("--corpus", dest="corpora", action="append", required=True,
                       help="goodfire | goodfire-dev | cheatbench | hackbench | staged:<set> | dataset:<id>; repeatable")
    _common(cover)
    cover.set_defaults(func=_coverage_command)
    from evallab.production.trace_harness_score import add_score_parser

    add_score_parser(actions)


def cohort_ids(corpus: str, *, database: Path = DEFAULT_DATABASE, repo_root: Path = Path(".")) -> frozenset[str]:
    """Run ids of a corpus: its label loader's cohort, or its run loader's runs when unlabelled. No label is returned."""
    if corpus.startswith("dataset:"):
        from evallab.production.corpus_queue import CorpusRegistry
        from evallab.production.run_atlas import (
            admitted_task_ids,
            cloud_trace_ids,
            connect_readonly,
        )

        connection = connect_readonly(database)
        try:
            admitted = admitted_task_ids(connection, CorpusRegistry(repo_root))
            return frozenset(cloud_trace_ids(
                connection, admitted=admitted, dataset_ids=[corpus.removeprefix("dataset:")],
            ))
        finally:
            connection.close()
    from evallab.production.trace_harness_corpora import CORPORA, load_corpus_labels

    if corpus in CORPUS_LOADERS and corpus not in CORPORA:
        # Unlabelled corpora (own runs): the cohort is what the run loader yields.
        return frozenset(run.run_id for run in resolve_runs(corpus, database=database, repo_root=repo_root))
    return load_corpus_labels(corpus, database=database, repo_root=repo_root).run_ids


def coverage(records: Mapping[Key, Mapping[str, Any]], cohorts: Mapping[str, frozenset[str]]) -> list[dict[str, Any]]:
    """One row per (detector identity, corpus): findings on the cohort and their verdicts.

    Registered detectors with no finding on any listed corpus get one row with zero
    findings, so an integration that never ran is visible rather than absent.
    """
    rows: list[dict[str, Any]] = []
    identities = sorted({key[1:] for key in records}, key=lambda key: (key[0], key[1], key[2] or ""))
    seen_names: set[str] = set()
    for detector, version, reader in identities:
        for corpus, ids in cohorts.items():
            found = [
                record for key, record in records.items()
                if key[1:] == (detector, version, reader) and key[0] in ids
            ]
            if not found:
                continue
            seen_names.add(detector)
            verdicts = {verdict: sum(r["verdict"] == verdict for r in found) for verdict in VERDICTS}
            rows.append({
                "detector": detector, "version": version, "reader": reader, "corpus": corpus,
                "cohort": len(ids), "findings": len(found), "verdicts": verdicts,
                "latest": max(r["produced_at"] for r in found),
            })
    for name in sorted(DETECTORS):
        if not any(seen == name or seen.startswith(name + ".") for seen in seen_names):
            rows.append({"detector": name, "version": None, "reader": None, "corpus": None,
                         "cohort": 0, "findings": 0, "verdicts": dict.fromkeys(VERDICTS, 0), "latest": None})
    return rows


def render_coverage(rows: Sequence[Mapping[str, Any]]) -> str:
    lines = ["| Detector | Version | Reader | Corpus | Findings / cohort | alarm / clear / abstain / unknown |",
             "|---|---|---|---|---|---|"]
    for row in rows:
        verdicts = " / ".join(str(row["verdicts"][v]) for v in VERDICTS)
        lines.append(
            f"| {row['detector']} | {row['version'] or '—'} | "
            f"{row['reader'] or ('deterministic' if row['version'] else '—')} | "
            f"{row['corpus'] or 'no findings on these corpora'} | {row['findings']} / {row['cohort']} | {verdicts} |"
        )
    return "\n".join(lines)


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--findings", type=Path, default=DEFAULT_FINDINGS)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--results-root", type=Path, help="Published results home (CheatBench)")


def _run_command(args: argparse.Namespace, root: Path, *, harbor: Any = None) -> int:
    del harbor
    import sys

    options = HarnessOptions(database=args.database, repo_root=root, allow_model=args.allow_model)
    detectors = load_detectors(
        [part.strip() for part in args.detectors.split(",") if part.strip()], options,
        include_cached=args.runs.split(":", 1)[0] == "detectors-v1",
    )
    runs = resolve_runs(args.runs, database=args.database, repo_root=root,
                        results_root=args.results_root, limit=args.limit)
    summary = run_detectors(runs, detectors, FindingStore(args.findings), refresh=args.refresh,
                            log=lambda message: print(message, file=sys.stderr, flush=True))
    summary["findings"] = str(args.findings)
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 0


def _card_command(args: argparse.Namespace, root: Path, *, harbor: Any = None) -> int:
    del harbor
    store = FindingStore(args.findings)
    cards = [card(run_id, store=store, database=args.database, repo_root=root,
                  results_root=args.results_root, max_tokens=args.max_tokens)
             for run_id in args.run_ids]
    print("\n\n".join(cards))
    return 0


def _coverage_command(args: argparse.Namespace, root: Path, *, harbor: Any = None) -> int:
    del harbor
    cohorts = {corpus: cohort_ids(corpus, database=args.database, repo_root=root) for corpus in args.corpora}
    print(render_coverage(coverage(FindingStore(args.findings).records(), cohorts)))
    return 0


def _merge_command(args: argparse.Namespace, root: Path, *, harbor: Any = None) -> int:
    del root, harbor
    print(json.dumps(merge_findings(args.inputs, FindingStore(args.findings)), sort_keys=True))
    return 0
