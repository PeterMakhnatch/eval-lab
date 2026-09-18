"""Retain released GEPA LM responses across target-approval interruptions.

This is a response journal, not a search algorithm. The pinned GEPA LM and
reflection strategy still generate candidates. Missing billing stays unknown.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .budget import AggregateBudget

if TYPE_CHECKING:
    from .opencode_transport import OpenCodeTransport


class ProposalUnavailable(BaseException):
    """A spent/unknown request must not become an automatic paid retry."""


def direct_proposer_blocker(model: str) -> str | None:
    """The existing Z.ai binding is a Coding Plan credential, not a general API grant."""
    if model.startswith(("zai/", "zai-coding-plan/")):
        return (
            "Direct GEPA/LiteLLM use of the Z.ai Coding Plan is not a qualified proposer route. "
            "The plan is restricted to supported tools: https://docs.z.ai/devpack/tool/others . "
            "Use a separately approved API route or qualify an actual supported-tool transport; "
            "do not spoof a tool identity or substitute a provider."
        )
    return None


class _FeedbackOnlyServer:
    """Keep live enforcement while excluding transient telemetry from reflection."""

    def __init__(self, server: Any) -> None:
        self.server = server

    def __getattr__(self, name: str) -> Any:
        return getattr(self.server, name)

    def evaluate(self, *args: Any, **kwargs: Any) -> Any:
        score, info = self.server.evaluate(*args, **kwargs)
        info.pop("_budget", None)
        return score, info

    def evaluate_batch(self, *args: Any, **kwargs: Any) -> Any:
        results = self.server.evaluate_batch(*args, **kwargs)
        for _score, info in results:
            info.pop("_budget", None)
        return results


class ReplaySafeGepaEngine:
    """Delegate released GEPA without making per-process counters prompt identity."""

    name = "gepa"

    def __init__(self, config: Any) -> None:
        from gepa.oa.engines.gepa import GepaEngine  # ty: ignore[unresolved-import]

        self.engine: Any = GepaEngine(config)

    def run(self, task: Any, server: Any) -> Any:
        # EvalServer adds _budget after our evaluator returns. Its used/remaining
        # counters restart on resume, otherwise changing an identical reflection
        # request into a new paid proposal. The original server still meters it.
        return self.engine.run(task, _FeedbackOnlyServer(server))

    def process_result(self, result: Any, output_dir: Path | None) -> None:
        self.engine.process_result(result, output_dir)


class JournaledReflectionLM:
    def __init__(
        self,
        *,
        model: str,
        directory: Path,
        max_requests: int,
        before_request: Callable[[], None] | None = None,
        budgets: tuple[AggregateBudget, ...] = (),
        transport: OpenCodeTransport | None = None,
    ) -> None:
        self.model = model
        self.directory = directory
        self.max_requests = max_requests
        self.before_request = before_request
        self.budgets = budgets
        self.transport = transport
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
        if self.before_request is not None:
            self.before_request()
        identity: dict[str, Any] = {"model": self.model, "prompt": prompt}
        if self.transport is not None:
            facts = self.transport.preflight()
            if facts["model"] != self.model:
                raise ProposalUnavailable("OpenCode transport model differs from campaign")
            identity["transport"] = facts
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
            self._settle_budget(path, receipt)
            self.replayed += 1
            return receipt["response"]
        receipts = self._receipts()
        if any(row.get("status") != "completed" for row in receipts):
            raise ProposalUnavailable("Previous proposer outcome is unknown; no new request")
        if len(receipts) >= self.max_requests:
            raise ProposalUnavailable("Campaign proposer request ceiling reached")
        if self.transport is None:
            blocker = direct_proposer_blocker(self.model)
            if blocker:
                raise ProposalUnavailable(blocker)
            from gepa.lm import LM  # ty: ignore[unresolved-import]

            if self._lm is None:
                self._lm = LM(self.model, max_tokens=4096, num_retries=0, timeout=60)
        receipt = {
            "identity": identity,
            "status": "sent_remote_outcome_unknown",
            "actual_cost_usd": None,
            "actual_usage": None,
            "accounting_limit": (
                "Broker physical usage and API-price estimate; actual subscription billing unknown"
                if self.transport is not None
                else "upstream LM converts missing cost/usage to zero; these are not authoritative billing receipts"
            ),
        }
        for budget in self.budgets:
            budget.reserve("proposer", str(path), metadata={"model": self.model})
        with path.open("x") as stream:
            json.dump(receipt, stream, indent=2)
        self.new_requests += 1
        if self.transport is not None:
            try:
                result = self.transport.request(prompt, directory=self.directory / key)
            except Exception as exc:
                raise ProposalUnavailable(
                    "OpenCode proposal failed; retained outcome must be inspected before continuing"
                ) from exc
            response = result["response"]
            receipt.update(
                status="completed",
                response=response,
                upstream_estimated_cost_usd=result["upstream_estimated_cost_usd"],
                actual_usage=result["usage"],
                transport=result["transport"],
            )
        else:
            assert self._lm is not None
            before = self._lm.total_cost
            response = self._lm(prompt)
            if not isinstance(response, str):
                raise ProposalUnavailable(
                    "Proposer returned no text; remote outcome remains retained"
                )
            receipt.update(
                status="completed",
                response=response,
                upstream_estimated_cost_usd=self._lm.total_cost - before,
            )
        path.write_text(json.dumps(receipt, indent=2) + "\n")
        self._settle_budget(path, receipt)
        return response

    def _settle_budget(self, path: Path, receipt: dict[str, Any]) -> None:
        # The upstream LM collapses missing estimates to zero; do not attest
        # those as known free requests. Replaying settlement is idempotent.
        estimate = receipt.get("upstream_estimated_cost_usd")
        known_estimate = estimate if isinstance(estimate, (int, float)) and estimate > 0 else None
        for budget in self.budgets:
            budget.complete(
                "proposer",
                str(path),
                status="completed",
                estimated_cost_usd=known_estimate,
                metadata={"receipt_path": str(path)},
            )
