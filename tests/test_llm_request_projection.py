from __future__ import annotations

import json
import shutil
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from evallab.evidence.facts import rebuild_from_raw
from evallab.evidence.llm_request import LlmRequestProjectionError, project_llm_requests
from evallab.results import load_job


def _job(tmp_path: Path):
    source = Path(__file__).parent / "fixtures/explorer/jobs/job-pass"
    job_path = tmp_path / "runs/job-pass"
    shutil.copytree(source, job_path)
    trajectory = json.loads((job_path / "t1/agent/trajectory.json").read_text())
    trajectory["steps"] = []
    (job_path / "t1/agent/trajectory.json").write_text(json.dumps(trajectory))
    return load_job(job_path)


def _assistant(call_id: str, name: str, arguments: dict[str, object]):
    return {
        "role": "assistant",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }],
        "content": None,
    }


def _write_request(
    trial: Path,
    index: int,
    messages: list[dict[str, object]],
    *,
    offered: tuple[str, ...] = ("shell", "todo_write"),
    usage: tuple[int, int, int] = (0, 0, 0),
) -> Path:
    path = trial / f"agent/goose/logs/llm_request.{index}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    request = {
        "model_config": {"model_name": "glm-4.7"},
        "input": {
            "model": "glm-4.7",
            "messages": messages,
            "tools": [
                {"type": "function", "function": {"name": name, "parameters": {}}}
                for name in offered
            ],
        },
    }
    event = {
        "data": {
            "created": 1_756_000_000 + index,
            "usage": {
                "prompt_tokens": usage[0],
                "completion_tokens": usage[1],
                "cached_tokens": usage[2],
            },
        }
    }
    path.write_text(json.dumps(request) + "\n" + json.dumps(event) + "\n")
    return path
