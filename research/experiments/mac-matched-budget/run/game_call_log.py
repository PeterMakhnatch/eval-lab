import contextvars
import json
import sys
import threading
import time
from typing import Any

LOG_VERSION = 1
EVENT = "game_call"
ACTIVE_EVENT = "game_call_log_active"
GAME_PREFIXES = ("/game/", "/api/game/")
ENDPOINTS = frozenset({"act", "status", "look", "events"})
MCP_TOOLS = {"holdout_act": "act", "holdout_status": "status", "holdout_events": "events", "holdout_look": "look"}
QUEUED_OPS = frozenset({"move_to", "chop", "quarry", "search", "forage", "fish", "pickup", "drop", "deposit",
                        "withdraw", "cook", "craft", "build", "repair", "patch", "guard", "wait", "signal", "shoot",
                        "hunt", "ask", "attack", "examine"})
WORLD_IMMEDIATE_OPS = frozenset({"stop", "eat", "start_build", "recharge", "admit", "turn_away", "quarantine",
                                 "assign"})
COMMS_OPS = frozenset({"say", "shout", "radio", "post_note"})
READ_OPS = frozenset({"read_notes"})
NO_WORLD_EFFECT_OPS = frozenset({"wait"})
COMMS_SIDE_EFFECTS = {"say": None, "shout": "noise", "radio": "noise_and_charge", "post_note": "noticeboard"}
KNOWN_OPS = QUEUED_OPS | WORLD_IMMEDIATE_OPS | COMMS_OPS | READ_OPS
assert frozenset(COMMS_SIDE_EFFECTS) == COMMS_OPS
TRANSPORTS = frozenset({"rest", "mcp", "direct"})
MAX_OPS_LOGGED = 16
MAX_CHAIN = 8
MAX_HEADERS = 256
MAX_ERROR_SCAN = 4096
METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
REST_REFUSALS = {400: "bad_json", 401: "auth", 404: "unknown_endpoint", 405: "method", 409: "guard",
                 413: "body_too_large", 422: "schema"}
HOST_REFUSALS = {400: "bad_request", 404: "unknown_endpoint", 409: "frozen", 429: "rate_limited"}
MCP_STATUS = {"auth": 401, "schema": 422, "guard": 409, "error": 500}
GUARD_ERRORS = frozenset({"unread_messages", "already_claimed"})

_CURRENT: contextvars.ContextVar = contextvars.ContextVar("game_call_log_call", default=None)


class _Call:
    __slots__ = ("transport", "token", "seat", "endpoint", "method", "started", "reached", "names", "ops_total",
                 "replace", "result", "code", "exc")

    def __init__(self, transport: str, token: str | None, endpoint: str, method: str | None = None):
        assert transport in TRANSPORTS
        assert endpoint in ENDPOINTS or endpoint == "unknown"
        assert method is None or method in METHODS or method == "other"
        self.transport = transport
        self.token = token
        self.seat: str | None = None
        self.endpoint = endpoint
        self.method = method
        self.started = time.time()
        self.reached = False
        self.names: list[tuple[str, str | None]] = []
        self.ops_total: int | None = None
        self.replace = False
        self.result: dict[str, Any] | None = None
        self.code: int | None = None
        self.exc: BaseException | None = None


def _endpoint(value: Any) -> str:
    return value if isinstance(value, str) and value in ENDPOINTS else "unknown"


def _bearer(value: Any) -> str | None:
    if isinstance(value, str) and value.lower().startswith("bearer "):
        return value[7:].strip() or None
    return None


def _op_key(op: Any) -> tuple[str, str | None]:
    if not isinstance(op, dict):
        return ("invalid", None)
    raw = op.get("op")
    if not isinstance(raw, str):
        return ("invalid", None)
    return (raw if raw in KNOWN_OPS else "unknown", raw)


def _op_kind(name: str) -> str:
    if name in QUEUED_OPS:
        return "queued"
    if name in WORLD_IMMEDIATE_OPS:
        return "immediate"
    if name in COMMS_OPS:
        return "comms"
    if name in READ_OPS:
        return "read"
    return "unknown"


def _capture(rec: _Call, seat: Any, body: Any) -> None:
    rec.reached = True
    if isinstance(seat, str):
        rec.seat = seat
    if rec.endpoint != "act" or not isinstance(body, dict):
        return
    ops = body.get("ops")
    rec.replace = bool(body.get("replace"))
    if not isinstance(ops, list):
        return
    rec.ops_total = len(ops)
    rec.names = [_op_key(o) for o in ops[:MAX_OPS_LOGGED]]
    assert len(rec.names) <= MAX_OPS_LOGGED


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _operations(rec: _Call) -> tuple[list[dict[str, Any]], bool | None]:
    results = rec.result.get("results") if rec.result is not None else None
    if not isinstance(results, list):
        results = None
    aligned = None
    if rec.endpoint == "act" and rec.result is not None:
        aligned = results is not None and len(results) == rec.ops_total
    rows: list[dict[str, Any]] = []
    for i, (name, raw) in enumerate(rec.names[:MAX_OPS_LOGGED]):
        res = results[i] if results is not None and i < len(results) and isinstance(results[i], dict) else None
        if res is not None and raw is not None and res.get("op") != raw:
            aligned = False
        accepted = res is not None and res.get("ok") is True
        kind = _op_kind(name)
        mutates = accepted and kind in ("queued", "immediate") and name not in NO_WORLD_EFFECT_OPS
        queued = _int_or_none(res.get("queued")) if accepted and kind == "queued" else None
        side_effect = COMMS_SIDE_EFFECTS[name] if accepted and kind == "comms" else None
        rows.append({"i": i, "op": name, "kind": kind, "accepted": accepted,
                     "mutates_world": mutates or side_effect is not None,
                     "comms_side_effect": side_effect, "queued": queued})
    assert len(rows) == len(rec.names)
    return rows, aligned


def _chain(exc: BaseException | None) -> list[BaseException]:
    out: list[BaseException] = []
    cur = exc
    for _ in range(MAX_CHAIN):
        if cur is None or any(cur is seen for seen in out):
            break
        out.append(cur)
        cur = cur.__cause__ or cur.__context__
    return out


def _is_validation(exc: BaseException) -> bool:
    cls = type(exc)
    return cls.__name__ == "ValidationError" and cls.__module__.startswith("pydantic")


def _is_guard_refusal(exc: BaseException) -> bool:
    text = str(exc)[:MAX_ERROR_SCAN]
    start = text.find("{")
    if start < 0:
        return False
    try:
        doc = json.loads(text[start:])
    except ValueError:
        return False
    error = doc.get("error") if isinstance(doc, dict) else None
    return isinstance(error, str) and error in GUARD_ERRORS


def _mcp_failure(rec: _Call) -> str:
    assert not rec.reached
    chain = _chain(rec.exc)
    if any(_is_validation(e) for e in chain):
        return "schema"
    if rec.seat is None:
        return "auth"
    if any(_is_guard_refusal(e) for e in chain):
        return "guard"
    return "error"


def _outcome_and_status(rec: _Call, http_status: int | None) -> tuple[str, int]:
    if rec.reached:
        outcome = "ok" if rec.result is not None else HOST_REFUSALS.get(rec.code, "error")
        status = 200 if rec.result is not None else (rec.code or 500)
    elif rec.transport == "mcp":
        outcome = _mcp_failure(rec)
        status = MCP_STATUS[outcome]
    else:
        outcome = REST_REFUSALS.get(http_status, "error")
        status = 500
    if rec.transport == "rest":
        assert http_status is not None
        status = http_status
    assert isinstance(status, int) and status > 0
    return outcome, status


def _row(rec: _Call, outcome: str, status: int) -> dict[str, Any]:
    rows, aligned = _operations(rec)
    replace_cleared = rec.endpoint == "act" and rec.reached and rec.result is not None and rec.replace
    fields: dict[str, Any] = {
        "log_version": LOG_VERSION, "transport": rec.transport, "endpoint": rec.endpoint, "status": status,
        "outcome": outcome, "reached_game": rec.reached, "started_ts": round(rec.started, 3),
        "duration_s": round(max(0.0, time.time() - rec.started), 3), "operations": rows,
        "ops_total": rec.ops_total, "ops_logged": len(rows),
        "world_ops": sum(1 for r in rows if r["mutates_world"] and r["kind"] != "comms"),
        "comms_side_effect_ops": sum(1 for r in rows if r["comms_side_effect"] is not None),
        "replace_cleared": replace_cleared, "results_aligned": aligned,
    }
    if rec.method is not None:
        fields["method"] = rec.method
    assert fields["world_ops"] <= len(rows) <= MAX_OPS_LOGGED
    return fields


class GameCallLog:
    def __init__(self, board: Any, board_error: type):
        assert board is not None
        assert issubclass(board_error, Exception)
        self.board = board
        self.board_error = board_error
        self.lock = threading.Lock()
        self.dropped = 0

    def seat_of(self, token: str | None) -> str | None:
        if token is None:
            return None
        try:
            seat = self.board.authenticate(token)
        except self.board_error:
            return None
        return seat if isinstance(seat, str) else None

    def finish(self, rec: _Call, http_status: int | None) -> None:
        if rec.seat is None and not rec.reached:
            rec.seat = self.seat_of(rec.token)
        rec.token = None
        outcome, status = _outcome_and_status(rec, http_status)
        fields = _row(rec, outcome, status)
        with self.lock:
            dropped = self.dropped
        if dropped:
            fields["log_dropped"] = dropped
        try:
            self.board._emit(EVENT, rec.seat, **fields)
        except Exception:  # noqa: BLE001
            with self.lock:
                self.dropped += 1


def _wrap_host(log: GameCallLog, host_cls: type, game_error: type) -> None:
    orig = host_cls.call
    if getattr(orig, "_game_call_log", False):
        raise RuntimeError("game_call_log already wraps GameHost.call")

    def call(self: Any, seat: Any, endpoint: Any, body: Any) -> Any:
        endpoint_name = _endpoint(endpoint)
        rec = _CURRENT.get()
        own = rec is None or rec.reached or rec.endpoint != endpoint_name
        if own:
            rec = _Call("direct", None, endpoint_name)
        _capture(rec, seat, body)
        try:
            result = orig(self, seat, endpoint, body)
        except BaseException as e:
            code = getattr(e, "code", None) if isinstance(e, game_error) else None
            rec.code = _int_or_none(code) or 500
            rec.exc = e
            if own:
                log.finish(rec, None)
            raise
        rec.result = result if isinstance(result, dict) else None
        if own:
            log.finish(rec, None)
        return result

    call._game_call_log = True
    host_cls.call = call


def _scope_header(scope: dict[str, Any], name: bytes) -> str | None:
    for k, v in list(scope.get("headers") or ())[:MAX_HEADERS]:
        if isinstance(k, bytes) and k.lower() == name and isinstance(v, bytes):
            return v.decode("latin-1")
    return None


def _rest_endpoint(scope: dict[str, Any]) -> str | None:
    path = scope.get("path")
    if scope.get("type") != "http" or not isinstance(path, str):
        return None
    root = scope.get("root_path")
    if isinstance(root, str) and root and path.startswith(root + "/"):
        path = path[len(root):]
    for prefix in GAME_PREFIXES:
        if path.startswith(prefix):
            return _endpoint(path[len(prefix):])
    return None


class _RestTap:
    def __init__(self, app: Any, log: GameCallLog):
        assert app is not None
        assert isinstance(log, GameCallLog)
        self.app = app
        self.log = log

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        endpoint = _rest_endpoint(scope)
        if endpoint is None:
            await self.app(scope, receive, send)
            return
        method = scope.get("method")
        method = method if isinstance(method, str) and method in METHODS else "other"
        rec = _Call("rest", _bearer(_scope_header(scope, b"authorization")), endpoint, method)
        status = [0]

        async def tap_send(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                status[0] = _int_or_none(message.get("status")) or 0
            await send(message)

        token = _CURRENT.set(rec)
        try:
            await self.app(scope, receive, tap_send)
        finally:
            _CURRENT.reset(token)
            self.log.finish(rec, status[0] or 500)


def _mcp_authorization(context: Any) -> str | None:
    try:
        headers = context.headers
        getter = getattr(headers, "get", None)
        value = getter("authorization") if callable(getter) else None
    except Exception:  # noqa: BLE001
        return None
    return value if isinstance(value, str) else None


def _wrap_mcp(log: GameCallLog, mcp: Any) -> list[str]:
    tm = getattr(mcp, "_tool_manager", None)
    tools = getattr(tm, "_tools", None)
    if not isinstance(tools, dict) or any(name not in tools for name in MCP_TOOLS):
        raise RuntimeError("game_call_log must load after game_plugin.py; Holdout MCP tools are missing")
    orig = tm.call_tool
    if getattr(orig, "_game_call_log", False):
        raise RuntimeError("game_call_log already wraps the MCP tool manager")

    async def call_tool(name: Any, arguments: Any, context: Any, convert_result: bool = False) -> Any:
        endpoint = MCP_TOOLS.get(name) if isinstance(name, str) else None
        if endpoint is None:
            return await orig(name, arguments, context, convert_result=convert_result)
        rec = _Call("mcp", _bearer(_mcp_authorization(context)), endpoint)
        token = _CURRENT.set(rec)
        try:
            return await orig(name, arguments, context, convert_result=convert_result)
        except BaseException as e:
            rec.exc = e
            raise
        finally:
            _CURRENT.reset(token)
            log.finish(rec, None)

    call_tool._game_call_log = True
    tm.call_tool = call_tool
    return sorted(MCP_TOOLS)


def _holdout_classes() -> tuple[type, type]:
    host_mod = sys.modules.get("holdout.host")
    engine = sys.modules.get("holdout.engine")
    if host_mod is None or engine is None:
        raise RuntimeError("game_call_log must load after game_plugin.py; holdout is not imported")
    host_cls = getattr(host_mod, "GameHost", None)
    game_error = getattr(engine, "GameError", None)
    if not isinstance(host_cls, type) or not isinstance(game_error, type):
        raise RuntimeError("game_call_log: holdout.host.GameHost or holdout.engine.GameError is missing")
    queued = frozenset(getattr(engine, "QUEUED", ()))
    immediate = frozenset(getattr(engine, "IMMEDIATE", ()))
    comms = frozenset(getattr(engine, "COMMS_OPS", ()))
    if queued != QUEUED_OPS or immediate != WORLD_IMMEDIATE_OPS | COMMS_OPS | READ_OPS \
            or comms != COMMS_OPS | READ_OPS:
        raise RuntimeError("game_call_log: the op table differs from holdout.engine")
    return host_cls, game_error


def _world_rule() -> dict[str, Any]:
    return {"world_queued": sorted(QUEUED_OPS - NO_WORLD_EFFECT_OPS), "world_immediate": sorted(WORLD_IMMEDIATE_OPS),
            "no_world_effect": sorted(NO_WORLD_EFFECT_OPS), "comms": sorted(COMMS_OPS), "read": sorted(READ_OPS),
            "comms_side_effects": {k: v for k, v in sorted(COMMS_SIDE_EFFECTS.items()) if v is not None},
            "replace_cleared": "act with truthy replace that the game accepted"}


def register(board: Any, api: Any, mcp: Any) -> GameCallLog:
    from multi_agent_harness.board.core import BoardError

    if getattr(board, "game_call_log", None) is not None:
        raise RuntimeError("game_call_log is already registered")
    host_cls, game_error = _holdout_classes()
    log = GameCallLog(board, BoardError)
    tools = _wrap_mcp(log, mcp)
    _wrap_host(log, host_cls, game_error)
    api.add_middleware(_RestTap, log=log)
    board.game_call_log = log
    board._emit(ACTIVE_EVENT, None, log_version=LOG_VERSION, wrapped_mcp_tools=tools,
                guard_loaded_before=getattr(board, "game_action_guard", None) is not None,
                world_rule=_world_rule())
    return log
