import json
from pathlib import Path

from stagekit import StagedWorld
from worldkit import Reject, serve

INSTANCE = Path(__file__).with_name("instance.json")


def legs(ticket):
    return [ticket["item"], ticket["to"]] if ticket["kind"] == "move" else [ticket["item"]]


def delta(ticket, item):
    match ticket["kind"]:
        case "adjust":
            return ticket["delta"]
        case "move":
            return -ticket["n"] if item == ticket["item"] else ticket["n"]
        case "draw":
            return -ticket["take"]
    raise AssertionError(ticket)


class Depot(StagedWorld):
    def __init__(self, instance, tokens=None, events=None):
        self.instance = instance
        self.channel = instance["channel"]
        self.guard = instance["guard"]
        self.budgets = {"write": instance["write_budget"]}
        self.book = {item: {"qty": qty, "version": 1} for item, qty in instance["items"].items()}
        self.writes = []
        self.declined = []
        self.tickets = {ticket["id"]: ticket for ticket in instance["tickets"]}
        self.queues = {seat: {ticket["id"]: ticket for ticket in queue}
                       for seat, queue in instance["queues"].items()}
        for unit in instance["stage"]["units"]:
            ticket = self.tickets[unit["ticket"]]
            self.queues.setdefault(unit["seat"], {})[ticket["id"]] = ticket
        self.pairs = {item: pair for pair in instance["pairs"] for item in pair["items"]}
        super().__init__(instance["stage"], tokens, events)

    def left(self, seat, kind):
        if kind != "write":
            return None
        return self.budgets[kind][seat] - self.used[seat].get(kind, 0)

    def get_rules(self, seat):
        return {**super().get_rules(seat), "pairs": self.instance["pairs"], "guard": self.guard,
                "clerks": self.instance["clerks"], "left": self.instance["left"]}

    def get_book(self, seat):
        return {"items": self.current(self.book)}

    def get_tickets(self, seat):
        return {"tickets": list(self.queues[seat].values())}

    def get_log(self, seat):
        return {"writes": list(self.writes), "declined": list(self.declined)}

    def ticket(self, seat, body):
        tid = body.get("ticket")
        if not isinstance(tid, str) or tid not in self.queues.get(seat, {}):
            raise Reject(400, "ticket not in your queue")
        return self.queues[seat][tid]

    def written(self, tid, item):
        return any(row["ticket"] == tid and row["item"] == item for row in self.writes)

    def current(self, items):
        return {item: dict(self.book[item]) for item in items}

    def permits(self, item, qty):
        pair = self.pairs[item]
        return qty + sum(self.book[key]["qty"] for key in pair["items"] if key != item) >= pair["floor"]


    def validate_action(self, seat, kind, body):
        if not isinstance(body, dict):
            raise Reject(400, "send a JSON object")
        if kind == "write":
            if self.left(seat, kind) <= 0:
                raise Reject(429, "no write calls left")
            return self.validate_write(seat, body)
        assert kind == "decline"
        ticket = self.ticket(seat, body)
        if ticket["kind"] != "draw":
            raise Reject(400, "only draw tickets can be declined")
        return ticket

    def would_apply(self, seat, kind, body):
        if kind not in ("write", "decline"):
            return True
        try:
            self.validate_action(seat, kind, body)
        except Reject:
            return False
        return True

    def answer(self, rule, seat, kind, method, path, token, body, client):
        status, response = super().answer(rule, seat, kind, method, path, token, body, client)
        if status == 504 and rule is not None and rule["mode"] != "lost_reply":
            assert kind == "write" and self.left(seat, kind) > 0
            self.used[seat][kind] = self.used[seat].get(kind, 0) + 1
        return status, response

    def validate_write(self, seat, body):
        ticket = self.ticket(seat, body)
        item, qty, expect = body.get("item"), body.get("qty"), body.get("expect", {})
        if not isinstance(item, str) or item not in legs(ticket) or type(qty) is not int or qty < 0:
            raise Reject(400, "invalid item or quantity")
        if not isinstance(expect, dict) or ("expect" in body and not 1 <= len(expect) <= 8) or any(
            key not in self.book or type(version) is not int or version < 1
            for key, version in expect.items()
        ):
            raise Reject(400, "invalid expect map")
        if self.guard and self.written(ticket["id"], item):
            raise Reject(409, "ticket already written for this item")
        if self.guard and item not in expect:
            raise Reject(428, "expect required for written item")
        if any(self.book[key]["version"] != version for key, version in expect.items()):
            raise Reject(409, "stale", current=self.current(expect))
        if self.guard and ticket["kind"] == "draw" and not self.permits(item, qty):
            raise Reject(409, "pair rule", current=self.current(self.pairs[item]["items"]))
        return ticket

    def post_write(self, seat, body):
        ticket = self.validate_action(seat, "write", body)
        return self.apply_write(seat, body, ticket)

    def apply_write(self, seat, body, ticket):
        item = body["item"]
        entry = {"qty": body["qty"], "version": self.book[item]["version"] + 1}
        self.book[item] = entry
        row = {"n": len(self.writes) + 1, "seat": seat, "ticket": ticket["id"], "item": item, **entry}
        self.writes.append(row)
        return dict(row)

    def post_decline(self, seat, body):
        ticket = self.validate_action(seat, "decline", body)
        row = {"seat": seat, "ticket": ticket["id"]}
        self.declined.append(row)
        return dict(row)

    def settle(self, pending):
        seat, body = pending["seat"], pending["body"]
        try:
            if pending["method"] != "POST" or pending["kind"] != "write":
                raise Reject(400, "not a write request")
            ticket = self.validate_write(seat, body)
            result = self.apply_write(seat, body, ticket)
        except Reject as refusal:
            self.note({"rule": pending["rule"]["id"], "lapsed": True, "error": refusal.body})
        else:
            self._log(seat, "write", "POST", body, 200,
                      {**result, "settled": True, "played": True,
                       "budget_left": self.left(seat, "write")})

    def run_unit(self, unit):
        seat, ticket = unit["seat"], self.tickets[unit["ticket"]]
        for item in [unit["item"]] if "item" in unit else legs(ticket):
            if self.written(ticket["id"], item):
                continue
            if any(row["ticket"] == ticket["id"] for row in self.declined):
                continue
            qty = self.book[item]["qty"] + delta(ticket, item)
            if ticket["kind"] == "draw" and not self.permits(item, qty):
                body = {"ticket": ticket["id"]}
                self.played(seat, "decline", body, self.post_decline(seat, body))
                continue
            keys = self.pairs[item]["items"] if ticket["kind"] == "draw" else [item]
            body = {"ticket": ticket["id"], "item": item, "qty": qty,
                    "expect": {key: self.book[key]["version"] for key in keys}}
            try:
                response = self.post_write(seat, body)
            except Reject as refusal:
                self.played(seat, "write", body, refusal.body, status=refusal.status)
            else:
                self.played(seat, "write", body, response)


if __name__ == "__main__":
    serve(Depot(json.loads(INSTANCE.read_text())))
