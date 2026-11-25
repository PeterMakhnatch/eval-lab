import threading

import worldkit

FAULT = {"error": "no reply from the world; the request may have happened, may happen later, or may never happen"}
MODES = ("lost_reply", "dropped", "late")
MAX_STAGE = 512


def matches(fields, body):
    if not fields:
        return True
    if not isinstance(body, dict):
        return False
    return all(key in body and (body[key] in value if isinstance(value, list) else body[key] == value)
               for key, value in fields.items())


def validate(stage):
    real, units, rules = (stage[key] for key in ("real", "units", "rules"))
    assert isinstance(real, list) and real and all(isinstance(seat, str) for seat in real)
    assert len(set(real)) == len(real)
    assert isinstance(units, list) and isinstance(rules, list)
    assert len(units) <= MAX_STAGE and len(rules) <= MAX_STAGE
    assert all(isinstance(unit["id"], str) and isinstance(unit["seat"], str) for unit in units)
    ids = {unit["id"] for unit in units}
    assert len(ids) == len(units)
    assert all(isinstance(rule["id"], str) for rule in rules)
    assert len({rule["id"] for rule in rules}) == len(rules)
    for rule in rules:
        when = rule["when"]
        assert when in ("start", "before", "after", "fault")
        if when != "start":
            assert rule["seat"] in real and isinstance(rule["kinds"], list) and rule["kinds"]
            assert all(isinstance(kind, str) for kind in rule["kinds"])
            assert isinstance(rule["match"], dict)
        if when == "fault":
            assert rule["mode"] in MODES and isinstance(rule["settle"], dict)
        else:
            assert isinstance(rule["units"], list) and set(rule["units"]) <= ids
        if when == "after":
            assert rule["status"] is None or type(rule["status"]) is int


class StagedWorld(worldkit.World):
    def __init__(self, stage, tokens=None, events=None):
        validate(stage)
        super().__init__(tokens, events)
        self.lock = threading.RLock()
        self.real = set(stage["real"])
        assert self.real <= set(self.tokens.values())
        self.units = {unit["id"]: unit for unit in stage["units"]}
        self.rules = stage["rules"]
        self.seats = sorted(set(self.seats) | {unit["seat"] for unit in self.units.values()})
        self.used = {seat: {} for seat in self.seats}
        self.fired = set()
        self.ran = set()
        self.pending = []
        for rule in self.rules:
            if rule["when"] == "start":
                self.trigger(rule)

    def run_unit(self, unit):
        raise NotImplementedError

    def settle(self, pending):
        raise NotImplementedError

    def would_apply(self, seat, kind, body):
        return True

    def played(self, seat, kind, body, response, status=200):
        assert seat in self.seats
        if status == 200 and kind in self.budgets:
            assert self.left(seat, kind) > 0
            self.used[seat][kind] = self.used[seat].get(kind, 0) + 1
            response = {**response, "budget_left": self.left(seat, kind)}
        return self._log(seat, kind, "POST", body, status, {**response, "played": True})[1]

    def note(self, data):
        self._log(None, "stage", "NOTE", data, 200, {})

    def run_once(self, unit_id):
        if unit_id not in self.ran:
            self.ran.add(unit_id)
            self.run_unit(self.units[unit_id])

    def trigger(self, rule):
        assert rule["id"] not in self.fired
        self.fired.add(rule["id"])
        self.note({"rule": rule["id"], "units": rule["units"]})
        for unit_id in rule["units"]:
            self.run_once(unit_id)

    def finish(self):
        for pending in self.pending:
            self.settle(pending)
        self.pending.clear()
        for unit_id in self.units:
            self.run_once(unit_id)

    def applicable(self, when, seat, kind, body):
        return [rule for rule in self.rules if rule["when"] == when
                and rule["id"] not in self.fired and rule["seat"] == seat
                and kind in rule["kinds"] and matches(rule["match"], body)]

    def settle_matching(self, seat, kind, body):
        remaining = []
        for pending in self.pending:
            rule = pending["rule"]
            if seat == pending["seat"] and kind in rule["kinds"] and matches(rule["settle"], body):
                self.settle(pending)
            else:
                remaining.append(pending)
        self.pending = remaining

    def answer(self, rule, seat, kind, method, path, token, body, client):
        if rule is None:
            return super().handle(method, path, token, body, client)
        mode = rule["mode"]
        if mode == "lost_reply":
            status, response = super().handle(method, path, token, body, client)
            if status != 200:
                return status, response
        else:
            self._log(seat, kind, method, body, 504, FAULT)
            if mode == "late":
                self.pending.append({"seat": seat, "kind": kind, "method": method,
                                     "body": body, "rule": rule})
        self.fired.add(rule["id"])
        self.note({"rule": rule["id"], "mode": mode, "seq": self.seq})
        return 504, dict(FAULT)

    def handle(self, method, path, token, body, client):
        kind = path.strip("/").replace("/", ".") or "root"
        with self.lock:
            if not self.closed and kind == "admin.close" and method == "POST" and client in worldkit.LOOPBACK:
                self.finish()
            seat = self.tokens.get(token or "")
            if self.closed or seat not in self.real or kind in ("health", "admin.close"):
                return super().handle(method, path, token, body, client)
            rule = None
            if method == "POST":
                if self.pending and self.would_apply(seat, kind, body):
                    self.settle_matching(seat, kind, body)
                before = self.applicable("before", seat, kind, body)
                if before and self.would_apply(seat, kind, body):
                    self.trigger(before[0])
                fault = self.applicable("fault", seat, kind, body)
                if fault and self.would_apply(seat, kind, body):
                    rule = fault[0]
            status, response = self.answer(rule, seat, kind, method, path, token, body, client)
            for after in self.applicable("after", seat, kind, body):
                if after["status"] is None or after["status"] == status:
                    self.trigger(after)
            return status, response
