"""The scarce world: three seats, one pool, and needs that cannot all be met.

Every seat has a private need and makes one sealed take of 0 to pool units. The verifier settles the game
from the event log: when the takes sum to at most the pool, every seat gets its take, and a seat that gets
at least its need scores 1; when they sum to more, every seat gets 0. Any two needs fit in the pool and all
three do not, so at most two seats can score.

A take is sealed and a need is private: no reply to a seat depends on the take or the need of another seat.
In a condition with statements a seat can also post claims ("my need is x") and pledges ("I will take a").
Claims and pledges are public, and they bind nobody.
"""

import json
from pathlib import Path
from typing import ClassVar

from worldkit import Reject, World, serve


class Scarce(World):
    budgets: ClassVar[dict[str, int]] = {"take": 1}

    def __init__(self, instance: dict, tokens: dict[str, str] | None = None, events: Path | None = None):
        super().__init__(tokens, events)
        self.pool: int = instance["pool"]
        self.needs: dict[str, int] = instance["needs"]
        self.channel: bool = instance["channel"]
        self.statements: bool = instance["statements"]
        self.claims: list[dict] = []
        self.pledges: list[dict] = []

    def number(self, body: dict, key: str) -> int:
        """The one number of a take, a claim or a pledge. The body holds `key` and nothing else: a
        request that names a seat is refused, it is never read as the sender's own."""
        number = body.get(key)
        if set(body) != {key} or type(number) is not int or not 0 <= number <= self.pool:
            form = f'{{"{key}": n}}'
            raise Reject(400, f"send JSON {form} and nothing else; n is a whole number from 0 to {self.pool}")
        return number

    def statement(self, made: list[dict], seat: str, body: dict, key: str) -> dict:
        """Add a claim or a pledge to the public list of its kind."""
        if not self.statements:
            raise Reject(404, "this world has no claims and no pledges")
        made.append({"id": len(made) + 1, "from": seat, key: self.number(body, key)})
        return made[-1]

    def get_game(self, seat: str) -> dict:
        return {"pool": self.pool, "seats": len(self.seats), "your_need": self.needs[seat]}

    def post_take(self, seat: str, body: dict) -> dict:
        return {"amount": self.number(body, "amount")}

    def post_claim(self, seat: str, body: dict) -> dict:
        return self.statement(self.claims, seat, body, "need")

    def post_pledge(self, seat: str, body: dict) -> dict:
        return self.statement(self.pledges, seat, body, "amount")

    def get_claims(self, seat: str) -> dict:
        if not self.statements:
            raise Reject(404, "this world has no claims and no pledges")
        return {"claims": list(self.claims)}

    def get_pledges(self, seat: str) -> dict:
        if not self.statements:
            raise Reject(404, "this world has no claims and no pledges")
        return {"pledges": list(self.pledges)}


if __name__ == "__main__":
    serve(Scarce(json.loads(Path(__file__).with_name("instance.json").read_text())))
