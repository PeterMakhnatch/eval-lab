#!/usr/bin/env python3
"""HAR-113 part 1 trials: nops and leak probes for a sample of leak variants.

For each task in ``SAMPLE`` (14 tasks from 14 projects: HAR-104's five
``pypi_fix_released`` tasks, the PyPI-facing ones whose hidden tests matched
the static check, and light ``sound`` tasks):

- a Daytona nop of the variant (``har113-vnop-<id>-<digest12>``); the
  parent's nop is HAR-108's, or ``har113-nop-<id>`` when it has none;
- a leak probe of the variant (``har113-probe-<id>-variant``): Harbor's
  oracle agent runs ``probe_solve.sh``, which applies the blocklist as the
  lab's Terminus harness does and runs ``pip download <project>==<fix>``;
  the verifier then grades the untouched repo with the blocklist in place,
  so its output also shows whether grading needs PyPI;
- for ``PARENT_PROBES``, the same probe on the original package, where the
  download is expected to succeed.

A probe package is the task package plus ``solution/solve.sh``; it lives in
``derived/har113-probes/`` and is never a variant.

Usage (from the worktree root):
    uv run python research/experiments/har113-variants/leak_specs.py
Writes specs to ``specs/leak/``; run them with ``runner.py``.
"""

from __future__ import annotations

import json
import shutil
import sys

from common import HERE, ROOT, census, original, package_dir, package_rel, records_by
from leak_variants import TRANSFORM

sys.path.insert(0, str(HERE.parent / "har105-exploration"))
from nopspec import nop_spec  # noqa: E402

SAMPLE = (
    "format-code-task-000226",  # waitress, HAR-104
    "format-code-task-000927",  # soupsieve, HAR-104
    "format-code-task-002256",  # persist-queue, HAR-104
    "format-code-task-002864",  # sqlglot, HAR-104
    "format-code-task-002407",  # python-control, HAR-104
    "format-code-task-001618",  # pip-tools: tests name the default index URL
    "format-code-task-002388",  # bandersnatch: tests name files.pythonhosted.org
    "format-code-task-002427",  # mypy: tests assert on the --install-types hint
    "format-code-task-002308",  # pre-commit
    "format-code-task-001647",  # django-environ
    "format-code-task-000803",  # mdutils
    "format-code-task-002974",  # vyper
    "format-code-task-002014",  # napalm
    "format-code-task-001265",  # pelican
)
PARENT_PROBES = (
    "format-code-task-000226",
    "format-code-task-000927",
    "format-code-task-002256",
    "format-code-task-002864",
    "format-code-task-002407",
)
PROBES = ROOT / "derived/har113-probes"
SPECS = HERE / "specs/leak"
#: The oracle runs the probe (pip retries, two HTTPS checks) before grading.
PROBE_MARGIN_S = 600


def short(task_id: str) -> str:
    return task_id.removeprefix("format-code-task-")


def probe_package(source, task_id: str, kind: str, entry: dict) -> str:
    """Copy ``source`` plus ``solution/solve.sh``; return its checkout path."""
    rel = f"derived/har113-probes/{short(task_id)}-{kind}"
    target = ROOT / rel
    if not target.exists():
        shutil.copytree(source, target)
        for path in (target, *target.rglob("*")):
            path.chmod(path.stat().st_mode | 0o200)
        solve = (HERE / "probe_solve.sh").read_text()
        solve = solve.replace("__PKG__", entry["pypi_project"])
        solve = solve.replace("__VER__", entry["fixed_release"])
        (target / "solution").mkdir()
        (target / "solution/solve.sh").write_text(solve)
        (target / "solution/solve.sh").chmod(0o755)
    return rel


def write(spec: dict) -> None:
    SPECS.mkdir(parents=True, exist_ok=True)
    spec["submitted_by"] = "har113-variants"
    (SPECS / f"{spec['name']}.json").write_text(json.dumps(spec, indent=1) + "\n")


def main() -> None:
    rows = census()
    leaks = json.loads((HERE / "leak_variants.json").read_text())["tasks"]
    records = records_by(TRANSFORM)
    for task_id in SAMPLE:
        record, entry = records[task_id], leaks[task_id]
        d12 = record.variant_digest[7:19]
        write(
            nop_spec(
                package_rel(record),
                f"har113-vnop-{short(task_id)}-{d12}",
                f"HAR-113 leak-closed variant of {task_id} nops clean like its parent",
            )
        )
        if rows[task_id]["nop_job_name"] is None:
            write(
                nop_spec(
                    f"derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks/{task_id}",
                    f"har113-nop-{short(task_id)}",
                    f"HAR-113 before-nop of {task_id} (HAR-108 never nopped it)",
                )
            )
        kinds = [("variant", package_dir(record))]
        if task_id in PARENT_PROBES:
            kinds.append(("parent", original(task_id)))
        for kind, source in kinds:
            spec = nop_spec(
                probe_package(source, task_id, kind, entry),
                f"har113-probe-{short(task_id)}-{kind}",
                f"HAR-113 leak probe on the {kind} of {task_id}: pip download "
                f"{entry['pypi_project']}=={entry['fixed_release']} after the blocklist",
            )
            spec["agent"] = "oracle"
            spec["timeout_seconds"] += PROBE_MARGIN_S
            write(spec)
    print(f"{len(list(SPECS.glob('*.json')))} specs in {SPECS.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
