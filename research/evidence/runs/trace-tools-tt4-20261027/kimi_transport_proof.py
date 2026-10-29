"""Offline proof that the TT-4b Kimi transport offers no tools and executes nothing.

Runs the real native METR ``reward_hacking_scanner`` through Inspect Scout and the real
``tt4openrouter`` provider + budgeted httpx2 transport against an in-memory wire that records
every request body. No network, credentials, subprocess or model call is possible: the default
HTTP transport, ``subprocess`` and ``os.system`` raise if touched. Also statically inspects the
four ``metr_scanners`` modules the reward-hacking path imports.

``python kimi_transport_proof.py <receipt.json>`` writes the receipt (needs the retained
Python 3.13 TT-4 runtime with Inspect Scout 0.5.4 / metr-scanners 0.1.11).
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
MOMENT = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)
TOOL_KEYS = ("tools", "tool_choice", "functions", "function_call", "parallel_tool_calls")
#: The only modules on the reward-hacking scan path (``reward_hacking`` imports ``scanner``,
#: which imports ``chunking`` and ``truncation``).
SCANNER_MODULES = ("reward_hacking", "scanner", "chunking", "truncation")
FORBIDDEN_IMPORTS = frozenset(
    {"subprocess", "multiprocessing", "pty", "socket", "ctypes", "importlib", "runpy", "shutil"}
)
FORBIDDEN_BARE_CALLS = frozenset({"exec", "eval", "compile", "__import__"})
FORBIDDEN_ATTRIBUTE_CALLS = frozenset({"system", "popen", "exec", "eval"})
#: Inspect Scout stamps each scan with git metadata; these are the only spawns it may make.
READ_ONLY_GIT = (
    ["rev-parse", "--short", "HEAD"],
    ["remote", "get-url", "origin"],
    ["status", "--porcelain"],
    ["describe", "--tags", "--dirty", "--always", "--long"],
)


def load_paid():
    """Import ``paid.py`` bound to the Kimi arm (the arm is fixed at import time)."""
    previous = os.environ.get("TT4_ARM")
    os.environ["TT4_ARM"] = "kimi-k3"
    sys.path.insert(0, str(HERE))
    try:
        spec = importlib.util.spec_from_file_location("tt4_kimi_proof_paid", HERE / "paid.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            os.environ.pop("TT4_ARM", None)
        else:
            os.environ["TT4_ARM"] = previous
    return module


def analyse_scanner_sources():
    """Static facts about the scanner path: imports, dynamic-execution calls, generate kwargs."""
    findings = {"modules": {}, "generate_calls": [], "generate_config_keywords": []}
    for name in SCANNER_MODULES:
        module = importlib.import_module(f"metr_scanners.{name}")
        path = Path(module.__file__)
        tree = ast.parse(path.read_text())
        imports, calls, tool_names = set(), set(), set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in TOOL_KEYS
            ):
                tool_names.add(node.value)
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call):
                func = node.func
                callee = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                # ``re.compile`` is a regex, not the builtin: only bare names count for these.
                if isinstance(func, ast.Name) and callee in FORBIDDEN_BARE_CALLS:
                    calls.add(callee)
                if isinstance(func, ast.Attribute) and callee in FORBIDDEN_ATTRIBUTE_CALLS:
                    calls.add(callee)
                tool_names.update(k.arg for k in node.keywords if k.arg in TOOL_KEYS)
                if callee == "generate":
                    findings["generate_calls"].append(
                        {
                            "module": name,
                            "positional": len(node.args),
                            "keywords": sorted(k.arg for k in node.keywords),
                        }
                    )
                if callee == "GenerateConfig":
                    findings["generate_config_keywords"].append(
                        {"module": name, "keywords": sorted(k.arg for k in node.keywords)}
                    )
        findings["modules"][name] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "forbidden_imports": sorted(imports & FORBIDDEN_IMPORTS),
            "forbidden_calls": sorted(calls),
            "mentions_tool_parameters": bool(tool_names),
        }
    return findings


def transcript_messages():
    """A long fixture transcript: multiple chunks plus the early-message prefix."""
    rows = [(0, "user", "fixture task: make the tests pass")]
    for step in range(1, 12):
        rows.append((step, "assistant", f"fixture reasoning step {step} " + "lorem ipsum " * 4000))
    return [
        {
            "step_index": n,
            "role": role,
            "content": content,
            "reasoning": None,
            "tool_calls": [],
            "tool_call_id": None,
        }
        for n, role, content in rows
    ]


def summarise_request(content):
    body = json.loads(content)
    parts = {
        part.get("type")
        for message in body["messages"]
        if isinstance(message.get("content"), list)
        for part in message["content"]
    }
    return {
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
        "top_level_keys": sorted(body),
        "tool_keys_present": sorted(set(TOOL_KEYS) & set(body)),
        "message_roles": sorted({message["role"] for message in body["messages"]}),
        "message_tool_calls": any(message.get("tool_calls") for message in body["messages"]),
        "content_part_types": sorted(parts),
        "model": body["model"],
        "provider": body["provider"],
        "reasoning": body["reasoning"],
        "max_tokens": body["max_tokens"],
        "response_format_type": body["response_format"]["type"],
    }


def run_native_scan(paid, workdir):
    """One real native scan over an in-memory wire; returns the recorded evidence."""
    import httpx2
    import inspect_scout as scout
    from openrouter_budget import Budget

    from evallab.readers.scout_clouddb import _check_transcripts, to_transcript

    opaque = "run-" + "f" * 24
    campaign, prior = workdir / "campaign", workdir / "prior"
    sentinel_hits, spawned = [], []
    original_popen_init = subprocess.Popen.__init__

    def guarded_popen(self, args, *more, **kwargs):
        argv = [str(part) for part in args] if isinstance(args, (list, tuple)) else [str(args)]
        name = Path(argv[0]).name
        read_only = (
            (name == "git" and argv[1:] in READ_ONLY_GIT)
            or (name == "uname" and all(part.startswith("-") for part in argv[1:]))
            # ``platform.architecture()`` asks ``file`` about the running interpreter.
            or (
                name == "file"
                and len(argv) == 3
                and argv[1] == "-b"
                and Path(argv[2]).resolve() == Path(sys.executable).resolve()
            )
        )
        if not kwargs.get("shell") and read_only:
            spawned.append(" ".join([name, *argv[1:]]))
            return original_popen_init(self, args, *more, **kwargs)
        sentinel_hits.append("subprocess.Popen:" + " ".join(argv[:4]))
        raise AssertionError("subprocess is forbidden in the offline proof")

    async def forbidden_network(*args, **kwargs):
        sentinel_hits.append("default-http-transport")
        raise AssertionError("the offline proof must never reach the network")

    def forbidden(name):
        def raiser(*args, **kwargs):
            sentinel_hits.append(name)
            raise AssertionError(f"{name} is forbidden in the offline proof")

        return raiser

    async def insert():
        async with scout.transcripts_db(str(prior / "transcripts")) as database:
            await database.insert([to_transcript(opaque, transcript_messages())])

    _check_transcripts(prior / "transcripts", claim=True)
    asyncio.run(insert())

    recorded = []

    class Wire(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request):
            recorded.append(summarise_request(request.content))
            body = {
                "id": f"gen-fixture-{len(recorded)}",
                "model": paid.MODEL,
                "provider": paid.PROVIDER_NAME,
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                    "cost": 0.000009,
                    "is_byok": False,
                    "completion_tokens_details": {"reasoning_tokens": 0},
                },
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {"quotes": [], "reason": "offline fixture only", "score": 3}
                            ),
                        },
                    }
                ],
            }
            return httpx2.Response(200, json=body, request=request)

    budget = Budget(workdir / "budget", "tt4", clock=lambda: MOMENT)
    original_transport = paid.guarded_transport

    def guarded(c, o, k):
        return original_transport(
            c,
            o,
            k,
            underlying=Wire(),
            budget=budget,
            cash_reader=lambda: {
                "available_usd": "100",
                "fetched_at": MOMENT.isoformat(),
                "source": "fixture",
            },
        )

    with (
        mock.patch.object(httpx2.AsyncHTTPTransport, "handle_async_request", forbidden_network),
        mock.patch("subprocess.Popen.__init__", guarded_popen),
        mock.patch("os.system", forbidden("os.system")),
        mock.patch("asyncio.create_subprocess_exec", forbidden("create_subprocess_exec")),
        mock.patch("asyncio.create_subprocess_shell", forbidden("create_subprocess_shell")),
        mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "not-a-real-key"}),
        mock.patch.multiple(
            paid,
            CAMPAIGN=campaign,
            TRANSCRIPTS=prior / "transcripts",
            SCANS=workdir / "scans",
            SPEND=workdir / "canonical.jsonl",
            STATE=workdir / "state",
            guarded_transport=guarded,
        ),
    ):
        paid.register_provider()
        status = paid.scan_one(opaque)
        score = paid.record_result(opaque, "fixture-trace", status.location)
    return {
        "scan_complete": bool(status.complete),
        "native_score": score,
        "requests": recorded,
        "sentinel_hits": sentinel_hits,
        "allowed_read_only_git_spawns": sorted(set(spawned)),
        "budget_used_usd": budget.snapshot()["arms"]["tt4"]["used_usd"],
    }


def prove(workdir=None):
    paid = load_paid()
    with tempfile.TemporaryDirectory() as scratch:
        scan = run_native_scan(paid, Path(workdir or scratch))
    static = analyse_scanner_sources()
    requests = scan["requests"]
    verdicts = {
        "wire_requests": len(requests),
        "chunked_path_exercised": len(requests) > 1,
        "no_request_carries_tool_keys": all(not row["tool_keys_present"] for row in requests),
        "no_message_carries_tool_calls": all(not row["message_tool_calls"] for row in requests),
        "text_only_content": all(row["content_part_types"] in ([], ["text"]) for row in requests),
        "exact_provider_policy": all(row["provider"] == paid.PROVIDER_POLICY for row in requests),
        "no_sentinel_hit": not scan["sentinel_hits"],
        "scanner_path_has_no_exec_or_process_imports": all(
            not info["forbidden_imports"] and not info["forbidden_calls"]
            for info in static["modules"].values()
        ),
        "scanner_never_mentions_tool_parameters": all(
            not info["mentions_tool_parameters"] for info in static["modules"].values()
        ),
        "scanner_generate_only_response_schema": all(
            call["keywords"] == ["config"] and call["positional"] == 1
            for call in static["generate_calls"]
        )
        and all(
            item["keywords"] == ["response_schema"] for item in static["generate_config_keywords"]
        )
        and bool(static["generate_calls"]),
    }
    return {
        "schema": "evallab.tt4b_transport_proof/v1",
        "arm": paid.ARM,
        "route": paid.ROUTE,
        "provider": paid.PROVIDER,
        "provider_policy": paid.PROVIDER_POLICY,
        "wire_validator": "paid.validate_request rejects tools/tool_choice/functions/"
        "function_call/parallel_tool_calls by key presence",
        "scan": scan,
        "static": static,
        "verdicts": verdicts,
        "proved": all(
            value is True or (isinstance(value, int) and value > 0) for value in verdicts.values()
        ),
    }


if __name__ == "__main__":
    result = prove()
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if target:
        target.write_text(text)
    print(json.dumps(result["verdicts"], indent=2), "proved:", result["proved"])
    sys.exit(0 if result["proved"] else 1)
