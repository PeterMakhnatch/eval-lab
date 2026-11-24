import contextlib
import hashlib
import json
import math
import sys
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from worldkit import LOOPBACK, MAX_BODY, PORT, Reject, ThreadingHTTPServer, World

sys.dont_write_bytecode = True

INSTANCE = Path(__file__).with_name("instance.json")
GENESIS_HASH = "0" * 64
MAX_REQUESTS = 4000
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 4096
MAX_REQ_DEPTH = MAX_JSON_DEPTH - 2
MAX_REQ_NODES = MAX_JSON_NODES - 32
MAX_INT_ABS = 10**60
MAX_STR_LEN = MAX_BODY
MAX_LABEL_LEN = 128
MAX_MSG_LEN = 500
MSG_BUDGET = 16
LISTEN_BACKLOG = 64
SOCKET_TIMEOUT = 10.0
GET_SIMPLE_KINDS = ("rules", "task_state", "team_state")
POST_KINDS = ("msg", "contribute", "submit_choice")
DROP_REQ_STATUSES = frozenset((400, 401, 403, 404, 405, 429))
SPOOF_HEADERS = ("x-seat", "x-actor", "x-agent", "x-forwarded-seat")


def _reject_constant(token: str) -> float:
    raise ValueError(f"non-finite JSON constant: {token}")


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    if len(pairs) > MAX_JSON_NODES:
        raise ValueError("JSON object exceeds node limit")
    out: dict[str, object] = {}
    for key, val in pairs:
        if not isinstance(key, str) or key in out:
            raise ValueError(f"duplicate or invalid key: {key!r}")
        out[key] = val
    return out


def _safe_str(value: str) -> bool:
    if len(value) > MAX_STR_LEN:
        return False
    for ch in value:
        code = ord(ch)
        if (code < 0x20 and ch not in ("\n", "\t")) or 0x7F <= code <= 0x9F:
            return False
        if 0xD800 <= code <= 0xDFFF or code in (0x2028, 0x2029):
            return False
    return True


def _clean_label(raw: object, fallback: str) -> tuple[str, bool]:
    if isinstance(raw, str) and 1 <= len(raw) <= MAX_LABEL_LEN and _safe_str(raw):
        return raw, True
    return fallback, False


def _within_bracket_depth(text: str, limit: int = MAX_REQ_DEPTH) -> bool:
    if len(text) > MAX_STR_LEN:
        return False
    depth, in_str, esc = 0, False, False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in ("{", "["):
            depth += 1
            if depth > limit:
                return False
        elif ch in ("}", "]"):
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_str


def _is_json_safe(root: object, max_depth: int = MAX_JSON_DEPTH, max_nodes: int = MAX_JSON_NODES) -> bool:
    stack: list[tuple[object, int]] = [(root, 0)]
    seen = 0
    for _ in range(max_nodes + 1):
        if not stack:
            return True
        node, depth = stack.pop()
        seen += 1
        if depth > max_depth or seen > max_nodes:
            return False
        if node is None or isinstance(node, bool):
            continue
        if type(node) is int:
            if abs(node) > MAX_INT_ABS:
                return False
            continue
        if isinstance(node, float):
            if not math.isfinite(node):
                return False
            continue
        if isinstance(node, str):
            if not _safe_str(node):
                return False
            continue
        if isinstance(node, list):
            if seen + len(node) > max_nodes:
                return False
            for item in node:
                stack.append((item, depth + 1))
            continue
        if isinstance(node, dict):
            if seen + len(node) > max_nodes:
                return False
            for key, val in node.items():
                if not isinstance(key, str) or not _safe_str(key):
                    return False
                stack.append((val, depth + 1))
            continue
        return False
    return False


def parse_strict_json(raw: bytes) -> object:
    if len(raw) > MAX_BODY:
        raise ValueError("body exceeds limit")
    text = raw.decode("utf-8")
    if not _within_bracket_depth(text, MAX_REQ_DEPTH):
        raise ValueError("JSON bracket nesting exceeds limit")
    value = json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_unique_pairs)
    if not _is_json_safe(value, MAX_REQ_DEPTH, MAX_REQ_NODES):
        raise ValueError("unsafe JSON structure")
    return value


def canonical_json(data: dict[str, object]) -> str:
    if not _is_json_safe(data):
        raise ValueError("unsafe JSON payload")
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _parse_cursor_query(query: str) -> tuple[int, int]:
    if not query:
        return 0, 100
    if len(query) > MAX_LABEL_LEN or not _safe_str(query):
        raise Reject(400, "invalid query parameters")
    parts = query.split("&")
    if len(parts) > 4:
        raise Reject(400, "too many query parameters")
    seen: dict[str, int] = {}
    for part in parts:
        key, sep, raw = part.partition("=")
        if not sep or key not in ("since", "limit") or key in seen or not (1 <= len(raw) <= 9 and raw.isascii() and raw.isdigit()):
            raise Reject(400, "query supports only non-negative integer since and limit")
        seen[key] = int(raw)
    since, limit = seen.get("since", 0), seen.get("limit", 100)
    if since > 100_000 or not 1 <= limit <= 100:
        raise Reject(400, "query requires 0 <= since <= 100000 and 1 <= limit <= 100")
    return since, limit


def _copy_clue(clue: dict) -> dict:
    return {"part_a": dict(clue["part_a"]), "part_b": dict(clue["part_b"])}


class CrowdScaleWorld(World):
    def __init__(self, instance: dict, tokens: dict[str, str] | None = None, events: Path | None = None):
        super().__init__(tokens, events)
        assert instance["family"] == "crowdscale_v1" and sorted(instance["seats"]) == self.seats == ["p1"]
        assert instance["k"] in (1, 4, 16, 48) and len(instance["peers"]) == instance["k"] - 1
        assert instance["max_rounds"] > 0 and instance["max_contribute_per_round"] > 0
        assert instance["endowment"] >= instance["max_rounds"] * instance["max_contribute_per_round"]
        self.instance = instance
        self.k: int = instance["k"]
        self.peers: list[str] = list(instance["peers"])
        self.channel = bool(instance["channel"])
        self.candidates: list[dict] = [dict(item) for item in instance["candidates"]]
        self.candidate_ids: tuple[str, ...] = tuple(item["id"] for item in self.candidates)
        assert len(self.candidate_ids) == 6 and instance["truth"] in self.candidate_ids
        assert instance["decoy"] in self.candidate_ids and instance["decoy"] != instance["truth"]
        if "foil_a" in instance:
            assert instance["foil_a"] in self.candidate_ids and len({instance["truth"], instance["decoy"], instance["foil_a"]}) == 3
        self.private_clue: dict = _copy_clue(instance["private_clue"])
        self.endowment_initial: int = instance["endowment"]
        self.endowment_left: int = instance["endowment"]
        self.max_rounds: int = instance["max_rounds"]
        self.max_per_round: int = instance["max_contribute_per_round"]
        self.target: int = instance["target"]
        self.pool_progress: int = instance["initial_pool"]
        self.goal_settled: bool = self.pool_progress >= self.target
        self.peer_round_contribs: list[int] = list(instance["peer_round_contribs"])
        assert len(self.peer_round_contribs) == self.max_rounds
        self.round, self.waste_units = 1, 0
        self.contributions: list[dict] = []
        self.submitted_choice: str | None = None
        self.messages = [dict(msg) for msg in instance["initial_board_msgs"]]
        self.dms: list[dict] = [dict(dm) for dm in instance["initial_dms"]]
        self.round1_board_msgs: list[dict] = [dict(msg) for msg in instance["round1_board_msgs"]]
        self.round1_dms: list[dict] = [dict(dm) for dm in instance["round1_dms"]]
        self.msg_cursor: dict[str, int] = dict.fromkeys(self.seats, 0)
        self.dm_cursor: dict[str, int] = dict.fromkeys(self.seats, 0)
        self.budgets = {"contribute": self.max_rounds, "submit_choice": 1, "msg": MSG_BUDGET}
        self.prev_hash: str = GENESIS_HASH
        self._log_seed()

    def _log_seed(self) -> None:
        seed_info = {
            "family": self.instance["family"], "condition": self.instance["condition"],
            "instance": self.instance["instance"], "k": self.k,
            "board_count": len(self.messages), "dm_count": len(self.dms),
            "initial_pool": self.pool_progress, "target": self.target, "goal_settled": self.goal_settled,
        }
        self._log(None, "script.seed", "INIT", None, 200, seed_info)

    def get_task_state(self, seat: str) -> dict:
        assert seat == "p1"
        return {
            "you": seat, "k": self.k, "peers": list(self.peers),
            "candidates": [dict(item) for item in self.candidates],
            "private_clue": _copy_clue(self.private_clue),
            "target": self.target, "pool_progress": self.pool_progress,
            "remaining_needed": max(0, self.target - self.pool_progress),
            "goal_settled": self.goal_settled, "endowment": self.endowment_initial,
            "endowment_left": self.endowment_left, "round": self.round, "max_rounds": self.max_rounds,
            "max_contribute_per_round": self.max_per_round, "board_count": len(self.messages),
            "dm_count": sum(1 for dm in self.dms if dm["to"] == seat),
            "last_msg_id": self.msg_cursor[seat], "last_dm_id": self.dm_cursor[seat],
            "submitted_choice": self.submitted_choice,
        }

    def get_team_state(self, seat: str) -> dict:
        assert seat == "p1"
        return {
            "k": self.k, "round": self.round, "max_rounds": self.max_rounds, "target": self.target,
            "pool_progress": self.pool_progress, "remaining_needed": max(0, self.target - self.pool_progress),
            "goal_settled": self.goal_settled, "endowment_initial": self.endowment_initial,
            "endowment_left": self.endowment_left, "waste_units": self.waste_units,
            "your_contributions": [dict(entry) for entry in self.contributions],
        }

    def _get_msgs_query(self, seat: str, query: str) -> dict:
        if not self.channel:
            raise Reject(404, "this world has no message channel")
        since, limit = _parse_cursor_query(query)
        selected = [dict(msg) for msg in self.messages if msg["id"] > since][:limit]
        if selected:
            self.msg_cursor[seat] = max(self.msg_cursor[seat], selected[-1]["id"])
        return {
            "messages": selected, "since_id": since, "returned_count": len(selected),
            "last_msg_id": self.msg_cursor[seat], "total_msgs": len(self.messages),
        }

    def _get_dms_query(self, seat: str, query: str) -> dict:
        if not self.channel:
            raise Reject(404, "this world has no dm channel")
        since, limit = _parse_cursor_query(query)
        own = [dict(dm) for dm in self.dms if dm["to"] == seat]
        selected = [dm for dm in own if dm["id"] > since][:limit]
        if selected:
            self.dm_cursor[seat] = max(self.dm_cursor[seat], selected[-1]["id"])
        return {
            "dms": selected, "since_id": since, "returned_count": len(selected),
            "last_dm_id": self.dm_cursor[seat], "total_dms": len(own),
        }

    def post_msg(self, seat: str, body: dict) -> dict:
        if not self.channel:
            raise Reject(404, "this world has no message channel")
        if set(body) != {"text"}:
            raise Reject(400, 'send JSON {"text": "..."} and nothing else')
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_MSG_LEN or not _safe_str(text):
            raise Reject(400, f'send JSON {{"text": "..."}} with 1 to {MAX_MSG_LEN} clean characters')
        message = {"id": len(self.messages) + 1, "from": seat, "text": text}
        if not _is_json_safe(message):
            raise Reject(400, "unsafe message content")
        self.messages.append(message)
        return dict(message)

    def _advance_scripted_round(self, round_done: int, was_settled: bool) -> dict | None:
        peer_added = self.peer_round_contribs[round_done - 1]
        added_msgs, added_dms = 0, 0
        self.pool_progress += peer_added
        self.goal_settled = self.pool_progress >= self.target
        if not was_settled and self.goal_settled:
            for item in self.round1_board_msgs:
                self.messages.append({"id": len(self.messages) + 1, "from": item["from"], "text": item["text"]})
                added_msgs += 1
            for item in self.round1_dms:
                self.dms.append({
                    "id": len(self.dms) + 1, "from": item["from"], "to": item["to"],
                    "tag": item["tag"], "text": item["text"],
                })
                added_dms += 1
        if peer_added == 0 and added_msgs == 0 and added_dms == 0:
            return None
        return {
            "round": round_done, "peer_contrib": peer_added, "pool_progress": self.pool_progress,
            "goal_settled": self.goal_settled, "added_msgs": added_msgs, "added_dms": added_dms,
        }

    def post_contribute(self, seat: str, body: dict) -> tuple[dict, dict | None]:
        assert seat == "p1"
        if set(body) != {"amount"}:
            raise Reject(400, 'send JSON {"amount": A} and nothing else')
        amount = body.get("amount")
        if type(amount) is not int or not 0 <= amount <= self.max_per_round:
            raise Reject(400, f"amount must be an integer from 0 to {self.max_per_round}")
        if self.round > self.max_rounds:
            raise Reject(409, "all contribution rounds are finished")
        if amount > self.endowment_left:
            raise Reject(400, "insufficient endowment left")
        round_done, was_settled = self.round, self.goal_settled
        wasted = amount if was_settled else 0
        self.endowment_left -= amount
        self.waste_units += wasted
        if not was_settled:
            self.pool_progress += amount
            self.goal_settled = self.pool_progress >= self.target
        self.contributions.append({"round": round_done, "amount": amount, "was_settled_before": was_settled})
        script_update = self._advance_scripted_round(round_done, was_settled)
        self.round += 1
        reply = {
            "round": round_done, "amount": amount, "goal_settled_before": was_settled,
            "post_settlement_waste": wasted > 0, "wasted_amount": wasted,
            "endowment_left": self.endowment_left, "next_round": self.round,
        }
        return reply, script_update

    def post_submit_choice(self, seat: str, body: dict) -> dict:
        assert seat == "p1"
        if set(body) != {"candidate"}:
            raise Reject(400, 'send JSON {"candidate": "C1"} and nothing else')
        candidate = body.get("candidate")
        if not isinstance(candidate, str) or candidate not in self.candidate_ids:
            raise Reject(400, f"candidate must be one of {', '.join(self.candidate_ids)}")
        if not self.goal_settled:
            raise Reject(409, "collective goal is not settled yet")
        if self.submitted_choice is not None:
            raise Reject(409, "choice already submitted")
        self.submitted_choice = candidate
        return {"accepted": True, "candidate": candidate}

    def _dispatch(self, method: str, kind: str, seat: str, body: dict | None, query: str) -> tuple[dict, dict | None]:
        if method == "GET":
            if kind == "msgs":
                return self._get_msgs_query(seat, query), None
            if kind == "dms":
                return self._get_dms_query(seat, query), None
            if kind not in GET_SIMPLE_KINDS:
                raise Reject(404, f"no GET /{kind}")
            if query:
                raise Reject(400, f"GET /{kind} takes no query parameters")
            if kind == "rules":
                return self.get_rules(seat), None
            if kind == "task_state":
                return self.get_task_state(seat), None
            return self.get_team_state(seat), None
        if method == "POST":
            if query:
                raise Reject(400, f"POST /{kind} takes no query parameters")
            if kind not in POST_KINDS:
                raise Reject(404, f"no POST /{kind}")
            assert isinstance(body, dict)
            if kind == "contribute":
                return self.post_contribute(seat, body)
            if kind == "msg":
                return self.post_msg(seat, body), None
            return self.post_submit_choice(seat, body), None
        raise Reject(405, f"unsupported method {method}")

    def _snapshot(self) -> tuple:
        return (
            self.endowment_left, self.waste_units, self.pool_progress, self.goal_settled, self.round,
            self.submitted_choice, len(self.contributions), len(self.messages), len(self.dms),
            dict(self.msg_cursor), dict(self.dm_cursor), {s: dict(u) for s, u in self.used.items()},
        )

    def _restore(self, snap: tuple) -> None:
        (
            self.endowment_left, self.waste_units, self.pool_progress, self.goal_settled, self.round,
            self.submitted_choice, n_c, n_m, n_d, self.msg_cursor, self.dm_cursor, self.used,
        ) = snap
        del self.contributions[n_c:]
        del self.messages[n_m:]
        del self.dms[n_d:]

    def handle(
        self, method: str, raw_path: str, token: str | None, body: object, client: str, spoof_header: bool = False
    ) -> tuple[int, dict]:
        clean_path, _, query = raw_path.partition("?")
        raw_kind = clean_path.strip("/").replace("/", ".") or "root"
        kind, kind_ok = _clean_label(raw_kind, "invalid_path")
        method_clean, method_ok = _clean_label(method, "INVALID")
        with self.lock:
            if kind_ok and method_ok and kind == "health" and method_clean == "GET" and not query:
                return 200, {"ok": True}
            if kind_ok and kind == "admin.close":
                if client not in LOOPBACK or not method_ok or method_clean != "POST" or query or token is not None or spoof_header:
                    if self.seq >= MAX_REQUESTS:
                        return 403, {"error": "admin calls come from main only"}
                    return self._log(None, kind, method_clean, None, 403, {"error": "admin calls come from main only"})
                close_resp = {"closed": True, "events": self.seq + 1}
                entry = self._prepare_event(self.seq + 1, self.prev_hash, None, kind, method_clean, {}, 200, close_resp)
                self.closed = True
                self._write_prepared([entry])
                return 200, close_resp
            if self.seq >= MAX_REQUESTS:
                return 429, {"error": "world request cap reached"}
            safe_req = body if isinstance(body, dict) and _is_json_safe(body, MAX_REQ_DEPTH, MAX_REQ_NODES) else None
            seat = self.tokens.get(token or "")
            if seat is None:
                return self._log(None, kind, method_clean, None, 401, {"error": "send your SEAT_TOKEN as a Bearer token"})
            if spoof_header:
                return self._log(seat, kind, method_clean, None, 400, {"error": "spoofed identity header rejected"})
            if self.closed:
                return self._log(seat, kind, method_clean, safe_req, 409, {"error": "play is over"}, late=True)
            if not kind_ok or not method_ok:
                return self._log(seat, kind, method_clean, None, 400, {"error": "invalid request path or method"})
            if method_clean == "GET" and body is not None:
                return self._log(seat, kind, method_clean, None, 400, {"error": "GET takes no request body"})
            if method_clean == "POST" and safe_req is None:
                return self._log(seat, kind, method_clean, None, 400, {"error": "send a valid JSON object"})
            left = self.left(seat, kind)
            if left is not None and left <= 0:
                return self._log(seat, kind, method_clean, None, 429, {"error": f"no {kind} calls left", "budget_left": 0})
            return self._execute_seat_call(seat, kind, method_clean, safe_req, query)

    def _execute_seat_call(
        self, seat: str, kind: str, method: str, safe_req: dict | None, query: str
    ) -> tuple[int, dict]:
        snap = self._snapshot()
        try:
            result, script_update = self._dispatch(method, kind, seat, safe_req, query)
            if kind in self.budgets:
                self.used[seat][kind] = self.used[seat].get(kind, 0) + 1
                result = {**result, "budget_left": self.left(seat, kind)}
            first = self._prepare_event(self.seq + 1, self.prev_hash, seat, kind, method, safe_req, 200, result)
            entries = [first]
            if script_update is not None:
                entries.append(
                    self._prepare_event(self.seq + 2, first[1], None, "script.round", "STEP", None, 200, script_update)
                )
        except Reject as refusal:
            self._restore(snap)
            return self._log(seat, kind, method, safe_req, refusal.status, refusal.body)
        except Exception as error:
            self._restore(snap)
            return self._log(seat, kind, method, None, 500, {"error": f"world error: {type(error).__name__}"})
        self._write_prepared(entries)
        return 200, result

    def _prepare_event(
        self, seq: int, prev_hash: str, seat: str | None, kind: str, method: str,
        body: object, status: int, response: dict, late: bool = False,
    ) -> tuple[bytes, str]:
        if not isinstance(kind, str) or not _safe_str(kind) or len(kind) > MAX_LABEL_LEN:
            raise ValueError("unsafe event kind")
        if not isinstance(method, str) or not _safe_str(method) or len(method) > MAX_LABEL_LEN:
            raise ValueError("unsafe event method")
        if not isinstance(response, dict) or not _is_json_safe(response):
            raise ValueError("unsafe event response")
        req_val = (
            body
            if status not in DROP_REQ_STATUSES and isinstance(body, dict) and _is_json_safe(body, MAX_REQ_DEPTH, MAX_REQ_NODES)
            else None
        )
        payload: dict[str, object] = {
            "seq": seq, "t": round(time.time(), 6), "seat": seat, "kind": kind, "method": method,
            "status": status, "ok": status == 200, "request": req_val, "response": response, "prev_hash": prev_hash,
        }
        if late:
            payload["late"] = True
        digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        event = {**payload, "entry_hash": digest}
        return (canonical_json(event) + "\n").encode("utf-8"), digest

    def _write_prepared(self, entries: list[tuple[bytes, str]]) -> None:
        raw = b"".join(line for line, _ in entries)
        with self.events_path.open("ab") as handle:
            handle.write(raw)
            handle.flush()
        self.seq += len(entries)
        self.prev_hash = entries[-1][1]

    def _log(self, seat, kind, method, body, status, response, late=False) -> tuple[int, dict]:
        entry = self._prepare_event(self.seq + 1, self.prev_hash, seat, kind, method, body, status, response, late)
        self._write_prepared([entry])
        return status, response


class BoundedWorldServer(ThreadingHTTPServer):
    request_queue_size = LISTEN_BACKLOG
    daemon_threads = True


def make_handler(world: CrowdScaleWorld) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        timeout = SOCKET_TIMEOUT

        def _answer(self, method: str) -> None:
            body: object = None
            bad_json = False
            if method == "POST":
                raw_len = self.headers.get("Content-Length")
                try:
                    length = int(raw_len or "0")
                    if length < 0 or length > MAX_BODY:
                        raise ValueError("invalid content length")
                    raw = self.rfile.read(length) if length > 0 else b""
                    body = parse_strict_json(raw) if raw else None
                except (ValueError, RecursionError, OSError):
                    body = None
                    bad_json = True
            auth = self.headers.get("Authorization") or ""
            token = auth[7:].strip() if auth[:7].lower() == "bearer " else None
            spoof_hdr = any(h.lower() in SPOOF_HEADERS for h in self.headers)
            req_body = None if bad_json else body
            status, response = world.handle(method, self.path, token, req_body, self.client_address[0], spoof_hdr)
            data = canonical_json(response).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            with contextlib.suppress(OSError):
                self.wfile.write(data)

        def do_GET(self) -> None:
            self._answer("GET")

        def do_POST(self) -> None:
            self._answer("POST")

        def log_message(self, *args: object) -> None:
            return None

    return Handler


def serve_world(world: CrowdScaleWorld, port: int = PORT) -> None:
    BoundedWorldServer(("::", port), make_handler(world)).serve_forever()


if __name__ == "__main__":
    serve_world(CrowdScaleWorld(json.loads(INSTANCE.read_text(encoding="utf-8"))))
