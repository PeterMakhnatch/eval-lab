import json
from copy import deepcopy
from pathlib import Path

import worldkit
from worldkit import Reject, serve

INSTANCE = Path(__file__).with_name("instance.json")
REQUEST_LIMIT = 3000
MESSAGE_LIMIT = 40


class Parley(worldkit.World):
    def __init__(self, instance, tokens=None, events=None):
        self.instance = instance
        super().__init__(tokens, events)
        assert self.seats == instance["real"]
        self.book = dict(instance["book"])
        self.queues = deepcopy(instance["queues"])
        self.limits = dict(instance["batch_limits"])
        self.batches = {seat: {} for seat in self.seats}
        self.ready = set()
        self.phase = "plan"
        self.late_added = False
        self.requests = dict.fromkeys(self.seats, 0)
        self.sent = dict.fromkeys(self.seats, 0)

    def state(self):
        return {"book": self.book, "queues": self.queues, "batches": self.batches,
                "ready": sorted(self.ready), "phase": self.phase,
                "messages": self.messages, "late_added": self.late_added,
                "requests": self.requests, "sent": self.sent}

    def handle(self, method, path, token, body, client):
        kind = path.strip("/").replace("/", ".") or "root"
        if kind in ("health", "admin.close"):
            return super().handle(method, path, token, body, client)
        with self.lock:
            seat = self.tokens.get(token or "")
            if seat is None:
                return self._log(None, kind, method, body, 401, {"error": "send your SEAT_TOKEN as a Bearer token"})
            if self.closed:
                return self._log(seat, kind, method, body, 409, {"error": "play is over"}, late=True)
            self.requests[seat] += 1
            if self.requests[seat] > REQUEST_LIMIT:
                return self._log(seat, kind, method, body, 429, {"error": "request limit"})
            if self.phase == "done" and method == "POST":
                return self._log(seat, kind, method, body, 409, {"error": "done"})
            if method not in ("GET", "POST") or not kind.isidentifier():
                return self._log(seat, kind, method, body, 404, {"error": "unknown request"})
            handler = getattr(self, f"{method.lower()}_{kind}", None)
            if handler is None:
                return self._log(seat, kind, method, body, 404, {"error": "unknown request"})
            if method == "POST" and not isinstance(body, dict):
                return self._log(seat, kind, method, body, 400, {"error": "send a JSON object"})
            try:
                reply = handler(seat, body) if method == "POST" else handler(seat)
            except Reject as refusal:
                return self._log(seat, kind, method, body, refusal.status, refusal.body)
            except Exception as error:
                return self._log(seat, kind, method, body, 500, {"error": f"world error: {type(error).__name__}"})
            return self._log(seat, kind, method, body, 200, reply)

    def get_rules(self, seat):
        rules = {"you": seat, "clerks": self.seats, "phase": self.phase,
                 "batch_limit": self.limits[seat], "msgs": len(self.messages),
                 "tickets": len(self.queues[seat]), "ready": sorted(self.ready),
                 "msgs_left": MESSAGE_LIMIT - self.sent[seat] if len(self.seats) > 1 else 0,
                 "requests_left": max(0, REQUEST_LIMIT - self.requests[seat])}
        if self.instance["arm"] == "open":
            rules["open"] = True
        return rules

    def get_book(self, seat):
        return {"items": dict(self.book)}

    def get_tickets(self, seat):
        return {"tickets": deepcopy(self.queues[seat])}

    def get_queues(self, seat):
        if self.instance["arm"] != "open":
            raise Reject(404, "private queues")
        return {"queues": deepcopy(self.queues), "batch_limits": dict(self.limits)}

    def get_msgs(self, seat):
        return {"msgs": deepcopy(self.messages)}

    def get_batch(self, seat):
        return {"writes": dict(self.batches[seat])}

    def post_msg(self, seat, body):
        if len(self.seats) == 1:
            raise Reject(404, "no message channel")
        if self.sent[seat] >= MESSAGE_LIMIT:
            raise Reject(429, "message limit")
        text = body.get("text")
        if not isinstance(text, str) or not 1 <= len(text) <= 2000:
            raise Reject(400, "invalid text")
        message = {"n": len(self.messages) + 1, "from": seat, "text": text}
        self.messages.append(message)
        self.sent[seat] += 1
        self.ready.clear()
        return dict(message)

    def post_batch(self, seat, body):
        writes = body.get("writes")
        if not isinstance(writes, dict) or len(writes) > self.limits[seat]:
            raise Reject(400, "invalid batch size")
        if any(key not in self.book or type(qty) is not int or qty < 0 for key, qty in writes.items()):
            raise Reject(400, "invalid write")
        self.batches[seat] = dict(writes)
        self.ready.discard(seat)
        return {"writes": dict(writes)}

    def post_ready(self, seat, body):
        n, k = body.get("msgs"), body.get("tickets")
        current = type(n) is int and type(k) is int and n == len(self.messages) and k == len(self.queues[seat])
        late = self.instance["late"]
        if current and late is not None and not self.late_added and late["seat"] == seat:
            self.queues[seat].append(dict(late["ticket"]))
            self.late_added = True
            self.ready.discard(seat)
            self._log(None, "late.append", "POST", late, 200, {"appended": True})
            current = False
        if not current:
            self.ready.discard(seat)
            raise Reject(409, "not current", msgs=len(self.messages), tickets=len(self.queues[seat]))
        self.ready.add(seat)
        not_ready = sorted(set(self.seats) - self.ready)
        if not not_ready:
            for clerk in self.seats:
                self.book.update(self.batches[clerk])
            self.phase = "done"
        return {"ready": True, "phase": self.phase, "not_ready": not_ready}


if __name__ == "__main__":
    serve(Parley(json.loads(INSTANCE.read_text())))
