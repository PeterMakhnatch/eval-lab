"""Command-line entry point for the QuixBugs Harbor adapter."""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from .adapter import QuixBugsAdapter

ADAPTER_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = ADAPTER_ROOT / "generated"
DEFAULT_SOURCE_URL = "https://github.com/jkoppel/QuixBugs.git"
DEFAULT_SOURCE_REF = "4257f44b0ff1181dedaedee6a447e133219fcebf"


def _run(command: list[str], cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def _checkout_source(destination: Path, source_url: str, source_ref: str) -> Path:
    """Fetch exactly one pinned QuixBugs revision into ``destination``."""
    checkout = destination / "QuixBugs"
    _run(["git", "init", "--quiet", str(checkout)])
    _run(["git", "remote", "add", "origin", source_url], cwd=checkout)
    _run(["git", "fetch", "--quiet", "--depth", "1", "origin", source_ref], cwd=checkout)
    _run(["git", "checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=checkout)
    actual = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if actual != source_ref:
        raise RuntimeError(f"Expected source commit {source_ref}, got {actual}")
    return checkout


def _validate_local_source(source_dir: Path, source_ref: str) -> Path:
    source_dir = source_dir.resolve()
    required = [
        source_dir / "LICENSE",
        source_dir / "python_programs",
        source_dir / "correct_python_programs",
        source_dir / "python_testcases",
        source_dir / "java_programs",
        source_dir / "correct_java_programs",
        source_dir / "java_testcases" / "junit",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise ValueError(f"Not a QuixBugs checkout; missing: {', '.join(missing)}")

    try:
        actual = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source_dir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise ValueError("--source-dir must be a git checkout at the pinned revision") from exc
    if actual != source_ref:
        raise ValueError(
            f"--source-dir is at {actual}; expected pinned QuixBugs commit {source_ref}"
        )
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=source_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if status:
        raise ValueError("--source-dir must be clean so generation matches the pinned commit")
    return source_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate Harbor tasks from the pinned QuixBugs benchmark"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write generated tasks (default: adapter/generated)",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Generate only the first N stable task IDs"
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="Atomically replace an existing output dataset"
    )
    parser.add_argument(
        "--task-ids",
        nargs="+",
        default=None,
        help=(
            "Only generate these stable IDs (for example quixbugs-python-gcd); "
            "bare program names select both language variants"
        ),
    )
    parser.add_argument(
        "--language",
        choices=("all", "python", "java"),
        default="all",
        help="Generate both language variants or one complete language split",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=None,
        help="Use an existing checkout at the pinned source revision instead of fetching",
    )
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL, help=argparse.SUPPRESS)
    parser.add_argument("--source-ref", default=DEFAULT_SOURCE_REF, help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be at least 1")

    temp_dir: Path | None = None
    try:
        if args.source_dir is not None:
            source_root = _validate_local_source(args.source_dir, args.source_ref)
        else:
            temp_dir = Path(tempfile.mkdtemp(prefix="quixbugs-source-"))
            source_root = _checkout_source(temp_dir, args.source_url, args.source_ref)

        adapter = QuixBugsAdapter(
            output_dir=args.output_dir.resolve(),
            source_root=source_root,
            source_url=args.source_url,
            source_ref=args.source_ref,
            language=args.language,
            limit=args.limit,
            overwrite=args.overwrite,
            task_ids=args.task_ids,
        )
        generated = adapter.run()
        logging.info("Generated %d task(s) in %s", len(generated), args.output_dir.resolve())
        return 0
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
