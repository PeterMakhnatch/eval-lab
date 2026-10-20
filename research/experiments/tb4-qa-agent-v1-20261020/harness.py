"""QA agent v1 tool host: v0's source tools with the diagnosed limits fixed.

One JSON request per stdin line, one JSON response per stdout line. A model
session is bound to one TB4 task through the sealed agent view (``EnvTools``
with ``--view``, exactly as v0's frozen campaign bound it) under an opaque
session id, so tool observations never carry the task name or digest.

v1 changes against v0 (``22eac188``, campaign ``qa2-frozen-test-20261020-01``):

- tool cap 8 -> 60 calls per task; wall clock 120 s -> 900 s per task;
- ``task-info`` reads up to 2,000 lines per call (v0: 200, refused above it;
  default 100) and clips a longer range with a continuation note;
- any number of ``record-finding`` calls (v0's prompt never said so and its
  8-call cap left room for at most one);
- source-only tool surface: ``task-info`` (read/grep/inventory), ``diff`` and
  ``record-finding``. v0 listed runtime tools that could not run (Docker
  admission unavailable); v1 does not offer them.

Findings are recorded by ``EnvTools.record-finding`` itself (canonical class,
bound subject, resolvable ``path:line`` citations, ``not_run``, never
self-verified). The host fills subject, reviewer and category from the bound
task and the declared class, so the model never needs the task identity.

Run from the repository root with the locked interpreter:
``.venv/bin/python research/experiments/tb4-qa-agent-v1-20261020/harness.py serve --out DIR``
``.venv/bin/python research/experiments/tb4-qa-agent-v1-20261020/harness.py prompt --session S``
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.qa.env import EnvTools
from evallab.qa.env_playbooks import defect_classes, load_playbooks

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SEED = 20261020
UPSTREAM = Path.home() / "Developer/eval-lab-data/tb4-20261020/upstream-git-v4.0.0/terminal-bench"
VIEWS = Path.home() / "Developer/eval-lab-results/2026-10-20/tb4-audit-v1/agent-inputs"
MAX_TOOL_CALLS = 60
MAX_SECONDS = 900.0
MODEL = "anthropic/claude-opus-5-5"
AGENT = "qa-env-audit-v1"
TOOLS = ("task-info", "diff", "record-finding")
#: Same class -> key-category conversion the plain-read baseline (#1119) applied to its findings.
CATEGORY = {
    "weak_check": "weak", "unstated_requirement": "prompt_misaligned",
    "instruction_grader_contradiction": "prompt_misaligned", "grader_tamperable": "tamperable",
    "answer_reachable": "reachable", "grader_logic_bug": "broken", "overstrict_oracle": "broken",
    "wrong_answer_key": "broken", "env_mismatch_or_flaky": "broken",
    "infeasible_as_written": "broken", "exploitable_llm_judge": "weak",
}
PROMPT_TEMPLATE = HERE / "prompt_v1.md"


def session_id(task: str) -> str:
    return "q-" + hashlib.sha256(f"tb4-qa-v1:{SEED}:{task}".encode()).hexdigest()[:8]


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def render_prompt(session: str) -> str:
    classes = "\n".join(f"- {name}" for name in defect_classes())
    return (
        PROMPT_TEMPLATE.read_text(encoding="utf-8")
        .replace("{SESSION}", session)
        .replace("{MAX_TOOL_CALLS}", str(MAX_TOOL_CALLS))
        .replace("{MAX_MINUTES}", str(int(MAX_SECONDS // 60)))
        .replace("{CLASSES}", classes)
        .replace("{PLAYBOOKS}", load_playbooks().strip())
    )


class Session:
    def __init__(self, sid: str, task: str, split: str, out: Path) -> None:
        self.sid, self.task, self.split = sid, task, split
        self.dir = out / "runs" / sid
        self.tools = EnvTools(
            UPSTREAM / task, self.dir, repo_root=REPO, max_seconds=MAX_SECONDS,
            view_dir=VIEWS / split,
        )
        self.started = time.monotonic()
        self.deadline = self.started + MAX_SECONDS
        self.tools.deadline = self.deadline
        self.calls = 0
        self.errors = 0
        self.findings: list[dict[str, Any]] = []
        self.final: dict[str, Any] | None = None
        self.stop: str | None = None
        (self.dir / "agent").mkdir(parents=True, exist_ok=True)
        self.journal = self.dir / "agent" / "journal.jsonl"
        self.record_path = self.dir / "agent" / "session.json"
        self.started_at = utcnow()
        self._log("session_started", task=task, split=split,
                  budgets={"max_tool_calls": MAX_TOOL_CALLS, "max_seconds": MAX_SECONDS})

    def _log(self, event: str, **fields: Any) -> None:
        with self.journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": utcnow(), "event": event, **fields}, default=str) + "\n")

    def persist(self) -> None:
        record = {
            "schema": "qa.env.audit.v1-omp/v1", "session": self.sid, "task": self.task,
            "split": self.split, "task_id": self.tools.task_id, "task_digest": self.tools.task_digest,
            "model": MODEL, "started_at": self.started_at, "ended_at": utcnow(),
            "seconds": round(time.monotonic() - self.started, 3), "tool_calls": self.calls,
            "tool_errors": self.errors, "budgets": {"max_tool_calls": MAX_TOOL_CALLS,
                                                    "max_seconds": MAX_SECONDS},
            "recorded_findings": self.findings, "final": self.final, "stop": self.stop,
        }
        self.record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")

    def budgets(self) -> dict[str, Any]:
        return {"remaining_tool_calls": MAX_TOOL_CALLS - self.calls,
                "remaining_seconds": max(0, int(self.deadline - time.monotonic()))}

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if self.final is not None:
            return {"ok": False, "error": "session already returned its final"}
        if tool not in TOOLS:
            return {"ok": False, "error": f"tool must be one of {', '.join(TOOLS)}"}
        if self.calls >= MAX_TOOL_CALLS:
            self.stop = self.stop or "tool_budget_exhausted"
            return {"ok": False, "error": "tool budget spent: call qa_final now",
                    "budgets": self.budgets()}
        if time.monotonic() >= self.deadline:
            self.stop = self.stop or "time_budget_exhausted"
            return {"ok": False, "error": "time budget spent: call qa_final now",
                    "budgets": self.budgets()}
        self.calls += 1
        if tool == "record-finding":
            args = self._finding_args(args)
        try:
            observation = self.tools.call(tool, args)
        except Exception as exc:  # noqa: BLE001 - every tool failure is an observation
            self.errors += 1
            message = f"{type(exc).__name__}: {exc}"[:4000]
            self._log("tool_error", tool=tool, args=args, error=message)
            self.persist()
            return {"ok": False, "error": message, "budgets": self.budgets()}
        result = observation["result"]
        self._log("tool_call", tool=tool, args=args, observation=observation)
        if tool == "record-finding":
            self.findings.append({"path": result["path"], "defect_class": result["defect_class"]})
            result = {"recorded": True, "defect_class": result["defect_class"],
                      "status": result["status"], "evidence_basis": result["evidence_basis"],
                      "finding_number": len(self.findings)}
        elif tool == "task-info" and not args:
            result = {k: v for k, v in result.items() if k not in {"finding_schema", "tools",
                                                                   "tool_arguments"}}
        self.persist()
        return {"ok": True, "result": result, "budgets": self.budgets()}

    def _finding_args(self, args: dict[str, Any]) -> dict[str, Any]:
        cls = args.get("defect_class")
        return {
            "defect_class": cls,
            "finding": {
                "schema": "qa.finding/v1",
                "subject": {"kind": "task", "task_id": self.tools.task_id,
                            "task_digest": self.tools.task_digest},
                "stage": "source_review", "verdict": "fail",
                "category": CATEGORY.get(cls, "weak"),
                "severity": args.get("severity"), "fault": args.get("fault"),
                "summary": args.get("summary"), "evidence_refs": args.get("evidence_refs") or [],
                "measurement": "not_run", "root_cause": [cls],
                "refutation": {"status": "unverified"},
                "reviewer": {"agent": AGENT, "model": MODEL, "cost_usd": 0},
            },
        }

    def finish(self, summary: str, limitations: list[str]) -> dict[str, Any]:
        if self.final is not None:
            return {"ok": False, "error": "final already recorded"}
        if not isinstance(summary, str) or not summary.strip():
            return {"ok": False, "error": "summary must be a nonempty string"}
        self.final = {"summary": summary, "limitations": [str(x) for x in limitations or []]}
        self.stop = self.stop or "final"
        self._log("final", final=self.final)
        self.persist()
        return {"ok": True, "recorded_findings": len(self.findings), "tool_calls": self.calls}


def serve(out: Path) -> None:
    sessions: dict[str, Session] = {}
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            op = request["op"]
            if op == "bind":
                sid = session_id(request["task"])
                if sid in sessions:
                    raise ValueError("session already bound; tasks run once")
                sessions[sid] = Session(sid, request["task"], request["split"], out)
                sessions[sid].persist()
                response = {"ok": True, "session": sid}
            elif op == "call":
                response = sessions[request["session"]].call(request["tool"], request.get("args") or {})
            elif op == "final":
                response = sessions[request["session"]].finish(request.get("summary"),
                                                               request.get("limitations") or [])
            elif op == "status":
                s = sessions[request["session"]]
                s.persist()
                response = {"ok": True, "tool_calls": s.calls, "findings": len(s.findings),
                            "final": s.final is not None, "budgets": s.budgets()}
            else:
                raise ValueError(f"unknown op {op!r}")
        except KeyError as exc:
            response = {"ok": False, "error": f"unknown session or missing field: {exc}"}
        except Exception as exc:  # noqa: BLE001
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        sys.stdout.write(json.dumps(response, default=str) + "\n")
        sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("prompt")
    p.add_argument("--session", required=True)
    args = parser.parse_args()
    if args.command == "serve":
        args.out.mkdir(parents=True, exist_ok=True)
        serve(args.out.resolve())
    else:
        sys.stdout.write(render_prompt(args.session))


if __name__ == "__main__":
    main()
