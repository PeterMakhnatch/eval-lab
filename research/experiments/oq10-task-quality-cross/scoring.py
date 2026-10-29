"""OQ-10 local-only scoring bridge onto the frozen Library Task QA scorer.

The Library ``score_task_qa_gate.py`` (``task-qa-gate-score/v1``) owns the gold
builders, the confusion/coverage arithmetic and the cluster percentile bootstrap
(10,000 resamples, seed 0). This module reuses exactly those public functions and
adds only what the OQ-10 sample needs:

* ``load_scorer`` executes the scorer from its own hash-pinned bytes at its own
  path, with the hash-pinned ``cross-test-registry.json`` beside it, so ``HERE``,
  ``REGISTRY`` and the scorer's ``script_sha256`` stay those of the pinned copy.
* ``load_native_gold`` runs the scorer's own ``gold_automationbench`` /
  ``gold_paichecker`` on explicit staged bundles, after predictions exist. Both
  registered adapter relpaths must already be regular files under the frozen
  eval-lab root, so the scorer's ``origin/main`` adapter fallback can never run;
  adapters and the bundle files the builders read are checked against prospective
  SHA-256 pins before any label is constructed. This is the only label read;
  native task labels and groups are used as is.
* ``bind_inputs`` (freeze time, no gold) binds the public case manifest (opaque
  ``Cdddd`` ids, corpus, preparation status, source hashes; no label or identity)
  to the sealed scorer manifest (case id -> native identity) by case id, corpus
  and ``source_record_sha256``.
* ``score_cross_test`` repeats that binding, joins each sealed identity to exactly
  one native task, filters the native gold to the selected tasks and calls the
  scorer's ``score`` once per view, role and gate. Views: the AutomationBench sample
  (named by its sealed sub-corpus), and each PAIChecker sub-corpus separately
  (filtered by native sub-corpus tag before any join, so a bare instance id shared
  by both sub-corpora never collides; no pooled view).

A decision exists only when the cell's root ``status`` and ``prediction.status``
are ``ok`` and both exported gate flags are booleans. A null flag, a
source-unavailable case and a case without a cell are all missing coverage, never
an unflagged task. Records are never deduplicated; any duplicate stops the run.
Each role is scored on the same planned cases independently of every other role,
so a triage role can neither clear nor drop a case. Decisions go straight to the
scorer's ``score``; its ``load_gate`` only parses a JSONL file and ``_as_flag``,
which for explicit booleans is the identity.

No source body, prompt, model output or causal key is read, and no model is
called. Outputs carry counts, rates, intervals, digests and local paths only.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from statistics import NormalDist
from types import ModuleType
from typing import Any, NamedTuple

SCHEMA = "oq10.cross-test-score/v1"
SCORER_SCHEMA = "task-qa-gate-score/v1"
REGISTRY_NAME = "cross-test-registry.json"
STAGE_MANIFEST = "STAGE-MANIFEST.json"
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 0

AUTOMATIONBENCH = "automationbench-verified"
PAICHECKER = "paichecker"
CORPORA = (AUTOMATIONBENCH, PAICHECKER)
PAI_SUBCORPORA = ("swe_bench_multilingual", "swe_gym")
GATES = ("old_0_8", "new_verified_0_5")
PREDICTION_OK = "ok"
CASE_ID = re.compile(r"C[0-9]{4}\Z")
WILSON_Z = NormalDist().inv_cdf(0.975)

SAMPLE_RATES = (
    "complete-sample: every selected case has a decision; rates describe the seeded "
    "stratified sample, not the corpus population"
)
SAMPLE_NOTE = (
    "Cases are a seeded class-by-stratum round-robin sample with planned class counts; "
    "neither rates nor precision are population-weighted or at population prevalence."
)

_ROLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_PUBLIC_REQUIRED = frozenset({
    "case_id", "corpus", "preparation_status", "source_record_sha256", "record_binding_sha256",
})
_PUBLIC_FORBIDDEN = frozenset({
    "positive", "groups", "families", "issue_group", "label", "labels", "gold", "sampling",
    "task_id", "task_name", "instance_id", "example_id", "identity", "subcorpus",
    "sub_corpus", "stratum", "repo", "domain",
})
_SEALED_KEYS = frozenset({
    "case_id", "corpus", "subcorpus", "task_id", "identity", "source_record_sha256",
    "record_binding_sha256",
})
_IDENTITY_KEYS = {
    AUTOMATIONBENCH: frozenset({"task_name", "example_id", "domain", "cluster_key"}),
    PAICHECKER: frozenset({"instance_id", "sub_corpus", "repo"}),
}
_SOURCE_BINDING = (
    "record_binding_sha256", "source_record_sha256", "source_sha256", "prompt_sha256",
)
_RECORD_REQUIRED = ("case_id", "role", "route", "status", "flags", *_SOURCE_BINDING)
_SCORER_ATTRIBUTES = (
    "Gold", "Task", "ScoreError", "score", "digest_gold", "gold_automationbench", "gold_paichecker",
)
# Files the pinned scorer's gold builders open directly; PAIChecker label/task files
# are named by its adapter's SUB_CORPORA and checked after the build.
_GOLD_READS = {
    AUTOMATIONBENCH: frozenset({STAGE_MANIFEST, "tasks.csv", "fixes.csv"}),
    PAICHECKER: frozenset({STAGE_MANIFEST}),
}
_SCORER_ADAPTER_MODULE = "paichecker_adapter_for_scoring"  # name the scorer's _load_adapter registers


class ScoringBridgeError(ValueError):
    """Inputs do not bind exactly or a pinned dependency drifted.

    Messages name opaque case ids, field names and counts, never a task identity.
    """


class FrozenScorer(NamedTuple):
    module: ModuleType
    scorer_path: Path
    scorer_sha256: str
    registry_path: Path
    registry_sha256: str
    registry: dict[str, Any]


class NativeGold(NamedTuple):
    golds: dict[str, Any]
    provenance: dict[str, Any]


# --------------------------------------------------------------------------- helpers


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return _sha(text.encode("utf-8"))


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _rows(value: Any, label: str) -> list[Mapping[str, Any]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ScoringBridgeError(f"{label} must be a sequence of objects")
    if any(not isinstance(row, Mapping) for row in value):
        raise ScoringBridgeError(f"{label} rows must be objects")
    return list(value)


def _shown(case_ids: Any) -> str:
    ordered = sorted(case_ids)
    return ", ".join(ordered[:5]) + (f" (+{len(ordered) - 5} more)" if len(ordered) > 5 else "")


def _regular_file(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ScoringBridgeError(f"{label} is not a regular file: {path}")
    return path.read_bytes()


def _pinned(pin: Any, label: str) -> tuple[Path, str, bytes]:
    if not isinstance(pin, Mapping) or set(pin) != {"path", "sha256"}:
        raise ScoringBridgeError(f"{label} pin must be exactly {{path, sha256}}")
    if not _text(pin["path"]) or not isinstance(pin["sha256"], str) or not _HEX.fullmatch(pin["sha256"]):
        raise ScoringBridgeError(f"{label} pin needs a path and a lowercase SHA-256")
    path = Path(pin["path"])
    if not path.is_absolute() or path.resolve() != path:
        raise ScoringBridgeError(f"{label} path must be absolute and free of symlinks: {path}")
    data = _regular_file(path, label)
    if _sha(data) != pin["sha256"]:
        raise ScoringBridgeError(f"{label} bytes differ from the pinned SHA-256: {path}")
    return path, pin["sha256"], data


def _verify_unchanged(frozen: FrozenScorer) -> None:
    for path, sha, label in (
        (frozen.scorer_path, frozen.scorer_sha256, "scorer"),
        (frozen.registry_path, frozen.registry_sha256, "registry"),
    ):
        if _sha(_regular_file(path, label)) != sha:
            raise ScoringBridgeError(f"pinned {label} drifted after load: {path}")


# --------------------------------------------------------------------------- dependencies


def load_scorer(scorer: Mapping[str, str], registry: Mapping[str, str]) -> FrozenScorer:
    """Execute the pinned scorer in place; its registry must be the pinned file beside it."""
    scorer_path, scorer_sha, source = _pinned(scorer, "scorer")
    registry_path, registry_sha, registry_bytes = _pinned(registry, "registry")
    if registry_path != scorer_path.parent / REGISTRY_NAME:
        raise ScoringBridgeError(
            f"registry must be the scorer's own {REGISTRY_NAME} beside it; a relocated scorer "
            "or registry breaks the scorer's HERE/REGISTRY"
        )
    try:
        data = json.loads(registry_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ScoringBridgeError("registry is not JSON") from None
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ScoringBridgeError("registry has no entries list")
    for corpus in CORPORA:
        if sum(isinstance(e, dict) and e.get("id") == corpus for e in entries) != 1:
            raise ScoringBridgeError(f"registry must hold exactly one {corpus} entry")

    name = "oq10_frozen_task_qa_scorer_" + scorer_sha[:16]
    module = ModuleType(name)
    module.__file__ = str(scorer_path)
    # dataclasses resolves the scorer's string annotations through sys.modules.
    sys.modules[name] = module
    try:
        exec(compile(source, str(scorer_path), "exec", dont_inherit=True), module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    missing = [attr for attr in _SCORER_ATTRIBUTES if not hasattr(module, attr)]
    if missing:
        raise ScoringBridgeError("scorer lacks expected public names: " + ", ".join(missing))
    if getattr(module, "SCHEMA", None) != SCORER_SCHEMA:
        raise ScoringBridgeError(f"scorer schema is not {SCORER_SCHEMA}")
    if (getattr(module, "BOOTSTRAP_RESAMPLES", None), getattr(module, "BOOTSTRAP_SEED", None)) != (
        BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED,
    ):
        raise ScoringBridgeError("scorer bootstrap defaults differ from 10,000 resamples, seed 0")
    if Path(getattr(module, "REGISTRY", "")) != registry_path:
        raise ScoringBridgeError("scorer REGISTRY does not resolve to the pinned registry")
    return FrozenScorer(module, scorer_path, scorer_sha, registry_path, registry_sha, data)


def _safe_relpath(relpath: Any, label: str) -> str:
    if not _text(relpath) or Path(relpath).is_absolute() or any(
        part in {"", ".", ".."} for part in relpath.split("/")
    ) or "\\" in relpath:
        raise ScoringBridgeError(f"{label} is not a safe relative path")
    return relpath


def _check_files(directory: Path, pins: Mapping[str, str], label: str) -> None:
    for relpath, digest in sorted(pins.items()):
        path = directory / relpath
        if path.resolve() != path:
            raise ScoringBridgeError(f"{label} file {relpath} resolves through a symlink")
        if _sha(_regular_file(path, f"{label} file {relpath}")) != digest:
            raise ScoringBridgeError(f"{label} file {relpath} differs from its prospective pin")


def _bundle_pins(pins: Any, corpus: str) -> dict[str, str]:
    if not isinstance(pins, Mapping) or not pins:
        raise ScoringBridgeError(f"{corpus} bundle_files must map relative paths to SHA-256 pins")
    for relpath, digest in pins.items():
        _safe_relpath(relpath, f"{corpus} bundle file")
        if not isinstance(digest, str) or not _HEX.fullmatch(digest):
            raise ScoringBridgeError(f"{corpus} bundle file {relpath} needs a lowercase SHA-256")
    absent = _GOLD_READS[corpus] - set(pins)
    if absent:
        raise ScoringBridgeError(f"{corpus} bundle_files must pin " + ", ".join(sorted(absent)))
    return dict(sorted(pins.items()))


def load_native_gold(
    frozen: FrozenScorer,
    *,
    eval_lab_root: Path | str,
    adapters: Mapping[str, str],
    bundle_files: Mapping[str, Mapping[str, str]],
    automationbench_bundle: Path | str | None = None,
    paichecker_bundle: Path | str | None = None,
) -> NativeGold:
    """Build the scorer's own gold for explicit bundles. Reads labels: scoring time only.

    Every input is checked against a prospective pin before any gold builder runs:
    ``adapters`` is ``{corpus: sha256}`` for both registered adapter files under the
    frozen ``eval_lab_root``; ``bundle_files`` is ``{corpus: {relpath: sha256}}`` for
    each given bundle and must cover ``STAGE-MANIFEST.json`` plus every file the gold
    builder reads (AutomationBench ``tasks.csv``/``fixes.csv``; PAIChecker the label
    and task files named by the adapter's ``SUB_CORPORA``, enforced after the build
    because only the pinned adapter names them). Pins are re-checked afterwards.
    """
    bundles = {c: b for c, b in ((AUTOMATIONBENCH, automationbench_bundle), (PAICHECKER, paichecker_bundle)) if b}
    if not bundles:
        raise ScoringBridgeError("give at least one staged bundle")
    if not isinstance(adapters, Mapping) or set(adapters) != set(CORPORA) or not all(
        isinstance(v, str) and _HEX.fullmatch(v) for v in adapters.values()
    ):
        raise ScoringBridgeError("adapters must pin a lowercase SHA-256 for both registered corpora")
    if not isinstance(bundle_files, Mapping) or set(bundle_files) != set(bundles):
        raise ScoringBridgeError("bundle_files must pin exactly the given bundles' corpora")
    root = Path(eval_lab_root)
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        raise ScoringBridgeError("eval_lab_root must be an absolute, symlink-free directory")
    entries = {e["id"]: e for e in frozen.registry["entries"] if isinstance(e, dict) and e.get("id") in CORPORA}
    relpaths: dict[str, str] = {}
    for corpus in CORPORA:
        adapter = entries[corpus].get("adapter")
        relpath = _safe_relpath(
            adapter.get("module_path") if isinstance(adapter, Mapping) else None, f"registry {corpus} adapter"
        )
        path = root / relpath
        if path.is_symlink() or not path.is_file():
            raise ScoringBridgeError(
                f"registered {corpus} adapter is absent under eval_lab_root; refusing the "
                "scorer's origin/main fallback"
            )
        relpaths[corpus] = relpath
    adapter_pins = {relpaths[corpus]: adapters[corpus] for corpus in CORPORA}
    directories: dict[str, Path] = {}
    pins: dict[str, dict[str, str]] = {}
    for corpus, bundle in bundles.items():
        directory = Path(bundle)
        if not directory.is_absolute() or directory.resolve() != directory or not directory.is_dir():
            raise ScoringBridgeError(f"{corpus} bundle must be an absolute, symlink-free directory")
        directories[corpus], pins[corpus] = directory, _bundle_pins(bundle_files[corpus], corpus)

    def verify() -> None:
        _verify_unchanged(frozen)
        _check_files(root, adapter_pins, "adapter")
        for corpus, directory in directories.items():
            _check_files(directory, pins[corpus], f"{corpus} bundle")

    verify()
    module = frozen.module
    golds: dict[str, Any] = {}
    staged: dict[str, dict[str, Any]] = {}
    for corpus, directory in directories.items():
        sys.modules.pop(_SCORER_ADAPTER_MODULE, None)
        try:
            if corpus == AUTOMATIONBENCH:
                gold = module.gold_automationbench(str(directory))
            else:
                gold = module.gold_paichecker(str(directory), root)
        except module.ScoreError as error:
            raise ScoringBridgeError(f"{corpus}: frozen scorer refused the bundle: {error}") from None
        if gold.corpus != corpus:
            raise ScoringBridgeError(f"{corpus}: scorer returned gold for another corpus")
        if corpus == PAICHECKER:
            _check_pai_reads(root / relpaths[PAICHECKER], pins[corpus])
        golds[corpus] = gold
        staged[corpus] = {
            "bundle": str(directory), "files": pins[corpus],
            "native_tasks": len(gold.tasks), "native_gold_sha256": module.digest_gold(gold),
        }
    verify()
    return NativeGold(golds, {
        "eval_lab_root": str(root),
        "adapters": {c: {"module_path": relpaths[c], "sha256": adapters[c]} for c in CORPORA},
        "bundles": staged,
    })


def _check_pai_reads(adapter_path: Path, pins: Mapping[str, str]) -> None:
    """The scorer must have run the pinned adapter, and only on pinned files."""
    adapter = sys.modules.get(_SCORER_ADAPTER_MODULE)
    if adapter is None or Path(getattr(adapter, "__file__", "")).resolve() != adapter_path:
        raise ScoringBridgeError("the scorer did not load the pinned PAIChecker adapter")
    sources = getattr(adapter, "SUB_CORPORA", None)
    if not isinstance(sources, Mapping):
        raise ScoringBridgeError("PAIChecker adapter has no SUB_CORPORA mapping")
    read = {str(source[key]) for source in sources.values() for key in ("labels", "tasks")}
    unpinned = sorted(path for path in read if path not in pins)
    if unpinned:
        raise ScoringBridgeError("PAIChecker gold read unpinned bundle files: " + ", ".join(unpinned))


# --------------------------------------------------------------------------- records


def _check_roles(roles: Any) -> dict[str, str]:
    if not isinstance(roles, Mapping) or not roles:
        raise ScoringBridgeError("roles must be a non-empty {role: route} mapping")
    for role, route in roles.items():
        if not isinstance(role, str) or not _ROLE.fullmatch(role) or role in {".", ".."}:
            raise ScoringBridgeError("role names must be simple path-safe names")
        if not _text(route):
            raise ScoringBridgeError(f"role {role} needs a route")
    return dict(roles)


def _gate_flags(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(GATES):
        raise ScoringBridgeError(f"{label} must hold exactly the gates {', '.join(GATES)}")
    return {gate: value[gate] for gate in GATES}


def _project(value: Any) -> dict[str, Any]:
    """Keep only the fields scoring reads; parsed payloads and receipts are dropped."""
    if not isinstance(value, Mapping):
        raise ScoringBridgeError("a cell record must be an object")
    absent = [key for key in _RECORD_REQUIRED if key not in value]
    if absent:
        raise ScoringBridgeError("cell record lacks " + ", ".join(absent))
    record = {key: value[key] for key in _RECORD_REQUIRED if key != "flags"}
    record["flags"] = _gate_flags(value["flags"], "cell flags")
    if "terminal" in value:
        record["terminal"] = value["terminal"]
    prediction = value.get("prediction")
    if prediction is not None:
        if not isinstance(prediction, Mapping) or "status" not in prediction or "flags" not in prediction:
            raise ScoringBridgeError("cell prediction must carry status and flags")
        record["prediction"] = {
            "status": prediction["status"],
            "flags": _gate_flags(prediction["flags"], "prediction flags"),
            **{key: prediction.get(key) for key in _SOURCE_BINDING},
        }
    return record


def load_cells(cells_root: Path | str, roles: Mapping[str, str]) -> list[dict[str, Any]]:
    """Read ``<cells_root>/<role>/<Cdddd>.json`` terminal cells and project them.

    A role without a directory has no records (all of its cases are missing).
    """
    roles = _check_roles(roles)
    root = Path(cells_root)
    records: list[dict[str, Any]] = []
    for role in sorted(roles):
        directory = root / role
        if not directory.exists():
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise ScoringBridgeError(f"cells/{role} is not a directory")
        for path in sorted(directory.iterdir()):
            if path.suffix != ".json" or not CASE_ID.fullmatch(path.stem):
                raise ScoringBridgeError(f"unexpected entry in cells/{role}: {path.name}")
            try:
                value = json.loads(_regular_file(path, f"cells/{role}/{path.name}"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ScoringBridgeError(f"cells/{role}/{path.name} is not JSON") from None
            record = _project(value)
            if record["case_id"] != path.stem or record["role"] != role:
                raise ScoringBridgeError(f"cells/{role}/{path.name} names another case or role")
            records.append(record)
    return records


# --------------------------------------------------------------------------- binding


class _Selected(NamedTuple):
    case_id: str
    task: Any


class _View(NamedTuple):
    corpus: str
    gold: Any
    cases: list[_Selected]


class _Sealed(NamedTuple):
    case_id: str
    corpus: str
    view: str
    join: tuple[str, str]  # (native sub-corpus tag or "" for AutomationBench, native task key)
    repo: str | None  # native cluster (PAIChecker repo, AutomationBench domain)
    alias: str | None = None  # AutomationBench native example_id


def _source_hash(value: Any, label: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not _HEX.fullmatch(value)):
        raise ScoringBridgeError(f"{label}: source_record_sha256 must be a lowercase SHA-256 or null")
    return value


def _record_binding(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise ScoringBridgeError(f"{label}: record_binding_sha256 must be a lowercase SHA-256")
    return value


def _public_index(public_cases: Any) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    for row in _rows(public_cases, "public_cases"):
        absent = _PUBLIC_REQUIRED - set(row)
        if absent:
            raise ScoringBridgeError("public case lacks " + ", ".join(sorted(absent)))
        leaked = _PUBLIC_FORBIDDEN & set(row)
        if leaked:
            raise ScoringBridgeError(
                "public case metadata carries label/identity fields: " + ", ".join(sorted(leaked))
            )
        case_id = row["case_id"]
        if not isinstance(case_id, str) or not CASE_ID.fullmatch(case_id):
            raise ScoringBridgeError("public case ids must be opaque Cdddd values")
        if case_id in index:
            raise ScoringBridgeError(f"duplicate public case {case_id}")
        if row["corpus"] not in CORPORA:
            raise ScoringBridgeError(f"public case {case_id}: unknown corpus")
        if not _text(row["preparation_status"]):
            raise ScoringBridgeError(f"public case {case_id}: preparation_status must be text")
        _source_hash(row["source_record_sha256"], f"public case {case_id}")
        _record_binding(row["record_binding_sha256"], f"public case {case_id}")
        if row["preparation_status"] == "prepared":
            if row["source_record_sha256"] is None:
                raise ScoringBridgeError(f"public case {case_id}: prepared native source hash is null")
            for field in ("source", "prompt"):
                artifact = row.get(field)
                if not isinstance(artifact, Mapping) or not isinstance(artifact.get("sha256"), str) or not _HEX.fullmatch(artifact["sha256"]):
                    raise ScoringBridgeError(f"public case {case_id}: prepared {field} artifact is unbound")
        index[case_id] = row
    return index


def _sealed_case(row: Mapping[str, Any]) -> _Sealed:
    if set(row) != _SEALED_KEYS:
        raise ScoringBridgeError("sealed case keys must be exactly " + ", ".join(sorted(_SEALED_KEYS)))
    case_id, corpus, subcorpus = row["case_id"], row["corpus"], row["subcorpus"]
    if not isinstance(case_id, str) or not CASE_ID.fullmatch(case_id):
        raise ScoringBridgeError("sealed case ids must be opaque Cdddd values")
    if corpus not in CORPORA:
        raise ScoringBridgeError(f"sealed case {case_id}: unknown corpus")
    _source_hash(row["source_record_sha256"], f"sealed case {case_id}")
    identity = row["identity"]
    if not isinstance(identity, Mapping) or set(identity) != _IDENTITY_KEYS[corpus] or not all(
        _text(v) for v in identity.values()
    ):
        raise ScoringBridgeError(f"sealed case {case_id}: identity fields differ from the {corpus} identity")
    if corpus == AUTOMATIONBENCH:
        if not _text(subcorpus) or subcorpus in PAI_SUBCORPORA:
            raise ScoringBridgeError(f"sealed case {case_id}: AutomationBench sub-corpus malformed")
        if row["task_id"] != identity["task_name"]:
            raise ScoringBridgeError(f"sealed case {case_id}: task_id differs from identity.task_name")
        if identity["domain"] != identity["cluster_key"]:
            raise ScoringBridgeError(f"sealed case {case_id}: identity domain differs from its cluster_key")
        return _Sealed(
            case_id, corpus, subcorpus, ("", identity["task_name"]), identity["domain"], identity["example_id"],
        )
    if subcorpus not in PAI_SUBCORPORA or identity["sub_corpus"] != subcorpus:
        raise ScoringBridgeError(f"sealed case {case_id}: PAIChecker sub-corpus binding differs")
    if row["task_id"] != f"{subcorpus}:{identity['instance_id']}":
        raise ScoringBridgeError(f"sealed case {case_id}: task_id is not <sub_corpus>:<instance_id>")
    return _Sealed(case_id, corpus, subcorpus, (subcorpus, identity["instance_id"]), identity["repo"])


def _manifests(
    public_cases: Any, sealed_cases: Any,
) -> tuple[dict[str, Mapping[str, Any]], list[_Sealed], list[Mapping[str, Any]]]:
    public = _public_index(public_cases)
    sealed_rows = _rows(sealed_cases, "sealed_cases")
    sealed: list[_Sealed] = []
    seen_cases: set[str] = set()
    seen_tasks: set[tuple[str, str, str]] = set()
    ab_views: set[str] = set()
    for row in sealed_rows:
        case = _sealed_case(row)
        if case.case_id in seen_cases:
            raise ScoringBridgeError(f"duplicate sealed case {case.case_id}")
        seen_cases.add(case.case_id)
        if (case.corpus, *case.join) in seen_tasks:
            raise ScoringBridgeError(f"sealed case {case.case_id} repeats an already selected task")
        seen_tasks.add((case.corpus, *case.join))
        if case.corpus == AUTOMATIONBENCH:
            ab_views.add(case.view)
        sealed.append(case)
    if len(ab_views) > 1:
        raise ScoringBridgeError("AutomationBench sealed cases name several sub-corpora")
    only_public, only_sealed = set(public) - seen_cases, seen_cases - set(public)
    if only_public or only_sealed:
        raise ScoringBridgeError(
            f"public/sealed case ids differ: public-only [{_shown(only_public)}], "
            f"sealed-only [{_shown(only_sealed)}]"
        )
    for case, row in zip(sealed, sealed_rows, strict=True):
        if public[case.case_id]["corpus"] != case.corpus:
            raise ScoringBridgeError(f"case {case.case_id}: public and sealed corpus differ")
        if public[case.case_id]["source_record_sha256"] != row["source_record_sha256"]:
            raise ScoringBridgeError(f"case {case.case_id}: public and sealed source_record_sha256 differ")
        _record_binding(row["record_binding_sha256"], f"case {case.case_id}")
        if public[case.case_id]["record_binding_sha256"] != row["record_binding_sha256"]:
            raise ScoringBridgeError(f"case {case.case_id}: public and sealed record_binding_sha256 differ")
    return public, sealed, sealed_rows


def _manifest_digests(public: Mapping[str, Mapping[str, Any]], sealed_rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    try:
        return {
            "public_cases_sha256": _digest([dict(public[k]) for k in sorted(public)]),
            "sealed_cases_sha256": _digest(sorted((dict(r) for r in sealed_rows), key=lambda r: r["case_id"])),
        }
    except (TypeError, ValueError):
        raise ScoringBridgeError("case manifests must be plain finite JSON") from None


def bind_inputs(
    public_cases: Sequence[Mapping[str, Any]], sealed_cases: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Freeze-time binding of the public and sealed manifests; reads no gold.

    Returns a label-free summary: planned cases per view and both manifest digests.
    """
    public, sealed, sealed_rows = _manifests(public_cases, sealed_cases)
    views: dict[str, dict[str, Any]] = {}
    for case in sealed:
        entry = views.setdefault(case.view, {"corpus": case.corpus, "cases": 0})
        entry["cases"] += 1
    return {"cases": len(public), "views": dict(sorted(views.items())), **_manifest_digests(public, sealed_rows)}


def _native_index(gold: Any, corpus: str) -> dict[tuple[str, str], Any]:
    index: dict[tuple[str, str], Any] = {}
    for task in gold.tasks:
        scopes = ("",) if corpus == AUTOMATIONBENCH else tuple(s for s in PAI_SUBCORPORA if s in task.tags)
        if len(scopes) != 1:
            raise ScoringBridgeError(f"{corpus}: a native task is not in exactly one sub-corpus")
        key = (scopes[0], task.key)
        if key in index:
            raise ScoringBridgeError(f"{corpus}: native gold repeats a task within one sub-corpus")
        index[key] = task
    return index


def _views(native: NativeGold, sealed: Sequence[_Sealed]) -> dict[str, _View]:
    """Join each sealed identity to one native task; keep native labels and groups as is."""
    indexes: dict[str, dict[tuple[str, str], Any]] = {}
    selected: dict[str, list[_Selected]] = {}
    corpus_of: dict[str, str] = {}
    for case in sealed:
        if case.corpus not in native.golds:
            raise ScoringBridgeError(f"no native gold loaded for {case.corpus}")
        if case.corpus not in indexes:
            indexes[case.corpus] = _native_index(native.golds[case.corpus], case.corpus)
        task = indexes[case.corpus].get(case.join)
        if task is None:
            raise ScoringBridgeError(f"sealed case {case.case_id}: identity absent from the native gold")
        if case.repo is not None and task.cluster != case.repo:
            raise ScoringBridgeError(f"sealed case {case.case_id}: identity repo differs from the native repo")
        if case.alias is not None and case.alias not in task.aliases:
            raise ScoringBridgeError(f"sealed case {case.case_id}: identity example_id differs from the native aliases")
        corpus_of[case.view] = case.corpus
        selected.setdefault(case.view, []).append(_Selected(case.case_id, task))
    views: dict[str, _View] = {}
    for view, cases in selected.items():
        corpus = corpus_of[view]
        cases.sort(key=lambda s: s.case_id)
        tag = "all" if corpus == AUTOMATIONBENCH else view
        gold = dataclasses.replace(
            native.golds[corpus], tasks=[s.task for s in cases], views={view: (tag,)},
            expected={}, admission={},
        )
        views[view] = _View(corpus, gold, cases)
    return views


def _records(
    records: Any, roles: Mapping[str, str], public: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, dict[str, Any]]]:
    by_role: dict[str, dict[str, dict[str, Any]]] = {role: {} for role in roles}
    for raw in _rows(records, "records"):
        record = _project(raw)
        case_id, role = record["case_id"], record["role"]
        if not isinstance(case_id, str) or case_id not in public:
            raise ScoringBridgeError("a record names a case outside the manifests")
        if role not in roles:
            raise ScoringBridgeError(f"record for {case_id} names an unregistered role")
        if record["route"] != roles[role]:
            raise ScoringBridgeError(f"record {role}/{case_id}: route differs from the role's route")
        if case_id in by_role[role]:
            raise ScoringBridgeError(f"duplicate {role} record for {case_id}; records are never deduplicated")
        if "terminal" in record and record["terminal"] is not True:
            raise ScoringBridgeError(f"record {role}/{case_id} is not terminal (quota-pending is not a result)")
        if not _text(record["status"]):
            raise ScoringBridgeError(f"record {role}/{case_id}: status must be text")
        flags = record["flags"]
        if any(value is not None and type(value) is not bool for value in flags.values()):
            raise ScoringBridgeError(f"record {role}/{case_id}: flags must be true, false or null")
        prediction = record.get("prediction")
        decided = [value is not None for value in flags.values()]
        if all(decided):
            if record["status"] != PREDICTION_OK or prediction is None or prediction["status"] != PREDICTION_OK:
                raise ScoringBridgeError(
                    f"record {role}/{case_id}: boolean flags need status ok and prediction.status ok"
                )
            if any(prediction["flags"][gate] is not flags[gate] for gate in GATES):
                raise ScoringBridgeError(f"record {role}/{case_id}: prediction flags differ from cell flags")
            if public[case_id]["preparation_status"] != "prepared":
                raise ScoringBridgeError(f"record {role}/{case_id}: a decision on an unprepared case")
        elif any(decided):
            raise ScoringBridgeError(f"record {role}/{case_id}: gates must be all boolean or all null")
        elif record["status"] == PREDICTION_OK or (prediction is not None and prediction["status"] == PREDICTION_OK):
            raise ScoringBridgeError(f"record {role}/{case_id}: status ok with null flags")
        expected_binding = {
            "record_binding_sha256": public[case_id]["record_binding_sha256"],
            "source_record_sha256": public[case_id]["source_record_sha256"],
            "source_sha256": public[case_id].get("source", {}).get("sha256"),
            "prompt_sha256": public[case_id].get("prompt", {}).get("sha256"),
        }
        for field, expected in expected_binding.items():
            if record[field] != expected:
                raise ScoringBridgeError(f"record {role}/{case_id}: {field} differs from frozen case")
            if prediction is not None and prediction[field] != expected:
                raise ScoringBridgeError(f"record {role}/{case_id}: prediction {field} differs from frozen case")
        by_role[role][case_id] = record
    return by_role


# --------------------------------------------------------------------------- scoring


def _wilson(num: int, den: int) -> list[float] | None:
    if den == 0:
        return None
    p, z2 = num / den, WILSON_Z * WILSON_Z
    scale = 1 + z2 / den
    centre = (p + z2 / (2 * den)) / scale
    half = WILSON_Z * math.sqrt(p * (1 - p) / den + z2 / (4 * den * den)) / scale
    return [round(max(0.0, centre - half), 6), round(min(1.0, centre + half), 6)]


def _false_alarms(confusion: Mapping[str, int], verified: bool) -> tuple[int, int]:
    if verified:
        return confusion["fp_false_alarms"], confusion["tn"]
    return confusion["unverified_alarms"], confusion["unflagged_unverified"]


def _derived(scored: Mapping[str, Any], verified: bool) -> dict[str, Any]:
    confusion = scored["confusion"]
    tp, fn = confusion["tp"], confusion["fn"]
    fp, _ = _false_alarms(confusion, verified)
    name = "precision_on_human_aligned" if verified else "conditional_precision"
    return {
        name: {
            "value": round(tp / (tp + fp), 6) if tp + fp else None, "num": tp, "den": tp + fp,
            "basis": (
                "human-aligned negatives; measures issue/PR/test alignment, not verified weak-grader bugs"
                if verified else "assumes the unchanged, uncertified negatives are clean"
            ),
            "prevalence_note": SAMPLE_NOTE,
        },
        "recall_wilson95": {
            "interval": _wilson(tp, tp + fn), "num": tp, "den": tp + fn,
            "note": "binomial score interval ignoring clusters; the cluster bootstrap ci95 is in score",
        },
    }


def _score_cell(
    frozen: FrozenScorer, view: str, spec: _View, records: Mapping[str, Mapping[str, Any]], gate: str,
) -> dict[str, Any]:
    decisions: dict[str, int | None] = {}
    statuses: Counter[str] = Counter()
    rows = decided = 0
    for case in spec.cases:
        record = records.get(case.case_id)
        if record is None:
            continue
        rows += 1
        flag = record["flags"][gate]
        if flag is None:
            statuses[record["status"]] += 1
            decisions[case.task.key] = None
        else:
            decided += 1
            decisions[case.task.key] = int(flag)
    stats = {"rows": rows, "unknown_ids": 0, "duplicate_ids": 0, "no_decision_rows": rows - decided}
    result = frozen.module.score(spec.gold, decisions, stats)
    boot = result["bootstrap"]
    if (boot["resamples"], boot["seed"]) != (BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED):
        raise ScoringBridgeError("scorer bootstrap differs from 10,000 resamples, seed 0")
    if result["script_sha256"] != frozen.scorer_sha256 or result["ids_or_text_in_this_output"] is not False:
        raise ScoringBridgeError("scorer output provenance differs from the pinned scorer")
    scored = result["views"][view]
    scored["scorer_rates_are"] = scored["rates_are"]
    if scored["rates_are"] == "full-population":
        scored["rates_are"] = SAMPLE_RATES
    scored["population_note"] = SAMPLE_NOTE
    return {
        "coverage": {
            "planned": len(spec.cases), "records": rows, "decided": decided,
            "missing_no_record": len(spec.cases) - rows, "missing_null_flag": rows - decided,
            "null_flag_by_status": dict(sorted(statuses.items())),
        },
        "score": result,
        "derived": _derived(scored, spec.gold.negative_kind == "verified_aligned"),
    }


def score_cross_test(
    frozen: FrozenScorer,
    native: NativeGold,
    *,
    public_cases: Sequence[Mapping[str, Any]],
    sealed_cases: Sequence[Mapping[str, Any]],
    records: Sequence[Mapping[str, Any]],
    roles: Mapping[str, str],
) -> dict[str, Any]:
    """Score every view x role x gate with the frozen scorer; aggregates only.

    Call only after every planned role cell is final: this joins native labels.
    """
    roles = _check_roles(roles)
    _verify_unchanged(frozen)
    public, sealed, sealed_rows = _manifests(public_cases, sealed_cases)
    by_role = _records(records, roles, public)
    views = _views(native, sealed)
    out_views: dict[str, Any] = {}
    for view in sorted(views):
        spec = views[view]
        positives = sum(case.task.positive for case in spec.cases)
        out_views[view] = {
            "corpus": spec.corpus, "negative_kind": spec.gold.negative_kind,
            "cluster_kind": spec.gold.cluster_word, "planned_cases": len(spec.cases),
            "planned_positive": positives, "planned_negative": len(spec.cases) - positives,
            "roles": {
                role: {
                    "route": roles[role],
                    "gates": {gate: _score_cell(frozen, view, spec, by_role[role], gate) for gate in GATES},
                }
                for role in sorted(roles)
            },
        }
    _verify_unchanged(frozen)
    return {
        "schema": SCHEMA,
        "dependencies": {
            "scorer": {"path": str(frozen.scorer_path), "sha256": frozen.scorer_sha256, "schema": SCORER_SCHEMA},
            "registry": {"path": str(frozen.registry_path), "sha256": frozen.registry_sha256},
            "native_gold": native.provenance,
        },
        "bootstrap": {"resamples": BOOTSTRAP_RESAMPLES, "seed": BOOTSTRAP_SEED, "source": "frozen scorer defaults"},
        "inputs": {
            "roles": dict(sorted(roles.items())),
            **_manifest_digests(public, sealed_rows),
            "records_sha256": {
                role: _digest([by_role[role][k] for k in sorted(by_role[role])]) for role in sorted(roles)
            },
        },
        "views": out_views,
        "ids_or_text_in_this_output": False,
    }


def summarize_cell_inputs(result: Mapping[str, Any], *, view: str, role: str, gate: str) -> dict[str, Any]:
    """``report_metrics.summarize_cell`` keywords except the token and USD fields."""
    try:
        entry = result["views"][view]
        cell = entry["roles"][role]["gates"][gate]
    except KeyError:
        raise ScoringBridgeError(f"no scored cell for view {view}, role {role}, gate {gate}") from None
    scored = cell["score"]["views"][view]
    confusion = scored["confusion"]
    fp, tn = _false_alarms(confusion, entry["negative_kind"] == "verified_aligned")
    return {
        "corpus": view,
        "planned_positive": entry["planned_positive"],
        "planned_negative": entry["planned_negative"],
        "confusion": {"TP": confusion["tp"], "FN": confusion["fn"], "FP": fp, "TN": tn},
        "cluster_result": scored,
    }
