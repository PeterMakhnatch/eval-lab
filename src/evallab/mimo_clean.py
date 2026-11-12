"""Canonical @10 clean-chain builder and historical MiMo manifest readers.

The builder accepts only ``mimo-clean-v6``. Historical chain metadata and
manifest paths remain available for receipt readers, not package production.

Deterministic and idempotent: reuse is bound to the parent digest and transform
inputs. Missing records are rederived in isolated scratch storage; matching
shared packages are adopted without moving or rewriting their bytes. Records
use the existing ``library/task-variants/`` schema and materialization APIs.
The v6 builder requires all six Python (five non-Python) canonical edges,
preserves the ledger root's real ancestry, and writes a ``.lineage.json``
sidecar beside the manifest with ordered record references and digest bindings.
New records remain candidates; only identical existing records retain evidence.

Reference fixes prefer ``research/experiments/mimo-reference-fixes/index.csv``
when it exists, else the HAR-191 ``oracle_sweep.csv``. The manifest's
``verify`` column is ``unverified`` for every row until the fleet census
grades the packages.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import re
import tempfile
import tomllib
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evallab import mtime_normalize as _mtime_normalize_mod
from evallab.agent_network_policy import TRANSFORM_ID as AGENT_NETWORK_NONE_ID
from evallab.agent_network_policy import derive_agent_network_none
from evallab.mtime_normalize import MARKER as MTIME_MARKER
from evallab.mtime_normalize import TRANSFORM_ID as MTIME_ID
from evallab.mtime_normalize import derive_mtime_normalize
from evallab.purge_build_caches import MARKER_V4 as CACHE_ACTIVE_MARKER
from evallab.purge_build_caches import MARKER_V5 as CACHE_V5_MARKER
from evallab.purge_build_caches import TRANSFORM_ID_V4 as CACHE_ACTIVE_ID
from evallab.purge_build_caches import TRANSFORM_ID_V5 as CACHE_V5_ID
from evallab.purge_build_caches import derive_purge_build_caches_v4, derive_purge_build_caches_v5
from evallab.purge_installed_copies import MARKER_V2 as PURGE_V2_MARKER
from evallab.purge_installed_copies import (
    SETUP_PYTHON_PATH,
    build_setup_sh_v2,
    derive_purge_installed_copies_v2,
)
from evallab.purge_installed_copies import TRANSFORM_ID as PURGE_ID
from evallab.purge_installed_copies import TRANSFORM_ID_V2 as PURGE_V2_ID
from evallab.registry import harbor_task_digest
from evallab.separate_verifier import TRANSFORM_ID_V6 as SEPARATE_V6_ID
from evallab.separate_verifier import TRANSFORM_ID_V8 as SEPARATE_V8_ID
from evallab.separate_verifier import TRANSFORM_ID_V10 as SEPARATE_V10_ID
from evallab.separate_verifier import (
    build_changes_v10,
    derive_separate_verifier_v10,
)
from evallab.strip_future_history import STRIP_MARKER, derive_strip_future_history
from evallab.strip_future_history import TRANSFORM_ID as STRIP_ID
from evallab.task_variants import (
    MAX_INLINE_FILE_BYTES,
    RECORDS_DIRNAME,
    VariantInvalid,
    VariantRecord,
    lineage_chain,
    materialize,
    resolve_record,
    task_directory_digest,
)

#: Clean-set version id (manifest + job-name namespace).
CLEAN_SET_VERSION = "mimo-clean-v4"

#: Who the builder blames in lineage records.
CREATED_BY = "mimo-clean-v4"

#: Tracked manifest path (repo-relative). v1/v2/v3 stay as history untouched.
MANIFEST_REL = Path("research/experiments/mimo-clean-v4/manifest.csv")

#: Manifest columns (contract: at least task_id, domain, language, chain,
#: final_digest, package_path, reference_fix, status, reason; ``verify`` is
#: the fleet-census grade, ``unverified`` until the census runs).
MANIFEST_COLUMNS = (
    "task_id",
    "domain",
    "language",
    "chain",
    "final_digest",
    "package_path",
    "reference_fix",
    "status",
    "reason",
    "run_digest",
    "oracle_label",
    "verify",
)

#: ``verify`` value for every row until the fleet census grades the packages.
VERIFY_UNVERIFIED = "unverified"

#: Statuses a manifest row can carry.
STATUS_BUILT = "built"
STATUS_SKIPPED = "skipped"

#: Ledger verdicts selected into the clean set.
SELECTED_VERDICTS = frozenset({"keep", "fix"})


#: Active mtime-normalize generation. ``@2`` (Modal lazy-layer
#: materialization fix; Docker-neutral) is preferred once shipped;
#: unlike the pinned cache generation, this remains import-time resolution.
MTIME_ACTIVE_ID: str = getattr(_mtime_normalize_mod, "TRANSFORM_ID_V2", MTIME_ID)
MTIME_ACTIVE_MARKER: str = getattr(_mtime_normalize_mod, "MARKER_V2", MTIME_MARKER)


def _derive_mtime_active(
    parent_dir: Path | str,
    *,
    rationale: str | None = None,
    created_by: str = CREATED_BY,
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive the active mtime-normalize generation for a parent package."""
    derive_v2 = getattr(_mtime_normalize_mod, "derive_mtime_normalize_v2", None)
    derive = derive_v2 if derive_v2 is not None else derive_mtime_normalize
    kwargs: dict[str, Any] = {"created_by": created_by}
    if rationale is not None:
        kwargs["rationale"] = rationale
    if repo_root is not None:
        kwargs["repo_root"] = repo_root
    if parent_source is not None:
        kwargs["parent_source"] = parent_source
    if variants_root is not None:
        kwargs["variants_root"] = variants_root
    return derive(parent_dir, **kwargs)


#: Canonical Python chain. The verifier bundles all clean setup steps;
#: the final network declaration locks only the agent phase.
PYTHON_CHAIN = (
    STRIP_ID,
    PURGE_ID,
    CACHE_ACTIVE_ID,
    MTIME_ACTIVE_ID,
    SEPARATE_V6_ID,
    AGENT_NETWORK_NONE_ID,
)

#: Canonical non-Python chain: snapshot instead of a repairs run package, and
#: no ``purge-installed-copies`` (a Python pip mechanism, noted not derived).
NONPYTHON_CHAIN = (
    STRIP_ID,
    CACHE_ACTIVE_ID,
    MTIME_ACTIVE_ID,
    SEPARATE_V6_ID,
    AGENT_NETWORK_NONE_ID,
)
#: Canonical Python chain for ``mimo-clean-v5``: identical to v4 with
#: ``purge-installed-copies@2`` (all Python tasks; no-ops safely) and
#: ``separate-verifier@8`` in their slots. The cache slot stays
#: ``purge-build-caches@4`` fleet-wide (digest-stability: a uniform @5
#: rebuild churns every setup.sh text and invalidates all banked cells);
#: named node-build tasks fold onto @5 per row (see ``NODE_FOLD_TASKS``).
#: A future rebuild with budget for full re-verification can go uniform @5.
PYTHON_CHAIN_V5 = (
    STRIP_ID,
    PURGE_V2_ID,
    CACHE_ACTIVE_ID,
    MTIME_ACTIVE_ID,
    SEPARATE_V8_ID,
    AGENT_NETWORK_NONE_ID,
)

#: Canonical non-Python chain for ``mimo-clean-v5`` (same @4 slot note).
NONPYTHON_CHAIN_V5 = (
    STRIP_ID,
    CACHE_ACTIVE_ID,
    MTIME_ACTIVE_ID,
    SEPARATE_V8_ID,
    AGENT_NETWORK_NONE_ID,
)

#: Canonical chains for ``mimo-clean-v6``: v5 with ``separate-verifier@10``
#: (binary-artifact strip, runtime integrity gate, junit-only unittest
#: grading). Not the default: adopting it fleet-wide is a full rebuild +
#: re-verification decision for the MiMo lane, with its own budget.
PYTHON_CHAIN_V6 = (
    STRIP_ID,
    PURGE_V2_ID,
    CACHE_ACTIVE_ID,
    MTIME_ACTIVE_ID,
    SEPARATE_V10_ID,
    AGENT_NETWORK_NONE_ID,
)

#: Canonical non-Python chain for ``mimo-clean-v6``.
NONPYTHON_CHAIN_V6 = (
    STRIP_ID,
    CACHE_ACTIVE_ID,
    MTIME_ACTIVE_ID,
    SEPARATE_V10_ID,
    AGENT_NETWORK_NONE_ID,
)

#: Node-build tasks folded onto ``purge-build-caches@5`` inside mimo-clean-v5
#: (per-row chain records @5 honestly; everything else stays @4).
#: REAL: probe-v2-confirmed nested-output leaks (000919 node lib/,
#: 000435/000170 turbo/lerna dist/). AT_RISK: NodeLeak's fix-path-shape
#: class, never image-checked (17 enumerated built tasks; his "21" counts
#: the same lists). 000632/001763 stay on @4: their @5 setups fail closed
#: (coverage-report lib/; rebuild failure — guard evidence, routed to
#: NodeLeak's lane). Receipt: research/experiments/mimo-clean-v5-census/.
NODE_FOLD_REAL = frozenset(
    {
        "format-code-task-000919",
        "format-code-task-000435",
        "format-code-task-000170",
    }
)
NODE_FOLD_AT_RISK = frozenset(
    {
        "format-code-task-000271",
        "format-code-task-000776",
        "format-code-task-000867",
        "format-code-task-001550",
        "format-code-task-001576",
        "format-code-task-001672",
        "format-code-task-001901",
        "format-code-task-000794",
        "format-code-task-000876",
        "format-code-task-000934",
        "format-code-task-001437",
        "format-code-task-001808",
        "format-code-task-001890",
        "format-code-task-001931",
        "format-code-task-001932",
        "format-code-task-001933",
        "format-code-task-001934",
    }
)
NODE_FOLD_TASKS = NODE_FOLD_REAL | NODE_FOLD_AT_RISK

#: Tasks folded onto ``separate-verifier@9`` inside mimo-clean-v5 (per-row
#: chain records @9 honestly; everything else stays @8 for digest-stability:
#: a uniform @9 rebuild churns every test.sh and would invalidate all banked
#: census cells). 001431's working reference fix grades 0 under @8 (G5:
#: class-level collection error recorded with a raw-nodeid classname, no
#: file attr). @9 only ever clears fail-to-pass (never sets it), so folded
#: rows keep every @8 guarantee. A future rebuild with budget for full
#: re-verification can go uniform @9.
SEPARATE_V9_TASKS = frozenset({"format-code-task-001431"})

#: Tasks folded onto ``separate-verifier@10`` inside mimo-clean-v5 (per-row
#: chain records @10 honestly; everything else stays @8/@9 for
#: digest-stability). 000245 (TS) and 000048 (Go) are opaque-wrapper tasks
#: whose hidden patch creates mimo_build_env.tar.gz.b64 /
#: mimo_test_command.sh in the workdir; under RepeatVerifier (repeat_n=3,
#: same verifier container) @9 recaptured those leftovers as PRISTINE and
#: the repeat apply failed "already exists". @10 anchors the first
#: pristine tree and resets to it on every invocation, keeping every @9
#: grading rule (incl. 001431, which stays on @9). A future rebuild with
#: budget for full re-verification can go uniform @10.
SEPARATE_V10_TASKS = frozenset({"format-code-task-000245", "format-code-task-000048"})

#: Chain generations by clean-set version id (v4 stays the default until
#: the v5 census completes; NB: never mix generations in one manifest).
CLEAN_SET_VERSION_V5 = "mimo-clean-v5"
MANIFEST_V5_REL = Path("research/experiments/mimo-clean-v5/manifest.csv")
CLEAN_SET_VERSION_V6 = "mimo-clean-v6"
MANIFEST_V6_REL = Path("research/experiments/mimo-clean-v6/manifest.csv")
CHAIN_VERSIONS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    CLEAN_SET_VERSION: (PYTHON_CHAIN, NONPYTHON_CHAIN),
    CLEAN_SET_VERSION_V5: (PYTHON_CHAIN_V5, NONPYTHON_CHAIN_V5),
    CLEAN_SET_VERSION_V6: (PYTHON_CHAIN_V6, NONPYTHON_CHAIN_V6),
}


def chains_for_version(version: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(python chain, non-python chain) for a clean-set version id."""
    try:
        return CHAIN_VERSIONS[version]
    except KeyError:
        raise VariantInvalid(f"unknown clean-set version: {version!r}") from None


#: Tasks answer-visible through legitimate environment dependencies
#: (installed copies of the fixed project importable at grade time;
#: fix-probe-v2 real-leaks.md) plus the pnpm-blocked node-build task whose
#: fix-path shape cannot be cleared (node-build-leak.md). They stay built
#: (gradable) but are excluded from training; the v5 census marks the env
#: five ``fail:open-leak:env-dependency`` and 000928
#: ``fail:open-leak:node-build-pnpm``.
EXCLUDED_FROM_TRAINING: dict[str, str] = {
    "format-code-task-001478": "answer-visible via installed env dependency",
    "format-code-task-001671": "answer-visible via installed env dependency",
    "format-code-task-001945": "answer-visible via installed env dependency",
    "format-code-task-001971": "answer-visible via installed env dependency",
    "format-code-task-002414": "answer-visible via installed env dependency",
    "format-code-task-000928": "node-build outputs unfixable: pnpm-store leg fails setup before node handling",
}

#: Language plugs for the builder. ``python`` is the ledger pool;
#: every other resolved language takes the non-Python chain (including
#: ``unknown`` snapshot categories: @6 grades those with the exit-code
#: fallback under patch isolation). ``None`` (unresolvable task) has no chain.

#: Ledger layout constants (mirrors :mod:`evallab.exploit_probe`, kept local
#: so this module stays importable without the probe's heavy deps).
LEDGER_REL = Path("research/experiments/python-task-ledger/ledger.csv")
SWEEP_REL = Path("research/experiments/python-task-ledger/oracle_sweep.csv")
#: Reference-fix index (preferred when present; falls back to the sweep).
REFERENCE_INDEX_REL = Path("research/experiments/mimo-reference-fixes/index.csv")
SNAPSHOT_REL = Path("derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6")
VARIANTS_REL = Path("derived/task-store/variants")
HF_SOURCE = {
    "kind": "hf",
    "repo": "FineEnvs/MiMo-V2.6-RL-harbor-code",
    "revision": "5746e2f0c5c61af12d7c5bf15d7efdd77d1f0785",
}


def snapshot_category(task_dir: Path) -> str | None:
    """Lower-cased snapshot ``task.toml`` category, or None when unreadable."""
    toml_path = task_dir / "task.toml"
    try:
        metadata = tomllib.loads(toml_path.read_text(encoding="utf-8")).get("metadata", {})
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return None
    if not isinstance(metadata, dict):
        return None
    category = metadata.get("category", "")
    if not isinstance(category, str) or not category.strip():
        return None
    return category.strip().lower()


def resolve_language(*, ledger_row: bool, category: str | None) -> str | None:
    """Manifest language for a task.

    Ledger rows are ``python`` by definition (the ledger *is* the Python
    pool, e.g. 002361 whose snapshot category is Go but whose graded run
    package is Python). Snapshot tasks pass their category through;
    unresolvable snapshot tasks yield None (no chain).
    """
    if ledger_row:
        return "python"
    return category


#: One-line reasons the purge step is known fail-closed, per
#: ``exploit_probe.PURGE_INAPPLICABLE`` (HAR-194; reproduced in local Docker,
#: setup rc=1). The builder lazy-imports that tuple and only documents it
#: here so unit tests stay free of the probe's dependency tree.
PURGE_FAIL_CLOSED_REASONS = {
    "format-code-task-002552": (
        "poetry project on a python-3.8 image: the offline editable reinstall "
        "cannot use its backend and the link fallback has no worktree source"
    ),
    "format-code-task-000792": "no identifiable project name at the worktree",
    "format-code-task-002139": (
        "bitbake: pip installs a real copy instead of an editable one, "
        "so the purge verify step fails"
    ),
    "format-code-task-002391": (
        "PEP 668 externally-managed image refuses pip and the link fallback "
        "cannot identify the project version"
    ),
    "format-code-task-002486": "no identifiable project name at the worktree",
}

#: ``+def test_*`` lines of a hidden test patch (same shape as the probe's
#: ``new_test_names``; the marker is the first new test name).
_NEW_TEST_RE = re.compile(r"^\+\s*(?:async\s+)?def\s+(test_\w+)", re.MULTILINE)
#: Fallback: first file the hidden test patch touches.
_PATCH_FILE_RE = re.compile(r"^\+\+\+ b/(\S+)", re.MULTILINE)
#: Characters safe inside the probe hook's double-quoted grep pattern.
_MARKER_KEEP_RE = re.compile(r"[^A-Za-z0-9_:./-]+")

ORACLE_PASS_PREFIX = "oracle:pass"


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    """Read a CSV ledger into row dicts (empty strings, never None)."""
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def run_package_rel(row: Mapping[str, str]) -> Path:
    """Repo-relative path of the package the ledger runs for one row."""
    if row["run"] == "original":
        return SNAPSHOT_REL / "tasks" / row["task_id"]
    slug = f"mimo-v2.6-rl__{row['task_id']}"
    return VARIANTS_REL / slug / row["run_digest"].removeprefix("sha256:")[:12]


def variant_source(task_id: str, digest: str, row: Mapping[str, str]) -> dict[str, Any]:
    """Lineage ``parent_source`` for one chain derivation step."""
    if digest == row["run_digest"] and row["run"] == "original":
        return {**HF_SOURCE, "path": f"tasks/{task_id}"}
    slug = f"mimo-v2.6-rl__{task_id}"
    return {
        "kind": "variant",
        "record": (
            f"{RECORDS_DIRNAME.as_posix()}/{slug}/{digest.removeprefix('sha256:')[:12]}.json"
        ),
    }


def setup_carries(package: Path, marker: str) -> bool:
    """Whether the package setup already applies the marked transform."""
    setup = package / "environment" / "setup" / "setup.sh"
    return marker in setup.read_text(errors="replace") if setup.is_file() else False


def derive_marker(patch_text: str, task_id: str) -> str:
    """Hidden-test probe marker derived from the hidden test patch.

    First added ``test_*`` function name; else the first touched file's
    basename; else the task id. Sanitized for the probe hook's
    double-quoted grep pattern.
    """
    candidate: str | None = None
    match = _NEW_TEST_RE.search(patch_text)
    if match is not None:
        candidate = match.group(1)
    else:
        touched = _PATCH_FILE_RE.search(patch_text)
        if touched is not None:
            candidate = Path(touched.group(1)).name
    if not candidate:
        candidate = task_id
    marker = _MARKER_KEEP_RE.sub("", candidate)[:120]
    return marker or task_id


def build_solution_sh(
    *,
    task_id: str,
    label: str,
    fix_commit: str,
    patch_bytes: bytes,
) -> bytes:
    """Reference ``solution/solve.sh`` applying an oracle-pass patch.

    The patch embeds base64 (shell-safe for any diff content); the script
    resolves the agent worktree repo root itself and applies with
    ``git apply``. Deterministic in the patch bytes.
    """
    encoded = base64.b64encode(patch_bytes).decode("ascii")
    script = (
        "#!/bin/bash\n"
        f"# {CREATED_BY} oracle reference fix for {task_id} ({label} {fix_commit}).\n"
        "# Applies the HAR-191 oracle-pass patch to the agent worktree repo.\n"
        "set -euo pipefail\n"
        "ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)\n"
        f'echo "{encoded}" | base64 -d | git -C "$ROOT" apply --whitespace=nowarn -v\n'
    )
    payload = script.encode("utf-8")
    if len(payload) > MAX_INLINE_FILE_BYTES:
        raise VariantInvalid(
            f"oracle solution for {task_id} exceeds the inline limit "
            f"({len(payload)} > {MAX_INLINE_FILE_BYTES} bytes)"
        )
    return payload


@dataclass
class OracleInfo:
    """An oracle-pass reference fix resolved to patch bytes."""

    label: str
    fix_commit: str
    patch_path: str
    patch_bytes: bytes


def load_reference_index(index_path: Path) -> dict[str, dict[str, str]]:
    """Index a mimo-reference-fixes ``index.csv`` by task id.

    Columns: ``task_id,label,fix_commit,patch_path,patch_sha256,source``;
    ``patch_path`` is absolute (patch bytes live outside the repo) and only
    oracle-pass rows carry one.
    """
    out: dict[str, dict[str, str]] = {}
    for row in read_csv_rows(index_path):
        out[row["task_id"]] = {
            "label": row.get("label", ""),
            "fix_commit": row.get("fix_commit", ""),
            "patch_path": row.get("patch_path", ""),
            "patch_sha256": row.get("patch_sha256", ""),
            "source": row.get("source", ""),
        }
    return out


def oracle_info_from_index(
    task_id: str,
    index: Mapping[str, Mapping[str, str]],
    *,
    repo_root: Path,
) -> OracleInfo | None:
    """The oracle-pass reference fix from a reference index, or None.

    Returns None unless the row's label is an oracle pass, ``patch_path``
    names an existing file (absolute, else repo-relative), and the bytes
    match ``patch_sha256`` when one is recorded.
    """
    entry = index.get(task_id)
    if entry is None:
        return None
    label = entry.get("label", "")
    if not label.startswith(ORACLE_PASS_PREFIX):
        return None
    raw_path = entry.get("patch_path", "")
    if not raw_path:
        return None
    patch = Path(raw_path)
    if not patch.is_absolute():
        patch = repo_root / patch
    if not patch.is_file():
        return None
    patch_bytes = patch.read_bytes()
    expected = entry.get("patch_sha256", "")
    if expected and hashlib.sha256(patch_bytes).hexdigest() != expected.removeprefix("sha256:"):
        return None
    return OracleInfo(
        label=label,
        fix_commit=entry.get("fix_commit", ""),
        patch_path=str(patch),
        patch_bytes=patch_bytes,
    )


@dataclass
class ChainResult:
    """One manifest row's build outcome."""

    task_id: str
    status: str
    chain: list[str] = field(default_factory=list)
    final_digest: str = ""
    package_path: str = ""
    reference_fix: str = "none"
    reason: str = ""
    run_digest: str = ""
    oracle_label: str = ""
    language: str = ""
    verify: str = VERIFY_UNVERIFIED
    reused: bool = True
    lineage: dict[str, Any] = field(default_factory=dict)

    def manifest_row(self) -> dict[str, str]:
        return {
            "task_id": self.task_id,
            "domain": "code",
            "language": self.language,
            "chain": ">".join(self.chain),
            "final_digest": self.final_digest,
            "package_path": self.package_path,
            "reference_fix": self.reference_fix,
            "status": self.status,
            "reason": self.reason,
            "run_digest": self.run_digest,
            "oracle_label": self.oracle_label,
            "verify": self.verify,
        }


class ChainBuilder:
    """Derives the clean chain per task with per-task record reuse."""

    def __init__(
        self,
        repo_root: Path,
        primary: Path,
        *,
        variants_root: Path | None = None,
        created_by: str = CLEAN_SET_VERSION_V6,
        snapshot_root: Path | None = None,
        clean_version: str = CLEAN_SET_VERSION_V6,
    ) -> None:
        if clean_version != CLEAN_SET_VERSION_V6:
            raise VariantInvalid(f"retired clean-set producer: {clean_version!r}; use mimo-clean-v6")
        self.repo_root = repo_root
        self.primary = primary
        self.variants_root = variants_root
        self.created_by = created_by
        self.snapshot_root = snapshot_root or primary / SNAPSHOT_REL / "tasks"
        self._records: dict[str, dict[tuple[str, str], dict[str, VariantRecord]]] = {}
        self._rederivation_proofs: dict[str, dict[str, str]] = {}
        self.clean_version = clean_version
        self._chains = chains_for_version(clean_version)

    # -- record reuse -------------------------------------------------- #

    def _slug_records(self, task_id: str) -> None:
        self._records[task_id] = {}
        slug = f"mimo-v2.6-rl__{task_id}"
        records_dir = self.repo_root / RECORDS_DIRNAME / slug
        if not records_dir.is_dir():
            return
        for path in sorted(records_dir.glob("*.json")):
            try:
                record = resolve_record(path, repo_root=self.repo_root)
            except ValueError:
                continue
            if self._is_stale_snapshot(task_id, record):
                continue
            self._remember(task_id, record)

    @staticmethod
    def _matches(
        record: VariantRecord,
        expected_inputs: Mapping[str, Any] | None,
        parent_source: Mapping[str, Any] | None,
    ) -> bool:
        return (
            expected_inputs is None
            or all(key in record.inputs and record.inputs[key] == value for key, value in expected_inputs.items())
        ) and (
            parent_source is None
            or record.parent.source.model_dump(exclude_none=True)
            == {key: value for key, value in parent_source.items() if value is not None}
        )

    def _lookup(
        self,
        task_id: str,
        transform: str,
        parent_digest: str,
        *,
        expected_inputs: Mapping[str, Any] | None = None,
        parent_source: Mapping[str, Any] | None = None,
    ) -> VariantRecord | None:
        self._slug_records(task_id)
        records = self._records.get(task_id, {}).get((transform, parent_digest), {})
        matching = [
            record
            for record in records.values()
            if self._matches(record, expected_inputs, parent_source)
            and not self._is_stale_snapshot(task_id, record)
        ]
        # Multiple matching records are not ordered by digest filename.
        # Re-run the actual transform to select its exact output instead.
        return matching[0] if len(matching) == 1 else None

    def _remember(self, task_id: str, record: VariantRecord) -> None:
        records = self._records.setdefault(task_id, {}).setdefault(
            (record.transform, record.parent.digest), {}
        )
        records[record.variant_digest] = record

    def _derive_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "repo_root": self.repo_root,
            "created_by": self.created_by,
        }
        if self.variants_root is not None:
            kwargs["variants_root"] = self.variants_root
        return kwargs

    def _variants_dir(self) -> Path:
        if self.variants_root is not None:
            return Path(self.variants_root)
        from evallab.task_variants import default_variants_root

        return default_variants_root(self.repo_root)

    #: ``task.toml`` substring proving a package carries the post-#848
    #: snapshot collect hook (``git -C "$CWD" ls-files -c -o
    #: --exclude-standard`` instead of a full worktree tar). Pre-#848
    #: packages snapshot ``__pycache__`` bytes the current grader never
    #: sees (``pyc_shadow``); they are stale.
    _SNAPSHOT_HOOK_MARKER = "ls-files -c -o --exclude-standard"

    @staticmethod
    def _needs_snapshot_hook(transform: str) -> bool:
        """Whether ``transform`` packages must carry the #848 hook.

        ``separate-verifier@2``+ writes the hook into ``task.toml`` and
        ``agent-network-none@1`` only rewrites that file, so both must
        show it; ``separate-verifier@1`` keeps its full-worktree tar.
        """
        if transform == AGENT_NETWORK_NONE_ID:
            return True
        if not transform.startswith("separate-verifier@"):
            return False
        try:
            return int(transform.rpartition("@")[2]) >= 2
        except ValueError:
            return False

    def _is_stale_snapshot(self, task_id: str, record: VariantRecord) -> bool:
        """Whether the record's on-disk package predates #848."""
        if not self._needs_snapshot_hook(record.transform):
            return False
        package = (
            self._variants_dir()
            / f"mimo-v2.6-rl__{task_id}"
            / record.variant_digest.removeprefix("sha256:")[:12]
        )
        if not package.is_dir():
            return False
        toml = package / "task.toml"
        try:
            text = toml.read_text(errors="replace")
        except OSError:
            return True
        return self._SNAPSHOT_HOOK_MARKER not in text

    def _materialized(self, task_id: str, record: VariantRecord) -> Path | None:
        """The record's package dir when present with a matching digest."""
        package = (
            self._variants_dir()
            / f"mimo-v2.6-rl__{task_id}"
            / record.variant_digest.removeprefix("sha256:")[:12]
        )
        if package.is_dir() and task_directory_digest(package) == record.variant_digest:
            # Pre-#848 cached packages are stale even though their digest
            # still matches their (old) lineage record: force re-derivation
            # with current code. The stale <digest12> dir stays on disk so
            # historical receipts remain valid.
            if self._is_stale_snapshot(task_id, record):
                return None
            return package
        return None

    @staticmethod
    def _identity(record: VariantRecord) -> dict[str, Any]:
        identity = record.model_dump(
            mode="json",
            by_alias=True,
            exclude={"created_at", "created_by", "rationale", "status", "evidence"},
        )
        identity["files"] = sorted(identity["files"], key=lambda change: change["path"])
        identity["components_changed"] = sorted(identity["components_changed"])
        return identity

    def _rederived_equivalent(self, existing: VariantRecord, derived: VariantRecord) -> bool:
        """Allow absent v10 diagnostics only after real, byte-identical rederivation."""
        if (
            derived.transform != SEPARATE_V10_ID
            or derived.inputs.get("setup_python_path") != ""
            or set(derived.inputs).difference(existing.inputs)
            - {"setup_python_path", "wrapper_sha256"}
            or any(
                existing.inputs[key] != derived.inputs[key]
                for key in existing.inputs.keys() & derived.inputs.keys()
            )
        ):
            return False
        old_identity = self._identity(existing)
        new_identity = self._identity(derived)
        old_identity.pop("inputs")
        new_identity.pop("inputs")
        return old_identity == new_identity

    def _validate_edge(self, record: VariantRecord, parent: Path) -> None:
        """Prove replay and both digest namespaces without touching shared bytes."""
        if task_directory_digest(parent) != record.parent.digest:
            raise VariantInvalid(f"{record.transform}: parent package digest mismatch")
        if harbor_task_digest(parent) != record.parent.harbor_digest:
            raise VariantInvalid(f"{record.transform}: parent Harbor digest mismatch")
        with tempfile.TemporaryDirectory(prefix="evallab-mimo-replay-") as scratch:
            package = materialize(
                record,
                parent,
                repo_root=self.repo_root,
                variants_root=Path(scratch),
            )
            if harbor_task_digest(package) != record.variant_harbor_digest:
                raise VariantInvalid(f"{record.transform}: child Harbor digest mismatch")

    def _derive_isolated(
        self,
        parent: Path,
        derive,
        parent_source: Mapping[str, Any],
        derive_kwargs: Mapping[str, Any],
    ) -> VariantRecord:
        with tempfile.TemporaryDirectory(prefix="evallab-mimo-derive-") as scratch:
            kwargs = self._derive_kwargs()
            kwargs.update(derive_kwargs)
            kwargs.update(repo_root=Path(scratch), variants_root=Path(scratch) / "variants")
            return derive(parent, parent_source=dict(parent_source), **kwargs)

    def _adopt_record(self, task_id: str, record: VariantRecord, parent: Path) -> VariantRecord:
        """Publish a real derivation, never replace an existing record or package."""
        path = self.repo_root / record.record_relpath()

        def existing() -> VariantRecord:
            found = resolve_record(path, repo_root=self.repo_root)
            if self._identity(found) != self._identity(record):
                if not self._rederived_equivalent(found, record):
                    raise VariantInvalid(
                        f"{record.transform}: existing record has different inputs or provenance: "
                        f"{record.record_relpath()}"
                    )
                self._rederivation_proofs[found.record_relpath().as_posix()] = {
                    "identity_sha256": f"sha256:{hashlib.sha256(json.dumps(self._identity(record), sort_keys=True, separators=(',', ':')).encode()).hexdigest()}",
                    "setup_python_path": record.inputs["setup_python_path"],
                    "wrapper_sha256": record.inputs["wrapper_sha256"],
                }
            return found

        if path.exists():
            record = existing()
        self._validate_edge(record, parent)
        package = materialize(
            record, parent, repo_root=self.repo_root, variants_root=self.variants_root
        )
        if harbor_task_digest(package) != record.variant_harbor_digest:
            raise VariantInvalid(f"{record.transform}: cached child Harbor digest mismatch")
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    json.dump(record.model_dump(mode="json", by_alias=True), handle, indent=2, ensure_ascii=False)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    record = existing()
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        else:
            record = existing()
        self._remember(task_id, record)
        return record

    def find_or_derive(
        self,
        task_id: str,
        transform: str,
        parent: Path,
        parent_digest: str,
        derive,
        *,
        parent_source: Mapping[str, Any],
        expected_inputs: Mapping[str, Any] | None = None,
        **derive_kwargs: Any,
    ) -> tuple[VariantRecord, bool]:
        """Select matching inputs before reuse; derive missing edges in isolation.

        In particular, a shared orphan package cannot force a wrong-input
        record through a ``VariantExistsError`` fallback. ``materialize``
        adopts digest-identical cached bytes without moving or rewriting them.
        """
        if task_directory_digest(parent) != parent_digest:
            raise VariantInvalid(f"{transform}: requested parent digest does not match package")
        record = self._lookup(
            task_id,
            transform,
            parent_digest,
            expected_inputs=expected_inputs,
            parent_source=parent_source,
        )
        if record is not None:
            return self._adopt_record(task_id, record, parent), True
        record = self._derive_isolated(parent, derive, parent_source, derive_kwargs)
        if (
            record.task_name != f"mimo-v2.6-rl/{task_id}"
            or record.transform != transform
            or record.parent.digest != parent_digest
            or not self._matches(record, expected_inputs, parent_source)
        ):
            raise VariantInvalid(f"{transform}: derivation did not bind requested task, parent and inputs")
        return self._adopt_record(task_id, record, parent), False

    # -- chain ---------------------------------------------------------- #

    def _source(
        self,
        task_id: str,
        digest: str,
        start_digest: str,
        row: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        """Lineage ``parent_source`` for one chain derivation step."""
        if row is not None:
            return variant_source(task_id, digest, row)
        if digest == start_digest:
            return {**HF_SOURCE, "path": f"tasks/{task_id}"}
        slug = f"mimo-v2.6-rl__{task_id}"
        return {
            "kind": "variant",
            "record": (
                f"{RECORDS_DIRNAME.as_posix()}/{slug}/{digest.removeprefix('sha256:')[:12]}.json"
            ),
        }


    @staticmethod
    def _carries_v6(package: Path, transform: str) -> bool:
        marker = {
            STRIP_ID: STRIP_MARKER,
            PURGE_V2_ID: PURGE_V2_MARKER,
            CACHE_ACTIVE_ID: CACHE_ACTIVE_MARKER,
            CACHE_V5_ID: CACHE_V5_MARKER,
            MTIME_ACTIVE_ID: MTIME_ACTIVE_MARKER,
        }.get(transform)
        if marker is not None:
            return setup_carries(package, marker)
        config = tomllib.loads((package / "task.toml").read_text(encoding="utf-8"))
        if transform == SEPARATE_V10_ID:
            return config.get("verifier", {}).get("environment_mode") == "separate"
        return config.get("agent", {}).get("network_mode") == "no-network"

    def _v6_parameters(
        self,
        task_id: str,
        transform: str,
        parent: Path,
        row: Mapping[str, str] | None,
        oracle: OracleInfo | None,
    ) -> tuple[Any, Mapping[str, Any] | None, dict[str, Any]]:
        if transform == PURGE_V2_ID:
            if row is None:
                raise VariantInvalid("purge-installed-copies@2 requires a Python ledger root")
            setup, _ = build_setup_sh_v2(
                (parent / "environment/setup/setup.sh").read_text(encoding="utf-8")
            )
            return (
                derive_purge_installed_copies_v2,
                {
                    "setup_python_path": SETUP_PYTHON_PATH,
                    "repairs_digest": row["run_digest"],
                    "setup_after_sha256": f"sha256:{hashlib.sha256(setup.encode()).hexdigest()}",
                },
                {"repairs_digest": row["run_digest"]},
            )
        if transform == SEPARATE_V10_ID:
            patch = parent / "tests" / "test.patch"
            marker = derive_marker(patch.read_text(errors="replace") if patch.is_file() else "", task_id)
            solution = (
                build_solution_sh(
                    task_id=task_id,
                    label=oracle.label,
                    fix_commit=oracle.fix_commit,
                    patch_bytes=oracle.patch_bytes,
                )
                if oracle is not None and not (parent / "solution" / "solve.sh").is_file()
                else None
            )
            # The transform itself detects the bundled setup and renders the
            # actual wrapper, including the Python pin only for purge@2.
            _changes, inputs = build_changes_v10(parent, marker=marker, solution_sh=solution)
            return derive_separate_verifier_v10, inputs, {"marker": marker, "solution_sh": solution}
        return (
            {
                STRIP_ID: derive_strip_future_history,
                CACHE_ACTIVE_ID: derive_purge_build_caches_v4,
                CACHE_V5_ID: derive_purge_build_caches_v5,
                MTIME_ACTIVE_ID: _derive_mtime_active,
                AGENT_NETWORK_NONE_ID: derive_agent_network_none,
            }[transform],
            None,
            {},
        )

    def _snapshot_package(self, task_id: str, row: Mapping[str, str] | None) -> Path:
        return (
            self.primary / SNAPSHOT_REL / "tasks" / task_id
            if row is not None
            else self.snapshot_root / task_id
        )

    def _restore_canonical_ancestor(
        self,
        task_id: str,
        digest: str,
        record_path: str,
        chain: tuple[str, ...],
        row: Mapping[str, str] | None,
        oracle: OracleInfo | None,
    ) -> None:
        """Recover only an exact actual transform, never infer an edge from a marker."""
        expected_path = RECORDS_DIRNAME / f"mimo-v2.6-rl__{task_id}" / f"{digest.removeprefix('sha256:')[:12]}.json"
        if Path(record_path) != expected_path:
            raise VariantInvalid(f"noncanonical missing ancestor record path: {record_path}")
        if (self.repo_root / expected_path).exists():
            raise VariantInvalid(f"invalid ancestor record cannot be replaced: {record_path}")
        candidates: dict[str, tuple[Path, dict[str, Any]]] = {}
        original = self._snapshot_package(task_id, row)
        if original.is_dir():
            candidates[task_directory_digest(original)] = (
                original, {**HF_SOURCE, "path": f"tasks/{task_id}"}
            )
        records_dir = self.repo_root / RECORDS_DIRNAME / f"mimo-v2.6-rl__{task_id}"
        for path in sorted(records_dir.glob("*.json")):
            try:
                record = resolve_record(path, repo_root=self.repo_root)
            except ValueError:
                continue
            package = self._materialized(task_id, record)
            if package is None and all(
                step.parent_found for step in lineage_chain(record, repo_root=self.repo_root)
            ):
                try:
                    self._root_records(
                        task_id, record.variant_digest,
                        {"kind": "variant", "record": record.record_relpath().as_posix()},
                        chain, row, oracle,
                    )
                except (OSError, ValueError):
                    continue
                package = self._materialized(task_id, record)
            if package is not None and record.variant_digest != digest:
                candidates[record.variant_digest] = (
                    package, {"kind": "variant", "record": record.record_relpath().as_posix()}
                )
        matches: dict[str, tuple[VariantRecord, Path]] = {}
        for _parent_digest, (parent, source) in sorted(candidates.items()):
            for index, transform in enumerate(chain):
                if self._carries_v6(parent, transform) or not all(
                    self._carries_v6(parent, previous) for previous in chain[:index]
                ):
                    continue
                try:
                    derive, inputs, kwargs = self._v6_parameters(task_id, transform, parent, row, oracle)
                    record = self._derive_isolated(parent, derive, source, kwargs)
                except ValueError:
                    continue
                if (
                    record.variant_digest == digest
                    and record.record_relpath() == expected_path
                    and record.task_name == f"mimo-v2.6-rl/{task_id}"
                    and record.transform == transform
                    and self._matches(record, inputs, source)
                ):
                    identity = json.dumps(self._identity(record), sort_keys=True)
                    matches[identity] = (record, parent)
        if len(matches) != 1:
            raise VariantInvalid(
                f"missing ancestor {record_path}: exact canonical rederivation "
                f"found {len(matches)} matching identities"
            )
        record, parent = next(iter(matches.values()))
        self._adopt_record(task_id, record, parent)

    def _root_records(
        self,
        task_id: str,
        root_digest: str,
        source: Mapping[str, Any],
        chain: tuple[str, ...],
        row: Mapping[str, str] | None,
        oracle: OracleInfo | None,
    ) -> tuple[list[VariantRecord], dict[str, Path]]:
        if source["kind"] != "variant":
            return [], {}
        root_path = str(source["record"])
        for _attempt in range(64):
            if not (self.repo_root / root_path).is_file():
                self._restore_canonical_ancestor(task_id, root_digest, root_path, chain, row, oracle)
            root_record = resolve_record(root_path, repo_root=self.repo_root)
            if root_record.variant_digest != root_digest:
                raise VariantInvalid(f"ledger root record digest mismatch: {root_path}")
            steps = lineage_chain(root_record, repo_root=self.repo_root)
            broken = next((step for step in steps if not step.parent_found), None)
            if broken is None:
                break
            missing = broken.record.parent.source
            if missing.kind != "variant" or missing.record is None:
                raise VariantInvalid(broken.parent_error or "invalid root ancestry")
            self._restore_canonical_ancestor(
                task_id, broken.record.parent.digest, missing.record, chain, row, oracle
            )
        else:
            raise VariantInvalid("root ancestry exceeds 64 records")
        records = [step.record for step in reversed(steps)]
        first_source = records[0].parent.source
        if first_source.kind == "hf":
            if first_source.model_dump(exclude_none=True) != {**HF_SOURCE, "path": f"tasks/{task_id}"}:
                raise VariantInvalid("root ancestry does not terminate at the pinned MiMo snapshot")
            current = self._snapshot_package(task_id, row)
        elif first_source.kind == "local" and first_source.path:
            current = _resolve(self.repo_root, Path(first_source.path))
        else:
            raise VariantInvalid("root ancestry has no resolvable original package")
        parents: dict[str, Path] = {}
        previous: VariantRecord | None = None
        for record in records:
            if record.task_name != f"mimo-v2.6-rl/{task_id}":
                raise VariantInvalid("root ancestry crosses task identities")
            if previous is not None and (
                record.parent.source.record != previous.record_relpath().as_posix()
                or record.parent.digest != previous.variant_digest
                or record.parent.harbor_digest != previous.variant_harbor_digest
            ):
                raise VariantInvalid(f"root ancestry parent binding mismatch: {record.record_relpath()}")
            self._validate_edge(record, current)
            parents[record.variant_digest] = current
            current = materialize(
                record, current, repo_root=self.repo_root, variants_root=self.variants_root
            )
            if harbor_task_digest(current) != record.variant_harbor_digest:
                raise VariantInvalid(f"root ancestry child Harbor digest mismatch: {record.record_relpath()}")
            previous = record
        return records, parents

    def _record_proof(self, record: VariantRecord) -> dict[str, Any]:
        path = record.record_relpath()
        proof: dict[str, Any] = {
            "record": path.as_posix(),
            "record_sha256": f"sha256:{hashlib.sha256((self.repo_root / path).read_bytes()).hexdigest()}",
            "transform": record.transform,
            "parent_digest": record.parent.digest,
            "parent_harbor_digest": record.parent.harbor_digest,
            "variant_digest": record.variant_digest,
            "variant_harbor_digest": record.variant_harbor_digest,
            "variant_status": record.status,
        }
        if path.as_posix() in self._rederivation_proofs:
            proof["rederivation"] = self._rederivation_proofs[path.as_posix()]
        return proof

    def _build_v6_chain(
        self,
        result: ChainResult,
        package: Path,
        root_digest: str,
        root_source: Mapping[str, Any],
        chain: tuple[str, ...],
        row: Mapping[str, str] | None,
        oracle: OracleInfo | None,
    ) -> ChainResult:
        if result.language == "python" and row is None:
            result.reason = "python snapshot task without a ledger row"
            return result
        chain = tuple(
            CACHE_V5_ID if transform == CACHE_ACTIVE_ID and result.task_id in NODE_FOLD_TASKS else transform
            for transform in chain
        )
        current = package
        current_digest = root_digest
        source = dict(root_source)
        transform = "root ancestry"
        canonical: list[VariantRecord] = []
        try:
            if task_directory_digest(package) != root_digest:
                raise VariantInvalid("ledger run digest does not match root package bytes")
            root_harbor_digest = harbor_task_digest(package)
            records, parents = self._root_records(
                result.task_id, root_digest, source, chain, row, oracle
            )
            previous_position = -1
            for transform in chain:
                if self._carries_v6(current, transform):
                    matched = None
                    for position, candidate in enumerate(records):
                        if position <= previous_position or candidate.transform != transform:
                            continue
                        parameter_row = row
                        if transform == PURGE_V2_ID and row is not None:
                            # A carried edge retains its actual original
                            # repairs input, not a newly invented provenance.
                            parameter_row = {
                                **row, "run_digest": candidate.inputs.get("repairs_digest", ""),
                            }
                        derive, inputs, kwargs = self._v6_parameters(
                            result.task_id, transform, parents[candidate.variant_digest], parameter_row, oracle
                        )
                        matches = self._matches(candidate, inputs, None)
                        if not matches and transform == SEPARATE_V10_ID:
                            derived = self._derive_isolated(
                                parents[candidate.variant_digest],
                                derive,
                                candidate.parent.source.model_dump(exclude_none=True),
                                kwargs,
                            )
                            if self._rederived_equivalent(candidate, derived):
                                self._adopt_record(
                                    result.task_id, derived, parents[candidate.variant_digest]
                                )
                                matches = True
                        if matches:
                            matched = (position, candidate)
                    if matched is None:
                        raise VariantInvalid(
                            "setup/config carries the step without an ordered, input-matching "
                            "canonical ancestor record"
                        )
                    previous_position, record = matched
                else:
                    derive, inputs, kwargs = self._v6_parameters(
                        result.task_id, transform, current, row, oracle
                    )
                    record, reused = self.find_or_derive(
                        result.task_id, transform, current, current_digest, derive,
                        parent_source=source, expected_inputs=inputs, **kwargs,
                    )
                    result.reused = result.reused and reused
                    parents[record.variant_digest] = current
                    records.append(record)
                    previous_position = len(records) - 1
                    current = self._variants_dir() / record.task_slug / record.digest12
                    current_digest = record.variant_digest
                    source = {"kind": "variant", "record": record.record_relpath().as_posix()}
                canonical.append(record)
                result.chain.append(transform)
            transform = "complete lineage"
            steps = lineage_chain(records[-1], repo_root=self.repo_root)
            if any(not step.parent_found for step in steps) or [
                step.record.variant_digest for step in reversed(steps)
            ] != [record.variant_digest for record in records]:
                raise VariantInvalid("final record does not replay through the declared root ancestry")
            bindings = {records[0].parent.digest: records[0].parent.harbor_digest}
            bindings.update((record.variant_digest, record.variant_harbor_digest) for record in records)
            if bindings.get(root_digest) != root_harbor_digest or result.chain != list(chain):
                raise VariantInvalid("canonical chain is incomplete or not bound to the ledger/snapshot root")
            if task_directory_digest(current) != current_digest or harbor_task_digest(current) != records[-1].variant_harbor_digest:
                raise VariantInvalid("final package digest binding changed during build")
            result.lineage = {
                "task_id": result.task_id,
                "root": {
                    "digest": root_digest,
                    "harbor_digest": root_harbor_digest,
                    "source": dict(root_source),
                    "package_path": (
                        package.relative_to(self.primary) if package.is_relative_to(self.primary) else package
                    ).as_posix(),
                },
                "final_digest": current_digest,
                "final_harbor_digest": records[-1].variant_harbor_digest,
                "canonical_records": [record.record_relpath().as_posix() for record in canonical],
                "records": [self._record_proof(record) for record in records],
            }
        except (OSError, ValueError) as exc:
            result.reason = f"{transform} failed: {exc}"
            return result
        result.status = STATUS_BUILT
        result.final_digest = current_digest
        result.package_path = (VARIANTS_REL / records[-1].task_slug / records[-1].digest12).as_posix()
        result.reason = "clean chain complete; canonical records bound to ledger/snapshot root"
        if row is None:
            result.reason += f"; purge-installed-copies n/a to {result.language}: Python pip mechanism"
        if result.task_id in EXCLUDED_FROM_TRAINING:
            result.reason += f"; excluded-from-training: {EXCLUDED_FROM_TRAINING[result.task_id]}"
        return result

    def build_task(
        self,
        task_id: str,
        row: Mapping[str, str] | None = None,
        *,
        oracle: OracleInfo | None = None,
    ) -> ChainResult:
        """Derive the clean chain for one task.

        ``row`` is a python-task-ledger row (Python chain from the repairs
        run package); None starts from the snapshot task dir (non-Python
        chain). Ledger membership always means the Python pool, whatever
        the snapshot category says.
        """
        if row is not None:
            language: str | None = "python"
        else:
            language = resolve_language(
                ledger_row=False,
                category=snapshot_category(self.snapshot_root / task_id),
            )
        result = ChainResult(
            task_id=task_id,
            status=STATUS_SKIPPED,
            language=language or "",
            run_digest=row.get("run_digest", "") if row is not None else "",
            oracle_label=oracle.label if oracle is not None else "",
            reference_fix=oracle.patch_path if oracle is not None else "none",
        )
        python_chain, nonpython_chain = self._chains
        if language == "python":
            chain = python_chain
        elif language is None:
            chain = None
        else:
            chain = nonpython_chain
        if chain is None:
            result.reason = (
                f"no clean chain for language {language!r}: snapshot task.toml category unreadable"
            )
            return result

        if row is not None:
            try:
                package = self.primary / run_package_rel(row)
            except KeyError as exc:
                result.reason = f"ledger row is missing {exc}"
                return result
            if not row.get("run_digest"):
                result.reason = "ledger row has no run_digest"
                return result
            if not package.is_dir():
                result.reason = (
                    f"run package missing: {package.as_posix()} "
                    f"(run={row.get('run')} digest={row.get('run_digest')})"
                )
                return result
            current = package
            current_digest = row["run_digest"]
        else:
            package = self.snapshot_root / task_id
            if not package.is_dir():
                result.reason = f"snapshot package missing: {package.as_posix()}"
                return result
            current = package
            current_digest = task_directory_digest(package)
            result.run_digest = current_digest
        start_digest = current_digest
        source = self._source(task_id, current_digest, start_digest, row)
        return self._build_v6_chain(
            result, current, current_digest, source, chain, row, oracle
        )


def write_manifest(rows: Collection[Mapping[str, str]], path: Path) -> Path:
    """Write the tracked manifest (sorted by task id, deterministic)."""
    ordered = sorted(rows, key=lambda row: row["task_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(MANIFEST_COLUMNS))
        writer.writeheader()
        for row in ordered:
            writer.writerow({key: row.get(key, "") for key in MANIFEST_COLUMNS})
    return path


def write_lineage_index(results: Collection[ChainResult], manifest_path: Path) -> Path:
    """Emit v6 record references, not another copy of the variant ledger."""
    built = sorted(
        (result for result in results if result.status == STATUS_BUILT),
        key=lambda result: result.task_id,
    )
    if any(not result.lineage for result in built):
        raise VariantInvalid("built v6 task is missing its canonical lineage binding")
    path = manifest_path.with_suffix(".lineage.json")
    payload = {
        "schema": "evallab.mimo_clean_lineage/v1",
        "clean_version": CLEAN_SET_VERSION_V6,
        "manifest": manifest_path.name,
        "manifest_sha256": f"sha256:{hashlib.sha256(manifest_path.read_bytes()).hexdigest()}",
        "tasks": [result.lineage for result in built],
    }
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def load_manifest(path: Path) -> list[dict[str, str]]:
    """Read a manifest back (row dicts keyed by MANIFEST_COLUMNS)."""
    return read_csv_rows(path)


def summarize_trials(job_dir: Path) -> list[float | None]:
    """Mean verifier reward per trial under a run job dir (sorted)."""
    from evallab.cheat import trial_reward

    rewards: list[float | None] = []
    if not job_dir.is_dir():
        return rewards
    for trial in sorted(
        candidate
        for candidate in job_dir.iterdir()
        if candidate.is_dir() and (candidate / "result.json").is_file()
    ):
        try:
            payload = json.loads((trial / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rewards.append(None)
            continue
        rewards.append(trial_reward(payload))
    return rewards


def acceptance_pass(
    *,
    oracle_rewards: Collection[float | None],
    has_reference_fix: bool,
    nop_rewards: Collection[float | None],
    cracked: int,
) -> bool:
    """Per-task acceptance: oracle 1, nop 0, cheat clean.

    Tasks without a reference fix skip the oracle cell (vacuous pass).
    """
    oracle_ok = any(reward == 1 for reward in oracle_rewards) if has_reference_fix else True
    nop_ok = bool(nop_rewards) and all(reward == 0 for reward in nop_rewards)
    return oracle_ok and nop_ok and cracked == 0


# --------------------------------------------------------------------------- #
# CLI: ``evallab mimo-clean build|verify-local``
# --------------------------------------------------------------------------- #


def _resolve(root: Path, value: Path) -> Path:
    return value if value.is_absolute() else (root / value)


