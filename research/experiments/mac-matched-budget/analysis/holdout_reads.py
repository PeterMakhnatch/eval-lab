from __future__ import annotations

import argparse
import ast
import json
import random
import re
import shlex
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from itertools import islice
from pathlib import Path
from statistics import median

ARMS = ("solo", "silent", "team", "guard")
PILOT = Path.home() / "Developer/eval-lab-results/multiagent-campaign-2026-11-23/bespoke/study4-holdout"
UNKNOWN = "unknown"
LIMIT = 50_000
ENDPOINTS = {"events", "status", "look", "act"}
POST_OPS = {"say", "shout", "radio", "post_note"}
OPS = {
    "move_to", "chop", "quarry", "forage", "fish", "search", "pickup", "drop", "deposit",
    "withdraw", "build", "repair", "patch", "cook", "craft", "guard", "examine", "shoot",
    "attack", "ask", "hunt", "wait", "signal", "say", "shout", "radio", "post_note",
    "read_notes", "eat", "recharge", "start_build", "stop", "admit", "turn_away", "quarantine",
    "assign",
}
XY = {"chop", "quarry", "forage", "fish", "search", "pickup", "shoot", "move_to"}
ACTIVITY = {"chop", "quarry", "forage", "fish", "search", "pickup", "shoot", "attack", "ask", "examine", "admit", "turn_away", "quarantine", "start_build", "build", "repair", "patch", "guard", "assign", "move_to", "withdraw", "deposit", "craft", "cook"}
SHARED_WORK = {"chop", "quarry", "forage", "fish", "search", "pickup", "ask", "examine", "admit", "turn_away", "quarantine", "start_build"}
TIERS = r"campfire|palisade|storehouse|watchtower|workshop|garden|infirmary|stone_wall|forge|generator|radio_mast|helipad"
RESOURCE = r"chop(?:ping)?|quarry(?:ing)?|forag(?:e|ing)|fish(?:ing)?|search(?:ing)?"
COORD = r"\(?\s*(\d{1,2})\s*,\s*(\d{1,2})\s*\)?"
ANNOUNCE = re.compile(rf"(?:^|[.!?]\s+|:\s+|,\s+)(?:i(?: am|'m| will)\s+)?({RESOURCE}|taking the trees)\b[^.!?;]{{0,45}}?{COORD}(?=\s|[.,;!?]|$)(?!\s*[-–]\d)", re.I)
BUILD = re.compile(rf"\b(?:i(?: am|'m) (?:now )?building|joining|started|starting|opened) (?:the )?({TIERS})\b", re.I)
REQUEST_BUILD = re.compile(rf"\b(?:please build|build tier) (?:the )?({TIERS})\b", re.I)
REQUEST_RESOURCE = re.compile(rf"\bplease ({RESOURCE})\b\s+(?:at\s+)?{COORD}", re.I)
REQUEST_WOOD = re.compile(r"\b(?:please (?:bring wood to (?:the )?stockpile|deposit wood)|please [^.!?;]{1,75}, and deposit wood)\b", re.I)
REQUEST_START = re.compile(r"\bstart_build (palisade|watchtower|campfire)\b", re.I)
TOP_JSON = re.compile(r"(?m)^\{(?=\s*\"(?:ok|http_status)\"\s*:)")
ACT_PREFIX = re.compile(r'\{\s*"ok":\s*true,\s*"results":\s*\[')
EVENT_PREFIX = re.compile(r'\{\s*"ok":\s*true,\s*"events":\s*\[')
HTTP_PREFIX = re.compile(r'\{\s*"http_status":\s*(\d{3})')
ACT_COMPACT = re.compile(r"^(?:(?:True|False)(?: None)?\s+)?(\[.*\])$")
EVENT_PROJECTED = re.compile(r"^\d{2}:\d{2} (?:done|order_failed|built|discovered|stranger_gate|heard|contact|note|dawn|horde)\b")
HEREDOC = re.compile(r"cat\s*>\s*(\S+\.py)\s*<<-?\s*['\"]?(\w+)['\"]?\s*\n(.*?)^\2\s*$", re.M | re.S)


@dataclass
class Spec:
    endpoint: str
    via: str
    ops: list[dict] | None = None
    repeated: bool = False
    replace: bool = False


@dataclass
class Call:
    step: int
    call_id: str
    tool: str
    atif_time: str
    use_time: float | None
    result_time: float | None
    session: dict
    specs: list[Spec]
    result: str | None
    failed: bool | None
    ok_count_projection: bool

    @property
    def blocked(self):
        return (self.result or "").startswith("<tool_use_error>Blocked:")


@dataclass
class Seat:
    path: Path
    name: str
    slot: str
    calls: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    posts: list[dict] = field(default_factory=list)
    completions: list[dict] = field(default_factory=list)
    deliveries: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))
    start: float = 0.0
    end: float = 0.0


@dataclass
class Trial:
    path: Path
    arm: str
    start: float
    duration: float
    clock_line: int
    replay_end_line: int
    aliases: dict
    names: dict
    model: str
    posts: list[dict]
    proxy: list[tuple[int, dict]]
    replay: list[tuple[int, dict]]


def stamp(value):
    if isinstance(value, str):
        return datetime.fromisoformat(value).timestamp()
    assert isinstance(value, int | float)
    return float(value)


def jsonl(path):
    with path.open(encoding="utf-8") as file:
        rows = [(n, json.loads(line)) for n, line in enumerate(islice(file, LIMIT + 1), 1)]
    assert len(rows) <= LIMIT and all(isinstance(row, dict) for _, row in rows), path
    return rows


def payload(path):
    assert path.stat().st_size <= 64 * 1024 * 1024, path
    return json.loads(path.read_text(encoding="utf-8"))


def offset(value, start):
    return round(value - start, 3) if value is not None else UNKNOWN


def literal(node, fallback=None):
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return fallback


def operation_list(value):
    if not isinstance(value, list):
        return None
    return [op if isinstance(op, dict) and isinstance(op.get("op"), str) else {"op": UNKNOWN} for op in value]


def python_specs(source):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    control = (ast.For, ast.While, ast.If, ast.IfExp, ast.comprehension, ast.FunctionDef)
    stack = [(tree, False)]
    hits = []
    for _ in range(20_000):
        if not stack:
            break
        node, conditional = stack.pop()
        if isinstance(node, ast.Call) and node.args and (
            isinstance(node.func, ast.Name) and node.func.id == "call"
            or isinstance(node.func, ast.Attribute) and node.func.attr == "call"
        ):
            endpoint = literal(node.args[0])
            if isinstance(endpoint, str) and endpoint in ENDPOINTS:
                body = literal(node.args[1], {}) if len(node.args) > 1 else {}
                body = body if isinstance(body, dict) else {}
                spec = Spec(endpoint, "Bash/Python client", operation_list(body.get("ops")), conditional, bool(body.get("replace")))
                hits.append((node.lineno, node.col_offset, spec))
        stack.extend((child, conditional or isinstance(node, control)) for child in ast.iter_child_nodes(node))
    assert not stack
    return [spec for _, _, spec in sorted(hits, key=lambda hit: hit[:2])]


def cli_spec(endpoint, args, repeated):
    if endpoint != "act":
        return Spec(endpoint, "Bash/holdout.py", repeated=repeated)
    try:
        data = json.loads(args[0]) if args else None
    except json.JSONDecodeError:
        data = None
    words = list(islice(args, 1, 8))
    end = next((i for i, word in enumerate(words) if word in {"|", ";", "&&", "||", "&"}), len(words))
    return Spec(endpoint, "Bash/holdout.py", operation_list(data), repeated, "--replace" in words[:end])


def shell_specs(command, scripts):
    def capture(match):
        scripts[Path(match[1]).name] = match[3]
        return ";"

    command = HEREDOC.sub(capture, command)
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        tokens = list(islice(lexer, 10_001))
    except ValueError:
        return []
    assert len(tokens) <= 10_000
    specs = []
    repeated = False
    for i, token in enumerate(tokens):
        if token in {"for", "while"}:
            repeated = True
        elif token == "done":
            repeated = False
        if Path(token).name not in {"python", "python3", "python3.12"} or i + 1 >= len(tokens):
            continue
        arg = tokens[i + 1]
        arg2 = tokens[i + 2] if i + 2 < len(tokens) else ""
        if arg == "-c":
            specs.extend(python_specs(arg2))
        elif Path(arg).name == "holdout.py" and arg2 in ENDPOINTS:
            specs.append(cli_spec(arg2, tokens[i + 3 :], repeated))
        elif (code := scripts.get(Path(arg).name)) is not None:
            specs.extend(python_specs(code))
    if not specs and re.search(r"/api/game/|(?:tools/holdout\.py\s+(?:act|events|status|look))", command):
        specs.append(Spec(UNKNOWN, "Bash"))
    return specs


def session_times(directory):
    files = sorted(directory.glob("sessions/projects/-app/*.jsonl"))
    assert len(files) <= 16
    indexed = defaultdict(dict)
    for path in files:
        for line, row in jsonl(path):
            kind = row.get("type")
            if kind not in {"assistant", "user"} or not row.get("timestamp"):
                continue
            content = row.get("message", {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                role = block.get("type")
                if role not in {"tool_use", "tool_result"}:
                    continue
                key = block.get("id") if role == "tool_use" else block.get("tool_use_id")
                if key and role not in indexed[key]:
                    indexed[key][role] = {"utc": row["timestamp"], "file": str(path), "line": line}
    return indexed


def trajectory_calls(steps, sessions):
    scripts = {}
    for step in steps:
        observations = {o.get("source_call_id"): o for o in step.get("observation", {}).get("results", [])}
        for call in step.get("tool_calls", []):
            tool, args, cid = call["function_name"], call["arguments"], call["tool_call_id"]
            endpoint = tool.removeprefix("mcp__board__holdout_")
            if endpoint in ENDPOINTS and endpoint != tool:
                specs = [Spec(endpoint, "MCP", operation_list(args.get("ops")), replace=bool(args.get("replace")))]
            elif tool == "Bash":
                specs = shell_specs(args.get("command", ""), scripts)
            else:
                specs = [Spec(UNKNOWN, "MCP board")] if tool.startswith("mcp__board__") else []
            if not specs:
                continue
            obs = observations.get(cid, {})
            result = obs.get("content")
            assert result is None or isinstance(result, str)
            refs = sessions.get(cid, {})
            use = refs.get("tool_use", {}).get("utc")
            ret = refs.get("tool_result", {}).get("utc")
            failed = obs.get("extra", {}).get("tool_result_is_error")
            count_projection = bool(re.search(r"""grep -c ['"]"ok": true['"]""", args.get("command", "")))
            yield Call(step["step_id"], cid, tool, step["timestamp"], stamp(use) if use else None, stamp(ret) if ret else None, refs, specs, result, failed, count_projection)


def partial_reply(text, pos, decoder):
    if ACT_PREFIX.match(text, pos):
        next_reply = TOP_JSON.search(text, pos + 1)
        end = next_reply.start() if next_reply else len(text)
        match = ACT_PREFIX.match(text, pos, end)
        assert match is not None
        records = []
        cursor = match.end()
        for _ in range(16):
            cursor += len(text[cursor:end]) - len(text[cursor:end].lstrip())
            try:
                row, cursor = decoder.raw_decode(text, cursor)
            except json.JSONDecodeError:
                break
            if cursor > end or not isinstance(row, dict) or type(row.get("ok")) is not bool:
                break
            records.append(row)
            comma = re.match(r"\s*,", text[cursor:end])
            if not comma:
                break
            cursor += comma.end()
        return {"partial_act": records}, end
    if EVENT_PREFIX.match(text, pos):
        return {"partial_events": True}, pos + 1
    if match := HTTP_PREFIX.match(text, pos):
        return {"http_status": int(match[1]), "ok": False}, pos + 1
    return None, pos + 1


def projected_line(value):
    if EVENT_PROJECTED.match(value):
        return {"projected_events": True}
    if value in {"True []", "True None []"}:
        return ("all-ok",)
    if value == "[] []":
        return {"projected_events": True}
    if value.isascii() and value.isdecimal() and len(value) <= 3:
        return ("count", int(value))
    if (match := ACT_COMPACT.fullmatch(value)) and isinstance(result := literal(match[1]), list) and result:
        return result
    if (
        value.startswith("[") and "'seq':" in value
        and (match := re.match(r"^(\[.*?\])(?: \[|$)", value))
        and isinstance(result := literal(match[1]), list)
    ):
        return result
    if value.startswith("{") and "'" in value and value.endswith("}") and isinstance(result := literal(value), dict):
        return result
    return None


def fragments(text):
    if not text or text.startswith("<tool_use_error>"):
        return []
    text = text.split("\n[stdout]\n", 1)[0].split("\n[metadata] ", 1)[0]
    assert len(text) <= 2_000_000
    decoder = json.JSONDecoder()
    found = []
    spans = []
    stop = 0
    for hit in re.finditer(r"[\[{]", text):
        pos = hit.start()
        if pos < stop:
            continue
        try:
            item, stop = decoder.raw_decode(text, pos)
            spans.append((pos, stop))
        except json.JSONDecodeError:
            item, stop = partial_reply(text, pos, decoder) if hit[0] == "{" else (None, pos + 1)
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            item = literal_json(item["text"])
        if isinstance(item, dict | list):
            found.append((pos, item))
    pos = span_index = 0
    for line in text.splitlines(keepends=True):
        while span_index < len(spans) and spans[span_index][1] <= pos:
            span_index += 1
        covered = span_index < len(spans) and spans[span_index][0] <= pos < spans[span_index][1]
        if not covered and (item := projected_line(line.strip())) is not None:
            found.append((pos, item))
        pos += len(line)
    found.sort(key=lambda record: record[0])
    return [item for _, item in found]


def literal_json(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def descendants(items):
    stack = deque(items)
    for _ in range(LIMIT):
        if not stack:
            break
        item = stack.popleft()
        if isinstance(item, dict):
            yield item
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    assert not stack


def endpoint_result(item):
    if not isinstance(item, dict):
        return None
    for key, endpoint in (("results", "act"), ("events", "events"), ("time", "status"), ("radius", "look")):
        if key in item:
            return endpoint
    return None


def action_results(items, acts, call):
    if call.blocked:
        return [False] * len(acts)
    results = []
    for item in items:
        if endpoint_result(item) == "act" and isinstance(item["results"], list):
            results.append(item["results"])
        elif isinstance(item, dict) and "partial_act" in item:
            results.append(item["partial_act"])
        elif item == ("all-ok",):
            results.append(True)
        elif isinstance(item, tuple) and item[0] == "count" and call.ok_count_projection:
            spec = acts[0] if len(acts) == 1 else None
            if spec and spec.ops and item[1] == len(spec.ops) + 1:
                results.append(True)
        elif isinstance(item, dict) and item.get("ok") is False and type(item.get("http_status")) is int:
            results.append(False)
        elif isinstance(item, list) and item:
            if all(isinstance(r, dict) and type(r.get("ok")) is bool and "op" in r for r in item):
                results.append(item)
            elif all(isinstance(r, tuple) and len(r) >= 2 and r[0] in OPS and type(r[1]) is bool for r in item):
                results.append([{"op": r[0], "ok": r[1]} for r in item])
    if len(results) == len(acts):
        return results
    if len(acts) == 1 and call.tool != "Bash" and (call.failed is True or (call.result or "").startswith("Error")):
        return [False]
    return [None] * len(acts)


def status_for(result, index, op):
    if type(result) is bool:
        return "accepted" if result else "refused"
    if isinstance(result, list) and index < len(result):
        row = result[index]
        if isinstance(row, dict) and row.get("op", op) == op and type(row.get("ok")) is bool:
            return "accepted" if row["ok"] else "refused"
    return UNKNOWN


def target_for(order, names):
    op = order["op"]
    fields = {
        "build": "tier", "repair": "tier", "start_build": "tier", "patch": "side", "guard": "side",
        "ask": "stranger", "examine": "stranger", "admit": "stranger", "turn_away": "stranger",
        "quarantine": "stranger", "attack": "id", "shoot": "id", "assign": "helper", "search": "place",
        "move_to": "to", "deposit": "item", "withdraw": "item", "craft": "item",
    }
    value = order.get(fields.get(op, ""))
    if isinstance(value, str) and value:
        value = value.casefold().strip()
        if op in {"ask", "examine", "admit", "turn_away", "quarantine", "attack", "assign", "shoot"}:
            value = names.get(value, value)
        if op == "ask":
            return f"{value}/{order['topic']}" if order.get("topic") else UNKNOWN
        if op in {"guard", "patch"}:
            return {"n": "north", "e": "east", "s": "south", "w": "west"}.get(value[:1], UNKNOWN)
        return value
    if op in XY and all(type(order.get(axis)) is int for axis in ("x", "y")):
        tile = f"{order['x']},{order['y']}"
        return f"{tile}/{order.get('item', '*')}" if op == "pickup" else tile
    if op == "deposit":
        return "all"
    return "campfire" if op == "cook" else UNKNOWN


def base_ref(call, seat, start):
    return {
        "slot": seat.slot, "seat": seat.name, "trajectory": str(seat.path), "step": call.step,
        "tool": call.tool, "call_id": call.call_id, "atif_utc": call.atif_time, "session": call.session,
        "issued_s": offset(call.use_time, start), "returned_s": offset(call.result_time, start),
    }


def new_action(seat, ref, op, name, order, acceptance, trial):
    ids = sorted(mid for mid, receipts in seat.deliveries.items() if receipts[0]["step"] < ref["step"])
    return {
        **ref, "kind": "mutating order", "op_index": op, "op": name, "target": target_for(order, trial.names),
        "evidence": "in-context" if ids else UNKNOWN, "acceptance": acceptance, "in_context_ids": ids,
    }


def action_rows(call, ref, results, seat, trial):
    acts = [spec for spec in call.specs if spec.endpoint == "act"]
    accepted = list(zip(acts, results, strict=True)) if len(acts) == len(results) else [(spec, None) for spec in acts]
    out = []
    for act_index, (spec, result) in enumerate(accepted, 1):
        action_ref = {**ref, "act_index": act_index, "via": spec.via}
        if spec.ops is None:
            if result is not False and call.failed is not True:
                out.append(new_action(seat, action_ref, UNKNOWN, UNKNOWN, {"op": UNKNOWN}, UNKNOWN, trial))
            continue
        if spec.replace and spec.ops and result is not False and call.failed is not True:
            status = "accepted" if result is True or isinstance(result, list) else UNKNOWN
            out.append(new_action(seat, action_ref, 0, "replace", {"op": "replace"}, status, trial))
        for i, order in enumerate(spec.ops):
            name = order["op"]
            status = status_for(result, i, name)
            if name in POST_OPS:
                match_post(seat, trial, call, action_ref, i, order, status)
            elif name != "read_notes":
                action = new_action(seat, action_ref, i + 1, name, order, status, trial)
                if name == "start_build" and isinstance(result, list) and i < len(result) and isinstance(result[i], dict):
                    action["started"] = result[i].get("started", UNKNOWN)
                out.append(action)
    return out


def match_post(seat, trial, call, ref, i, order, status):
    text = order.get("text")
    if status == "refused" or not isinstance(text, str):
        return
    used = {p["id"] for p in seat.posts}
    low, high = ref["issued_s"], ref["returned_s"]
    matches = [p for p in trial.posts if p["sender"] == seat.name and p["id"] not in used and p["channel"] == ("noticeboard" if order["op"] == "post_note" else order["op"]) and text.strip()[: len(p["text"])] == p["text"]]
    bounded = [p for p in matches if between(p["sent_s"], low, high, margin=1)]
    if len(bounded) == 1 or (not bounded and len(matches) == 1 and (call.use_time is None or call.result_time is None)):
        post = (bounded or matches)[0]
        seat.posts.append({"id": post["id"], **ref, "op_index": i + 1, "evidence": "sent"})


def between(value, low, high, *, margin=0):
    return all(isinstance(v, int | float) for v in (value, low, high)) and low - margin <= value <= high + margin


def match_delivery(record, posts, seat, returned):
    sender = record.get("frm", record.get("from"))
    channel = record.get("how", "noticeboard" if sender is not None and "t" in record and "kind" not in record else None)
    if not sender or channel not in POST_OPS | {"noticeboard"} or not isinstance(record.get("text"), str):
        return None
    matches = [p for p in posts if p["sender"] == sender and p["channel"] == channel and p["text"] == record["text"] and (seat in p["game_recipients"] or channel == "noticeboard") and all(record.get(k, p[k]) == p[k] for k in ("day", "clock", "t")) and (returned == UNKNOWN or returned + 1 >= p["sent_s"])]
    return matches[0] if len(matches) == 1 else None


def observe(seat, call, ref, items, trial):
    hits = {}
    for record in descendants(items):
        match = match_delivery(record, trial.posts, seat.name, ref["returned_s"])
        if match:
            existing = hits.setdefault(match["id"], set())
            if record.get("kind") == "heard" and type(record.get("seq")) is int:
                existing.add(record["seq"])
        if record.get("kind") == "done" and isinstance(record.get("text"), str) and type(record.get("t")) is int:
            seat.completions.append({"t": record["t"], "text": record["text"], "seq": record.get("seq", UNKNOWN), **ref})
    endpoints = {}
    for mid, seqs in sorted(hits.items()):
        endpoints[mid] = read_via(call, items, mid, seqs, trial)
        seat.deliveries[mid].append({**ref, "event_seqs": sorted(seqs), "evidence": "delivered", "read_via": endpoints[mid]})
    return endpoints


def read_via(call, items, mid, seqs, trial):
    endpoints = sorted({spec.endpoint for spec in call.specs})
    post = next(p for p in trial.posts if p["id"] == mid)
    if post["channel"] == "noticeboard" and "act" in endpoints:
        return "read_notes"
    if len(endpoints) == 1:
        return endpoints[0]
    proven = {kind for item in items if (kind := endpoint_result(item)) and any(r.get("text") == post["text"] and r.get("from", r.get("frm")) == post["sender"] for r in descendants([item]))}
    if len(proven) == 1:
        return next(iter(proven))
    return "events" if seqs and not proven else UNKNOWN


def event_reply(item):
    if isinstance(item, dict):
        return endpoint_result(item) == "events" or "partial_events" in item or "projected_events" in item
    return isinstance(item, list) and bool(item) and all(isinstance(row, dict) and "seq" in row for row in item)


def call_rows(call, ref, items, deliveries, results):
    counts = Counter(endpoint_result(item) for item in items)
    act_position = 0
    event_positions = sum(spec.endpoint == "events" for spec in call.specs)
    if event_positions == 1 and any(event_reply(item) for item in items):
        counts["events"] = 1
    for i, spec in enumerate(call.specs, 1):
        endpoint = spec.endpoint
        result = None
        if endpoint == "act":
            result = results[act_position]
            act_position += 1
        receipt = "confirmed" if counts[endpoint] > 0 else UNKNOWN
        if endpoint == "act" and (result is True or isinstance(result, list)):
            receipt = "confirmed"
        if result is False or call.blocked:
            receipt = "refused"
        if counts[endpoint] > 0:
            counts[endpoint] -= 1
        ids = [mid for mid, via in deliveries.items() if via == endpoint]
        multiple = sum(other.endpoint == endpoint for other in call.specs) > 1
        complete = len(call.specs) == 1 and any(
            endpoint_result(o) == endpoint and "heard" in o and "heard_more" not in o for o in items
        )
        yield {
            **ref, "endpoint": endpoint, "via": spec.via, "endpoint_index": i,
            "kind": "board read" if endpoint == "events" else "game response" if endpoint == "act" else "observation",
            "receipt": receipt, "multiplicity": UNKNOWN if spec.repeated else 1,
            "message_ids_in_tool_result": ids if (ids and not multiple) or complete else UNKNOWN,
            "tool_result_unassigned_message_ids": [mid for mid, via in deliveries.items() if via == UNKNOWN or (multiple and via == endpoint)],
            "evidence": "delivered" if ids else UNKNOWN,
        }
        for n, order in enumerate(spec.ops or []):
            if order["op"] != "read_notes":
                continue
            status = status_for(result, n, "read_notes")
            note_ids = [mid for mid, via in deliveries.items() if via == "read_notes"]
            yield {
                **ref, "endpoint": "read_notes", "via": spec.via, "endpoint_index": i,
                "op_index": n + 1, "kind": "board read",
                "receipt": {"accepted": "confirmed", "refused": "refused"}.get(status, UNKNOWN),
                "multiplicity": 1, "message_ids_in_tool_result": note_ids if note_ids else UNKNOWN,
                "evidence": "delivered" if note_ids else UNKNOWN,
            }


def load_seat(trial, slot, name):
    path = trial.path / "agent" / slot / "trajectory.json"
    document = payload(path)
    assert document["schema_version"] == "ATIF-v1.7", path
    steps = document["steps"]
    assert 0 < len(steps) <= LIMIT and [r["step_id"] for r in steps] == list(range(1, len(steps) + 1)), path
    sessions = session_times(path.parent)
    seat = Seat(path, name, slot)
    seat.start = max(0.0, stamp(steps[0]["timestamp"]) - trial.start)
    ends = [stamp(r["tool_result"]["utc"]) for r in sessions.values() if "tool_result" in r]
    seat.end = max([stamp(steps[-1]["timestamp"]), *ends]) - trial.start
    assert seat.end > seat.start, path
    for call in trajectory_calls(steps, sessions):
        ref = base_ref(call, seat, trial.start)
        items = fragments(call.result)
        ids = observe(seat, call, ref, items, trial)
        results = action_results(items, [spec for spec in call.specs if spec.endpoint == "act"], call)
        seat.calls.extend(call_rows(call, ref, items, ids, results))
        seat.actions.extend(action_rows(call, ref, results, seat, trial))
    return seat


def read_trial(path, arm):
    harbor = payload(path / "result.json")
    aliases = harbor["agent_result"]["metadata"]["agent_names"]
    assert len(aliases) == (1 if arm == "solo" else 2)
    board = jsonl(path / "board-events.jsonl")
    (clock_line, clock), = [(n, r) for n, r in board if r["event"] == "holdout_start"]
    assert set(aliases.values()) == set(clock["seats"])
    assert not any(r["event"] in {"holdout_resume", "holdout_clock_resumed"} for _, r in board)
    assert not any(r["event"] in {"send", "post", "notify", "inbox", "history", "read"} for _, r in board), path
    replay = jsonl(path / "verifier/replay.jsonl")
    assert replay[0][1]["type"] == "header" and replay[-1][1]["type"] == "frame"
    header = replay[0][1]
    assert {s["seat"] for s in header["survivors"]} == set(aliases.values())
    names = {s["name"].casefold(): s["id"] for s in header["strangers"]}
    posts = [
        {"id": f"game:L{n}", "line": n, "sender": r["seat"], "game_recipients": r["to"], "channel": r["channel"], "sent_s": r["wall"], "t": r["t"], "day": r["day"], "clock": r["clock"], "text": r["text"], "evidence": "sent", "file": str(path / "verifier/messages.jsonl")}
        for n, r in jsonl(path / "verifier/messages.jsonl")
    ]
    assert arm not in {"silent", "solo"} or not posts
    assert all(p["sender"] in aliases.values() and set(p["game_recipients"]) <= set(aliases.values()) for p in posts)
    proxy = jsonl(path / "proxy-usage.jsonl")
    models = {r["model"] for _, r in proxy if r.get("model")}
    assert len(models) <= 1
    return Trial(path, arm, stamp(clock["ts"]), replay[-1][1]["wall"], clock_line, replay[-1][0], aliases, names, next(iter(models), UNKNOWN), posts, proxy, replay)


def resource_verb(verb):
    return next(op for op in ("chop", "quarry", "forage", "fish", "search") if verb.casefold().startswith(op.rstrip("e").rstrip("y")))


def announcements(text):
    runs = {(resource_verb(m[1]) if m[1].casefold() != "taking the trees" else "chop", f"{m[2]},{m[3]}") for m in ANNOUNCE.finditer(text)}
    for m in ANNOUNCE.finditer(text):
        more = re.match(rf"\s*(?:and|/|,)\s*{COORD}", text[m.end():])
        if more and m[1].casefold().startswith(("chop", "taking")):
            runs.add(("chop", f"{more[1]},{more[2]}"))
    runs.update(("build", m[1].casefold()) for m in BUILD.finditer(text) if m[0].casefold().startswith(("i", "joining")))
    runs.update(("start_build", m[1].casefold()) for m in BUILD.finditer(text) if m[0].casefold().startswith(("started", "starting", "opened")))
    return sorted(runs)


def requests(text):
    keys = {("build", m[1].casefold()) for m in REQUEST_BUILD.finditer(text)}
    keys.update((resource_verb(m[1]), f"{m[2]},{m[3]}") for m in REQUEST_RESOURCE.finditer(text))
    keys.update(("start_build", m[1].casefold()) for m in REQUEST_START.finditer(text))
    if REQUEST_WOOD.search(text):
        keys.add(("deposit", "wood"))
    return sorted(keys)


def good_actions(seat):
    return [a for a in seat.actions if a["acceptance"] == "accepted"]


def done_matches(event, op, target):
    text = event["text"]
    if (op, target) == ("deposit", "wood"):
        return bool(re.match(r"Deposited\s+(?:[^.]*?, )?\d+ wood\b", text))
    prefix = {"chop": "Chopped", "quarry": "Quarried", "forage": "Picked", "search": "Pulled"}.get(op)
    return prefix is not None and text.startswith(prefix + " ") and f"({target})" in text


def request_result(post, seat, receipt, op, target, trial, seconds):
    sent, until = post["sent_s"], post["sent_s"] + seconds
    relevant = [a for a in seat.actions if a["acceptance"] != "refused" and a["op"] == op and a["target"] in {target, "all" if op == "deposit" else target}]
    accepted = [a for a in relevant if a["acceptance"] == "accepted" and between(a["issued_s"], sent, until) and between(a["returned_s"], sent, until)]
    completion = next((e for e in seat.completions if done_matches(e, op, target) and sent + 1 < game_wall(e["t"], trial) < until - 1), None)
    action = next(iter(accepted), None)
    exposure = "in-context" if receipt and action and receipt["step"] < action["step"] else "delivered same step" if receipt and action and receipt["step"] == action["step"] else UNKNOWN
    observable = op in {"deposit", "chop", "quarry", "forage", "search"}
    return {
        "op": op, "target": target, "window_end_s": round(until, 3), "window_censored": until > min(trial.duration, seat.end),
        "accepted_order_within_window": bool(action) if action else UNKNOWN,
        "done_within_window": True if completion or (op == "start_build" and action and action.get("started") == target) else UNKNOWN,
        "completion_trackable_from_receiver_event": observable,
        "action": action or UNKNOWN, "completion": completion or UNKNOWN, "evidence_at_action": exposure,
    }


def game_wall(minute, trial):
    header = trial.replay[0][1]
    return (minute - header["start_minute"]) * header["real_sec_per_day"] / header["day_minutes"]


def latency(post, seat, receipt):
    sent = post["sent_s"]
    if not receipt or receipt["returned_s"] == UNKNOWN:
        return {"seconds_to_first_visible_result": UNKNOWN, "actions_before_delivery": UNKNOWN, "confirmed_actions_before_delivery": UNKNOWN, "acted_before_delivery": UNKNOWN}
    returned = receipt["returned_s"]
    before = 0
    ambiguous = False
    for a in seat.actions:
        if a["acceptance"] == "refused" or a["step"] > receipt["step"]:
            continue
        low, high = a["issued_s"], a["returned_s"]
        if high != UNKNOWN and high < sent - 1 or low != UNKNOWN and low > returned:
            continue
        if a["step"] < receipt["step"] and a["acceptance"] == "accepted" and low != UNKNOWN and low > sent + 1 and high != UNKNOWN and high <= returned:
            before += 1
        else:
            ambiguous = True
    return {"seconds_to_first_visible_result": round(max(0.0, returned - sent), 3), "actions_before_delivery": UNKNOWN if ambiguous else before, "confirmed_actions_before_delivery": before, "acted_before_delivery": True if before else UNKNOWN if ambiguous else False}


def message_rows(trial, seats, seconds):
    for post in trial.posts:
        sender = seats[post["sender"]]
        own = next((p for p in sender.posts if p["id"] == post["id"]), None)
        for name, seat in sorted(seats.items()):
            if name == sender.name:
                continue
            receipts = seat.deliveries.get(post["id"], [])
            first = min(receipts, key=lambda r: (r["step"], r["returned_s"] if r["returned_s"] != UNKNOWN else float("inf"))) if receipts else None
            reachable = name in post["game_recipients"]
            status = "delivered" if first else "not game-routed" if not reachable and post["channel"] != "noticeboard" else UNKNOWN
            yield {
                **post, "sender_slot": sender.slot, "sender_tool": own or UNKNOWN, "receiver": name, "receiver_slot": seat.slot, "receiver_trajectory": str(seat.path),
                "game_routed": reachable if post["channel"] != "noticeboard" else UNKNOWN, "delivery": status,
                "first_delivery": first or UNKNOWN, "all_deliveries": receipts,
                **latency(post, seat, first), "announced_targets": announcements(post["text"]),
                "requests": [request_result(post, seat, first, op, target, trial, seconds) for op, target in requests(post["text"])],
            }


def definitely_before(a, b):
    if a["seat"] == b["seat"] and a["step"] != b["step"]:
        return a["step"] < b["step"]
    return a["returned_s"] != UNKNOWN and b["issued_s"] != UNKNOWN and a["returned_s"] < b["issued_s"]


def announcement_evidence(first, second, messages):
    key = (second["op"], second["target"])
    matching = [p for p in messages if p["sender"] == first["seat"] and p["receiver"] == second["seat"] and key in p["announced_targets"]]
    before, unclear = [], []
    for p in matching:
        receipt = p["first_delivery"]
        if receipt != UNKNOWN and receipt["step"] < second["step"]:
            before.append(("in-context", p))
        elif receipt != UNKNOWN and receipt["step"] == second["step"]:
            before.append(("delivered same step", p))
        elif second["issued_s"] != UNKNOWN and p["sent_s"] + 1 < second["issued_s"]:
            before.append(("posted not delivered", p))
        elif second["returned_s"] == UNKNOWN or p["sent_s"] - 1 <= second["returned_s"]:
            unclear.append(p)
    for evidence in ("in-context", "delivered same step", "posted not delivered"):
        candidates = [p for label, p in before if label == evidence]
        if candidates:
            return evidence, [p["id"] for p in candidates]
    return (UNKNOWN, [p["id"] for p in unclear]) if unclear else ("no post", [])


def bounded_sort(a):
    return (a["issued_s"] if a["issued_s"] != UNKNOWN else float("inf"), a["seat"], a["step"], a["act_index"], -1 if a["op_index"] == UNKNOWN else a["op_index"])


def duplicates(seats, messages):
    history = defaultdict(dict)
    candidates = sorted((a for s in seats.values() for a in s.actions if a["acceptance"] != "refused" and a["target"] != UNKNOWN and a["op"] in ACTIVITY), key=bounded_sort)
    for second in candidates:
        key = second["op"], second["target"]
        prior = history[key]
        peer_orders = [a for name, orders in prior.items() if name != second["seat"] for a in orders]
        if peer_orders:
            first = next((a for a in peer_orders if a["acceptance"] == "accepted" and definitely_before(a, second)), peer_orders[0])
            order_known = definitely_before(first, second)
            evidence, ids = announcement_evidence(first, second, messages) if order_known else (UNKNOWN, [])
            gap = [round(second["issued_s"] - first["returned_s"], 3), round(second["returned_s"] - first["issued_s"], 3)] if order_known and first["issued_s"] != UNKNOWN and second["returned_s"] != UNKNOWN else UNKNOWN
            yield {"first": first, "second": second, "order": "confirmed" if order_known else UNKNOWN, "accepted_pair": first["acceptance"] == second["acceptance"] == "accepted", "shared_work_subset": second["op"] in SHARED_WORK, "gap_bounds_s": gap, "announcement_evidence": evidence, "announcement_ids": ids}
        prior.setdefault(second["seat"], []).append(second)


def proxy_seat(trial, name):
    rows = [(n, r) for n, r in trial.proxy if r.get("agent") == name]
    costs = [r.get("usd") for _, r in rows if r.get("status") == 200]
    return {
        "file": str(trial.path / "proxy-usage.jsonl"), "rows": len(rows),
        "model": trial.model, "http_status": dict(Counter(str(r.get("status", UNKNOWN)) for _, r in rows)),
        "first_line": rows[0][0] if rows else UNKNOWN, "last_line": rows[-1][0] if rows else UNKNOWN,
        "first_utc": rows[0][1].get("ts", UNKNOWN) if rows else UNKNOWN,
        "last_utc": rows[-1][1].get("ts", UNKNOWN) if rows else UNKNOWN,
        "success_usd": round(sum(costs), 9) if costs and all(isinstance(c, int | float) for c in costs) else UNKNOWN,
    }


def read_metrics(seat):
    reads = [c for c in seat.calls if c["kind"] == "board read" and c["receipt"] != "refused"]
    verified = [c for c in reads if c["receipt"] == "confirmed"]
    actions = good_actions(seat)
    possible_actions = [a for a in seat.actions if a["acceptance"] == UNKNOWN]
    counts = Counter(a["step"] for a in actions)
    possible = Counter(a["step"] for a in possible_actions)
    confirmed_steps = sorted({c["step"] for c in verified})
    observed_steps = sorted({c["step"] for c in reads})
    unknown_steps = set(observed_steps) - set(confirmed_steps)
    gaps = [
        UNKNOWN if any(lo < s < hi for s in possible.keys() | unknown_steps)
        else sum(n for step, n in counts.items() if lo < step < hi)
        for lo, hi in zip(confirmed_steps, confirmed_steps[1:], strict=False)
    ]
    first = min(counts, default=0)
    last = max(counts, default=0)
    inner_read = any(first < s < last for s in confirmed_steps)
    last_early_read = max((s for s in confirmed_steps if s < first), default=0)
    if last_early_read:
        first_order_read = UNKNOWN if any(s <= last_early_read for s in possible) else True
    else:
        first_order_read = UNKNOWN if not first or any(s < first for s in unknown_steps) else False
    one_batch = UNKNOWN
    if len(counts) > 1 or len(verified) > 1:
        one_batch = False
    elif len(verified) == len(reads) == 1 and not possible and reads[0]["multiplicity"] == 1:
        one_batch = bool(first and confirmed_steps[0] < first == last)
    fresh = uncertain = previous = 0
    for step, n in sorted(counts.items()):
        lower = max((s for s in possible if previous < s < step), default=previous)
        if any(lower < s < step for s in confirmed_steps) and not possible[step]:
            fresh += 1
        elif any(previous < s < step for s in observed_steps):
            uncertain += 1
        previous = step
        assert n > 0
    return {
        "seat_seconds": round(seat.end - seat.start, 3), "confirmed_reads": len(verified), "unknown_read_receipts": len(reads) - len(verified),
        "dynamic_read_sites": sum(c["multiplicity"] == UNKNOWN for c in reads), "read_steps": len(confirmed_steps),
        "accepted_actions": len(actions), "unknown_action_receipts": len(possible_actions), "refused_orders": sum(a["acceptance"] == "refused" for a in seat.actions),
        "target_keyed_actions": sum(a["op"] in ACTIVITY and a["target"] != UNKNOWN for a in actions),
        "shared_work_target_keyed_actions": sum(a["op"] in SHARED_WORK and a["target"] != UNKNOWN for a in actions),
        "fresh": fresh, "fresh_unknown": uncertain,
        "read_gaps": gaps, "uncertain_read_gaps": gaps.count(UNKNOWN),
        "one_read_then_single_mutation_step": one_batch,
        "confirmed_read_before_first_mutation_step": first_order_read,
        "no_read_between_first_and_last": False if inner_read else UNKNOWN if not first or possible or any(first < s < last for s in unknown_steps) else True,
        "largest_mutation_step": max(counts.values(), default=UNKNOWN),
        "observations": sum(c["endpoint"] in {"status", "look"} for c in seat.calls),
        "message_bearing_result_calls": len({r["call_id"] for receipts in seat.deliveries.values() for r in receipts}),
    }


def distribution(values):
    values = [v for v in values if v != UNKNOWN]
    return {"n": len(values), "median": median(values) if values else UNKNOWN, "max": max(values) if values else UNKNOWN}


def summarize(metrics, messages, collisions):
    counts = Counter()
    sum_fields = ("seat_seconds", "confirmed_reads", "unknown_read_receipts", "dynamic_read_sites", "read_steps", "accepted_actions", "unknown_action_receipts", "refused_orders", "target_keyed_actions", "shared_work_target_keyed_actions", "fresh", "fresh_unknown", "uncertain_read_gaps", "observations", "message_bearing_result_calls")
    for m in metrics:
        for key in sum_fields:
            counts[key] += m[key]
    delivered = [m for m in messages if m["delivery"] == "delivered"]
    asks = [r for p in messages if p["game_routed"] is not False for r in p["requests"]]
    confirmed = [c for c in collisions if c["order"] == "confirmed" and c["accepted_pair"]]
    shared = [c for c in confirmed if c["shared_work_subset"]]
    return {
        **counts, "seat_seconds": round(counts["seat_seconds"], 3), "agents": len(metrics),
        "confirmed_reads_per_10m_lower_bound": round(600 * counts["confirmed_reads"] / counts["seat_seconds"], 3),
        "fresh_share_among_confirmed_actions_lower_bound": counts["fresh"] / counts["accepted_actions"] if counts["accepted_actions"] else UNKNOWN,
        "actions_strictly_between_confirmed_read_steps": distribution([g for m in metrics for g in m["read_gaps"]]),
        "largest_mutation_step": distribution([m["largest_mutation_step"] for m in metrics]),
        "one_read_then_single_mutation_step": dict(Counter(str(m["one_read_then_single_mutation_step"]).lower() for m in metrics)),
        "confirmed_read_before_first_mutation_step": dict(Counter(str(m["confirmed_read_before_first_mutation_step"]).lower() for m in metrics)),
        "no_read_between_first_and_last": dict(Counter(str(m["no_read_between_first_and_last"]).lower() for m in metrics)),
        "message_receiver_pairs": len(messages), "game_routed_pairs": sum(m["game_routed"] is True for m in messages), "delivery": dict(Counter(m["delivery"] for m in messages)),
        "delivery_seconds": distribution([m["seconds_to_first_visible_result"] for m in delivered]),
        "actions_before_delivery": distribution([m["actions_before_delivery"] for m in delivered]),
        "acted_before_delivery": dict(Counter(str(m["acted_before_delivery"]).lower() for m in messages)),
        "request_pairs": len(asks), "requests_accepted_in_window": sum(r["accepted_order_within_window"] is True for r in asks),
        "requests_done_in_window": sum(r["done_within_window"] is True for r in asks),
        "requests_done_unknown": sum(r["done_within_window"] == UNKNOWN for r in asks),
        "request_windows_censored": sum(r["window_censored"] for r in asks),
        "unrouted_request_pairs": sum(len(p["requests"]) for p in messages if p["game_routed"] is False),
        "confirmed_cross_seat_repeats": len(confirmed), "repeat_candidates_unknown": len(collisions) - len(confirmed),
        "shared_work_repeats": len(shared),
        "shared_work_repeat_evidence": dict(Counter(r["announcement_evidence"] for r in shared)),
        "repeat_evidence": dict(Counter(r["announcement_evidence"] for r in confirmed)),
        "shared_work_repeat_gap_within_15_seconds": sum(r["gap_bounds_s"] != UNKNOWN and r["gap_bounds_s"][1] <= 15 for r in shared),
        "shared_work_repeat_gap_unknown_at_15_seconds": sum(r["gap_bounds_s"] == UNKNOWN or r["gap_bounds_s"][0] <= 15 < r["gap_bounds_s"][1] for r in shared),
    }


def analyze(path, arm, seconds):
    trial = read_trial(path, arm)
    seats = {name: load_seat(trial, slot, name) for slot, name in sorted(trial.aliases.items())}
    messages = list(message_rows(trial, seats, seconds))
    collisions = list(duplicates(seats, messages))
    metrics = {s.slot: read_metrics(s) for s in seats.values()}
    agents = {}
    for s in seats.values():
        post_steps = {p["id"]: p for p in s.posts}
        posts = [{**p, "source_tool": post_steps.get(p["id"], UNKNOWN)} for p in trial.posts if p["sender"] == s.name]
        own_messages = [m for m in messages if m["receiver"] == s.name]
        own_collisions = [c for c in collisions if c["second"]["seat"] == s.name]
        agents[s.slot] = {
            "name": s.name, "trajectory": str(s.path), "metrics": summarize([metrics[s.slot]], own_messages, own_collisions),
            "proxy": proxy_seat(trial, s.name), "trace_window_s_from_game_start": [round(s.start, 3), round(s.end, 3)],
            "timeline": sorted(
                [*s.calls, *s.actions, *({**p, "kind": "post"} for p in posts)],
                key=timeline_order,
            ),
        }
    return {
        "arm": arm, "trial": path.name, "root": str(path), "model": trial.model, "agents": agents,
        "message_rows": messages, "duplicate_rows": collisions, "metrics": summarize(list(metrics.values()), messages, collisions),
        "world_seconds": trial.duration, "game_start_epoch": trial.start,
        "board_start": {"file": str(path / "board-events.jsonl"), "line": trial.clock_line},
        "replay_end": {"file": str(path / "verifier/replay.jsonl"), "line": trial.replay_end_line},
        "seat_measure_inputs": list(metrics.values()),
    }


def timeline_order(event):
    elapsed = event.get("sent_s", event.get("issued_s", UNKNOWN))
    operation = event.get("op_index", 0)
    return (
        elapsed if elapsed != UNKNOWN else float("inf"),
        event.get("step", LIMIT), event.get("endpoint_index", 0),
        event.get("act_index", 0), operation if operation != UNKNOWN else LIMIT,
    )


METHOD = {
    "evidence": "sent = world message log; delivered = exact sender, channel, text and available game time in the receiver's tool result; in-context = matching result on a strictly earlier ATIF step; unknown = missing or ambiguous evidence. No claim about attention or intent.",
    "sources": {
        "agent/*/trajectory.json": "ATIF-v1.7 schema_version, steps.step_id, timestamp, tool_calls.{tool_call_id,function_name,arguments}, observation.results.{source_call_id,content,extra.tool_result_is_error}",
        "agent/*/sessions/projects/-app/*.jsonl": "tool_use id and tool_result tool_use_id, timestamps and raw line numbers; bounds, not exact server times",
        "result.json": "agent_result.metadata.agent_names maps slots to game names",
        "board-events.jsonl": "holdout_start.ts, seats; external board has no teammate posts in this cohort",
        "verifier/messages.jsonl": "line ID (game:L<n>), seat, channel, to, text, wall, t, day, clock. to is in-world routing, not a model receipt",
        "verifier/replay.jsonl": "header.survivors, strangers, start_minute, day_minutes, real_sec_per_day; final frame.wall. Replay is sampled state, not an action ledger",
        "proxy-usage.jsonl": "agent, model, ts, status, usd; no message evidence",
    },
    "importer": "Direct stdlib read. The local Bespoke adapter normalizes harness seats, proxy and ATIF, but its board parser covers harness traffic and may infer fetched IDs. This reader needs exact game payloads, game logs and tool receipt bounds.",
    "reads": "Explicit board read = events or read_notes. Status/look/act can return heard but do not count as explicit reads. Confirmed calls in loops count once per site. Gap distributions use consecutive confirmed read steps. They exclude boundary steps and gaps with unknown internal receipts. The first action counts as fresh only if a read is on an earlier step. Same-step reads get no credit. Rates use recorded seat time.",
    "actions": "Mutating non-message ops include queued orders, wait/stop and replace; acknowledgments do not establish completion. Refused orders do not count. Bash is parsed statically and projections or missing results can leave order acceptance or read count unknown.",
    "target": "Same op and exact API target: x,y for terrain; place ID for named search; stranger ID and topic for ask; person ID for decisions; tier for construction; side for wall work; helper ID for assignment; item (or all) for stock; named or coordinate destination for move. Pickup also keeps the item selector. No alias between a place and a coordinate. Same-target movement, stock, or parallel building need not be waste. Shared-work subset is terrain resource work, search/pickup, interviews or decisions and start_build.",
    "duplicates": "One row per later matching order, anchored to the first provably earlier accepted peer order when available. Acceptance and server order are separate. Order is provable across seats only when tool intervals do not overlap. No post = no mechanically matching logged announcement before the repeat, not proof of no peer text. Posted not delivered = no tool result with earlier or same-step evidence, not a server receipt verdict.",
    "announcements": "First-person or sentence-initial concrete terrain verb with coordinates (also 'taking the trees'), and explicit build/start verbs with tier. No unrestricted text, intent or broad map areas. Announcements may precede or follow the first action.",
    "requests": "Exact 'please' + resource verb + tile, 'please build' or 'build tier' + tier, 'start_build' + listed tier, or 'please [bring wood to stockpile|deposit wood]' including a coordinated deposit clause. Count per teammate per matched target from send time. Accepted order and completion are separate. A completion requires a matching receiver done event or the instant start_build acknowledgment. No individual build completion is inferred from a team state.",
    "timing": "UTC offsets relative to board holdout_start and rounded game wall seconds; allow 1 s at clock joins. Delivery lag measures first visible tool result end, not when survivor hears. Actions in same step or with overlapping bounds can make exact action lag unknown. No proxy timestamps are used as delivery times.",
}


def collect(root, minutes):
    assert root.is_dir() and 0 < minutes <= 30
    trials = []
    for arm in ARMS:
        paths = sorted((root / "arms" / arm).glob(f"study4-{arm}-*__task__*"))
        assert len(paths) == 2, (arm, paths)
        trials.extend(analyze(path, arm, minutes * 60) for path in paths)
    arms = {}
    for arm in ARMS:
        selected = [t for t in trials if t["arm"] == arm]
        arms[arm] = summarize([m for t in selected for m in t.pop("seat_measure_inputs")], [m for t in selected for m in t["message_rows"]], [c for t in selected for c in t["duplicate_rows"]])
    return {"root": str(root), "request_minutes": minutes, "method": METHOD, "arms": arms, "trials": trials}


def row(label, m):
    gaps = m["actions_strictly_between_confirmed_read_steps"]
    delivery = m["delivery"].get("delivered", 0)
    unknown = m["delivery"].get(UNKNOWN, 0)
    return [
        label, str(m["agents"]), f"{m['confirmed_reads']}/{m['seat_seconds']:.1f}",
        str(m["confirmed_reads_per_10m_lower_bound"]),
        f"{m['unknown_read_receipts']}/{m['dynamic_read_sites']}",
        f"{gaps['median']}/{gaps['max']}/{gaps['n']} (+{m['uncertain_read_gaps']}?)",
        f"{m['fresh']}/{m['accepted_actions']} (+{m['fresh_unknown']}?)", str(m["unknown_action_receipts"]),
        f"{delivery}/{m['message_receiver_pairs']} ({unknown}?)",
        f"{m['requests_done_in_window']}/{m['request_pairs']} ({m['requests_done_unknown']}?)",
        f"{m['confirmed_cross_seat_repeats']}/{m['target_keyed_actions']} (+{m['repeat_candidates_unknown']}?)",
    ]


def evidence_row(label, m):
    buckets = ("no post", "posted not delivered", "delivered same step", "in-context", UNKNOWN)
    elapsed = m["delivery_seconds"]
    actions = m["actions_before_delivery"]
    acted = m["acted_before_delivery"]
    return [
        label, f"{elapsed['median']}/{elapsed['max']}/{elapsed['n']}",
        f"{actions['median']}/{actions['max']}/{actions['n']}",
        "/".join(str(acted.get(k, 0)) for k in ("true", "false", UNKNOWN)),
        f"{m['requests_accepted_in_window']}/{m['request_pairs']}",
        str(m["request_windows_censored"]),
        "/".join(str(m["repeat_evidence"].get(k, 0)) for k in buckets),
        "/".join(str(m["shared_work_repeat_evidence"].get(k, 0)) for k in buckets),
    ]


def print_table(rows):
    header = ["Scope", "Seats", "Reads/seat-s", "Reads/10m", "Read?/loop", "Gap med/max/n", "Fresh/actions", "Ops?", "Visible/msgs", "Done/reqs", "Repeats/keyed"]
    print_rows(header, [row(label, m) for label, m in rows])
    print()
    header = ["Scope", "Lag s med/max/n", "Ops before med/max/n", "Acted yes/no/?", "Accepted/reqs", "Censored", "Repeat evidence*", "Shared work*"]
    print_rows(header, [evidence_row(label, m) for label, m in rows])


def print_rows(header, rendered):
    widths = [max(map(len, column)) for column in zip(header, *rendered, strict=True)]
    for values in [header, ["-" * width for width in widths], *rendered]:
        print("  ".join(value.ljust(width) for value, width in zip(values, widths, strict=True)).rstrip())


def print_text(report, detailed):
    print("OBSERVED: Study 4 Holdout. Counts are trace-confirmed lower bounds where evidence is incomplete.")
    for label, text in report["method"].items():
        if label == "sources":
            for source, fields in text.items():
                print(f"{source}: {fields}")
        else:
            print(f"{label}: {text}")
    print(f"Request window: {report['request_minutes']:g} wall minutes from the post.")
    print("* Repeat evidence order: no post / posted not delivered / delivered same step / in-context / unknown.")
    print("? marks an unknown count, not a negative result.")
    print("See --json --trials for all timelines, evidence, and raw file references.")
    print("\nArm totals")
    print_table(list(report["arms"].items()))
    for trial in report["trials"]:
        print(f"\n{trial['trial']}")
        print_table([("trial", trial["metrics"]), *((slot, agent["metrics"]) for slot, agent in trial["agents"].items())])
        if detailed:
            for key in ("message_rows", "duplicate_rows"):
                for item in trial[key]:
                    print(f"{key}: {json.dumps(item, sort_keys=True, ensure_ascii=False)}")


def audit_cases(report):
    all_dups = [(t, c) for t in report["trials"] for c in t["duplicate_rows"] if c["order"] == "confirmed" and c["accepted_pair"]]
    all_msgs = [(t, m) for t in report["trials"] for m in t["message_rows"] if m["delivery"] == "delivered"]
    rng = random.Random(0)
    groups = [("duplicate", pair) for pair in rng.sample(all_dups, min(4, len(all_dups)))]
    groups.extend(("message", pair) for pair in rng.sample(all_msgs, min(4, len(all_msgs))))
    remaining = [("duplicate", pair) for pair in all_dups if ("duplicate", pair) not in groups]
    remaining.extend(("message", pair) for pair in all_msgs if ("message", pair) not in groups)
    assert len(remaining) >= 12 - len(groups)
    return groups + rng.sample(remaining, 12 - len(groups))


def step_citation(ref):
    result = ref["session"].get("tool_result")
    session = f"; `{result['file']}` line {result['line']}" if result else ""
    return f"`{ref['trajectory']}` step {ref['step']}, call `{ref['call_id']}`{session}"


def print_audit(report):
    print("# Holdout hand-audit checklist\n\nOBSERVED: The reader selects 12 cases with `random.Random(0)`.\nNo hand checks are recorded here. Check each item against the raw files.\n")
    for i, (kind, (trial, item)) in enumerate(audit_cases(report), 1):
        print(f"## {i}. {kind.capitalize()}: {trial['trial']}\n")
        if kind == "message":
            rec = item["first_delivery"]
            print(f"1. Check the sender, channel and send time in `{item['file']}` line {item['line']}.")
            print(f"2. Check that the exact message is in the receiver's result at {step_citation(rec)}.")
            print(f"3. Check earlier tool results in `{rec['trajectory']}` steps 1–{rec['step'] - 1} for this message.")
        else:
            a, b = item["first"], item["second"]
            print(f"1. Check the first accepted `{a['op']}` on `{a['target']}` at {step_citation(a)}, op {a['op_index']}.")
            print(f"2. Check the second agent's matching accepted order at {step_citation(b)}, op {b['op_index']}.")
            path = Path(trial["root"]) / "verifier/messages.jsonl"
            lines = [m["line"] for m in trial["message_rows"] if m["id"] in item["announcement_ids"]]
            location = f"lines {', '.join(map(str, lines))}" if lines else "all lines (the file may be empty)"
            print(f"3. Check the announcement evidence: `{item['announcement_evidence']}`. Use `{path}`, {location}, and the second agent's results through step {b['step']}.")
        print()


def main():
    parser = argparse.ArgumentParser(description="Read the 8 stored scored MAC Bench Holdout trials. No calls are executed.")
    parser.add_argument("--root", type=Path, default=PILOT, help="Study 4 directory containing arms/")
    parser.add_argument("--request-minutes", type=float, default=5)
    parser.add_argument("--json", action="store_true", help="write the measurements as JSON")
    parser.add_argument("--trials", action="store_true", help="include all message and duplicate rows")
    parser.add_argument("--hand-audit", action="store_true", help="write the 12-case hand-audit checklist")
    args = parser.parse_args()
    if not 0 < args.request_minutes <= 30:
        parser.error("--request-minutes must be greater than 0 and at most 30")
    report = collect(args.root.resolve(), args.request_minutes)
    if args.hand_audit:
        print_audit(report)
    elif args.json:
        if not args.trials:
            for trial in report["trials"]:
                for name in ("message_rows", "duplicate_rows"):
                    del trial[name]
                for agent in trial["agents"].values():
                    del agent["timeline"]
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
    else:
        print_text(report, args.trials)


if __name__ == "__main__":
    main()
