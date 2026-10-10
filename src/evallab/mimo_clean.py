"""mimo-clean-v3: canonical clean chain for the MiMo code pool (all languages).

Builds one clean package per code task:

- Python tasks (the python-task-ledger ``keep``/``fix`` rows; ``discard`` rows
  are skipped with reason): ledger run package (repairs) ->
  ``strip-future-history@1`` -> ``purge-installed-copies@1`` (CONFIRMED_PURGE
  targets and run packages that already carry the marker; fail-closed tasks
  skip with reason, everything else skips per the HAR-194 scope stance) ->
  ``purge-build-caches@3`` -> ``mtime-normalize@2`` ->
  ``separate-verifier@4`` last, with ``solution/solve.sh`` built from the
  reference fix when one exists, and the probe marker auto-derived from the
  hidden test patch. (Cache ``@3`` and mtime ``@2`` are the active
  generations, resolved at import and recorded per row in the manifest
  ``chain``.)
- Non-Python tasks (every snapshot task whose ``task.toml`` category is not
  Python, minus ledger members which the ledger row owns): snapshot task dir
  -> ``strip-future-history@1`` -> ``purge-build-caches@3`` ->
  ``mtime-normalize@2`` -> ``separate-verifier@4``. ``purge-installed-copies``
  is a Python pip mechanism and never applies; it is noted, not derived.

Deterministic and idempotent: every step reuses the existing lineage record
for ``(task, transform, parent digest)`` and derives (content-addressed via
:func:`evallab.task_variants.derive_task`) only what is missing, so
re-running yields the same digests and never duplicates records. Derived
packages materialize into the shared variants store (ignored); lineage
records land in ``library/task-variants/`` (tracked but not committed by
this slice: only the manifest is committed).

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
import re
import tomllib
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evallab import mtime_normalize as _mtime_normalize_mod
from evallab import purge_build_caches as _purge_build_caches_mod
from evallab.hardening import CONFIRMED_PURGE
from evallab.mtime_normalize import MARKER as MTIME_MARKER
from evallab.mtime_normalize import TRANSFORM_ID as MTIME_ID
from evallab.mtime_normalize import derive_mtime_normalize
from evallab.purge_build_caches import MARKER_V2 as CACHE_V2_MARKER
from evallab.purge_build_caches import TRANSFORM_ID_V2 as CACHE_V2_ID
from evallab.purge_build_caches import derive_purge_build_caches_v2
from evallab.purge_installed_copies import MARKER as PURGE_MARKER
from evallab.purge_installed_copies import TRANSFORM_ID as PURGE_ID
from evallab.purge_installed_copies import derive_purge_installed_copies
from evallab.separate_verifier import TRANSFORM_ID_V4 as SEPARATE_V4_ID
from evallab.separate_verifier import derive_separate_verifier_v4
from evallab.strip_future_history import STRIP_MARKER, derive_strip_future_history
from evallab.strip_future_history import TRANSFORM_ID as STRIP_ID
from evallab.task_variants import (
    MAX_INLINE_FILE_BYTES,
    RECORDS_DIRNAME,
    VariantExistsError,
    VariantInvalid,
    VariantRecord,
    resolve_record,
    task_directory_digest,
)

#: Clean-set version id (manifest + job-name namespace).
CLEAN_SET_VERSION = "mimo-clean-v3"

#: Who the builder blames in lineage records.
CREATED_BY = "mimo-clean-v3"

#: Tracked manifest path (repo-relative). v1/v2 stay as history untouched.
MANIFEST_REL = Path("research/experiments/mimo-clean-v3/manifest.csv")

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

#: Active purge-build-caches generation. ``@3`` (node build-output handling)
#: is preferred once ``purge_build_caches`` ships it; until then ``@2``.
#: Resolved at import so a rebase onto the @3 merge switches the chain (and
#: its manifest strings) with no further edit.
CACHE_ACTIVE_ID: str = getattr(_purge_build_caches_mod, "TRANSFORM_ID_V3", CACHE_V2_ID)
CACHE_ACTIVE_MARKER: str = getattr(_purge_build_caches_mod, "MARKER_V3", CACHE_V2_MARKER)


def _derive_cache_active(
    parent_dir: Path | str,
    *,
    rationale: str | None = None,
    created_by: str = CREATED_BY,
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive the active purge-build-caches generation for a parent package."""
    derive_v3 = getattr(_purge_build_caches_mod, "derive_purge_build_caches_v3", None)
    derive = derive_v3 if derive_v3 is not None else derive_purge_build_caches_v2
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


#: Active mtime-normalize generation. ``@2`` (Modal lazy-layer
#: materialization fix; Docker-neutral) is preferred once
#: ``mtime_normalize`` ships it; until then ``@1``. Same import-time
#: resolution as the cache generation above.
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


#: Canonical Python chain, in application order. ``separate-verifier@4`` is
#: always last: it bundles the parent's clean setup chain into the verifier.
PYTHON_CHAIN = (
    STRIP_ID,
    PURGE_ID,
    CACHE_ACTIVE_ID,
    MTIME_ACTIVE_ID,
    SEPARATE_V4_ID,
)

#: Canonical non-Python chain: snapshot instead of a repairs run package, and
#: no ``purge-installed-copies`` (a Python pip mechanism, noted not derived).
NONPYTHON_CHAIN = (
    STRIP_ID,
    CACHE_ACTIVE_ID,
    MTIME_ACTIVE_ID,
    SEPARATE_V4_ID,
)

#: Language plugs for the builder. ``python`` is the ledger pool;
#: every other resolved language takes the non-Python chain (including
#: ``unknown`` snapshot categories: @4 grades those with the exit-code
#: fallback under patch isolation). ``None`` (unresolvable task) has no chain.
LANGUAGE_CHAINS: dict[str, tuple[str, ...] | None] = {
    "python": PYTHON_CHAIN,
}

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


def chain_for_language(language: str | None) -> tuple[str, ...] | None:
    """Clean chain for a resolved language (``None`` = no chain)."""
    if language is None:
        return None
    if language == "python":
        return PYTHON_CHAIN
    return NONPYTHON_CHAIN


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


def select_usable(rows: Collection[Mapping[str, str]]) -> list[dict[str, str]]:
    """Ledger rows the clean set builds: verdict ``keep`` or ``fix``."""
    return [dict(row) for row in rows if row.get("verdict") in SELECTED_VERDICTS]


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


def load_oracle_sweep(
    sweep_path: Path, *, results_home: Path | None = None
) -> dict[str, dict[str, str]]:
    """Index oracle_sweep.csv by task id (label, fix_commit, evidence)."""
    out: dict[str, dict[str, str]] = {}
    for row in read_csv_rows(sweep_path):
        out[row["task_id"]] = {
            "label": row.get("label", ""),
            "fix_commit": row.get("fix_commit", ""),
        }
    return out


def oracle_info_for(
    task_id: str,
    sweep: Mapping[str, Mapping[str, str]],
    *,
    results_home: Path,
) -> OracleInfo | None:
    """The oracle-pass reference fix for a task, or None.

    Returns None unless the sweep row's label is an oracle pass *and* the
    HAR-191 solution patch file exists.
    """
    entry = sweep.get(task_id)
    if entry is None:
        return None
    label = entry.get("label", "")
    if not label.startswith(ORACLE_PASS_PREFIX):
        return None
    patch = results_home / "tasks" / task_id / "oracle" / "solution-patch.stdout.log"
    if not patch.is_file():
        return None
    return OracleInfo(
        label=label,
        fix_commit=entry.get("fix_commit", ""),
        patch_path=str(patch),
        patch_bytes=patch.read_bytes(),
    )


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


def purge_inapplicable() -> Collection[str]:
    """Tasks where purge-installed-copies breaks setup fail-closed."""
    from evallab.exploit_probe import PURGE_INAPPLICABLE

    return PURGE_INAPPLICABLE


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
        purge_skip: Collection[str] | None = None,
        created_by: str = CREATED_BY,
        snapshot_root: Path | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.primary = primary
        self.variants_root = variants_root
        self.created_by = created_by
        self.snapshot_root = snapshot_root or primary / SNAPSHOT_REL / "tasks"
        self._purge_skip = set(purge_skip) if purge_skip is not None else set(purge_inapplicable())
        self._records: dict[tuple[str, str, str], VariantRecord] = {}

    # -- record reuse -------------------------------------------------- #

    def _slug_records(self, task_id: str) -> None:
        slug = f"mimo-v2.6-rl__{task_id}"
        records_dir = self.repo_root / RECORDS_DIRNAME / slug
        if not records_dir.is_dir():
            return
        for path in sorted(records_dir.glob("*.json")):
            try:
                record = resolve_record(path, repo_root=self.repo_root)
            except ValueError:
                continue
            key = (task_id, record.transform, record.parent.digest)
            self._records.setdefault(key, record)

    def _lookup(self, task_id: str, transform: str, parent_digest: str) -> VariantRecord | None:
        key = (task_id, transform, parent_digest)
        record = self._records.get(key)
        if record is None:
            self._slug_records(task_id)
            record = self._records.get(key)
        return record

    def _remember(self, task_id: str, record: VariantRecord) -> None:
        self._records.setdefault((task_id, record.transform, record.parent.digest), record)

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

    def _materialized(self, task_id: str, record: VariantRecord) -> Path | None:
        """The record's package dir when present with a matching digest."""
        package = (
            self._variants_dir()
            / f"mimo-v2.6-rl__{task_id}"
            / record.variant_digest.removeprefix("sha256:")[:12]
        )
        if package.is_dir() and task_directory_digest(package) == record.variant_digest:
            return package
        return None

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
        """Reuse the record for ``(task, transform, parent)`` or derive it.

        ``expected_inputs`` guards parameterized transforms (``@2`` marker /
        solution): a same-parent record with different inputs is a different
        package, so derive fresh instead of reusing it.
        """
        record = self._lookup(task_id, transform, parent_digest)
        if record is not None and expected_inputs is not None:
            for key, value in expected_inputs.items():
                if record.inputs.get(key) != value:
                    record = None
                    break
        if record is not None:
            package = self._materialized(task_id, record)
            if package is not None:
                return record, True
        kwargs = self._derive_kwargs()
        kwargs.update(derive_kwargs)
        try:
            record = derive(
                parent,
                parent_source=dict(parent_source),
                **kwargs,
            )
        except VariantExistsError as exc:
            record = self._lookup(task_id, transform, parent_digest)
            if record is None:
                self._slug_records(task_id)
                record = self._records.get((task_id, transform, parent_digest))
            if record is None:
                # No lineage record, but a package blocks derivation (an orphan
                # from a partial run or store cleanup): move it aside and
                # re-derive deterministically, then reconcile by digest.
                return self._recover_orphan(
                    task_id,
                    transform,
                    parent,
                    parent_digest,
                    derive,
                    parent_source=parent_source,
                    expected_inputs=expected_inputs,
                    derive_kwargs=derive_kwargs,
                    clash=str(exc),
                )
            if self._materialized(task_id, record) is None:
                from evallab.task_variants import materialize

                materialize(
                    record,
                    parent,
                    repo_root=self.repo_root,
                    variants_root=self.variants_root,
                )
                if self._materialized(task_id, record) is None:
                    raise VariantExistsError(
                        f"record exists but the package cannot be materialized: "
                        f"{task_id} {transform}"
                    ) from None
            return record, True
        self._remember(task_id, record)
        return record, False

    def _recover_orphan(
        self,
        task_id: str,
        transform: str,
        parent: Path,
        parent_digest: str,
        derive,
        *,
        parent_source: Mapping[str, Any],
        expected_inputs: Mapping[str, Any] | None,
        derive_kwargs: Mapping[str, Any],
        clash: str,
    ) -> tuple[VariantRecord, bool]:
        """Re-derive past an orphan package, reconciling by digest."""
        import shutil

        slug_dir = self._variants_dir() / f"mimo-v2.6-rl__{task_id}"
        candidates = [
            path
            for path in sorted(slug_dir.iterdir())
            if path.is_dir() and path.as_posix() in clash
        ]
        if len(candidates) != 1:
            raise VariantExistsError(f"cannot derive {task_id} {transform}: {clash}") from None
        orphan = candidates[0]
        backup = orphan.with_name(orphan.name + ".orphan")
        if backup.exists():
            raise VariantExistsError(
                f"cannot derive {task_id} {transform}: orphan backup exists: {backup}"
            ) from None
        orphan.rename(backup)
        try:
            record, _ = self.find_or_derive(
                task_id,
                transform,
                parent,
                parent_digest,
                derive,
                parent_source=parent_source,
                expected_inputs=expected_inputs,
                **derive_kwargs,
            )
        except BaseException:
            backup.rename(orphan)
            raise
        if task_directory_digest(backup) == record.variant_digest:
            shutil.rmtree(backup)
        else:
            print(
                f"warning {task_id}: orphan {orphan.name} differs from "
                f"re-derived {record.variant_digest[:19]}; kept as {backup.name}"
            )
        return record, False

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

    def _advance(
        self,
        task_id: str,
        current_digest: str,
        row: Mapping[str, str] | None,
        start_digest: str,
    ) -> tuple[Path, str, dict[str, Any]]:
        """The materialized package, digest, and source after one chain step."""
        source = self._source(task_id, current_digest, start_digest, row)
        current = (
            self._variants_dir()
            / f"mimo-v2.6-rl__{task_id}"
            / current_digest.removeprefix("sha256:")[:12]
        )
        return current, current_digest, source

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
        chain = chain_for_language(language)
        if chain is None:
            result.reason = (
                f"no clean chain for language {language!r}: snapshot task.toml category unreadable"
            )
            return result

        notes: list[str] = []
        start_kind = "run package" if row is not None else "snapshot package"
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

        # strip-future-history@1 (fatal: the foundation of the chain).
        if setup_carries(current, STRIP_MARKER):
            notes.append(f"strip-future-history@1 already in {start_kind}")
        else:
            try:
                record, _ = self.find_or_derive(
                    task_id,
                    STRIP_ID,
                    current,
                    current_digest,
                    derive_strip_future_history,
                    parent_source=source,
                )
            except VariantInvalid as exc:
                result.reason = f"strip-future-history@1 failed: {exc}"
                return result
            current_digest = record.variant_digest
            current, current_digest, source = self._advance(
                task_id, current_digest, row, start_digest
            )
        result.chain.append(STRIP_ID)

        # purge-installed-copies@1: Python chain only (CONFIRMED_PURGE +
        # already-carried); elsewhere it is a Python pip mechanism, noted.
        if PURGE_ID in chain and row is not None:
            if setup_carries(current, PURGE_MARKER):
                notes.append("purge-installed-copies@1 already in run package")
                result.chain.append(PURGE_ID)
            elif task_id in self._purge_skip:
                detail = PURGE_FAIL_CLOSED_REASONS.get(task_id, "fail-closed")
                notes.append(f"purge-installed-copies@1 skipped fail-closed: {detail}")
            elif task_id in CONFIRMED_PURGE:
                try:
                    record, _ = self.find_or_derive(
                        task_id,
                        PURGE_ID,
                        current,
                        current_digest,
                        derive_purge_installed_copies,
                        parent_source=source,
                        repairs_digest=row["run_digest"],
                    )
                except VariantInvalid as exc:
                    notes.append(f"purge-installed-copies@1 skipped: {exc}")
                else:
                    current_digest = record.variant_digest
                    current, current_digest, source = self._advance(
                        task_id, current_digest, row, start_digest
                    )
                    result.chain.append(PURGE_ID)
            else:
                notes.append(
                    "purge-installed-copies@1 skipped: scope is CONFIRMED_PURGE "
                    "plus already-carried run packages (HAR-194 stance)"
                )
        else:
            notes.append(f"purge-installed-copies@1 n/a to {result.language}: Python pip mechanism")

        # Active purge-build-caches and mtime-normalize generations
        # (mechanical, continue the chain past a single-step failure).
        for transform, marker, derive in (
            (CACHE_ACTIVE_ID, CACHE_ACTIVE_MARKER, _derive_cache_active),
            (MTIME_ACTIVE_ID, MTIME_ACTIVE_MARKER, _derive_mtime_active),
        ):
            if setup_carries(current, marker):
                notes.append(f"{transform} already in {start_kind}")
                result.chain.append(transform)
                continue
            try:
                record, _ = self.find_or_derive(
                    task_id,
                    transform,
                    current,
                    current_digest,
                    derive,
                    parent_source=source,
                )
            except VariantInvalid as exc:
                notes.append(f"{transform} skipped: {exc}")
                continue
            current_digest = record.variant_digest
            current, current_digest, source = self._advance(
                task_id, current_digest, row, start_digest
            )
            result.chain.append(transform)

        # separate-verifier@4 last: marker from the hidden test patch,
        # solution from the oracle-pass reference fix when one exists.
        patch_file = current / "tests" / "test.patch"
        marker = derive_marker(
            patch_file.read_text(errors="replace") if patch_file.is_file() else "",
            task_id,
        )
        solution_sh: bytes | None = None
        if oracle is not None:
            if (current / "solution" / "solve.sh").is_file():
                notes.append(
                    "solution/solve.sh already present; reference fix recorded in manifest only"
                )
            else:
                try:
                    solution_sh = build_solution_sh(
                        task_id=task_id,
                        label=oracle.label,
                        fix_commit=oracle.fix_commit,
                        patch_bytes=oracle.patch_bytes,
                    )
                except VariantInvalid as exc:
                    notes.append(f"reference solution omitted: {exc}")
        solution_tag = (
            "absent" if solution_sh is None else f"sha256:{hashlib.sha256(solution_sh).hexdigest()}"
        )
        try:
            record, _ = self.find_or_derive(
                task_id,
                SEPARATE_V4_ID,
                current,
                current_digest,
                derive_separate_verifier_v4,
                parent_source=source,
                expected_inputs={"marker": marker, "solution": solution_tag},
                marker=marker,
                solution_sh=solution_sh,
            )
        except VariantInvalid as exc:
            result.reason = f"separate-verifier@4 failed: {exc}"
            return result
        current_digest = record.variant_digest
        result.chain.append(SEPARATE_V4_ID)

        result.status = STATUS_BUILT
        result.final_digest = current_digest
        slug = f"mimo-v2.6-rl__{task_id}"
        result.package_path = (
            VARIANTS_REL / slug / current_digest.removeprefix("sha256:")[:12]
        ).as_posix()
        result.reason = "; ".join(notes) if notes else "clean chain complete"
        return result


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


def snapshot_pool(snapshot_tasks: Path, ledger_ids: Collection[str]) -> dict[str, str | None]:
    """Non-ledger snapshot tasks: task id -> category (None when unreadable)."""
    pool: dict[str, str | None] = {}
    if not snapshot_tasks.is_dir():
        return pool
    for task_dir in sorted(p for p in snapshot_tasks.iterdir() if p.is_dir()):
        if task_dir.name in ledger_ids:
            continue
        pool[task_dir.name] = snapshot_category(task_dir)
    return pool


def build_mimo_clean_parser(commands) -> None:
    """Register the ``evallab mimo-clean`` subcommand (one self-contained block)."""
    mimo = commands.add_parser(
        "mimo-clean",
        help="Build and locally verify the mimo-clean-v3 task set ($0, model-free)",
        description=__doc__.split("\n\n")[0] if __doc__ else "mimo-clean-v3",
    )
    sub = mimo.add_subparsers(dest="mimo_clean_cmd", required=True)
    build = sub.add_parser("build", help="Derive the canonical clean chain for code tasks")
    build.add_argument(
        "--tasks",
        default=None,
        help="comma-separated task ids (default: ledger pool + snapshot pool)",
    )
    build.add_argument("--ledger", type=Path, default=LEDGER_REL, help="Python task ledger CSV")
    build.add_argument("--sweep", type=Path, default=SWEEP_REL, help="oracle sweep CSV")
    build.add_argument(
        "--reference-index",
        type=Path,
        default=REFERENCE_INDEX_REL,
        help="mimo-reference-fixes index CSV (preferred over --sweep when present)",
    )
    build.add_argument(
        "--snapshot",
        type=Path,
        default=SNAPSHOT_REL / "tasks",
        help="snapshot tasks dir (resolved against the primary checkout)",
    )
    build.add_argument(
        "--results-home",
        type=Path,
        default=None,
        help="HAR-191 oracle-sweep results dir holding per-task solution patches",
    )
    build.add_argument("--manifest", type=Path, default=MANIFEST_REL, help="manifest CSV to write")
    build.add_argument("--workers", type=int, default=8, help="parallel chain builders")
    build.set_defaults(func=_mimo_clean_command)
    verify = sub.add_parser(
        "verify-local",
        help="Run oracle/nop/cheat-ladder on local Docker for clean packages",
    )
    verify.add_argument("--tasks", required=True, help="comma-separated task ids")
    verify.add_argument("--manifest", type=Path, default=MANIFEST_REL, help="manifest CSV to read")
    verify.add_argument("--jobs-dir", type=Path, default=Path("runs/mimo-clean-v3"))
    verify.add_argument("--timeout-seconds", type=int, default=1800)
    verify.set_defaults(func=_mimo_clean_command)


def _default_results_home() -> Path:
    return Path.home() / "Developer" / "eval-lab-results" / "2026-10-07" / "HAR-191-oracle-sweep"


def _build_command(args, root: Path) -> int:
    import concurrent.futures

    from evallab.storage.paths import shared_checkout_root

    ledger_path = _resolve(root, args.ledger)
    sweep_path = _resolve(root, args.sweep)
    index_path = _resolve(root, args.reference_index)
    manifest_path = _resolve(root, args.manifest)
    results_home = args.results_home or _default_results_home()
    primary = shared_checkout_root(root)
    snapshot_tasks = args.snapshot if args.snapshot.is_absolute() else primary / args.snapshot
    rows = read_csv_rows(ledger_path)
    by_ledger_id = {row["task_id"]: row for row in rows}
    wanted = (
        {part.strip() for part in args.tasks.split(",") if part.strip()} if args.tasks else None
    )
    selected = select_usable(rows)
    if wanted is not None:
        selected = [row for row in selected if row["task_id"] in wanted]
    if index_path.is_file():
        index = load_reference_index(index_path)
        print(f"reference fixes: index {index_path} ({len(index)} rows)")

        def oracle_for(task_id: str) -> OracleInfo | None:
            return oracle_info_from_index(task_id, index, repo_root=root)
    else:
        sweep = load_oracle_sweep(sweep_path)
        print(f"reference fixes: sweep {sweep_path} (index absent)")

        def oracle_for(task_id: str) -> OracleInfo | None:
            return oracle_info_for(task_id, sweep, results_home=results_home)

    builder = ChainBuilder(repo_root=root, primary=primary, snapshot_root=snapshot_tasks)
    by_id = {row["task_id"]: row for row in selected}
    pool = snapshot_pool(snapshot_tasks, set(by_ledger_id))
    if wanted is not None:
        pool = {task_id: category for task_id, category in pool.items() if task_id in wanted}

    def one_python(task_id: str) -> ChainResult:
        return builder.build_task(
            task_id,
            by_id[task_id],
            oracle=oracle_for(task_id),
        )

    def one_snapshot(task_id: str) -> ChainResult:
        return builder.build_task(task_id, None, oracle=oracle_for(task_id))

    results: list[ChainResult] = []
    workers = max(1, args.workers)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool_exec:
        futures = {pool_exec.submit(one_python, row["task_id"]): row["task_id"] for row in selected}
        futures.update({pool_exec.submit(one_snapshot, task_id): task_id for task_id in pool})
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    have = {result.task_id for result in results}
    if wanted is not None:
        # Skips for explicitly requested tasks outside both pools' selection.
        for task_id in sorted(wanted):
            if task_id in have:
                continue
            if task_id in by_ledger_id:
                row = by_ledger_id[task_id]
                results.append(
                    ChainResult(
                        task_id=task_id,
                        status=STATUS_SKIPPED,
                        language="python",
                        run_digest=row.get("run_digest", ""),
                        reason=(f"ledger verdict={row.get('verdict')} status={row.get('status')}"),
                    )
                )
            else:
                results.append(
                    ChainResult(
                        task_id=task_id,
                        status=STATUS_SKIPPED,
                        reason="not a keep/fix ledger row or snapshot task",
                    )
                )
    else:
        # Discard rows are skips with reason, so the manifest covers the ledger.
        for row in rows:
            if row["task_id"] not in have:
                results.append(
                    ChainResult(
                        task_id=row["task_id"],
                        status=STATUS_SKIPPED,
                        language="python",
                        run_digest=row.get("run_digest", ""),
                        reason=(
                            f"ledger verdict={row.get('verdict')} "
                            f"status={row.get('status')}: {row.get('reason', '')}"
                        ),
                    )
                )
        # Python-category snapshot tasks without a ledger row cannot start
        # the Python chain; uncategorized ones have no chain at all.
        for task_id in sorted(pool):
            if task_id in have:
                continue
            category = pool[task_id]
            if category == "python":
                results.append(
                    ChainResult(
                        task_id=task_id,
                        status=STATUS_SKIPPED,
                        language="python",
                        reason="python snapshot task without a ledger row",
                    )
                )
            else:
                results.append(
                    ChainResult(
                        task_id=task_id,
                        status=STATUS_SKIPPED,
                        reason="snapshot task.toml category unreadable",
                    )
                )
    write_manifest([result.manifest_row() for result in results], manifest_path)
    built = sum(1 for result in results if result.status == STATUS_BUILT)
    print(f"built {built}/{len(results)} -> {manifest_path}")
    for result in sorted(results, key=lambda item: item.task_id):
        if result.status != STATUS_BUILT:
            print(f"skip {result.task_id}: {result.reason}")
    return 0


def _next_free_name(task_jobs: Path, base: str, *, task_id: str) -> tuple[str, bool]:
    """Next free job name: ``base`` unless taken, else ``base-attemptN``.

    Returns ``(name, fresh)`` where ``fresh`` is True when ``base`` itself
    is free (the caller reuses it only after checking its trials completed).
    """
    attempt = 1
    name = base
    while (task_jobs / name).exists():
        attempt += 1
        name = f"{base}-attempt{attempt}"
    if attempt > 1:
        print(f"note {task_id}: {base} incomplete; launching {name}")
    return name, attempt == 1


def _verify_local_command(args, root: Path) -> int:
    import os

    from evallab.cheat import _CHEAT_ENV_LOCK, build_verdicts
    from evallab.execution_contracts import CHEAT_AGENT, CHEAT_ATTACKS_ENV_VAR, RunRequest
    from evallab.harbor_view import installed_harbor_version
    from evallab.queue import Executor

    version = installed_harbor_version()
    if version is None or version < (0, 24):
        print(
            "error: evallab mimo-clean verify-local needs Harbor >= 0.24 on PATH; "
            "install the locked laminar extra: uv sync --frozen --extra laminar",
        )
        return 2
    from evallab.storage.paths import shared_checkout_root

    manifest_path = _resolve(root, args.manifest)
    jobs_dir = _resolve(root, args.jobs_dir)
    primary = shared_checkout_root(root)
    manifest = {row["task_id"]: row for row in load_manifest(manifest_path)}
    wanted = [part.strip() for part in args.tasks.split(",") if part.strip()]
    if not wanted:
        print("error: --tasks needs at least one task id")
        return 2
    rows: list[dict[str, Any]] = []
    failed = False
    for task_id in wanted:
        row = manifest.get(task_id)
        if row is None:
            print(f"skip {task_id}: not in manifest {manifest_path}")
            failed = True
            continue
        if row["status"] != STATUS_BUILT or not row["package_path"]:
            print(f"skip {task_id}: manifest status={row['status']}: {row['reason']}")
            failed = True
            continue
        package = primary / Path(row["package_path"])
        short = task_id.removeprefix("format-code-task-")
        task_jobs = jobs_dir / task_id

        def cell(
            agent: str,
            suffix: str,
            attacks: str | None = None,
            *,
            short: str = short,
            task_id: str = task_id,
            task_jobs: Path = task_jobs,
        ) -> tuple[Path, bool]:
            """Run one acceptance cell, reusing a completed job dir if present.

            Returns ``(job_dir, reused)``. A job dir counts as completed when
            it holds trials whose rewards are all recorded; anything else
            launches under the next free ``-attemptN`` name so no evidence is
            ever clobbered.
            """
            base = f"{CLEAN_SET_VERSION}-{short}-{suffix}"
            candidate = task_jobs / base
            rewards = summarize_trials(candidate)
            if rewards and all(reward is not None for reward in rewards):
                print(f"reused {candidate}")
                return candidate, True
            name, _ = _next_free_name(task_jobs, base, task_id=task_id)
            return launch(agent, name, attacks), False

        def launch(
            agent: str,
            name: str,
            attacks: str | None = None,
            *,
            package: Path = package,
            task_jobs: Path = task_jobs,
        ) -> Path:
            request = RunRequest(
                task=package,
                agent=agent,
                name=name,
                jobs_dir=task_jobs,
                environment="docker",
                model=None,
                concurrency=1,
                attempts=1,
                timeout_seconds=args.timeout_seconds,
                allow_billable=False,
            )
            if attacks is None:
                return Executor.from_repo(root).execute_direct(request)
            with _CHEAT_ENV_LOCK:
                previous = os.environ.get(CHEAT_ATTACKS_ENV_VAR)
                os.environ[CHEAT_ATTACKS_ENV_VAR] = attacks
                try:
                    return Executor.from_repo(root).execute_direct(request)
                finally:
                    if previous is None:
                        os.environ.pop(CHEAT_ATTACKS_ENV_VAR, None)
                    else:
                        os.environ[CHEAT_ATTACKS_ENV_VAR] = previous

        has_fix = row["reference_fix"] not in ("", "none")
        oracle_rewards: list[float | None] = []
        if has_fix:
            oracle_dir, _ = cell("oracle", "oracle")
            oracle_rewards = summarize_trials(oracle_dir)
        else:
            print(f"note {task_id}: no reference fix; oracle cell n/a")
        nop_dir, _ = cell("nop", "nop")
        nop_rewards = summarize_trials(nop_dir)
        cheat_dir, _ = cell(CHEAT_AGENT, "cheat", attacks="")
        verdicts = build_verdicts(cheat_dir)
        trials = verdicts.get("trials", [])
        cracked = sum(1 for trial in trials if trial.get("verdict") == "cracked")
        ok = acceptance_pass(
            oracle_rewards=oracle_rewards,
            has_reference_fix=has_fix,
            nop_rewards=nop_rewards,
            cracked=cracked,
        )
        failed = failed or not ok
        oracle_cell = (
            ",".join(
                "1" if reward == 1 else "0" if reward == 0 else "?" for reward in oracle_rewards
            )
            if has_fix
            else "n/a"
        )
        nop_cell = ",".join(
            "1" if reward == 1 else "0" if reward == 0 else "?" for reward in nop_rewards
        )
        print(
            f"| {task_id} | {oracle_cell} | {nop_cell} | "
            f"{cracked}/{len(trials)} cracked | {'PASS' if ok else 'FAIL'} |"
        )
        rows.append(
            {
                "task_id": task_id,
                "oracle": oracle_cell,
                "nop": nop_cell,
                "cheat": f"{cracked}/{len(trials)}",
                "pass": ok,
            }
        )
    print(f"acceptance {sum(1 for row in rows if row['pass'])}/{len(rows)} pass")
    return 1 if failed else 0


def _mimo_clean_command(args, root: Path, **_: Any) -> int:
    if args.mimo_clean_cmd == "build":
        return _build_command(args, root)
    if args.mimo_clean_cmd == "verify-local":
        return _verify_local_command(args, root)
    raise ValueError(f"unknown mimo-clean subcommand: {args.mimo_clean_cmd}")


__all__ = [
    "CACHE_ACTIVE_ID",
    "CACHE_ACTIVE_MARKER",
    "CACHE_V2_ID",
    "CACHE_V2_MARKER",
    "CLEAN_SET_VERSION",
    "CREATED_BY",
    "LANGUAGE_CHAINS",
    "MANIFEST_COLUMNS",
    "MANIFEST_REL",
    "MTIME_ACTIVE_ID",
    "MTIME_ACTIVE_MARKER",
    "MTIME_ID",
    "NONPYTHON_CHAIN",
    "ORACLE_PASS_PREFIX",
    "PURGE_ID",
    "PYTHON_CHAIN",
    "REFERENCE_INDEX_REL",
    "SELECTED_VERDICTS",
    "SEPARATE_V4_ID",
    "STATUS_BUILT",
    "STATUS_SKIPPED",
    "STRIP_ID",
    "VERIFY_UNVERIFIED",
    "ChainBuilder",
    "ChainResult",
    "OracleInfo",
    "acceptance_pass",
    "build_mimo_clean_parser",
    "build_solution_sh",
    "chain_for_language",
    "derive_marker",
    "load_manifest",
    "load_oracle_sweep",
    "load_reference_index",
    "oracle_info_for",
    "oracle_info_from_index",
    "purge_inapplicable",
    "read_csv_rows",
    "resolve_language",
    "run_package_rel",
    "select_usable",
    "setup_carries",
    "snapshot_category",
    "snapshot_pool",
    "summarize_trials",
    "variant_source",
    "write_manifest",
]
