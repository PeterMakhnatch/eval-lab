"""Campaign ownership adapter for the disabled continuous operator.

This module connects operator kill/drain primitives to one frozen campaign.  It
never submits, approves, resumes, or dispatches campaign work.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.campaigns import CampaignAttempt, CampaignEvent, CampaignManifest, CampaignStore
from evallab.ops_continuous import REASON_DEFAULT_DISABLED, read_mode
from evallab.queue import DirectoryQueue, load_events

_TERMINAL_CAMPAIGN_EVENTS = frozenset({"attempt_completed", "attempt_failed"})
_TERMINAL_QUEUE_STATES = frozenset({"done", "failed"})


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _safe_campaign_id(campaign_id: str) -> str:
    if not campaign_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in campaign_id):
        raise ValueError("campaign_id must be a simple repository identifier")
    return campaign_id


class CampaignWorkloadOwner:
    """Observe and cancel exact leases belonging to one frozen campaign."""

    def __init__(self, repo_root: Path, manifest: CampaignManifest) -> None:
        self.repo_root = repo_root.resolve()
        self.manifest = manifest
        self.queue = DirectoryQueue(self.repo_root / "queue", create=False)
        self.store = CampaignStore(
            self.repo_root / "runs" / "campaigns",
            manifest.campaign_id,
        )
        self._attempts: dict[str, CampaignAttempt] = {}
        for attempt in manifest.attempts:
            identifiers = {
                attempt.attempt_id,
                attempt.spec_id,
                self.queue.lease_path(attempt.spec).name,
            }
            for identifier in identifiers:
                if identifier in self._attempts:
                    raise ValueError(f"duplicate campaign lease identifier: {identifier}")
                self._attempts[identifier] = attempt

    @classmethod
    def from_repo(cls, repo_root: Path, campaign_id: str) -> CampaignWorkloadOwner:
        root = repo_root.resolve()
        identifier = _safe_campaign_id(campaign_id)
        store = CampaignStore(root / "runs" / "campaigns", identifier)
        manifest = store.load_manifest()
        if manifest.campaign_id != identifier:
            raise ValueError("campaign manifest identity does not match its state directory")
        return cls(root, manifest)

    def _attempt(self, lease_id: str) -> CampaignAttempt | None:
        return self._attempts.get(lease_id)

    def request_cancel(self, lease_ids: list[str]) -> Mapping[str, Any]:
        if self.queue.root.is_symlink():
            raise RuntimeError("campaign queue root cannot be a symlink")
        self.queue.root.mkdir(parents=True, exist_ok=True)
        if self.queue.root.is_symlink():
            raise RuntimeError("campaign queue root cannot be a symlink")
        self.queue.stop()
        results: dict[str, str] = {}
        for lease_id in lease_ids:
            attempt = self._attempt(lease_id)
            if attempt is None:
                results[lease_id] = "unknown"
                continue
            if self.queue.request_cancel(attempt.spec):
                results[lease_id] = "signalled"
                continue
            try:
                state = self.queue.locate(attempt.spec_id).parent.name
            except ValueError:
                results[lease_id] = "missing"
            else:
                results[lease_id] = "terminal" if state in _TERMINAL_QUEUE_STATES else "not_active"
        return {
            "requested": True,
            "executed": any(result == "signalled" for result in results.values()),
            "owner": "campaign-queue",
            "campaign_id": self.manifest.campaign_id,
            "queue_stopped": self.queue.stop_path.is_file(),
            "lease_ids": list(lease_ids),
            "results": results,
        }

    def _terminal_campaign_event(self, attempt: CampaignAttempt) -> CampaignEvent | None:
        matches = [
            event
            for event in self.store.events(self.manifest)
            if event.attempt_id == attempt.attempt_id
            and event.event in _TERMINAL_CAMPAIGN_EVENTS
        ]
        return matches[-1] if matches else None

    def observe_lease(self, lease_id: str) -> Mapping[str, Any] | None:
        attempt = self._attempt(lease_id)
        if attempt is None:
            return None
        lease_path = self.queue.lease_path(attempt.spec)
        try:
            queue_path = self.queue.locate(attempt.spec_id)
        except ValueError:
            if not lease_path.is_file():
                return None
            return {
                "alive": True,
                "queue_state": "running",
                "evidence": {"lease": lease_path.relative_to(self.repo_root).as_posix()},
            }

        queue_state = queue_path.parent.name
        alive = lease_path.is_file()
        if alive or queue_state not in _TERMINAL_QUEUE_STATES:
            return {
                "alive": alive,
                "queue_state": queue_state,
                "evidence": {"queue_record": queue_path.relative_to(self.repo_root).as_posix()},
            }

        queue_events = [
            event for event in load_events(self.queue.events_path) if event.spec_id == attempt.spec_id
        ]
        if not queue_events:
            return None
        final_queue_event = queue_events[-1]
        campaign_event = self._terminal_campaign_event(attempt)
        evidence: dict[str, Any] = {
            "queue_record": queue_path.relative_to(self.repo_root).as_posix(),
            "queue_event_id": final_queue_event.event_id,
        }
        if campaign_event is not None:
            evidence["campaign_event_digest"] = campaign_event.event_digest
            cas_uri = campaign_event.details.get("cas_uri")
            if isinstance(cas_uri, str) and cas_uri:
                evidence["cas_uri"] = cas_uri
        settlement = {
            "campaign_id": self.manifest.campaign_id,
            "manifest_digest": self.manifest.manifest_digest,
            "attempt_id": attempt.attempt_id,
            "spec_id": attempt.spec_id,
            "spec_digest": attempt.spec_digest,
            "queue_state": queue_state,
            "queue_event": final_queue_event.model_dump(mode="json", exclude_none=True),
            "campaign_event_digest": campaign_event.event_digest if campaign_event is not None else None,
            "evidence": evidence,
        }
        return {
            "alive": False,
            "queue_state": "complete" if queue_state == "done" else "failed",
            "settlement_digest": _canonical_digest(settlement),
            "evidence": evidence,
        }


@dataclass(frozen=True)
class DisabledCampaignControlLoop:
    """One-shot campaign loop facade with no dispatch path."""

    state_dir: Path
    owner: CampaignWorkloadOwner

    def tick(self) -> Mapping[str, Any]:
        return {
            "campaign_id": self.owner.manifest.campaign_id,
            "mode": read_mode(self.state_dir),
            "running": False,
            "dispatched": 0,
            "reason": REASON_DEFAULT_DISABLED,
        }
