"""Score the separate continuation using the byte-pinned original TT-7 scorer.

Run only after the parent closes readings: --readings-closed is mandatory.
Inputs are fixed to this campaign's manifest, combined private snapshot, and
private privacy audit. --out may select only a fresh aggregate destination
inside this campaign's public or private namespace. No provider calls are made.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType


def continuation_module() -> ModuleType:
    path = Path(__file__).resolve().with_name("finalize.py")
    spec = importlib.util.spec_from_file_location("tt7_continuation_finalize", path)
    if spec is None or spec.loader is None:
        raise ImportError("continuation finalizer is unavailable at its known sibling path")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fresh_output(path: Path, root: Path, private: Path) -> Path:
    target = path.expanduser().resolve()
    if not any(target.is_relative_to(allowed.resolve()) for allowed in (root, private)):
        raise ValueError("aggregate output must stay inside this campaign's public or private namespace")
    json_path = target if target.suffix.lower() == ".json" else target / "score.json"
    markdown_path = json_path.with_suffix(".md")
    if any(candidate.exists() or candidate.is_symlink() for candidate in (json_path, markdown_path)):
        raise ValueError("aggregate output already exists; never overwrite evidence or linked old files")
    if any(json_path.parent.glob(f"{json_path.stem}-replays-*.json")):
        raise ValueError("aggregate replay output already exists; choose a fresh continuation destination")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readings-closed", action="store_true", required=True,
                        help="Parent attests all continuation reading has stopped; permits the local label join")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent,
                        help="Fresh aggregate JSON file or directory inside this campaign's public/private namespace")
    args = parser.parse_args()
    previous_bytecode, previous_argv = sys.dont_write_bytecode, sys.argv
    sys.dont_write_bytecode = True
    try:
        continuation = continuation_module()
        campaign, receipt, lineage = continuation.campaign_inputs()
        manifest, _ = continuation.load_object(continuation.MANIFEST)
        finalized_at = datetime.fromisoformat(manifest["at"])
        if (finalized_at.utcoffset() is None
                or finalized_at < datetime.fromisoformat(receipt["finished_at"])):
            raise ValueError("continuation manifest must be finalized after the terminal phase receipt")
        destination = fresh_output(args.out, continuation.OUT, continuation.PRIVATE)
        with continuation.original_modules() as (common, scorer):
            expected = continuation.build_manifest(common, scorer, campaign, receipt, lineage,
                                                   at=manifest["at"])
            if manifest != expected:
                raise ValueError("manifest no longer matches the pinned original arms and separate terminal campaign")
            # Pass explicit paths to the original parser; neither its globals nor
            # its __file__ are rewritten. The original main retains every metric,
            # Wilson CI, evidence/resource calculation, retained baseline, and pair.
            sys.argv = [str(continuation.ORIGINAL / "score.py"),
                        "--readings-finalized", str(continuation.MANIFEST),
                        "--results", str(continuation.SNAPSHOT),
                        "--privacy-audit", str(continuation.PRIVACY_AUDIT),
                        "--out", str(destination)]
            scorer.main()
    finally:
        sys.argv = previous_argv
        sys.dont_write_bytecode = previous_bytecode


if __name__ == "__main__":
    main()
