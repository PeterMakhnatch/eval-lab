from __future__ import annotations

import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.harbor_telemetry import HARBOR_TELEMETRY_ENV, HARBOR_TELEMETRY_OFF

DEFAULT_MODEL = "openai/glm-5.3-flash"
DEFAULT_AGENT = "mini-swe-agent"
DEFAULT_ROOT = Path.home() / "Library" / "Application Support" / "evallab"
ANALYZE_CHECKS = ("reward_hacking", "task_specification")
PRICE_PER_MILLION = {"input": 0.15, "cached_input": 0.03, "output": 0.50}
BLIND_INPUT_POLICY = "evallab.reader_input/blind-v2"
_LABEL_LINE = re.compile(
    r"^[ \t]*(?:integrity|reward_gated)[ \t]*:[^\r\n]*(?:\r?\n|$)", re.MULTILINE
)


def flash_cost(input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
    if not 0 <= cached_tokens <= input_tokens:
        raise ValueError("Cached input must be between zero and total input")
    return (
        (input_tokens - cached_tokens) * PRICE_PER_MILLION["input"]
        + cached_tokens * PRICE_PER_MILLION["cached_input"]
        + output_tokens * PRICE_PER_MILLION["output"]
    ) / 1_000_000


def configure_evaluator(agent: str = DEFAULT_AGENT, model: str = DEFAULT_MODEL) -> None:
    if agent != DEFAULT_AGENT or model != DEFAULT_MODEL:
        raise ValueError(f"Unpriced evaluator route: {agent}/{model}")
    source = "ZAI_OPENAPI_API_KEY"
    if not os.environ.get(source):
        raise RuntimeError(f"{source} is unset; run with `keys run --`")
    os.environ.pop("MSWEA_API_KEY", None)
    os.environ["OPENAI_API_KEY"] = os.environ[source]
    for key in ("OPENAI_BASE_URL", "OPENAI_API_BASE"):
        os.environ[key] = "https://api.z.ai/api/paas/v4"


def resolve_trial_task(source: Path) -> Path:
    result = json.loads((source / "result.json").read_text())
    original = Path(result["config"]["task"]["path"])
    if (original / "instruction.md").is_file():
        return original
    spec_path = source.parent / "experiment-spec.json"
    if original.parent.name == ".exec-stage" and spec_path.is_file():
        spec = json.loads(spec_path.read_text())
        candidate = original.parent.parent.parent / spec["task_path"]
        if (candidate / "instruction.md").is_file():
            return candidate
    raise FileNotFoundError(f"Trial task is unavailable at {original}; provide --task")


def _optional_trial_task(source: Path) -> Path | None:
    try:
        return resolve_trial_task(source)
    except (OSError, KeyError, ValueError, TypeError, AttributeError):
        return None


def stage_trial(source: Path, root: Path = DEFAULT_ROOT, task: Path | None = None) -> Path:
    source = Path(source).resolve()
    dest = root / "analyze-src" / source.parent.name / source.name
    if dest.resolve() == source:
        raise ValueError("Analyze source and writable copy must differ")
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        dest,
        ignore=shutil.ignore_patterns(
            ".git",
            "analysis.json",
            "analysis.md",
            "processed",
            "watch",
            "reward-details.json",
            ".evallab-*",
            "laminar-trace.json",
        ),
    )
    result_path = dest / "result.json"
    result = json.loads(result_path.read_text())
    verifier = result.get("verifier_result")
    if verifier is not None and verifier.get("rewards") is not None:
        verifier["rewards"] = {"reward": verifier["rewards"].get("reward")}
    if task is not None:
        result["config"]["task"]["path"] = str(task.resolve())
        config_path = dest / "config.json"
        config = json.loads(config_path.read_text())
        config["task"]["path"] = str(task.resolve())
        config_path.write_text(json.dumps(config, indent=2))
    result_path.write_text(json.dumps(result, indent=2))
    reward_path = dest / "verifier" / "reward.json"
    if reward_path.exists():
        rewards = json.loads(reward_path.read_text())
        reward_path.write_text(json.dumps({"reward": rewards.get("reward")}))
    for pattern in ("*.txt", "*.log"):
        for path in (dest / "verifier").glob(pattern):
            text = path.read_text()
            blinded = _LABEL_LINE.sub("", text)
            if blinded != text:
                path.write_text(blinded)
    return dest


def stage_task(task: Path, source: Path, root: Path = DEFAULT_ROOT) -> Path:
    task = Path(task).resolve()
    dest = root / "analyze-task" / source.parent.name / source.name / task.name
    if dest.resolve() == task:
        raise ValueError("Task source and writable copy must differ")
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    detector_files = {
        "run_integrity.py",
        "integrity_core.py",
        "copy_check_vendored.py",
        "copy_check_v1.py",
        "out_of_base_read.py",
        "upstream_fetch.py",
        "grader_tamper.py",
    }

    def ignore(directory: str, names: list[str]) -> set[str]:
        relative = Path(directory).relative_to(task)
        excluded = {
            name
            for name in names
            if name in {".git", "__pycache__"} or name.startswith(".evallab-")
        }
        if relative.parts and relative.parts[0] == "tests":
            excluded.update(
                name
                for name in names
                if name in detector_files or name in {"integrity", "reward_gated"}
            )
        return excluded

    shutil.copytree(task, dest, ignore=ignore)
    verifier = dest / "tests" / "test.sh"
    if verifier.exists():
        text = verifier.read_text()
        marker = "# --- rewardkit-integrity@"
        if marker in text:
            verifier.write_text(text.split(marker, 1)[0])
    return dest


def _agent_kwargs(cost_limit: float) -> dict[str, Any]:
    return {
        "cost_limit": str(cost_limit),
        "max_tokens": 4096,
        "config": {"model": {"model_kwargs": {"extra_body": {"thinking": {"type": "disabled"}}}}},
        "litellm_model_registry": {
            DEFAULT_MODEL: {
                "input_cost_per_token": 0.15e-6,
                "cache_read_input_token_cost": 0.03e-6,
                "output_cost_per_token": 0.50e-6,
                "max_input_tokens": 200_000,
                "max_output_tokens": 4096,
                "litellm_provider": "openai",
                "mode": "chat",
            }
        },
    }


async def run_analyze(
    source: Path,
    *,
    root: Path = DEFAULT_ROOT,
    task: Path | None = None,
    model: str = DEFAULT_MODEL,
    agent: str = DEFAULT_AGENT,
    job_name: str | None = None,
    cost_limit: float = 0.03,
) -> tuple[Any, Path]:
    os.environ[HARBOR_TELEMETRY_ENV] = HARBOR_TELEMETRY_OFF
    from harbor.analyze.analyzer import run_analyze as harbor_run  # ty: ignore[unresolved-import]
    from harbor.models.environment_type import EnvironmentType  # ty: ignore[unresolved-import]

    configure_evaluator(agent, model)
    explicit = task if task is not None else _optional_trial_task(source)
    independent_task = stage_task(explicit, source, root) if explicit is not None else None
    staged = stage_trial(source, root, independent_task)
    report, job_dir = await harbor_run(
        path=staged,
        agent=agent,
        model=model,
        environment=EnvironmentType.DOCKER,
        n_concurrent=1,
        n_attempts=1,
        jobs_dir=root / "jobs",
        job_name=job_name,
        agent_kwargs=_agent_kwargs(cost_limit),
        quiet=True,
    )
    (job_dir / "input-policy.json").write_text(json.dumps({"policy": BLIND_INPUT_POLICY}))
    return report, job_dir


def parse_checks(data: dict[str, Any], expected: tuple[str, ...] = ()) -> tuple[dict, dict]:
    outcomes = {"pass": False, "fail": True, "not_applicable": None}
    checks = {name: None for name in expected}
    explanations = {name: "Reader did not decide." for name in expected}
    for name, result in (data.get("checks") or {}).items():
        outcome = result.get("outcome")
        if outcome not in outcomes:
            raise ValueError(f"Invalid Harbor outcome for {name}: {outcome!r}")
        checks[name] = outcomes[outcome]
        explanations[name] = str(result.get("explanation", ""))[:300]
    if data.get("error"):
        for name in checks:
            explanations[name] = str(data["error"])[-300:]
    return checks, explanations


def _reader_trial(job_dir: Path, source_name: str, reader: str) -> tuple[Path, dict]:
    prefix = "analyze-" if reader == "harbor_analyze" else "check-"
    matches = []
    for path in job_dir.glob("*/result.json"):
        data = json.loads(path.read_text())
        wrapper = Path(data.get("config", {}).get("task", {}).get("path", "")).name
        if wrapper == prefix + source_name:
            matches.append((path.parent, data))
    if len(matches) != 1:
        raise ValueError(f"Expected one {reader} result for {source_name}, got {len(matches)}")
    return matches[0]


def reader_usage(result: dict) -> dict:
    data = result.get("agent_result") or {}
    raw_input, raw_output = data.get("n_input_tokens"), data.get("n_output_tokens")
    if raw_input is None or raw_output is None:
        return {"tokens": None, "cached_tokens": None, "cost_usd": None}
    counts = {"input": int(raw_input), "output": int(raw_output)}
    cached = int(data.get("n_cache_tokens") or 0)
    return {
        "tokens": counts,
        "cached_tokens": cached,
        "cost_usd": flash_cost(counts["input"], counts["output"], cached),
    }


def _check_criteria() -> tuple[str, ...]:
    from harbor.analyze.models import load_rubric  # ty: ignore[unresolved-import]

    return tuple(criterion.name for criterion in load_rubric().criteria)


def write_verdict(
    job_dir: Path,
    *,
    source_name: str,
    source_job: str = "",
    task_name: str | None = None,
    root: Path = DEFAULT_ROOT,
    model: str = DEFAULT_MODEL,
) -> tuple[Path, dict]:
    reader = "harbor_check" if task_name is not None else "harbor_analyze"
    report_name = "check_report.json" if task_name is not None else "analysis.json"
    report = json.loads((job_dir / report_name).read_text())
    key = "task_name" if task_name is not None else "trial_name"
    rows = [row for row in report["results"] if row.get(key) == source_name]
    if len(rows) != 1:
        raise ValueError(f"Expected one report row for {source_name}, got {len(rows)}")
    row = rows[0]
    trial_dir, result = _reader_trial(job_dir, source_name, reader)
    artifact_name = "check-result.json" if task_name is not None else "analysis.json"
    artifact = trial_dir / "artifacts" / artifact_name
    raw_dest = root / "raw" / reader / source_job / source_name / artifact_name
    raw_dest.parent.mkdir(parents=True, exist_ok=True)
    raw_dest.write_text(artifact.read_text() if artifact.exists() else json.dumps(row, indent=2))
    usage = reader_usage(result)
    limit = float(result.get("config", {}).get("agent", {}).get("kwargs", {}).get("cost_limit", 0))
    if (
        row.get("error")
        and limit > 0
        and usage["cost_usd"] is not None
        and usage["cost_usd"] >= limit
    ):
        row = {**row, "checks": {}, "error": "budget cap"}
    expected = ANALYZE_CHECKS if task_name is None else _check_criteria()
    checks, explanations = parse_checks(row, expected)
    verdict = {
        "schema": "evallab.reader_verdict/v1",
        "reader": reader,
        "trial": source_name if task_name is None else "",
        "job": source_job,
        "checks": checks,
        "explanations": explanations,
        "model": model,
        "tokens": usage["tokens"],
        "cost_usd": usage["cost_usd"],
        "at": datetime.now(UTC).isoformat(),
        "raw": str(raw_dest.relative_to(root)),
    }
    policy_path = job_dir / "input-policy.json"
    if task_name is None and policy_path.exists():
        verdict["input_policy"] = json.loads(policy_path.read_text())["policy"]
    if row.get("error"):
        verdict["error"] = row["error"]
    dest = (
        root / "readers" / "_tasks" / task_name
        if task_name is not None
        else root / "readers" / source_job / source_name
    ) / f"{reader}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(verdict, indent=2))
    elapsed = (
        datetime.fromisoformat(result["finished_at"]) - datetime.fromisoformat(result["started_at"])
    ).total_seconds()
    receipt = {
        "reader": reader,
        "source": source_name,
        "job_dir": str(job_dir),
        "verdict": str(dest),
        "elapsed_seconds": elapsed,
        "error": row.get("error"),
        "cost_limit": limit,
        **usage,
    }
    (raw_dest.parent / "usage.json").write_text(json.dumps(receipt, indent=2))
    return dest, receipt
