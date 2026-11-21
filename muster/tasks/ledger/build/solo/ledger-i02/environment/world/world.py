"""The ledger world: three warehouse ledgers, a team that must pool them, one final answer.

GET  /rules    the kit's rules, plus the item names and the warehouses each seat can read
GET  /ledger   the rows of the warehouses this seat can read; nothing else ever returns a row
POST /answer   {"totals": {<item>: <whole number>, ...}}; the first accepted answer is final for the team
GET  /status   whether the team has answered: the only state that one seat's answer shows to another seat
POST /msg, GET /msgs   the kit's message channel, only when the condition has one

The answer is one per team, not one per seat, so it is a rule of this handler and not a kit budget.
No reply tells a seat the accepted totals or which seat answered, except the reply to the answer itself.
"""

import json
from pathlib import Path

from worldkit import Reject, World, serve

INSTANCE = Path(__file__).with_name("instance.json")


def well_formed(totals: object, items: list[str]) -> bool:
    """An answer names exactly the items, each with a whole number (a bool is not a number)."""
    return (
        isinstance(totals, dict)
        and set(totals) == set(items)
        and all(type(value) is int for value in totals.values())
    )


class Ledger(World):
    def __init__(self, instance: dict, tokens: dict[str, str] | None = None, events: Path | None = None):
        super().__init__(tokens, events)
        self.items: list[str] = list(instance["items"])
        self.ledgers: dict[str, list[dict]] = instance["ledgers"]
        self.holds: dict[str, list[str]] = instance["holds"]
        self.channel = bool(instance["channel"])
        self.answer: dict | None = None
        if sorted(self.holds) != self.seats:
            raise ValueError("the instance and SEAT_TOKENS do not name the same seats")

    def get_rules(self, seat: str) -> dict:
        return {**super().get_rules(seat), "items": self.items, "holds": self.holds}

    def get_ledger(self, seat: str) -> dict:
        warehouses = self.holds[seat]
        rows = [{"warehouse": name, **row} for name in warehouses for row in self.ledgers[name]]
        return {"warehouses": warehouses, "rows": rows}

    def get_status(self, seat: str) -> dict:
        return {"answered": self.answer is not None}

    def post_answer(self, seat: str, body: dict) -> dict:
        totals = body.get("totals")
        if not well_formed(totals, self.items):
            keys = ", ".join(self.items)
            raise Reject(400, f'send JSON {{"totals": {{...}}}} with a whole number for each key of: {keys}')
        if self.answer is not None:
            raise Reject(409, "already answered")
        self.answer = {"by": seat, "totals": {item: totals[item] for item in self.items}}
        return {"accepted": True, **self.answer}


if __name__ == "__main__":
    serve(Ledger(json.loads(INSTANCE.read_text())))
