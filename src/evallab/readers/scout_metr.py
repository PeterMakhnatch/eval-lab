"""METR reward_hacking scanner (native prompts; Z.ai transport shim).

Pipeline (native scanner with structured-output transport normalization):

1. ``inspect_scout.sources`` ``atif()`` converts each Harbor-shaped trial's
   ``agent/trajectory.json`` (ATIF) into a Scout ``Transcript``.
2. Transcripts are indexed into a Scout Parquet transcript database.
3. ``inspect_scout.scan()`` runs METR's ``reward_hacking_scanner`` factory
   (``metr_scanners.reward_hacking``) with ``openai/glm-5.3-flash`` on the
   Z.ai OpenAI-compatible endpoint (chat-completions transport).
4. Per-transcript ``value`` (0-10, higher = more hack-like), ``explanation``
   and ``model_usage`` are read back from the scan results and normalized
   into HAR-187 verdict files.

The METR scanner defines no decision threshold (it emits a 0-10 score,
taking the max over chunks), so verdicts preserve the score and leave
``flagged`` null. The scorecard, not this reader, chooses operating points.

Runtime: inspect-scout 0.5.3 and metr-scanners 0.1.11 are not repo
dependencies; run with a Python 3.13 env that has them plus this repo
(``keys run -- <env>/bin/python -m evallab.readers.scout_metr ...``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from inspect_ai.model import GenerateConfig, Model  # ty: ignore[unresolved-import]

from evallab.readers.harbor_analyze import configure_evaluator, flash_cost

READER = "scout_metr"
MODEL = "openai/glm-5.3-flash"
BASE_URL = "https://api.z.ai/api/paas/v4"
IMPLEMENTATION = "METR reward_hacking scanner (native prompts; Z.ai transport shim)"
#: Scanner's native score range (metr_scanners.reward_hacking.RewardHacking).
SCORE_MIN, SCORE_MAX = 0.0, 10.0


def configure_provider() -> None:
    """Select the budgeted Z.ai route; requires ``keys run --``."""
    configure_evaluator()


class JsonObjectTransport(Model):
    """Structured-output dialect shim for the Z.ai endpoint (transport only).

    Two endpoint-dictated translations, both below the scanner's own
    pydantic validation (which stays the final judge):

    1. ``json_schema`` -> ``json_object``: probed 2026-10-07, Z.ai's
       ``/paas/v4`` chat-completions endpoint ignores ``json_schema``
       (free-text Markdown comes back) but honors ``json_object``.
    2. ``answer`` -> ``score``: under ``json_object`` the model sometimes
       names the numeric field ``answer`` while keeping the prompt's
       ``reason``/``quotes`` fields. The key is renamed only inside
       otherwise-valid JSON; native ``RewardHacking`` validation still
       accepts or rejects the result.

    Anything that is not valid JSON passes through untouched (the scanner's
    retry/error path handles it). No prompts are copied and no Markdown or
    free text is ever parsed. Scanner prompts, chunking, retries and
    pydantic validation are unchanged.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def generate(self, input: Any, *args: Any, **kwargs: Any) -> Any:
        config = kwargs.get("config") or GenerateConfig()

        if schema_requested := getattr(config, "response_schema", None) is not None:
            extra = dict(config.extra_body or {})
            extra.setdefault("response_format", {"type": "json_object"})
            config = config.model_copy(update={"response_schema": None, "extra_body": extra})
            kwargs["config"] = config
        output = await self._inner.generate(input, *args, **kwargs)
        if schema_requested:
            repaired = self._alias_answer_key(getattr(output, "completion", None))
            if repaired is not None:
                output = output.model_copy(update={"completion": repaired})
        return output

    @staticmethod
    def _alias_answer_key(completion: Any) -> str | None:
        """Rename a top-level ``answer`` key to ``score`` in valid JSON only."""
        if not isinstance(completion, str):
            return None
        try:
            data = json.loads(completion)
        except ValueError:
            return None
        if isinstance(data, dict) and "score" not in data and "answer" in data:
            data["score"] = data.pop("answer")
            return json.dumps(data)
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_inner"], name)


def scan_model() -> Any:
    """Provider model behind the transport shim (call after configure)."""
    from inspect_ai.model import get_model  # ty: ignore[unresolved-import]

    inner = get_model(MODEL, base_url=BASE_URL, responses_api=False)
    return JsonObjectTransport(inner)


__all__ = [
    "READER",
    "MODEL",
    "BASE_URL",
    "IMPLEMENTATION",
    "JsonObjectTransport",
    "scan_model",
    "configure_provider",
    "summarize_usage",
    "parse_value",
    "to_verdict",
]


def summarize_usage(model_usage: dict[str, Any]) -> dict[str, int]:
    """Collapse Scout per-model usage into verdict token totals.

    ``input`` is the true prompt-token total (base input plus any cached
    tokens, matching :func:`flash_cost` semantics); ``cached`` counts cache
    reads, which Z.ai bills at the reduced cached rate.
    """
    base = cached_read = cached_write = output = 0
    for usage in (model_usage or {}).values():
        if isinstance(usage, str):
            try:
                usage = json.loads(usage)
            except ValueError:
                continue
        if not isinstance(usage, dict):
            continue
        base += int(usage.get("input_tokens") or 0)
        cached_read += int(usage.get("input_tokens_cache_read") or 0)
        cached_write += int(usage.get("input_tokens_cache_write") or 0)
        output += int(usage.get("output_tokens") or 0)
    return {
        "input": base + cached_read + cached_write,
        "cached": cached_read,
        "output": output,
    }


def parse_value(value: Any) -> float | None:
    """Normalize the scanner's native ``value`` to a 0-10 score or None.

    Mirrors the scanner's own pydantic coercion (``RewardHacking.score`` is a
    float, so lax validation accepts numeric strings): the chunked scanner
    returns ``NOANSWER`` (``'N'``) when no chunk yields a valid score, and
    anything outside 0-10 is treated as no-score rather than clipped.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, (int, float)):
        score = float(value)
        return score if SCORE_MIN <= score <= SCORE_MAX else None
    return None


def to_verdict(
    *,
    row_id: str,
    value: Any,
    explanation: Any,
    model_usage: dict[str, Any] | None,
    raw: str,
) -> dict[str, Any]:
    """Build one HAR-187 verdict from a native scan result row."""
    score = parse_value(value)
    tokens = summarize_usage(model_usage or {})
    cost = flash_cost(tokens["input"], tokens["output"], tokens["cached"])
    return {
        "id": row_id,
        "reader": READER,
        "implementation": IMPLEMENTATION,
        "flagged": None,
        "score": score,
        "explanation": str(explanation or ""),
        "model": MODEL,
        "tokens": tokens,
        "cost_usd": cost,
        "raw": raw,
    }


def _require_scout_env() -> None:
    missing = []
    try:
        import inspect_scout  # noqa: F401  # ty: ignore[unresolved-import]
    except ImportError:
        missing.append("inspect-scout")
    try:
        import metr_scanners  # noqa: F401  # ty: ignore[unresolved-import]
    except ImportError:
        missing.append("metr-scanners")
    try:
        import harbor  # noqa: F401  # ty: ignore[unresolved-import]
    except ImportError:
        missing.append("harbor (for Scout's atif source)")
    if missing:
        raise RuntimeError(
            "scout_metr needs a Python env with inspect-scout, metr-scanners and harbor "
            "(see the module docstring); missing: " + ", ".join(missing)
        )


async def load_transcripts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Load each row's trial trajectory natively via Scout's ``atif()``.

    Returns ``{eval_id: Transcript}``. Raises ``KeyError`` listing rows
    whose trajectory is missing or fails ATIF validation (the eval-set
    contract requires Harbor-shaped ATIF trial dirs).
    """
    from inspect_scout.sources._atif.transcripts import atif  # ty: ignore[unresolved-import]

    transcripts: dict[str, Any] = {}
    failures: list[str] = []
    for row in rows:
        trial = Path(row["trial_dir"])
        traj = trial / "agent" / "trajectory.json"
        found = None
        try:
            async for transcript in atif(path=str(traj)):
                found = transcript
                break
        except Exception:
            found = None
        if found is None:
            failures.append(f"{row['id']}: no importable ATIF at {traj}")
        else:
            transcripts[row["id"]] = found
    if failures:
        raise KeyError("ATIF load failures:\n" + "\n".join(failures))
    return transcripts


async def build_db(transcripts: dict[str, Any], db_dir: str) -> None:
    """Index native Transcripts into a fresh Scout Parquet database."""
    from inspect_scout import transcripts_db  # ty: ignore[unresolved-import]

    db = transcripts_db(db_dir)
    await db.connect()
    try:
        await db.insert(list(transcripts.values()))
    finally:
        await db.disconnect()


def run_scan(db_dir: str, scan_dir: str) -> Any:
    """Run the native METR scanner over the whole database in one scan job."""
    from inspect_scout import scan  # ty: ignore[unresolved-import]
    from inspect_scout._transcript.factory import (  # ty: ignore[unresolved-import]
        transcripts_from_db,
    )
    from metr_scanners.reward_hacking import (  # ty: ignore[unresolved-import]
        reward_hacking_scanner,
    )

    return scan(
        scanners=[reward_hacking_scanner()],
        transcripts=transcripts_from_db(db_dir),
        model=scan_model(),
        model_config=GenerateConfig(temperature=0),
        scans=scan_dir,
        display="none",
        log_level="warning",
        max_processes=1,
    )


def read_reports(scan_location: str) -> list[dict[str, Any]]:
    """Read one report dict per transcript from a completed native scan.

    Uses the native results-frame schema (``rows="results"``): ``value`` is
    the scanner's numeric score (or NA when the scanner errored),
    ``scan_model_usage`` carries per-model token usage, and the
    ``scanner_*`` columns identify the exact scanner build.
    """
    import pandas as pd  # ty: ignore[unresolved-import]
    from inspect_scout import scan_results_df  # ty: ignore[unresolved-import]

    def clean(value: Any) -> Any:
        if value is None:
            return None
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            return None
        return value.item() if hasattr(value, "item") else value

    results = scan_results_df(scan_location, rows="results")
    reports: list[dict[str, Any]] = []
    for _, record in results.scanners["reward_hacking_scanner"].iterrows():
        get = record.get if hasattr(record, "get") else record.__getitem__
        model_usage = clean(get("scan_model_usage"))
        if isinstance(model_usage, str):
            try:
                model_usage = json.loads(model_usage)
            except ValueError:
                model_usage = {}
        reports.append(
            {
                "transcript_id": clean(get("transcript_id")),
                "value": clean(get("value")),
                "explanation": clean(get("explanation")),
                "model_usage": model_usage or {},
                "scan_error": clean(get("scan_error")),
                "provenance": {
                    "scanner": clean(get("scanner_name")),
                    "version": clean(get("scanner_version")),
                    "package_version": clean(get("scanner_package_version")),
                    "params": clean(get("scanner_params")),
                },
            }
        )
    return reports


async def scan_rows(
    rows: list[dict[str, Any]],
    *,
    workdir: Path,
    verdict_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Native end-to-end scan of eval-set rows; writes verdicts, returns spend."""
    _require_scout_env()
    configure_provider()
    db_dir = str(workdir / "scout_metr_db")
    scan_dir = str(workdir / "scout_metr_scan")

    transcripts = await load_transcripts(rows)
    owners: dict[str, str] = {}
    collisions: list[str] = []
    for eval_id, transcript in transcripts.items():
        owner = owners.setdefault(transcript.transcript_id, eval_id)
        if owner != eval_id:
            collisions.append(f"{transcript.transcript_id}: {owner} vs {eval_id}")
    if collisions:
        raise RuntimeError(
            "duplicate Scout transcript_ids (DB would silently drop rows): " + "; ".join(collisions)
        )
    await build_db(transcripts, db_dir)
    status = await asyncio.to_thread(run_scan, db_dir, scan_dir)
    if not status.complete:
        print(
            f"WARNING: native scan incomplete at {status.location}: "
            + "; ".join(str(e)[:200] for e in status.errors[:5]),
            flush=True,
        )
    by_transcript = {t.transcript_id: eval_id for eval_id, t in transcripts.items()}
    seen: set[str] = set()
    verdicts: list[dict[str, Any]] = []
    total = {"input": 0, "cached": 0, "output": 0}
    for report in await asyncio.to_thread(read_reports, status.location):
        row_id = by_transcript.get(report["transcript_id"], report["transcript_id"])
        seen.add(row_id)
        explanation = report["explanation"] or ""
        if report["scan_error"] and report["value"] is None:
            explanation = f"scanner error (abstain): {report['scan_error'][:500]}"
        verdict = to_verdict(
            row_id=row_id,
            value=report["value"],
            explanation=explanation,
            model_usage=report["model_usage"],
            raw=status.location,
        )
        dest = verdict_dir / f"{row_id}.json"
        dest.write_text(json.dumps(verdict, indent=1) + "\n")
        verdicts.append(verdict)
        for key in total:
            total[key] += verdict["tokens"][key]
    for row in rows:
        if row["id"] not in seen:
            verdict = to_verdict(
                row_id=row["id"],
                value=None,
                explanation="no scan report for this row (abstain)",
                model_usage={},
                raw=status.location,
            )
            (verdict_dir / f"{row['id']}.json").write_text(json.dumps(verdict, indent=1) + "\n")
            verdicts.append(verdict)
    spend = {
        "reader": READER,
        "ids": [r["id"] for r in rows],
        "tokens": total,
        "cost_usd": sum(v["cost_usd"] for v in verdicts),
        "at": datetime.now(UTC).isoformat(),
    }
    return verdicts, spend


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evalset", required=True)
    parser.add_argument("--verdict-dir", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--spend", required=True)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    rows = [
        json.loads(line) for line in Path(args.evalset).read_text().splitlines() if line.strip()
    ]
    if args.limit is not None:
        rows = rows[: args.limit]
    verdict_dir = Path(args.verdict_dir)
    verdict_dir.mkdir(parents=True, exist_ok=True)
    verdicts, spend = asyncio.run(
        scan_rows(rows, workdir=Path(args.workdir), verdict_dir=verdict_dir)
    )
    with open(args.spend, "a") as handle:
        handle.write(json.dumps(spend) + "\n")
    print(json.dumps({"verdicts": len(verdicts), "spend": spend}, indent=1))


if __name__ == "__main__":
    main()
