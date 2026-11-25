"""World kit: the shared part of every muster world (docs/DESIGN.md, section 4).

A family's world.py subclasses `World`, adds handlers, and calls `serve(MyWorld())`.
The kit gives token identity, the event log, per-seat budgets, the message channel and the close switch.
The generator copies this file next to world.py, so a task directory imports nothing from outside.

Handlers are methods named `post_<kind>(self, seat, body) -> dict` or `get_<kind>(self, seat) -> dict`.
They run under the world lock. They raise `Reject(status, error)` to refuse a request.
"""

import json
import math
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer as _StockServer
from pathlib import Path
from typing import ClassVar

EVENTS = Path(os.environ.get("WORLD_EVENTS", "/logs/world/events.jsonl"))
REWARD = Path(os.environ.get("WORLD_REWARD", "/logs/verifier/reward.json"))
PORT = 8080
MAX_BODY = 64 * 1024
LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


class ThreadingHTTPServer(_StockServer):
    """The kit's server. It listens on IPv6 and IPv4: the seat networks of the kit are IPv6 only, and
    loopback can be either. `serve` makes its server through this name, so a test can put its own here."""

    address_family = socket.AF_INET6

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


class Reject(Exception):
    """A handler refuses the request. The event is logged with ok = false."""

    def __init__(self, status: int, error: str, **extra: object):
        super().__init__(error)
        self.status = status
        self.body = {"error": error, **extra}


class World:
    """State and rules of one world. A subclass sets `channel` and `budgets` and adds handlers."""

    channel = False  # True: POST /msg and GET /msgs exist
    budgets: ClassVar[dict[str, int]] = {}  # kind -> successful calls allowed per seat

    def __init__(self, tokens: dict[str, str] | None = None, events: Path | None = None):
        self.tokens = dict(tokens if tokens is not None else json.loads(os.environ["SEAT_TOKENS"]))
        self.seats = sorted(set(self.tokens.values()))
        self.events_path = events if events is not None else EVENTS
        self.lock = threading.Lock()
        self.seq = 0
        self.closed = False
        self.used: dict[str, dict[str, int]] = {seat: {} for seat in self.seats}
        self.messages: list[dict] = []
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self.events_path.write_text("")

    # -- rules every world has -------------------------------------------------------------

    def left(self, seat: str, kind: str) -> int | None:
        """Calls of this kind the seat can still make. None means no limit."""
        if kind not in self.budgets:
            return None
        return self.budgets[kind] - self.used[seat].get(kind, 0)

    def get_rules(self, seat: str) -> dict:
        return {
            "you": seat,
            "seats": self.seats,
            "channel": self.channel,
            "budget_left": {kind: self.left(seat, kind) for kind in sorted(self.budgets)},
        }

    def post_msg(self, seat: str, body: dict) -> dict:
        if not self.channel:
            raise Reject(404, "this world has no message channel")
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise Reject(400, 'send JSON {"text": "..."} with 1 to 2000 characters')
        message = {"id": len(self.messages) + 1, "from": seat, "text": text}
        self.messages.append(message)
        return message

    def get_msgs(self, seat: str) -> dict:
        if not self.channel:
            raise Reject(404, "this world has no message channel")
        return {"messages": list(self.messages)}

    # -- request handling ------------------------------------------------------------------

    def handle(
        self, method: str, path: str, token: str | None, body: object, client: str
    ) -> tuple[int, dict]:
        """Answer one request and log it as one event. Returns (status, response body)."""
        kind = path.strip("/").replace("/", ".") or "root"
        with self.lock:
            if kind == "health":
                return 200, {"ok": True}  # not an event: Compose polls it
            if kind == "admin.close":
                if client not in LOOPBACK or method != "POST":
                    return self._log(
                        None, kind, method, body, 403, {"error": "admin calls come from main only"}
                    )
                self.closed = True
                return self._log(None, kind, method, body, 200, {"closed": True, "events": self.seq + 1})
            seat = self.tokens.get(token or "")
            if seat is None:
                return self._log(
                    None, kind, method, body, 401, {"error": "send your SEAT_TOKEN as a Bearer token"}
                )
            if self.closed:
                return self._log(seat, kind, method, body, 409, {"error": "play is over"}, late=True)
            handler = getattr(self, f"{method.lower()}_{kind}", None) if kind.isidentifier() else None
            if handler is None:
                return self._log(seat, kind, method, body, 404, {"error": f"no {method} /{kind}"})
            if method == "POST" and not isinstance(body, dict):
                return self._log(seat, kind, method, body, 400, {"error": "send a JSON object"})
            left = self.left(seat, kind)
            if left is not None and left <= 0:
                return self._log(
                    seat, kind, method, body, 429, {"error": f"no {kind} calls left", "budget_left": 0}
                )
            try:
                result = handler(seat, body) if method == "POST" else handler(seat)
            except Reject as refusal:
                return self._log(seat, kind, method, body, refusal.status, refusal.body)
            except Exception as error:  # a bug in a family handler must not leave a gap in the log
                return self._log(
                    seat, kind, method, body, 500, {"error": f"world error: {type(error).__name__}"}
                )
            if kind in self.budgets:
                self.used[seat][kind] = self.used[seat].get(kind, 0) + 1
                result = {**result, "budget_left": self.left(seat, kind)}
            return self._log(seat, kind, method, body, 200, result)

    def _log(self, seat, kind, method, body, status, response, late=False) -> tuple[int, dict]:
        self.seq += 1
        event = {
            "seq": self.seq,
            "t": round(time.time(), 6),
            "seat": seat,
            "kind": kind,
            "method": method,
            "status": status,
            "ok": status == 200,
            "request": body if isinstance(body, dict) else None,
            "response": response,
        }
        if late:
            event["late"] = True
        with self.events_path.open("a") as log:
            log.write(json.dumps(event, sort_keys=True) + "\n")
        return status, response


def serve(world: World, port: int = PORT) -> None:
    """Run the world's HTTP server until the container stops."""

    class Handler(BaseHTTPRequestHandler):
        def _answer(self, method: str) -> None:
            body: object = None
            if method == "POST":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(min(length, MAX_BODY)) or b"null")
                except ValueError:
                    body = None
            auth = self.headers.get("Authorization") or ""
            token = auth[7:].strip() if auth.startswith("Bearer ") else None
            status, response = world.handle(
                method, self.path.split("?")[0], token, body, self.client_address[0]
            )
            data = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self._answer("GET")

        def do_POST(self) -> None:
            self._answer("POST")

        def log_message(self, *args: object) -> None:
            pass

    ThreadingHTTPServer(("::", port), Handler).serve_forever()


# -- helpers for verifiers (they run in main, after play) ----------------------------------


def close_world(port: int = PORT) -> None:
    """Stop all changes. The verifier calls this first, then grades from the event log."""
    import urllib.request

    request = urllib.request.Request(f"http://127.0.0.1:{port}/admin/close", data=b"{}", method="POST")
    with urllib.request.urlopen(request, timeout=10) as reply:
        reply.read()


def load_events(path: Path | None = None) -> list[dict]:
    """Events in order, up to the close. Raises ValueError when the log has a gap or is out of order."""
    events = []
    for line in (path or EVENTS).read_text().splitlines():
        if line.strip():
            events.append(json.loads(line))
    for index, event in enumerate(events, start=1):
        if event.get("seq") != index:
            raise ValueError(f"event log is not continuous at line {index}")
    return events


def in_play(events: list[dict]) -> list[dict]:
    """Successful seat events before the close: the only events a verifier gives credit for."""
    played = []
    for event in events:
        if event["kind"] == "admin.close" and event["ok"]:
            break
        if event["ok"] and event["seat"] is not None:
            played.append(event)
    return played


def write_rewards(rewards: dict[str, float], path: Path | None = None, events: Path | None = None) -> None:
    """Write reward.json. Every value must be a finite number.

    The graded event log goes next to it as events.jsonl. Harbor collects the task's artifacts before the
    verifier runs, so that copy of the log can miss the last events. This copy is the log that was graded,
    and `muster regrade` recomputes the reward from it.
    """
    clean = {}
    for key, value in rewards.items():
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"reward {key!r} is not a finite number")
        clean[key] = round(number, 6)
    target = path or REWARD
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(clean, sort_keys=True) + "\n")
    log = events or EVENTS
    if log.is_file():
        (target.parent / "events.jsonl").write_text(log.read_text())
