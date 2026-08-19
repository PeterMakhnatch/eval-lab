"""GYM-RUN cycle 1: freeze a gym generation into an immutable manifest.

Why this exists as code rather than a hand-written JSON file: the manifest's whole
value is that it says what was *true* at freeze time, so it is generated from the
registry at runtime and never typed by hand. Placed under `library/frozen/gym-v0/`
to stay inside GYM-RUN's declared lease, following the existing precedent of
`library/curated/_emit_card.py`.

The freeze contract:

- A frozen manifest is **never edited**. `gym-v1` is a new directory, not an edit.
- Every campaign result cites the generation it ran against, which is what makes
  next month's numbers comparable to tomorrow's.
- Writing over an existing manifest is refused. Use ``--out`` to regenerate
  elsewhere for comparison.

Usage::

    python library/frozen/gym-v0/_freeze.py --repo-root . --generation gym-v0
    python library/frozen/gym-v0/_freeze.py --out /tmp/compare.json   # comparison
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.registry import TaskRegistry

MANIFEST_SCHEMA_VERSION = 1


class FreezeRefused(RuntimeError):
    """Raised when a freeze would overwrite an existing frozen manifest."""


def _head_sha(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def task_entry(record: Any) -> dict[str, Any]:
    """Project one registry record into its frozen form.

    Task digest and battery evidence pointers are copied from the registry rather
    than recomputed: the freeze records what the registry asserted, so a later
    mismatch is detectable instead of being silently papered over.
    """
    return {
        "task_id": record.task_id,
        "version": record.version,
        "task_path": record.task_path,
        "state": record.state,
        "digests": {
            "package": record.digests.package,
            "task_toml": record.digests.task_toml,
            "instruction": record.digests.instruction,
            "environment": record.digests.environment,
            "verifier": record.digests.verifier,
        },
        "battery_evidence": {
            "oracle": record.control_evidence.oracle.model_dump(mode="json"),
            "nop": record.control_evidence.nop.model_dump(mode="json"),
        },
    }


def build_manifest(
    repo_root: Path,
    *,
    generation: str = "gym-v0",
    frozen_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the frozen manifest for every task the registry currently registers."""
    registry = TaskRegistry.from_repo(repo_root)
    records = [r for r in registry.list_records() if r.state == "registered"]
    entries = [task_entry(record) for record in records]

    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generation": generation,
        "frozen": True,
        "frozen_at": (frozen_at or datetime.now(UTC)).date().isoformat(),
        "repo_commit": _head_sha(repo_root),
        "task_count": len(entries),
        "tasks": entries,
    }

    if not entries:
        manifest["note"] = (
            "The registry held ZERO registered task records at freeze time, so this "
            "generation is the empty set. Measured with `evallab registry list`, which "
            "printed: 'No task records found in library/registry/.' — the directory "
            "contains only .gitkeep. registry.py refuses any experiment spec whose task "
            "is not registered, so no campaign trial can be submitted against gym-v0. "
            "Registry promotion is human-only, so closing this is a Peter decision "
            "(register the curated-nominee slice, or reject the study). This manifest is "
            "deliberately kept rather than withheld: a frozen record of an empty gym is "
            "the honest baseline, and it is what makes the later non-empty generation "
            "visibly different."
        )
    return manifest


def render(manifest: dict[str, Any]) -> str:
    """Deterministic bytes: sorted keys, fixed indent, trailing newline."""
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def write_manifest(
    path: Path,
    manifest: dict[str, Any],
    *,
    allow_overwrite: bool = False,
) -> Path:
    """Write the manifest, refusing to overwrite a frozen one.

    This refusal is the entire meaning of "frozen": without it the file is just a
    cache that quietly tracks whatever the registry says today, and every campaign
    number that cites it becomes uncomparable.
    """
    if path.exists() and not allow_overwrite:
        raise FreezeRefused(
            f"{path} already exists and a frozen manifest is never rewritten. "
            "Start a new generation directory (gym-v1), or pass --out to render a "
            "throwaway copy for comparison."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(manifest), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="freeze", description="Freeze a gym generation")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--generation", default="gym-v0")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write elsewhere (comparison run); default is the generation's manifest.json",
    )
    args = parser.parse_args(argv)

    manifest = build_manifest(args.repo_root, generation=args.generation)
    default = args.repo_root / "library" / "frozen" / args.generation / "manifest.json"
    destination = args.out or default
    try:
        write_manifest(destination, manifest, allow_overwrite=args.out is not None)
    except FreezeRefused as exc:
        print(f"refused: {exc}")
        return 1
    print(f"froze {manifest['task_count']} task(s) -> {destination}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
