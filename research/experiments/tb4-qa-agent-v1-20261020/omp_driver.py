# ruff: noqa
"""OMP Python-eval driver for QA agent v1 (native subagent route).

Loaded with ``%load`` in an OMP eval kernel, which provides the ``tool`` decorator
and ``agent`` spawner. It exposes exactly four kernel-hosted tools to each subagent,
all forwarding to one ``harness.py serve`` process (the locked repository
interpreter): ``qa_task_info``, ``qa_diff``, ``qa_record_finding`` and ``qa_final``.
Each task is bound to an opaque session just before its subagent is spawned; at
most three subagents run at once, every one on ``anthropic/claude-opus-5-5``, with
the rendered ``prompt_v1.md`` as its whole assignment. Not importable outside OMP.
"""

import asyncio
import json
import pathlib
import subprocess
import threading
import time

W = pathlib.Path(__file__).resolve().parents[3] if "__file__" in globals() else pathlib.Path(
    "/Users/petermakhnatch/Developer/eval-lab/.worktrees/qa-agent-v1-20261020")
EXP = W / "research/experiments/tb4-qa-agent-v1-20261020"
RES = pathlib.Path.home() / "Developer/eval-lab-results/2026-10-20/tb4-qa-agent-v1"
MODEL = "anthropic/claude-opus-5-5"
QA_TOOLS = ["qa_task_info", "qa_diff", "qa_record_finding", "qa_final"]
_srv = {}
_lock = threading.Lock()
RUNLOG = {}


def start_server(name):
    out = RES / name
    old = _srv.get("p")
    if old is not None and old.poll() is None:
        old.stdin.close()
        old.wait(timeout=30)
    _srv["p"] = subprocess.Popen(
        [str(W / ".venv/bin/python"), str(EXP / "harness.py"), "serve", "--out", str(out)],
        cwd=W, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=open(f"/tmp/qa-v1-{name}.err", "a"), text=True, bufsize=1)


def rpc(req):
    with _lock:
        p = _srv["p"]
        p.stdin.write(json.dumps(req) + "\n")
        p.stdin.flush()
        return json.loads(p.stdout.readline())


@tool
def qa_task_info(session: str, path: str = "", start_line: int = 0, end_line: int = 0, pattern: str = "") -> dict:
    """QA v1 source tool. No path: package inventory + instruction + defect classes. path: read that package file with line numbers (up to 2000 lines; start_line/end_line continue a longer file). path+pattern: case-insensitive literal search in that file or directory ('.' = whole package)."""
    args = {}
    if path:
        args["path"] = path
    if pattern:
        args["pattern"] = pattern
    elif path:
        if start_line:
            args["start_line"] = start_line
        if end_line:
            args["end_line"] = end_line
    return rpc({"op": "call", "session": session, "tool": "task-info", "args": args})


@tool
def qa_diff(session: str, left: str, right: str) -> dict:
    """QA v1 source tool: unified diff of two package files (source text only, never execution evidence)."""
    return rpc({"op": "call", "session": session, "tool": "diff", "args": {"left": left, "right": right}})


@tool
def qa_record_finding(session: str, defect_class: str, severity: str, fault: str, summary: str, evidence_refs: list[str]) -> dict:
    """QA v1: persist one suspected task-side defect. defect_class: canonical class id; severity: low|medium|high|critical; fault: eval|platform|agent; summary: what is wrong and how a wrong solution passes or a correct one fails; evidence_refs: 'path:line' or 'path:start-end' citations you read in this session."""
    return rpc({"op": "call", "session": session, "tool": "record-finding",
                "args": {"defect_class": defect_class, "severity": severity, "fault": fault,
                         "summary": summary, "evidence_refs": evidence_refs}})


@tool
def qa_final(session: str, summary: str, limitations: list[str]) -> dict:
    """QA v1: end the review (call exactly once, last). summary names recorded findings and classes checked/skipped; limitations lists checks not run and unknowns."""
    return rpc({"op": "final", "session": session, "summary": summary, "limitations": limitations})


def render(sid):
    return subprocess.run([str(W / ".venv/bin/python"), str(EXP / "harness.py"), "prompt", "--session", sid],
                          cwd=W, capture_output=True, text=True, check=True).stdout


async def run_campaign(name, tasks, split, conc=3, bound=None):
    out = RES / name
    (out / "prompts").mkdir(parents=True, exist_ok=True)
    queue, running, bound = list(tasks), {}, bound or {}
    while queue or running:
        while queue and len(running) < conc:
            task = queue.pop(0)
            sid = bound.get(task) or rpc({"op": "bind", "task": task, "split": split})["session"]
            prompt = render(sid)
            (out / "prompts" / f"{sid}.md").write_text(prompt)
            handle = agent(prompt, model=MODEL, tools=QA_TOOLS, label=f"qa-v1-{sid}")
            running[sid] = (task, handle, time.time())
            RUNLOG[(name, sid)] = {"task": task, "agent": handle.id, "spawned": time.time()}
        await asyncio.sleep(10)
        for sid, (task, handle, started) in list(running.items()):
            if handle.done():
                try:
                    text = handle.wait()
                except Exception as exc:  # noqa: BLE001
                    text = f"ERROR {type(exc).__name__}: {exc}"
                RUNLOG[(name, sid)].update(done=time.time(), secs=round(time.time() - started),
                                           text=str(text)[:2000],
                                           status=rpc({"op": "status", "session": sid}))
                del running[sid]
        json.dump({k[1]: v for k, v in RUNLOG.items() if k[0] == name}, open(out / "runlog.json", "w"),
                  indent=1, default=str)
