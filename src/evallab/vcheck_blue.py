"""Blue-solver probes for VerifierCheck.

Tokenless EnvCheck rebuild: per-task blue agents answer the task's own
prompts (baseline, then hinted with red-authored failure hints) so the red
agent can compare transcripts and construct wrong submissions that the
task's own verifier scores.

Two benchmark families:

- BFCL (function calling): one live model turn with the task's tool
  schemas in OpenAI tools format. Baseline sends the task's user turn
  alone; hinted appends the hint as a second user message, recorded
  verbatim so adjudication can distinguish hint influence.
- Terminal-Bench 4 (file/container tasks, e.g. cargo-flight-dispatch):
  no live rollout in this pass — the agent works by editing files under
  ``/app`` (task.toml ``artifacts``) and the verifier runs ``tests/test.sh``
  (``dispatch.py`` + ``pytest test_outputs.py`` → ``reward.txt``), so there
  is no tool-call surface to probe. The red agent authors file submissions
  directly (auditor-authored path).

Transcript records follow the shared vocabulary
(``sequence`` 1-based, ``kind``, ``data``, ``previous``, ``sha256``),
hash-chained so tampering is detectable. The chain schema mirrors
``evallab.vcheck_client.chain_append``; it is reimplemented locally here
so this module stays importable (and unit-testable) before that module
lands or when only probes are needed.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: Returned by :func:`blue_tb4` for every TB4-style task in this pass.
BLUE_SKIP_REASON = "auditor-authored path; rollout via Harbor preamble seam is future work"

#: Pinned gorilla commit whose checker semantics :func:`grade_bfcl_call`
#: replays (mirrors the ``grade.py`` in the 2026-10-bfcl findings).
GORILLA_PIN = "6ea57973c7a6097fd7c5915698c54c17c5b1b6c8"
GORILLA_URL = "https://github.com/ShishirPatil/gorilla"
BFCL_SUBDIR = "berkeley-function-call-leaderboard"
BFCL_EVAL_RUNNER_REL = "bfcl_eval/eval_checker/eval_runner.py"
BFCL_REGISTRY = "gpt-4o-2024-11-20-FC"

#: Default local gorilla checkout; missing → FileNotFoundError, never a clone.
DEFAULT_GORILLA_ROOT = Path.home() / "Developer" / "agent-evals" / "gorilla"

#: Model defaults shared by all vcheck agents.
BLUE_MODEL = "glm-5.3"
BLUE_TEMPERATURE = 0.1
BLUE_MAX_TOKENS = 4096

CONDITION_BASELINE = "target_baseline"
CONDITION_HINTED = "target_hinted"

_client_chain_append: Any = None
_client_chain_probed = False


def _shared_chain_append() -> Any:
    """Import the shared chain helper on first use; None until it lands."""
    global _client_chain_append, _client_chain_probed
    if not _client_chain_probed:
        _client_chain_probed = True
        try:
            from evallab.vcheck_client import chain_append
        except ImportError:
            chain_append = None
        _client_chain_append = chain_append
    return _client_chain_append


def _chain_append(records: list[dict[str, Any]], kind: str, data: dict[str, Any]) -> dict[str, Any]:
    """Append a hash-chained trajectory record (request|response|...).

    Reuses ``evallab.vcheck_client.chain_append`` once that module lands;
    until then falls back to this identical local schema so probes stay
    importable and unit-testable offline.
    """
    shared = _shared_chain_append()
    if shared is not None:
        return shared(records, kind, data)
    previous = records[-1]["sha256"] if records else "genesis"
    record: dict[str, Any] = {
        "sequence": len(records) + 1,
        "kind": kind,
        "data": data,
        "previous": previous,
    }
    record["sha256"] = hashlib.sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    records.append(record)
    return record


def _invoke_client(client: Any, **kwargs: Any) -> Any:
    """Call ``client.chat_completion`` (or ``client`` itself) tolerantly.

    The metered client (``evallab.vcheck_client``) takes ``run_dir`` and
    ``key_provider`` alongside the model arguments; test fakes take a
    subset. Filter by signature so both work; pass everything through
    when the target accepts ``**kwargs``.
    """
    fn = getattr(client, "chat_completion", None)
    if fn is None:
        fn = client
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(**kwargs)
    params = sig.parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(**kwargs)
    return fn(**{k: v for k, v in kwargs.items() if k in params})


def _coerce_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, Mapping):
        return dict(arguments)
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except (json.JSONDecodeError, ValueError):
            return {"_raw": arguments}
        return dict(parsed) if isinstance(parsed, Mapping) else {"_raw": arguments}
    return {"_raw": arguments}


def _normalize_decoded_call(call: Mapping[str, Any]) -> dict[str, Any] | None:
    """Normalise one call to ``{"name", "arguments"}``; None if not a call."""
    if "name" in call:
        return {"name": call["name"], "arguments": _coerce_arguments(call.get("arguments", {}))}
    if len(call) == 1:
        ((name, arguments),) = list(call.items())
        if isinstance(name, str):
            return {"name": name, "arguments": _coerce_arguments(arguments)}
    return None


def _normalize_openai_tool_call(call: Mapping[str, Any]) -> dict[str, Any] | None:
    function = call.get("function")
    if isinstance(function, Mapping) and isinstance(function.get("name"), str):
        return {
            "name": function["name"],
            "arguments": _coerce_arguments(function.get("arguments", {})),
        }
    return _normalize_decoded_call(call)


def _extract_calls(response: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """Extract model-issued calls; None when the response carries none."""
    for key in ("calls", "tool_calls"):
        raw = response.get(key)
        if isinstance(raw, list):
            calls = [
                norm
                for item in raw
                if isinstance(item, Mapping)
                and (norm := _normalize_openai_tool_call(item)) is not None
            ]
            if calls or not raw:
                return calls
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        message = first.get("message") if isinstance(first, Mapping) else None
        if isinstance(message, Mapping):
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                calls = [
                    norm
                    for item in tool_calls
                    if isinstance(item, Mapping)
                    and (norm := _normalize_openai_tool_call(item)) is not None
                ]
                return calls
            function_call = message.get("function_call")
            if isinstance(function_call, Mapping) and isinstance(function_call.get("name"), str):
                norm = _normalize_openai_tool_call(function_call)
                return [norm] if norm is not None else []
            if "content" in message or "refusal" in message:
                return None
    if "response" in response and "choices" not in response:
        return None
    return None


def _response_text(response: Mapping[str, Any]) -> str:
    """Best-effort refusal/model text across OpenAI and CLIENT shapes."""
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, Mapping):
            message = first.get("message")
            if isinstance(message, Mapping):
                for key in ("content", "refusal", "text"):
                    value = message.get(key)
                    if isinstance(value, str) and value:
                        return value
    for key in ("content", "text"):
        value = response.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _submission_from_response(response: Mapping[str, Any]) -> dict[str, Any]:
    """Build the submission; refusals become an abstain marker, never a call.

    A turn with no calls but non-empty model text is a refusal/abstention
    (the metered client returns ``{content, tool_calls: []}`` for these).
    A call-free turn with no text is a genuine empty submission.
    """
    calls = _extract_calls(response)
    text = _response_text(response)
    if calls is None or (not calls and text):
        return {"calls": [], "abstained": True, "text": text}
    return {"calls": calls or []}


def probe_bfcl(
    task: Mapping[str, Any],
    hint: str | None = None,
    *,
    client: Any,
    budget: Any,
    model: str = BLUE_MODEL,
    temperature: float = BLUE_TEMPERATURE,
    max_tokens: int = BLUE_MAX_TOKENS,
    run_dir: Any = None,
    key_provider: Any = None,
) -> dict[str, Any]:
    """Run ONE blue model turn against a BFCL-style function-calling task.

    ``task`` carries ``tools`` (OpenAI tools format) and ``user_turn``.
    Baseline sends the user turn alone (``target_baseline``); hinted
    appends ``hint`` as a second user message, recorded verbatim
    (``target_hinted``). Returns ``{condition, transcript, submission}``
    where submission is ``{calls: [{name, arguments}]}`` — or an abstain
    marker ``{calls: [], abstained: True, text}`` when the model refuses
    instead of calling (a call is never fabricated).
    """
    tools = list(task.get("tools", []))
    user_turn = task["user_turn"]
    condition = CONDITION_HINTED if hint is not None else CONDITION_BASELINE
    messages = [{"role": "user", "content": user_turn}]
    if hint is not None:
        messages.append({"role": "user", "content": hint})

    transcript: list[dict[str, Any]] = []
    _chain_append(
        transcript,
        "request",
        {
            "messages": messages,
            "tools": tools,
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
    )
    response = _invoke_client(
        client,
        model=model,
        messages=messages,
        tools=tools,
        temperature=temperature,
        max_tokens=max_tokens,
        budget=budget,
        run_dir=run_dir,
        key_provider=key_provider,
    )
    if not isinstance(response, Mapping):
        response = {"text": str(response)}
    response = dict(response)
    _chain_append(transcript, "response", {"response": response})
    return {
        "condition": condition,
        "transcript": transcript,
        "submission": _submission_from_response(response),
    }


def blue_tb4(
    task: Any,
    hint: str | None = None,
    *,
    client: Any = None,
    budget: Any = None,
    **_ignored: Any,
) -> dict[str, Any]:
    """TB4-style file/container tasks get no live rollout in this pass.

    Rationale: EnvCheck's own published TB effects are all constructed
    inputs, none a wild agent exploit — and TB4 agents work by editing
    files under ``/app`` (see task.toml ``artifacts``) while
    ``tests/test.sh`` re-runs ``dispatch.py`` plus pytest outside any
    function-call surface a blue probe could exercise. The red agent
    authors file submissions directly instead.
    """
    _ = (task, client, budget, _ignored)
    condition = CONDITION_HINTED if hint is not None else CONDITION_BASELINE
    return {"condition": condition, "skipped": True, "reason": BLUE_SKIP_REASON}


def grade_bfcl_call(
    repo_root: Path | str = DEFAULT_GORILLA_ROOT,
    *,
    task_id: str,
    calls: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Replay BFCL's own checker over decoded calls; GradeResult-shaped dict.

    Mirrors the ``grade.py`` shipped in the 2026-10-bfcl findings: decoded
    calls are re-encoded as the JSON strings a function-calling model
    returns, then graded by
    ``bfcl_eval.eval_checker.eval_runner.evaluate_task`` — the function
    ``bfcl evaluate`` runs per category — with the OpenAI
    function-calling registry handler. No model is called.

    Returns ``{reward, status, verifier_outputs, job_dir, script}`` matching
    ``evallab.vcheck.GradeResult`` (``status`` ``"ok"`` on a successful
    replay, ``reward`` 1.0/0.0, checker's error payload under
    ``verifier_outputs``). Refuses (like ``grade.py``) unless the checker
    imports from a clean gorilla checkout at :data:`GORILLA_PIN`.
    """
    root = Path(repo_root).expanduser()
    runner_path = root / BFCL_SUBDIR / BFCL_EVAL_RUNNER_REL
    if not runner_path.is_file():
        raise FileNotFoundError(
            f"gorilla checkout not found at {root}; expected layout: "
            f"{root}/{BFCL_SUBDIR}/{BFCL_EVAL_RUNNER_REL} with bfcl_eval "
            f"installed editable from a gorilla checkout at {GORILLA_PIN} "
            f"(git clone {GORILLA_URL} && git checkout {GORILLA_PIN}; "
            f"pip install -e {BFCL_SUBDIR}); "
            f"default path: ~/Developer/agent-evals/gorilla"
        )

    def _git(*args: str) -> str | None:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
        return done.stdout.strip() if done.returncode == 0 else None

    head = _git("rev-parse", "HEAD")
    if head != GORILLA_PIN:
        raise RuntimeError(
            f"grade_bfcl_call: gorilla checkout at {root} is "
            f"{'not in a git checkout' if head is None else 'at commit ' + head}; "
            f"expected {GORILLA_PIN} (see findings REPRODUCE.md)"
        )
    changed = _git("status", "--porcelain", "--untracked-files=no", "--", BFCL_SUBDIR)
    if changed:
        raise RuntimeError(
            f"grade_bfcl_call: gorilla checkout has local changes under {BFCL_SUBDIR}:\n{changed}"
        )

    os.environ.setdefault("OPENAI_API_KEY", "unused-placeholder")
    sys.path.insert(0, str(root / BFCL_SUBDIR))
    try:
        import bfcl_eval  # noqa: E402  # ty: ignore[unresolved-import]
        from bfcl_eval.eval_checker import (  # ty: ignore[unresolved-import]
            eval_runner,  # noqa: E402
        )
        from bfcl_eval.utils import (  # noqa: E402  # ty: ignore[unresolved-import]
            extract_test_category_from_id,
        )

        package = Path(bfcl_eval.__file__).resolve().parent
        if Path(root).resolve() not in package.parents and package != Path(root).resolve():
            raise RuntimeError(
                f"grade_bfcl_call: bfcl_eval imported from {package}, "
                f"outside the gorilla checkout at {root}"
            )
        normalized = [
            {"name": str(call["name"]), "arguments": dict(call.get("arguments", {}))}
            for call in calls
        ]
        entry = {
            "id": task_id,
            "result": [{call["name"]: json.dumps(call["arguments"])} for call in normalized],
        }
        category = extract_test_category_from_id(task_id)
        handler = eval_runner.get_handler(BFCL_REGISTRY)
        with tempfile.TemporaryDirectory() as tmp:
            score_dir = Path(tmp) / "score"
            with contextlib.redirect_stdout(io.StringIO()):
                table = eval_runner.evaluate_task(
                    category,
                    Path(tmp) / "result",
                    score_dir,
                    [entry],
                    BFCL_REGISTRY,
                    handler,
                    {},
                    allow_missing=True,
                )
            lines = [
                json.loads(line)
                for f in score_dir.rglob("*_score.json")
                for line in f.read_text().splitlines()
                if line.strip()
            ]
        failure = next((line for line in lines[1:] if line.get("id") == task_id), None)
        error = (
            {k: failure[k] for k in ("error", "error_type") if failure.get(k) is not None}
            if failure
            else None
        )
        reward = float(table[BFCL_REGISTRY][category]["accuracy"])
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(str(root / BFCL_SUBDIR))
    return {
        "reward": reward,
        "status": "ok",
        "verifier_outputs": {"id": task_id, "category": category, "error": error},
        "job_dir": None,
        "script": "bfcl_eval.eval_checker.eval_runner.evaluate_task",
    }
