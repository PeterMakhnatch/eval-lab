"""Retain released GEPA LM responses across target-approval interruptions.

This is a response journal, not a search algorithm. The pinned GEPA LM and
reflection strategy still generate candidates. Missing billing stays unknown.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class ProposalUnavailable(BaseException):
    """A spent/unknown request must not become an automatic paid retry."""


class JournaledReflectionLM:
    def __init__(self, *, model: str, directory: Path, max_requests: int) -> None:
        self.model = model
        self.directory = directory
        self.max_requests = max_requests
        directory.mkdir(parents=True, exist_ok=True)
        self.replayed = 0
        self.new_requests = 0
        self._lm = None

    def _receipts(self) -> list[dict[str, Any]]:
        receipts = []
        for path in sorted(self.directory.glob("*.json")):
            if path.is_symlink():
                raise ValueError("Proposer receipts must not be symlinks")
            receipts.append(json.loads(path.read_text()))
        return receipts

    @property
    def total_cost(self) -> float:
        # GEPA expects a numeric meter. This is its own catalog-cost estimate,
        # never a claim about provider billing or complete accounting.
        return sum(row.get("upstream_estimated_cost_usd") or 0.0 for row in self._receipts())

    def __call__(self, prompt: str | list[dict[str, Any]]) -> str:
        identity = {"model": self.model, "prompt": prompt}
        key = hashlib.sha256(
            json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        path = self.directory / f"{key}.json"
        if path.is_symlink():
            raise ValueError("Proposer receipt must not be a symlink")
        if path.exists():
            receipt = json.loads(path.read_text())
            if receipt.get("identity") != identity:
                raise ValueError("Proposer receipt identity mismatch")
            if receipt["status"] != "completed":
                raise ProposalUnavailable(
                    "Previous proposer request has unknown outcome; no automatic retry"
                )
            self.replayed += 1
            return receipt["response"]
        if len(self._receipts()) >= self.max_requests:
            raise ProposalUnavailable("Campaign proposer request ceiling reached")
        from gepa.lm import LM  # ty: ignore[unresolved-import]

        if self._lm is None:
            self._lm = LM(self.model, max_tokens=4096, num_retries=0, timeout=60)
        receipt = {
            "identity": identity,
            "status": "sent_remote_outcome_unknown",
            "actual_cost_usd": None,
            "actual_usage": None,
            "accounting_limit": "upstream LM converts missing cost/usage to zero; these are not authoritative billing receipts",
        }
        with path.open("x") as stream:
            json.dump(receipt, stream, indent=2)
        self.new_requests += 1
        before = self._lm.total_cost
        response = self._lm(prompt)
        if not isinstance(response, str):
            raise ProposalUnavailable("Proposer returned no text; remote outcome remains retained")
        receipt.update(
            status="completed",
            response=response,
            upstream_estimated_cost_usd=self._lm.total_cost - before,
        )
        path.write_text(json.dumps(receipt, indent=2) + "\n")
        return response
