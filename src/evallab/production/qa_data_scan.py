"""``qa-data scan``: deterministic Scout text scanners over a blinded ``export --format scout`` file.

No model call and no writes. The scanners need ``inspect_scout``, which the main virtualenv does not
install but the deployed scout-watch runtime does; when it is missing here, the same scan runs in a
child process under that runtime's interpreter with this checkout's ``src`` on ``PYTHONPATH``.
Only the four text scanners run: the six ``rule_*`` scanners need a native ``metadata.trial_dir``,
which a blinded export does not carry.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from evallab.production.qa_data import Budget, QaDataError

TEXT_SCANNERS = (
    "repeated_assistant_message", "harness_parse_errors", "native_tool_call_markup", "restored_tool_calls",
)
MAX_TRANSCRIPTS = 200
RUNTIME = Path.home() / "Library/Application Support/evallab/scout-watch/runtime"


async def _scan(transcripts: Sequence[Any], module: Any, names: Sequence[str]) -> list[dict[str, Any]]:
    rows = []
    for transcript in transcripts:
        for name in names:
            result = await getattr(module, name)()(transcript)
            results = result if isinstance(result, list) else [result]  # some scanners return one Result per finding
            rows.append({
                "blind_id": transcript.transcript_id, "messages": len(transcript.messages or []), "scanner": name,
                "results": len(results), "values": [item.value for item in results],
                "references": sum(len(item.references or []) for item in results),
            })
    return rows


def _transcript_lines(path: Path) -> list[str]:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) > MAX_TRANSCRIPTS:
        raise QaDataError(f"at most {MAX_TRANSCRIPTS} transcripts per scan")  # no count: a size is a probe channel
    return lines


def _scan_here(path: Path, names: Sequence[str]) -> dict[str, Any]:
    from evallab.readers.scout_clouddb import _scanner_module

    transcript_type = importlib.import_module("inspect_scout").Transcript

    lines = _transcript_lines(path)
    transcripts = [transcript_type.model_validate_json(line) for line in lines]
    rows = asyncio.run(_scan(transcripts, _scanner_module(), names))
    return {
        "transcripts": len(transcripts), "rows": rows,
        "inspect_scout": importlib.metadata.version("inspect-scout"), "model_calls": 0,
    }


def _runtime_python() -> Path | None:
    revisions = sorted(
        (entry for entry in RUNTIME.iterdir() if (entry / ".venv/bin/python").is_file()),
        key=lambda entry: entry.stat().st_mtime, reverse=True,
    ) if RUNTIME.is_dir() else []
    return revisions[0] / ".venv/bin/python" if revisions else None


def _fit(payload: dict[str, Any], budget: Budget) -> dict[str, Any]:
    """Keep whole scanner rows that fit the budget; count the rest rather than refuse the call."""
    frame = {**payload, "rows": [], "omitted_rows": len(payload["rows"]), "truncated": True}
    budget.take(frame)  # the fixed fields count too: rows are charged on top of them
    rows = [row for row in payload["rows"] if budget.take([row])]
    return {**frame, "rows": rows, "omitted_rows": len(payload["rows"]) - len(rows), "truncated": len(rows) < len(payload["rows"])}


def scan(transcripts: Path, scanners: Sequence[str] | None, budget: Budget) -> dict[str, Any]:
    """Scanner rows for every transcript: messages, results, values and reference counts."""
    names = list(scanners or TEXT_SCANNERS)
    unknown = [name for name in names if name not in TEXT_SCANNERS]
    if unknown:
        raise QaDataError(f"unknown scanner {unknown[0]!r}; choose from {', '.join(TEXT_SCANNERS)}")
    path = Path(transcripts).expanduser()
    if not path.is_file():
        raise QaDataError("pass the transcripts.jsonl file of an `export --format scout` directory")  # never echoes the input
    _transcript_lines(path)  # the size refusal never depends on which runtime would scan
    if importlib.util.find_spec("inspect_scout") is not None:
        return _fit(_scan_here(path, names), budget)
    interpreter = _runtime_python()
    if interpreter is None:
        raise QaDataError("inspect_scout is not installed and no scout-watch runtime was found")
    source_root = Path(__file__).resolve().parents[2]
    done = subprocess.run(
        [str(interpreter), "-B", "-m", "evallab.production.qa_data_scan", str(path), *names],
        capture_output=True, text=True, check=False, timeout=600,
        env={**os.environ, "PYTHONPATH": str(source_root), "PYTHONWARNINGS": "ignore", "EVALLAB_DISABLE_LOCAL_DOCKER": "1"},
    )
    lines = done.stdout.strip().splitlines()
    if done.returncode or not lines:
        raise QaDataError(f"scan failed in the scout runtime: {done.stderr.strip()[-200:] or done.stdout[-200:]}")
    payload = json.loads(lines[-1])
    if "error" in payload:
        raise QaDataError(payload["error"])
    return _fit(payload, budget)


def _main(argv: Sequence[str]) -> int:
    """Child entry point: ``python -m evallab.production.qa_data_scan FILE [SCANNER ...]``."""
    try:
        print(json.dumps(_scan_here(Path(argv[0]), list(argv[1:]) or list(TEXT_SCANNERS))))
    except QaDataError as exc:
        print(json.dumps({"error": str(exc)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
