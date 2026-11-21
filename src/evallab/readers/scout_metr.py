from __future__ import annotations

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
SCORE_MIN, SCORE_MAX = 0.0, 10.0


def configure_provider() -> None:
    configure_evaluator()


class JsonObjectTransport(Model):

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
    from inspect_scout import transcripts_db  # ty: ignore[unresolved-import]

    db = transcripts_db(db_dir)
    await db.connect()
    try:
        await db.insert(list(transcripts.values()))
    finally:
        await db.disconnect()


def run_scan(db_dir: str, scan_dir: str) -> Any:
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
