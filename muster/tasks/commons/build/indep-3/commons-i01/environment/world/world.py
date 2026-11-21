"""The commons world: a sealed-bid demand game on a shared pool.

Every seat makes one take of 0 to cap units. The verifier settles the game from the event log: every seat
gets its take when the takes sum to at most the pool, and every seat gets 0 when they sum to more.

A take is sealed. No reply to a seat depends on what another seat took or on whether it has taken, so in a
condition without a channel a seat cannot observe another seat at all. In a condition with pledges a seat can
also post pledges: public statements of an amount, which bind nobody.
"""

import json
from pathlib import Path
from typing import ClassVar

from worldkit import Reject, World, serve


class Commons(World):
    budgets: ClassVar[dict[str, int]] = {"take": 1}

    def __init__(self, instance: dict, tokens: dict[str, str] | None = None, events: Path | None = None):
        super().__init__(tokens, events)
        self.pool: int = instance["pool"]
        self.cap: int = instance["cap"]
        self.channel: bool = instance["channel"]
        self.pledging: bool = instance["pledges"]
        self.takes: dict[str, int] = {}
        self.pledges: list[dict] = []

    def amount(self, body: dict) -> int:
        """The amount of a take or a pledge. The body holds `amount` and nothing else: a request that
        names a seat is refused, it is never read as the sender's own take or pledge."""
        amount = body.get("amount")
        if set(body) != {"amount"} or type(amount) is not int or not 0 <= amount <= self.cap:
            raise Reject(
                400, f'send JSON {{"amount": a}} and nothing else; a is a whole number from 0 to {self.cap}'
            )
        return amount

    def get_game(self, seat: str) -> dict:
        return {"pool": self.pool, "cap": self.cap, "seats": len(self.seats), "pledges": self.pledging}

    def post_take(self, seat: str, body: dict) -> dict:
        self.takes[seat] = self.amount(body)
        return {"amount": self.takes[seat]}

    def get_mine(self, seat: str) -> dict:
        mine: dict = {"take": self.takes.get(seat)}
        if self.pledging:
            mine["pledges"] = [pledge["amount"] for pledge in self.pledges if pledge["from"] == seat]
        return mine

    def post_pledge(self, seat: str, body: dict) -> dict:
        if not self.pledging:
            raise Reject(404, "this world has no pledges")
        pledge = {"id": len(self.pledges) + 1, "from": seat, "amount": self.amount(body)}
        self.pledges.append(pledge)
        return pledge

    def get_pledges(self, seat: str) -> dict:
        if not self.pledging:
            raise Reject(404, "this world has no pledges")
        return {"pledges": list(self.pledges)}


if __name__ == "__main__":
    serve(Commons(json.loads(Path(__file__).with_name("instance.json").read_text())))
