"""PAIChecker source preparation for the OQ10 task-quality cross-test.

Four plain-dict entry points, no framework:

* ``metadata_population`` -- labelled ``(task_id, positive, stratum, subcorpus)``
  rows built from the adapter's label loader and ONLY the ``instance_id`` and
  ``repo`` task columns. Unlabelled Gym grader rows are omitted, never negatives.
* ``select_cases`` -- seeded, per-subcorpus, class-by-repo round-robin sampler.
  It guarantees minimum repo coverage; it is NOT population-weighted sampling.
* ``render_auditor_input`` -- exact eight-field adapter ``auditor_input`` to a
  relative-path -> text mapping (``instruction.md`` plus ``tests/*``). No
  truncation, no rewriting, no added descriptive text.
* ``iter_selected`` -- hydrate selected tasks through the adapter's existing
  ``specs`` projection and yield one record per selected id.

Hydration caveat: the adapter exposes no per-id lookup. ``adapter.specs(bundle)``
reads all eight auditor columns of every labelled row of BOTH sub-corpora (in
128-row parquet batches). ``iter_selected`` drains that generator fully so the
adapter's own label/grader join check runs, keeps only the selected specs, and
drops every other spec immediately without rendering, hashing or retaining it.

``patch`` is the benchmark's reference PR; it is auditor-visible by adapter
contract and stays intact. The PAIChecker misalignment gold, human labels,
families, ``instance_id`` and ``sub_corpus`` never enter ``auditor_files``.
``gold`` is read only to cross-check the selected ``positive`` flag.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

DEFAULT_SEED = "oq10-cross-v1-20261028"
SUBCORPORA = ("swe_bench_multilingual", "swe_gym")
AUDITOR_INPUT_FIELDS = (
    "repo",
    "base_commit",
    "problem_statement",
    "hints_text",
    "test_patch",
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
    "patch",
)

_STRING_FIELDS = ("repo", "base_commit", "problem_statement", "test_patch", "patch")
_TEST_LIST_FIELDS = ("FAIL_TO_PASS", "PASS_TO_PASS")
_POPULATION_KEYS = frozenset({"task_id", "positive", "stratum", "subcorpus"})
_ENVELOPE_KEYS = frozenset({"auditor_input", "gold", "identity"})
_IDENTITY_KEYS = frozenset({"instance_id", "sub_corpus", "repo"})
_METADATA_COLUMNS = ("instance_id", "repo")


class SourceShapeError(ValueError):
    """An auditor_input differs from the strict native PAIChecker shape.

    Messages name fields and types only, never source values.
    """


def task_id_for(subcorpus: str, instance_id: str) -> str:
    """Pooled-unique task id; the native instance id stays recoverable and scorer-side."""
    return f"{subcorpus}:{instance_id}"


def canonical_json(value: Any) -> str:
    return (
        json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
        + "\n"
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- metadata


def metadata_population(adapter: Any, bundle: Path) -> list[dict[str, Any]]:
    """Labelled population rows for both sub-corpora; metadata columns only.

    Execute only after the code-level freeze. Task parquet columns other than
    ``instance_id`` and ``repo`` are never requested, so no source text is
    deserialized. Duplicate/empty ids, unknown label schema (raised by the
    adapter's loader), a label id absent from the grader, or a label-count
    mismatch against the adapter's pinned ``expected_rows`` fail closed.
    """
    sources = adapter.SUB_CORPORA
    if set(sources) != set(SUBCORPORA):
        raise ValueError("adapter sub-corpora differ from the expected pair")
    rows: list[dict[str, Any]] = []
    for subcorpus in SUBCORPORA:
        source = sources[subcorpus]
        labels = adapter.load_labels(bundle / source["labels"])
        if len(labels) != source["expected_rows"]:
            raise ValueError(f"{subcorpus}: label rows differ from the pinned expected_rows")
        seen: set[str] = set()
        matched: set[str] = set()
        for row in adapter.task_rows(bundle / source["tasks"], _METADATA_COLUMNS):
            if set(row) != set(_METADATA_COLUMNS):
                raise ValueError(f"{subcorpus}: task row columns differ from the metadata request")
            instance_id = row["instance_id"]
            if not isinstance(instance_id, str) or not instance_id:
                raise ValueError(f"{subcorpus}: empty or non-string grader identity")
            if instance_id in seen:
                raise ValueError(f"{subcorpus}: duplicate grader identity")
            seen.add(instance_id)
            if instance_id not in labels:
                continue  # unlabelled: omitted, never a negative
            families = labels[instance_id]
            if not isinstance(families, tuple) or any(not isinstance(f, str) for f in families):
                raise ValueError(f"{subcorpus}: label families are not a tuple of strings")
            repo = row["repo"]
            if not isinstance(repo, str) or not repo:
                raise ValueError(f"{subcorpus}: labelled task has an empty or non-string repo")
            matched.add(instance_id)
            rows.append(
                {
                    "task_id": task_id_for(subcorpus, instance_id),
                    "positive": bool(families),
                    "stratum": repo,
                    "subcorpus": subcorpus,
                }
            )
        if matched != set(labels):
            raise ValueError(f"{subcorpus}: label/grader join incomplete")
    rows.sort(key=lambda r: (r["subcorpus"], r["task_id"]))
    return rows


# --------------------------------------------------------------------------- selection


def _validated_rows(population: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(population, (str, bytes)) or not isinstance(population, Sequence):
        raise ValueError("population must be a sequence of mappings")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in population:
        if not isinstance(item, Mapping) or set(item) != _POPULATION_KEYS:
            raise ValueError("population row keys differ from task_id/positive/stratum/subcorpus")
        task_id, stratum, subcorpus = item["task_id"], item["stratum"], item["subcorpus"]
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("population task_id must be a non-empty string")
        if not isinstance(stratum, str) or not stratum:
            raise ValueError("population stratum must be a non-empty string")
        if subcorpus not in SUBCORPORA:
            raise ValueError("population subcorpus is not a known PAIChecker sub-corpus")
        if not isinstance(item["positive"], bool):
            raise ValueError("population positive must be a bool; unlabelled rows are omitted")
        if task_id in seen:
            raise ValueError("duplicate population task_id")
        seen.add(task_id)
        rows.append(dict(item))
    return rows


def _round_robin(rows: list[dict[str, Any]], count: int, seed: str, what: str) -> list[dict[str, Any]]:
    if len(rows) < count:
        raise ValueError(f"{what}: {len(rows)} available, {count} requested")

    def item_key(row: dict[str, Any]) -> tuple[str, str]:
        return (_sha256_text(row["task_id"] + seed), row["task_id"])

    by_stratum: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_stratum[row["stratum"]].append(row)
    strata = sorted(by_stratum, key=lambda s: (_sha256_text(s + seed), s))
    queues = [sorted(by_stratum[s], key=item_key) for s in strata]
    chosen: list[dict[str, Any]] = []
    depth = 0
    while len(chosen) < count:
        for queue in queues:
            if depth < len(queue):
                chosen.append(queue[depth])
                if len(chosen) == count:
                    break
        depth += 1
    return chosen


def select_cases(
    population: Sequence[Mapping[str, Any]],
    *,
    seed: str = DEFAULT_SEED,
    positives: int = 30,
    negatives: int = 30,
    subcorpus: str | None = None,
) -> list[dict[str, Any]]:
    """Independent per-sub-corpus selection, class-by-repo round robin.

    Per sub-corpus and class: non-empty repo strata are ordered by
    ``SHA256(stratum + seed)``, items inside a stratum by ``SHA256(task_id + seed)``,
    then one item per stratum per round until the requested count. Coverage is
    ``min(count, strata)`` repos per class; sampling is not population-weighted.
    The selected classes of a sub-corpus are combined in task-hash order, and
    sub-corpora are emitted in fixed order (never pooled into one draw).
    ``subcorpus=None`` selects every sub-corpus independently. Shortages and
    duplicate ids raise; the request is never narrowed.
    """
    if not isinstance(seed, str) or not seed:
        raise ValueError("seed must be a non-empty string")
    for name, value in (("positives", positives), ("negatives", negatives)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if subcorpus is not None and subcorpus not in SUBCORPORA:
        raise ValueError("unknown sub-corpus")
    rows = _validated_rows(population)
    targets = SUBCORPORA if subcorpus is None else (subcorpus,)
    selected: list[dict[str, Any]] = []
    for target in targets:
        pool = [r for r in rows if r["subcorpus"] == target]
        chosen = _round_robin(
            [r for r in pool if r["positive"]], positives, seed, f"{target} positives"
        ) + _round_robin(
            [r for r in pool if not r["positive"]], negatives, seed, f"{target} negatives"
        )
        chosen.sort(key=lambda r: (_sha256_text(r["task_id"] + seed), r["task_id"]))
        selected.extend(chosen)
    return selected


# --------------------------------------------------------------------------- rendering


def render_auditor_input(auditor_input: Mapping[str, Any]) -> dict[str, str]:
    """Render the exact eight-field adapter ``auditor_input`` into auditor files.

    Original string fields are emitted byte-for-byte. ``hints_text`` of ``None``
    is the explicit JSON ``null`` line. Any other field set, a spec envelope, or a
    non-native type raises ``SourceShapeError``.
    """
    if not isinstance(auditor_input, Mapping):
        raise SourceShapeError("auditor_input is not a mapping")
    keys = set(auditor_input)
    expected = set(AUDITOR_INPUT_FIELDS)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(str(key) for key in keys - expected)
        raise SourceShapeError(
            f"auditor_input fields differ from the exact allowlist; missing={missing} extra={extra}"
        )
    for field in _STRING_FIELDS:
        if not isinstance(auditor_input[field], str):
            raise SourceShapeError(f"{field} must be str, got {type(auditor_input[field]).__name__}")
    hints = auditor_input["hints_text"]
    if hints is not None and not isinstance(hints, str):
        raise SourceShapeError(f"hints_text must be str or None, got {type(hints).__name__}")
    for field in _TEST_LIST_FIELDS:
        names = auditor_input[field]
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            raise SourceShapeError(f"{field} must be a list of str")
    return {
        "instruction.md": auditor_input["problem_statement"],
        "tests/00-hints.txt": "null\n" if hints is None else hints,
        "tests/01-test.patch": auditor_input["test_patch"],
        "tests/02-target-PR.patch": auditor_input["patch"],
        "tests/03-test-names.json": canonical_json(
            {
                "FAIL_TO_PASS": list(auditor_input["FAIL_TO_PASS"]),
                "PASS_TO_PASS": list(auditor_input["PASS_TO_PASS"]),
            }
        ),
        "tests/04-source-provenance.json": canonical_json(
            {"repo": auditor_input["repo"], "base_commit": auditor_input["base_commit"]}
        ),
    }


# --------------------------------------------------------------------------- hydration


def _source_record_sha256(auditor_input: Any, identity: Any) -> str | None:
    """SHA-256 of the canonical source record (auditor_input + identity, never gold)."""
    try:
        text = canonical_json({"auditor_input": auditor_input, "identity": identity})
        return _sha256_text(text)
    except (TypeError, ValueError):
        return None


def iter_selected(
    adapter: Any, bundle: Path, selected: list[dict[str, Any]]
) -> Iterator[dict[str, Any]]:
    """Hydrate selected tasks through ``adapter.specs``; yield in ``selected`` order.

    The full adapter pass runs eagerly before the first record is yielded, so a
    missing/duplicate id or adapter contract violation raises atomically with no
    partial output. A selected task whose ``auditor_input`` breaks the strict native
    shape is yielded as ``source_unavailable`` with empty files, never repaired.
    Records carry no gold or label field; ``identity`` is scorer-only and must
    never be written into model-visible files.
    """
    if tuple(adapter.AUDITOR_INPUT_FIELDS) != AUDITOR_INPUT_FIELDS:
        raise ValueError("adapter auditor field allowlist differs from the expected eight fields")
    wanted: dict[str, dict[str, Any]] = {row["task_id"]: row for row in _validated_rows(selected)}

    found: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for spec in adapter.specs(bundle):
        if not isinstance(spec, Mapping) or set(spec) != _ENVELOPE_KEYS:
            raise ValueError("adapter spec envelope differs from auditor_input/gold/identity")
        identity = spec["identity"]
        if not isinstance(identity, Mapping) or set(identity) != _IDENTITY_KEYS:
            raise ValueError("adapter identity keys differ from instance_id/sub_corpus/repo")
        instance_id, sub_corpus = identity["instance_id"], identity["sub_corpus"]
        if not isinstance(instance_id, str) or not instance_id or sub_corpus not in SUBCORPORA:
            raise ValueError("adapter identity has an empty id or unknown sub-corpus")
        task_id = task_id_for(sub_corpus, instance_id)
        if task_id in seen:
            raise ValueError("adapter yielded a duplicate task identity")
        seen.add(task_id)
        want = wanted.get(task_id)
        if want is None:
            continue  # unselected: dropped without rendering or hashing
        if want["subcorpus"] != sub_corpus:
            raise ValueError("selected subcorpus differs from adapter identity")
        gold = spec["gold"]
        if not isinstance(gold, Mapping) or gold.get("positive") is not want["positive"]:
            raise ValueError("selected positive flag differs from adapter gold")
        auditor_input = spec["auditor_input"]
        files: dict[str, str] = {}
        status, error = "prepared", None
        try:
            files = render_auditor_input(auditor_input)
            if identity["repo"] != auditor_input["repo"]:
                raise SourceShapeError("identity repo differs from auditor_input repo")
        except SourceShapeError as exc:
            files, status, error = {}, "source_unavailable", str(exc)
        found[task_id] = {
            "task_id": task_id,
            "subcorpus": sub_corpus,
            "auditor_files": files,
            "identity": {
                "instance_id": instance_id,
                "sub_corpus": sub_corpus,
                "repo": identity["repo"],
            },
            "source_record_sha256": _source_record_sha256(auditor_input, identity),
            "preparation_status": status,
            "preparation_error": error,
        }

    missing = sorted(set(wanted) - set(found))
    if missing:
        raise ValueError(f"selected task ids absent from adapter output: {missing}")
    return iter([found[row["task_id"]] for row in selected])
