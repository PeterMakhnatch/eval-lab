"""Host subprocess helpers for the sheet-recalc hidden verifiers.

Both task packages ship the same hidden verifier (``tests/test_sheet.py``) and
read the submission path from ``SHEET_RECALC_SUBMITTED``. This module runs that
verifier in a child pytest process, never by import, so the verifier's
module-level formula load and its scratch-package runs stay out of the root
suite.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("sheet-recalc", "sheet-recalc-cutoff")


def package_path(package: str, relative: str) -> Path:
    return ROOT / "library" / "tasks" / package / relative


def run_verifier(
    package: str,
    submission: Path,
    *,
    selector: str | None,
    timeout: float,
    args: Sequence[str] = (),
) -> subprocess.CompletedProcess[str]:
    verifier = package_path(package, "tests/test_sheet.py")
    assert verifier.is_file(), verifier
    assert submission.is_file(), submission
    command = [
        sys.executable,
        "-I",
        "-m",
        "pytest",
        "-q",
        "-c",
        os.devnull,
        "--noconftest",
        "--import-mode=importlib",
        "-p",
        "no:cacheprovider",
        f"--rootdir={verifier.parent}",
    ]
    if selector is not None:
        command += ["-k", selector]
    command.extend(args)
    command.append(str(verifier))
    return subprocess.run(
        command,
        cwd=ROOT,
        env={**os.environ, "SHEET_RECALC_SUBMITTED": str(submission)},
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def transcript(completed: subprocess.CompletedProcess[str]) -> str:
    return completed.stdout + completed.stderr
