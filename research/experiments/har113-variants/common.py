"""Shared pieces for the HAR-113 scripts: paths, census rows, variant derivation."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE.parent / "har105-exploration"))

import pyarrow.parquet as pq  # noqa: E402
from setup_payload import embedded, encode  # noqa: E402

from evallab.registry import task_directory_digest  # noqa: E402
from evallab.storage.paths import shared_checkout_root  # noqa: E402
from evallab.task_variants import (  # noqa: E402
    VariantRecord,
    derive_task,
    load_records,
)

PRIMARY = shared_checkout_root(ROOT)
CENSUS = ROOT / "research/experiments/har108-python-census"
REPO = "FineEnvs/MiMo-V2.6-RL-harbor-code"
REVISION = "5746e2f0c5c61af12d7c5bf15d7efdd77d1f0785"
SNAPSHOT = PRIMARY / "derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6"
VARIANTS = PRIMARY / "derived/task-store/variants"
SETUP = "environment/setup"
BLOCKLIST = f"{SETUP}/files/blocklist"
SETUP_SH = f"{SETUP}/setup.sh"


def census() -> dict[str, dict]:
    """HAR-108 task-health rows by task id (``format-code-task-NNNNNN``)."""
    rows = pq.read_table(CENSUS / "task_health.parquet").to_pylist()
    return {row["task_id"]: row for row in rows}


def pypi_entries() -> dict[str, dict]:
    return json.loads((CENSUS / "pypi.json").read_text())["tasks"]


def original(task_id: str) -> Path:
    return SNAPSHOT / "tasks" / task_id


def hf_source(task_id: str) -> dict:
    return {"kind": "hf", "repo": REPO, "revision": REVISION, "path": f"tasks/{task_id}"}


def records_by(transform: str) -> dict[str, VariantRecord]:
    """This checkout's lineage records for one transform, by task id."""
    return {
        record.task_name.split("/", 1)[1]: record
        for record in load_records(ROOT)
        if record.transform == transform
    }


def find_record(transform: str, parent: Path, inputs: dict) -> VariantRecord | None:
    """The record of ``transform`` already derived from ``parent`` with ``inputs``."""
    digest = task_directory_digest(parent)
    return next(
        (
            record
            for record in load_records(ROOT)
            if record.transform == transform
            and record.parent.digest == digest
            and record.inputs == inputs
        ),
        None,
    )


def record_path(record: VariantRecord) -> str:
    slug = record.task_name.replace("/", "__")
    return f"library/task-variants/{slug}/{record.variant_digest[7:19]}.json"


def package_dir(record: VariantRecord) -> Path:
    slug = record.task_name.replace("/", "__")
    return VARIANTS / slug / record.variant_digest[7:19]


def package_rel(record: VariantRecord) -> str:
    """Checkout-relative path of a variant package, as queue specs name tasks."""
    return package_dir(record).relative_to(PRIMARY).as_posix()


def with_setup(task: Path, changed: dict[str, bytes]) -> bytes:
    """``task.toml`` with ``environment/setup`` re-embedded after ``changed``.

    The healthcheck runs the embedded copy, not the files on disk, so a setup
    change without this never runs (``setup_payload.py``).
    """
    text = (task / "task.toml").read_text()
    old = embedded(task)
    if text.count(old) != 1:
        raise SystemExit(f"{task.name}: setup payload appears {text.count(old)} times")
    if encode(task / SETUP) != old:
        raise SystemExit(f"{task.name}: on-disk setup does not reproduce the payload")
    with tempfile.TemporaryDirectory(prefix="har113-setup-") as scratch:
        # Fresh files, not copytree: snapshot files are read-only, and the
        # payload sets every member's mode itself.
        setup = Path(scratch) / "setup"
        for path in (task / SETUP).rglob("*"):
            if path.is_file():
                rel = path.relative_to(task).as_posix()
                target = setup / path.relative_to(task / SETUP)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(changed.get(rel, path.read_bytes()))
        return text.replace(old, encode(setup)).encode()


def derive(
    parent: Path,
    changed: dict[str, bytes],
    *,
    transform: str,
    rationale: str,
    created_by: str,
    inputs: dict,
    parent_source: dict,
) -> VariantRecord:
    """Derive a variant whose setup files change, with the payload re-embedded."""
    changes = {**changed, "task.toml": with_setup(parent, changed)}
    return derive_task(
        parent,
        changes=changes,
        transform=transform,
        rationale=rationale,
        created_by=created_by,
        inputs=inputs,
        parent_source=parent_source,
        repo_root=ROOT,
    )
