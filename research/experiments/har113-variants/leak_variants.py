#!/usr/bin/env python3
"""HAR-113 part 1: leak-closed variants for the ``pypi_fix_released`` tasks.

HAR-108's leak column marks a task ``pypi_fix_released`` when the project
published a release after the upstream issue closed, so ``pip download
<project>==<release>`` inside the sandbox fetches the fix. The FineEnvs
answer-leak blocklist (``environment/setup/files/blocklist``, appended to
``/etc/hosts`` by the agent harness right after it installs) names git hosts
and search engines but no PyPI host.

Each variant appends the PyPI hosts in ``PYPI_HOSTS`` to the blocklist and
re-embeds ``environment/setup`` in the ``[environment.healthcheck]`` payload,
which is what actually runs. Nothing else changes.

A task is left unfixed when grading needs PyPI. Setup cannot: it runs in the
healthcheck, before the harness applies the blocklist. The verifier runs
after, so this checks what it runs: the test command (``mimo_test_command.sh``
in the hidden patch, plus ``tests/test_command.sh``) must not install or
fetch packages, and the hidden tests must not name a PyPI host or install
anything. Every match is either reviewed by hand in ``REVIEWED`` or leaves
the task unfixed.

Usage (from the worktree root):
    uv run python research/experiments/har113-variants/leak_variants.py
Writes ``leak_variants.json``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from common import (
    BLOCKLIST,
    HERE,
    census,
    derive,
    hf_source,
    original,
    package_rel,
    pypi_entries,
    record_path,
    records_by,
)

TRANSFORM = "leak-close-pypi@1"
CREATED_BY = "har113-leak"
#: pip's index and file hosts, and the legacy index old pips default to.
PYPI_HOSTS = ("pypi.org", "files.pythonhosted.org", "pypi.python.org")
ADDED = "# HAR-113: PyPI hosts (the fix is a PyPI release)\n" + "".join(
    f"0.0.0.0 {host}\n" for host in PYPI_HOSTS
)
#: The test command installs or fetches something.
FETCHES = re.compile(
    r"\b(pip3?|uv|poetry|pipenv|pdm|hatch|conda|mamba|easy_install)\b[^\n]*"
    r"\b(install|download|sync|add|lock)\b|\b(tox|nox)\b|setup\.py\s+(develop|install)|https?://",
    re.I,
)
#: A hidden test names a PyPI host or installs something.
TOUCHES_PYPI = re.compile(
    r"pypi\.org|pythonhosted\.org|pypi\.python\.org|install[-_]types|ensurepip|get-pip"
    r"|\bpip3?\b[^\n]*\b(install|download)\b|index[-_]url",
    re.I,
)
#: Matches read by hand: none needs PyPI while the tests run.
REVIEWED = {
    "format-code-task-001618": "pip-tools builds a PyPIRepository with the default index URL "
    "and checks its cache and download paths; constructing it makes no request",
    "format-code-task-002388": "files.pythonhosted.org URLs are expected strings in a PEP 700 "
    "JSON fixture; bandersnatch's mirror is mocked",
    "format-code-task-002427": "asserts on the text of mypy's --install-types hint; "
    "nothing is installed",
}


def added_lines(patch: str, *, command: bool) -> list[str]:
    """Added lines of the hidden patch: the test command's file, or the rest."""
    lines: list[str] = []
    current = ""
    for line in patch.splitlines():
        if line.startswith("diff --git a/"):
            current = line.split()[2][2:]
            continue
        is_command = current in {"mimo_test_command.sh", "test_commands.json"}
        if is_command == command and line.startswith("+") and not line.startswith("+++"):
            lines.append(line[1:])
    return lines


def pypi_at_test_time(task: Path) -> list[str]:
    """What in grading could reach PyPI: matching lines, prefixed by where."""
    patch = (task / "tests/test.patch").read_text(errors="replace")
    command = (
        added_lines(patch, command=True)
        + (task / "tests/test_command.sh").read_text(errors="replace").splitlines()
    )
    hits = [f"command: {line.strip()}" for line in command if FETCHES.search(line)]
    hits += [
        f"tests: {line.strip()}"
        for line in added_lines(patch, command=False)
        if TOUCHES_PYPI.search(line)
    ]
    return [hit[:200] for hit in hits]


def rationale(entry: dict) -> str:
    issue = (entry.get("issue") or {}).get("url") or "the upstream issue"
    return (
        f"close the PyPI answer leak: {entry['pypi_project']} "
        f"{entry['first_release_after_close']} was released after {issue} closed, so "
        "pip download fetches the fix; add pypi.org, files.pythonhosted.org and "
        "pypi.python.org to the answer-leak blocklist the harness appends to /etc/hosts, "
        "and re-embed environment/setup in the healthcheck payload that runs it. Setup "
        "runs before the blocklist and grading fetches nothing, so the verifier is unchanged"
    )


def main() -> None:
    rows = census()
    leaks = pypi_entries()
    done = records_by(TRANSFORM)
    tasks = sorted(t for t, row in rows.items() if row["leak_channel"] == "pypi_fix_released")
    out: dict[str, dict] = {}
    for task_id in tasks:
        row, entry, task = rows[task_id], leaks[task_id], original(task_id)
        hits = pypi_at_test_time(task)
        entry_out = {
            "label": row["label"],
            "split": row["split"],
            "image_mib": row["image_mib"],
            "pypi_project": entry["pypi_project"],
            "fixed_release": entry["first_release_after_close"],
            "issue_url": (entry.get("issue") or {}).get("url"),
            "repo": entry.get("repo_url"),
            "pypi_at_test_time": hits,
            "reviewed": REVIEWED.get(task_id),
        }
        if hits and task_id not in REVIEWED:
            out[task_id] = {**entry_out, "status": "unfixed_needs_pypi"}
            continue
        record = done.get(task_id)
        if record is None:
            blocklist = (task / BLOCKLIST).read_text()
            present = [h for h in PYPI_HOSTS if re.search(rf"\s{re.escape(h)}$", blocklist, re.M)]
            if present:
                raise SystemExit(f"{task_id}: blocklist already names {present}")
            record = derive(
                task,
                {BLOCKLIST: (blocklist.rstrip("\n") + "\n" + ADDED).encode()},
                transform=TRANSFORM,
                rationale=rationale(entry),
                created_by=CREATED_BY,
                inputs={
                    "pypi_project": entry["pypi_project"],
                    "fixed_release": entry["first_release_after_close"],
                    "issue_url": entry_out["issue_url"],
                    "hosts_added": list(PYPI_HOSTS),
                },
                parent_source=hf_source(task_id),
            )
        out[task_id] = {
            **entry_out,
            "status": "variant",
            "parent_digest": record.parent.digest,
            "variant_digest": record.variant_digest,
            "record": record_path(record),
            "package": package_rel(record),
        }
    (HERE / "leak_variants.json").write_text(json.dumps({"tasks": out}, indent=1) + "\n")
    statuses = [e["status"] for e in out.values()]
    print(
        f"{len(tasks)} pypi_fix_released tasks: {statuses.count('variant')} variants, "
        f"{statuses.count('unfixed_needs_pypi')} left unfixed (grading needs PyPI)"
    )


if __name__ == "__main__":
    main()
