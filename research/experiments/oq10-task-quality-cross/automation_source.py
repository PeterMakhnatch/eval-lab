"""OQ10 cross-test source preparation for AutomationBench (parsewave verified).

Four plain-dict entry points:

* ``metadata_population``: metadata/labels only, from ``tasks.csv`` + ``fixes.csv``.
  Executed only after the code-level freeze.
* ``select_cases``: deterministic SHA256 round-robin stratified sampler. It
  guarantees minimum stratum coverage; it is not population-weighted sampling.
* ``render_auditor_input``: the adapter's ``auditor_input`` -> relative path -> text.
* ``iter_selected``: adapter projection for the selected task ids only.

Source-disclosure notes (irreducible, not hidden):

* ``repository``/``revision`` are explicitly admitted provenance
  (``tests/02-source-provenance.json``) and name the benchmark.
* ``tests/original-grader/*`` keep their native ``automationbench/...`` paths and
  original text, so the benchmark identity is visible in the model-facing package.
* Nested business fields (``scored``, ``status``, ``*_id``, benchmark-like values)
  are original source and are never rewritten or removed; ``key_name_audit`` only
  records where identifier-like key names occur.
* ``adapter.specs`` is the authoritative projection. It constructs *all* original
  tasks (pinned upstream data constructors run locally) before this module filters
  to the selected ids; there is no selective path that preserves native semantics.
  The unfiltered envelope (gold/labels/reconstruction) is read only to take the
  adapter status and the four native identity fields; labels are never copied out.
* Adapter statuses other than ``ready`` can correlate with the label (a verified
  task may have an undefined original side). Such tasks are reported as
  ``source_unavailable`` (missing), never silently replaced or dropped.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Collection, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

DEFAULT_SEED = "oq10-cross-v1-20261028"

AUDITOR_FIELDS = frozenset({"repository", "revision", "prompt", "answer", "info", "grader_material"})
INFO_FIELDS = frozenset({"initial_state", "assertions", "zapier_tools", "meta"})
IDENTITY_FIELDS = ("task_name", "example_id", "domain", "cluster_key")

INSTRUCTION_PATH = "instruction.md"
INFO_PATH = "tests/00-task-info.json"
ANSWER_PATH = "tests/01-task-answer.json"
PROVENANCE_PATH = "tests/02-source-provenance.json"
GRADER_PREFIX = "tests/original-grader/"

PREPARED = "prepared"
SOURCE_UNAVAILABLE = "source_unavailable"

_DRIVE = re.compile(r"^[A-Za-z]:")


class AutomationSourceError(ValueError):
    """Fail-closed source error. Messages carry field names/paths, never task values."""


# ---------------------------------------------------------------------------
# Canonical JSON and hashing
# ---------------------------------------------------------------------------


def _check_json(value: Any, where: str) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AutomationSourceError(f"{where}: non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_json(item, f"{where}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise AutomationSourceError(f"{where}: non-string object key")
            _check_json(item, f"{where}.{key}")
        return
    raise AutomationSourceError(f"{where}: unsupported type {type(value).__name__}")


def _require_utf8(text: str, where: str) -> None:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise AutomationSourceError(f"{where}: text is not UTF-8 encodable") from None


def canonical_json(value: Any, where: str = "value") -> str:
    """Sorted keys, UTF-8 (no ASCII escaping), indent 2, no NaN, one trailing newline."""
    _check_json(value, where)
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    _require_utf8(text, where)
    return text


def source_record_sha256(auditor_input: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical JSON of the complete adapter ``auditor_input``."""
    text = canonical_json(dict(auditor_input), "auditor_input")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest(text: str, seed: str) -> str:
    return hashlib.sha256((text + seed).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Metadata population (labels/metadata only)
# ---------------------------------------------------------------------------


def _read_csv(
    path: Path, required: Sequence[str], optional: Sequence[str] = ()
) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames
        if fields is None:
            raise AutomationSourceError(f"{path.name}: missing header")
        missing = [name for name in required if name not in fields]
        if missing:
            raise AutomationSourceError(f"{path.name}: missing columns {missing}")
        wanted = [*required, *(name for name in optional if name in fields)]
        return [{name: row.get(name) or "" for name in wanted} for row in reader]


def metadata_population(adapter: Any, bundle: Path) -> list[dict[str, Any]]:
    """Return ``task_id/positive/stratum/subcorpus`` rows from tasks.csv + fixes.csv.

    ``task_name`` is the only identity (``example_id`` is duplicated). A task is
    positive iff it has a fixes.csv row (defective_verified); every other task is
    unchanged/uncertified. Rows without a task name or domain are unlabelled and
    omitted, never reported as negatives. Duplicate ids, fixes rows that do not
    join, domain disagreement and unknown domains raise.
    """
    root = Path(bundle)
    domains = tuple(adapter.DOMAINS)
    corpus = adapter.CORPUS
    if not isinstance(corpus, str) or not corpus:
        raise AutomationSourceError("adapter CORPUS must be a non-empty string")

    task_domain: dict[str, str] = {}
    for row in _read_csv(root / "tasks.csv", ("task_name", "domain")):
        name = row["task_name"]
        if not name:
            continue
        if name in task_domain:
            raise AutomationSourceError("tasks.csv: duplicate task_name")
        task_domain[name] = row["domain"]

    fixed: set[str] = set()
    for row in _read_csv(root / "fixes.csv", ("task_name",), ("domain",)):
        name = row["task_name"]
        if not name:
            raise AutomationSourceError("fixes.csv: row without task_name")
        if name in fixed:
            raise AutomationSourceError("fixes.csv: duplicate task_name")
        if name not in task_domain:
            raise AutomationSourceError("fixes.csv: task_name absent from tasks.csv")
        if not task_domain[name]:
            raise AutomationSourceError("fixes.csv: fixed task has no domain in tasks.csv")
        if row.get("domain") and row["domain"] != task_domain[name]:
            raise AutomationSourceError("fixes.csv: domain disagrees with tasks.csv")
        fixed.add(name)

    population: list[dict[str, Any]] = []
    for name in sorted(task_domain):
        domain = task_domain[name]
        if not domain:
            continue
        if domain not in domains:
            raise AutomationSourceError("tasks.csv: domain outside adapter DOMAINS")
        population.append(
            {"task_id": name, "positive": name in fixed, "stratum": domain, "subcorpus": corpus}
        )
    return population


# ---------------------------------------------------------------------------
# Sampler
# ---------------------------------------------------------------------------


def _count(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AutomationSourceError(f"{name} must be a positive integer")
    return value


def _validated_population(population: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in population:
        task_id, positive = row.get("task_id"), row.get("positive")
        stratum, subcorpus = row.get("stratum"), row.get("subcorpus")
        if not isinstance(task_id, str) or not task_id:
            raise AutomationSourceError("population: task_id must be a non-empty string")
        if not isinstance(positive, bool):
            raise AutomationSourceError("population: positive must be a bool")
        if not isinstance(stratum, str) or not stratum:
            raise AutomationSourceError("population: stratum must be a non-empty string")
        if not isinstance(subcorpus, str) or not subcorpus:
            raise AutomationSourceError("population: subcorpus must be a non-empty string")
        if task_id in seen:
            raise AutomationSourceError("population: duplicate task_id")
        seen.add(task_id)
        rows.append(
            {"task_id": task_id, "positive": positive, "stratum": stratum, "subcorpus": subcorpus}
        )
    return rows


def _item_order(rows: Sequence[dict[str, Any]], seed: str) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: (_digest(r["task_id"], seed), r["task_id"]))


def _round_robin(rows: Sequence[dict[str, Any]], count: int, seed: str) -> list[dict[str, Any]]:
    by_stratum: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_stratum[row["stratum"]].append(row)
    order = sorted(by_stratum, key=lambda s: (_digest(s, seed), s))
    queues = {stratum: _item_order(by_stratum[stratum], seed) for stratum in order}
    picked: list[dict[str, Any]] = []
    depth = 0
    while len(picked) < count:
        progressed = False
        for stratum in order:
            if depth < len(queues[stratum]):
                picked.append(queues[stratum][depth])
                progressed = True
                if len(picked) == count:
                    break
        if not progressed:
            raise AutomationSourceError(f"positive shortage: {len(picked)} of {count} available")
        depth += 1
    return picked


def select_cases(
    population: Sequence[Mapping[str, Any]],
    *,
    seed: str = DEFAULT_SEED,
    positives: int = 30,
    negatives: int = 30,
    subcorpus: str | None = None,
) -> list[dict[str, Any]]:
    """Select positives by stratum round-robin, then exact per-stratum matched negatives.

    Strata are ordered by SHA256(stratum+seed), items by SHA256(task_id+seed); each
    round takes one item per non-exhausted stratum, so every stratum with a positive
    is covered once ``positives`` >= the number of such strata. Negatives take, for
    each stratum, exactly as many items as positives were selected there. Shortages
    raise. The union is returned in SHA256(task_id+seed) order.
    """
    if not isinstance(seed, str) or not seed:
        raise AutomationSourceError("seed must be a non-empty string")
    positives = _count(positives, "positives")
    negatives = _count(negatives, "negatives")
    if negatives != positives:
        raise AutomationSourceError("exact stratum matching requires negatives == positives")

    rows = _validated_population(population)
    if subcorpus is None:
        names = {row["subcorpus"] for row in rows}
        if len(names) != 1:
            raise AutomationSourceError("population spans zero or several subcorpora; pass subcorpus")
    else:
        rows = [row for row in rows if row["subcorpus"] == subcorpus]
        if not rows:
            raise AutomationSourceError("no population rows for the requested subcorpus")

    chosen_positive = _round_robin([r for r in rows if r["positive"]], positives, seed)
    needed: dict[str, int] = defaultdict(int)
    for row in chosen_positive:
        needed[row["stratum"]] += 1
    negative_pool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not row["positive"]:
            negative_pool[row["stratum"]].append(row)

    chosen_negative: list[dict[str, Any]] = []
    for stratum in sorted(needed):
        pool = _item_order(negative_pool.get(stratum, []), seed)
        if len(pool) < needed[stratum]:
            raise AutomationSourceError(f"negative shortage in stratum {stratum!r}")
        chosen_negative.extend(pool[: needed[stratum]])

    combined = [*chosen_positive, *chosen_negative]
    return [dict(row) for row in _item_order(combined, seed)]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _grader_parts(path: Any) -> tuple[str, ...]:
    if not isinstance(path, str) or not path:
        raise AutomationSourceError("grader_material: path must be a non-empty string")
    if "\\" in path or any(ord(char) < 32 for char in path) or _DRIVE.match(path):
        raise AutomationSourceError(f"grader_material: unsafe path {path!r}")
    parts = tuple(path.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise AutomationSourceError(f"grader_material: unsafe path {path!r}")
    return parts


def _grader_files(material: Any) -> dict[str, str]:
    if not isinstance(material, dict) or not material:
        raise AutomationSourceError("grader_material must be a non-empty object")
    entries: list[tuple[tuple[str, ...], str, str]] = []
    for path, text in material.items():
        parts = _grader_parts(path)
        if not isinstance(text, str):
            raise AutomationSourceError(f"grader_material: text for {path!r} must be a string")
        _require_utf8(text, f"grader_material {path!r}")
        entries.append((parts, path, text))

    keys = {unicodedata.normalize("NFC", path).casefold() for _, path, _ in entries}
    if len(keys) != len(entries):
        raise AutomationSourceError("grader_material: path collision")
    for parts, path, _ in entries:
        for end in range(1, len(parts)):
            prefix = "/".join(parts[:end])
            if unicodedata.normalize("NFC", prefix).casefold() in keys:
                raise AutomationSourceError(f"grader_material: file/directory collision at {path!r}")
    entries.sort(key=lambda entry: entry[0])
    return {GRADER_PREFIX + path: text for _, path, text in entries}


def render_auditor_input(auditor_input: Mapping[str, Any]) -> dict[str, str]:
    """Render exactly the adapter ``auditor_input`` into relative path -> complete text.

    Nothing is truncated, cleaned or removed. Any field outside the six adapter
    fields (for example a whole-spec envelope) is rejected.
    """
    if not isinstance(auditor_input, Mapping):
        raise AutomationSourceError("auditor_input must be a mapping")
    keys = set(auditor_input)
    if keys != AUDITOR_FIELDS:
        missing = sorted(AUDITOR_FIELDS - keys)
        extra = sorted(repr(key) for key in keys - AUDITOR_FIELDS)
        raise AutomationSourceError(f"auditor_input fields: missing {missing}, unexpected {extra}")

    repository, revision = auditor_input["repository"], auditor_input["revision"]
    for name, value in (("repository", repository), ("revision", revision)):
        if not isinstance(value, str) or not value:
            raise AutomationSourceError(f"{name} must be a non-empty string")
    prompt, info = auditor_input["prompt"], auditor_input["info"]
    if not isinstance(prompt, list):
        raise AutomationSourceError("prompt must be a list")
    if not isinstance(info, dict):
        raise AutomationSourceError("info must be an object")
    extra_info = sorted(repr(key) for key in set(info) - INFO_FIELDS)
    if extra_info:
        raise AutomationSourceError(f"info: unexpected root fields {extra_info}")

    files = {
        INSTRUCTION_PATH: canonical_json(prompt, "prompt"),
        INFO_PATH: canonical_json(info, "info"),
        ANSWER_PATH: canonical_json(auditor_input["answer"], "answer"),
        PROVENANCE_PATH: canonical_json(
            {"repository": repository, "revision": revision}, "provenance"
        ),
    }
    files.update(_grader_files(auditor_input["grader_material"]))
    return files


def key_name_audit(auditor_input: Mapping[str, Any], names: Collection[str]) -> list[str]:
    """JSON-pointer paths whose key *name* is in ``names``, is ``id`` or ends in ``_id``.

    Key names only: values are never inspected and the source is never modified.
    Covers prompt/answer/info; grader file names are not business keys.
    """
    found: set[str] = set()

    def walk(value: Any, pointer: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                escaped = str(key).replace("~", "~0").replace("/", "~1")
                path = f"{pointer}/{escaped}"
                if key in names or key == "id" or str(key).endswith("_id"):
                    found.add(path)
                walk(item, path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{pointer}/{index}")

    for field in ("prompt", "answer", "info"):
        if field in auditor_input:
            walk(auditor_input[field], f"/{field}")
    return sorted(found)


# ---------------------------------------------------------------------------
# Selected-record hydration
# ---------------------------------------------------------------------------


def _selected_order(adapter: Any, selected: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    if not selected:
        raise AutomationSourceError("selected is empty")
    order: dict[str, str] = {}
    for row in selected:
        task_id, subcorpus = row.get("task_id"), row.get("subcorpus")
        if not isinstance(task_id, str) or not task_id:
            raise AutomationSourceError("selected: task_id must be a non-empty string")
        if subcorpus != adapter.CORPUS:
            raise AutomationSourceError("selected: subcorpus does not match the adapter corpus")
        if task_id in order:
            raise AutomationSourceError("selected: duplicate task_id")
        order[task_id] = subcorpus
    return order


def _identity(spec: Any) -> dict[str, str]:
    identity = spec.get("identity") if isinstance(spec, Mapping) else None
    if not isinstance(identity, Mapping):
        raise AutomationSourceError("spec: identity envelope missing")
    values: dict[str, str] = {}
    for name in IDENTITY_FIELDS:
        value = identity.get(name)
        if not isinstance(value, str):
            raise AutomationSourceError(f"spec: identity.{name} must be a string")
        values[name] = value
    if not values["task_name"]:
        raise AutomationSourceError("spec: empty task_name")
    return values


def _record(
    identity: dict[str, str],
    subcorpus: str,
    files: dict[str, str],
    digest: str | None,
    error: str | None,
) -> dict[str, Any]:
    return {
        "task_id": identity["task_name"],
        "subcorpus": subcorpus,
        "auditor_files": files,
        "identity": identity,
        "source_record_sha256": digest,
        "preparation_status": PREPARED if error is None else SOURCE_UNAVAILABLE,
        "preparation_error": error,
    }


def _prepare(spec: Mapping[str, Any], identity: dict[str, str], subcorpus: str) -> dict[str, Any]:
    status = spec.get("auditor_input_status")
    payload = spec.get("auditor_input")
    if not isinstance(status, str) or not isinstance(payload, Mapping):
        raise AutomationSourceError("spec: auditor_input/auditor_input_status malformed")
    try:
        digest: str | None = source_record_sha256(payload)
    except AutomationSourceError:
        digest = None
    if status != "ready":
        return _record(identity, subcorpus, {}, digest, f"adapter_status:{status}")
    try:
        files = render_auditor_input(payload)
    except AutomationSourceError as error:
        return _record(identity, subcorpus, {}, digest, f"invalid_auditor_input: {error}")
    return _record(identity, subcorpus, files, digest, None)


def iter_selected(
    adapter: Any, bundle: Path, selected: Sequence[Mapping[str, Any]]
) -> Iterator[dict[str, Any]]:
    """Yield one prepared record per selected id, in ``selected`` order.

    ``adapter.specs`` is consumed in full (it cannot be filtered earlier). Only the
    adapter status, ``auditor_input`` and the four native identity fields of selected
    specs are read; gold/labels/reconstruction are never copied. Duplicate or missing
    ids raise before any record is yielded.
    """
    order = _selected_order(adapter, selected)
    return _iterate(adapter, Path(bundle), order)


def _iterate(adapter: Any, bundle: Path, order: dict[str, str]) -> Iterator[dict[str, Any]]:
    seen: set[str] = set()
    prepared: dict[str, dict[str, Any]] = {}
    for spec in adapter.specs(bundle):
        identity = _identity(spec)
        task_id = identity["task_name"]
        if task_id in seen:
            raise AutomationSourceError("adapter yielded a duplicate task identity")
        seen.add(task_id)
        if task_id in order:
            prepared[task_id] = _prepare(spec, identity, order[task_id])
    missing = [task_id for task_id in order if task_id not in prepared]
    if missing:
        raise AutomationSourceError(f"selected ids absent from adapter output: {len(missing)}")
    for task_id in order:
        yield prepared[task_id]
