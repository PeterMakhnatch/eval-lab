"""Execute only an explicitly Verifier-accepted v7 release.

Canonical execution also requires an admitted parent envelope within Trainer's
combined $12 approval, raised only by an explicit current aggregate authority
carrying the window2 authorization identity (Peter's 2026-10-18 11AM ET
cumulative $14.50, no training). Its heldout cap continues the same cumulative
journal: raising that allocation never resets charges, and retries exist only
through the smallest explicit opt-in ``heldout_regrade.retry_grant`` bound to
the exact pinned original49 journal prefix, a unique grant identity, a deadline
inside the parent expiry, and exactly the authorized original UID/grader pairs.
Staged runs retain their frozen historical $3 cap and approval, without a new
envelope. Financial admission never releases a scientific HOLD.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import runpy
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import UUID

from evallab import runner
from evallab.qa import counterfactual as cf
from evallab.registry import harbor_task_digest
from evallab.regrade import CloudSpendGate, SpendCapReached

HERE = Path(__file__).resolve().parent
INVENTORY = runpy.run_path(str(HERE / "regrade.py"))
REPORT = runpy.run_path(str(HERE / "report.py"))
file_ref = INVENTORY["file_ref"]
bound_bytes = INVENTORY["bound_bytes"]
create_json = INVENTORY["create_json"]
APP = "mimo-clean-census-v7-classifiers-20261017"
# The historical combined approval stays $12 unless an explicit current
# aggregate_authority raises it; the authorized 2026-10-18 11AM ET Trainer
# window2 cumulative approval is $14.50 and carries no training reservation.
AGGREGATE_LEGACY_USD = Decimal("12")
AGGREGATE_CURRENT_USD = Decimal("14.50")
# A raised ceiling binds only this exact current authority identity, never any
# nonempty citation text.
AGGREGATE_AUTHORITY_ID = "trainer-finish-data-20261018-window-2"
# The window2 retry grant is the smallest possible duplicate exception: exactly
# these four authorized original (source UID, grader) pairs on this one task may
# each append one second provenance-marked reservation to the bound journal.
RETRY_GRANT_TASK = "mimo-v2.6-rl/format-code-task-001246"
RETRY_GRANT_DIGEST = "sha256:aa6cae30652ed7f270b3279a4ef1f6b9924efb799f8858d0344c827be9441d9d"
RETRY_GRANT_CELLS = frozenset(
    (job, trial, RETRY_GRANT_DIGEST)
    for job, trial in (
        ("409b9226-faa9-41e9-8161-3fc9e532ff2c", "d58deb1e-082f-406f-9c44-d8d880fb4613"),
        ("8c17271a-a875-445e-8206-1ad499bf4cc3", "cccc88a4-a489-471a-afe7-495356a09a20"),
        ("f0f72b17-7229-44e7-9558-a701bbd3e2d5", "92dfa5d0-a350-4d54-bdf7-5e0d658fc020"),
        ("d94ccebf-a7e3-4bda-9d16-a92fae7bce57", "8548c859-0819-479e-9fec-4f6f5d640dce"),
    )
)
# A grant binds only the exact original 49-row journal prefix at the pinned
# quote; an internally consistent alternative prefix — expanded or otherwise —
# is refused even under a fresh grant identity.
RETRY_GRANT_QUOTE = Decimal("0.0666")
RETRY_GRANT_PREFIX = {
    "rows": 49,
    "size_bytes": 14192,
    "sha256": "sha256:374d448801156994ecce3766b029db8517770c63470113b61590085c523e0fb2",
    "reserved_usd": "3.2634",
}


def _money(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{name} must be a finite nonnegative monetary amount")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a finite nonnegative monetary amount") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{name} must be a finite nonnegative monetary amount")
    return amount


def checked_budget(
    path: Path, *, journal: Path, usd_per_execution: float, now: datetime | None = None,
) -> tuple[dict, dict]:
    """Read a hash-bound parent allocation without dispatching or changing its journal."""
    reference = file_ref(path)
    envelope = json.loads(bound_bytes(reference)[0])
    if (not isinstance(envelope, dict) or envelope.get("schema") != "mimo.phase2-envelope/v1"
            or envelope.get("admitted") is not True):
        raise ValueError("an admitted mimo.phase2-envelope/v1 parent budget is required")
    if not isinstance(envelope.get("approval"), str) or not envelope["approval"].strip():
        raise ValueError("the parent budget requires a nonempty approval")
    if envelope.get("ledger_tag") != "2026-10-17-v7":
        raise ValueError("the parent budget must retain ledger 2026-10-17-v7")
    if not isinstance(envelope.get("expires_at"), str):
        raise ValueError("the parent budget requires a timezone-aware expiry")
    expires = datetime.fromisoformat(envelope["expires_at"])
    instant = now if now is not None else datetime.now(UTC)
    if (expires.tzinfo is None or expires.utcoffset() is None
            or instant.tzinfo is None or instant.utcoffset() is None or expires <= instant):
        raise ValueError("the parent budget must be unexpired with a timezone-aware clock and expiry")

    approved = _money(envelope.get("approved_usd"), "approved_usd")
    limit = _money(envelope.get("require_envelope_sum_at_most_usd"), "require_envelope_sum_at_most_usd")
    ceiling = AGGREGATE_LEGACY_USD
    authority = envelope.get("aggregate_authority")
    if authority is not None:
        if (not isinstance(authority, dict) or authority.get("schema") != "mimo.phase2-authority/v1"
                or authority.get("ledger_tag") != "2026-10-17-v7"
                or authority.get("authorization_id") != AGGREGATE_AUTHORITY_ID
                or not isinstance(authority.get("text"), str) or not authority["text"].strip()
                or authority.get("training_authorized") is not False):
            raise ValueError("a raised aggregate approval requires an explicit current authority")
        declared = _money(authority.get("approved_usd_at_most"), "aggregate_authority.approved_usd_at_most")
        if not AGGREGATE_LEGACY_USD < declared <= AGGREGATE_CURRENT_USD:
            raise ValueError("a raised aggregate authority exceeds the actual current cumulative authorization")
        ceiling = declared
    if not 0 < approved <= ceiling or not 0 < limit <= approved:
        raise ValueError("the aggregate parent approval and envelope ceiling must be positive and within the actual cumulative approval")
    for name in ("serving", "native", "heldout_regrade"):
        if not isinstance(envelope.get(name), dict) or "cap_usd" not in envelope[name]:
            raise ValueError(f"the parent budget requires a {name} cap")
    caps = [_money(section["cap_usd"], f"{name}.cap_usd") for name, section in envelope.items()
            if isinstance(section, dict) and "cap_usd" in section]
    total = sum(caps, Decimal(0)) + _money(envelope.get("training_reservation_usd", 0), "training_reservation_usd")
    if total > approved or total > limit:
        raise ValueError("aggregate envelope caps plus training reservation exceed the parent approval or envelope ceiling")
    if authority is not None and _money(envelope.get("training_reservation_usd", 0), "training_reservation_usd") != 0:
        raise ValueError("the current aggregate authority admits no training reservation")

    heldout = envelope["heldout_regrade"]
    unit = _money(usd_per_execution, "execution quote")
    if (_money(heldout["cap_usd"], "heldout_regrade.cap_usd") <= 0 or unit <= 0
            or _money(heldout.get("usd_per_execution"), "heldout_regrade.usd_per_execution") != unit):
        raise ValueError("the heldout cap must be positive and its unit must exactly match the positive execution quote")
    if (heldout.get("reset_authorized") is not False
            or heldout.get("canonical_reuses_exact_measurements_only") is not True):
        raise ValueError("the heldout budget must forbid journal resets and require exact measurement reuse")
    try:
        expected = journal.expanduser().resolve(strict=True)
        admitted_journal = Path(heldout["durable_journal_path"]).expanduser().resolve(strict=True)
    except (OSError, KeyError, TypeError) as exc:
        raise ValueError("the heldout budget must bind the existing cumulative journal") from exc
    if not expected.is_file() or admitted_journal != expected:
        raise ValueError("the heldout budget binds a different cumulative journal")
    retry_grant(envelope, journal=expected, usd_per_execution=usd_per_execution, now=instant)

    sources = envelope.get("sources", {})
    if not isinstance(sources, dict):
        raise ValueError("parent budget sources must be named file references")
    for source in sources.values():
        if (not isinstance(source, dict) or not isinstance(source.get("path"), str)
                or not isinstance(source.get("sha256"), str)):
            raise ValueError("each parent budget source requires a path and SHA-256")
        bound_bytes(source)
    return envelope, reference


def retry_grant(
    envelope: dict[str, Any], *, journal: Path, usd_per_execution: float, now: datetime | None = None,
) -> dict[str, Any] | None:
    """Normalize an explicitly declared heldout retry grant, or None when absent.

    A grant is the smallest opt-in duplicate exception on the cumulative
    journal: a unique canonical-UUID identity, a recorded approval, a live
    deadline inside the parent expiry, an exact immutable journal-prefix
    declaration, and exactly the authorized original UID/grader cells. Each
    granted cell permits a single second reservation carrying the grant
    identity; anything else keeps the legacy refusal.
    """
    heldout = envelope["heldout_regrade"]
    grant = heldout.get("retry_grant")
    if grant is None:
        return None
    instant = now if now is not None else datetime.now(UTC)
    unit = _money(usd_per_execution, "execution quote")
    if (not isinstance(grant, dict) or grant.get("schema") != "heldout-v7.retry-grant/v1"
            or not isinstance(grant.get("approval"), str) or not grant["approval"].strip()):
        raise ValueError("a heldout retry grant requires its exact schema and recorded approval")
    grant_id = grant.get("grant_id")
    try:
        identity = isinstance(grant_id, str) and str(UUID(grant_id)) == grant_id
    except (ValueError, AttributeError, TypeError):
        identity = False
    if not identity:
        raise ValueError("a heldout retry grant requires a unique canonical UUID identity")
    if not isinstance(envelope.get("expires_at"), str) or not isinstance(grant.get("expires_at"), str):
        raise ValueError("grant and parent deadlines must be timezone-aware instants")
    parent_expires = datetime.fromisoformat(envelope["expires_at"])
    deadline = datetime.fromisoformat(grant["expires_at"])
    if (deadline.tzinfo is None or deadline.utcoffset() is None or parent_expires.tzinfo is None
            or instant.tzinfo is None or instant.utcoffset() is None
            or deadline <= instant or deadline > parent_expires):
        raise ValueError("the grant deadline must be live and within the parent expiry")
    declared = grant.get("journal_prefix")
    if (not isinstance(declared, dict) or unit != RETRY_GRANT_QUOTE
            or declared.get("rows") != RETRY_GRANT_PREFIX["rows"]
            or declared.get("size_bytes") != RETRY_GRANT_PREFIX["size_bytes"]
            or declared.get("sha256") != RETRY_GRANT_PREFIX["sha256"]
            or _money(declared.get("reserved_usd"), "retry_grant.journal_prefix.reserved_usd")
            != Decimal(RETRY_GRANT_PREFIX["reserved_usd"])):
        raise ValueError("a retry grant binds only the exact authorized original49 journal prefix at the pinned quote")
    raw = journal.expanduser().resolve(strict=True).read_bytes()
    head = raw[: RETRY_GRANT_PREFIX["size_bytes"]]
    if (len(raw) < RETRY_GRANT_PREFIX["size_bytes"]
            or INVENTORY["sha"](head) != RETRY_GRANT_PREFIX["sha256"]
            or len(head.decode("utf-8").splitlines()) != RETRY_GRANT_PREFIX["rows"]):
        raise ValueError("the cumulative journal differs from the granted immutable prefix")
    entries = grant.get("entries")
    if not isinstance(entries, list) or len(entries) != len(RETRY_GRANT_CELLS):
        raise ValueError("a retry grant must name exactly the authorized original cells")
    cells: set[tuple[str, str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("each granted retry entry must be an object")
        uid = entry.get("source_uid")
        try:
            valid_uid = (isinstance(uid, list) and len(uid) == 2
                         and all(isinstance(part, str) and str(UUID(part)) == part for part in uid))
        except (ValueError, AttributeError, TypeError):
            valid_uid = False
        if (entry.get("task_id") != RETRY_GRANT_TASK or not valid_uid
                or entry.get("grader_digest") != RETRY_GRANT_DIGEST):
            raise ValueError("each granted retry must bind an authorized original UID/grader pair")
        key = (uid[0], uid[1], RETRY_GRANT_DIGEST)
        if key not in RETRY_GRANT_CELLS or key in cells:
            raise ValueError("granted retries must be exactly the authorized original pairs")
        cells.add(key)
    if cells != RETRY_GRANT_CELLS:
        raise ValueError("granted retries must be exactly the authorized original pairs")
    return {
        "schema": "heldout-v7.retry-grant/v1", "grant_id": grant_id, "approval": grant["approval"],
        "expires_at": deadline, "cells": frozenset(cells), "prefix": dict(RETRY_GRANT_PREFIX),
    }


def admit(
    inventory: dict[str, Any], manifest_path: Path, accepted_sha: str, verifier_receipt: Path,
) -> tuple[dict[str, Any], dict[str, cf.Grader]]:
    """Bind a released producer artifact to an independently accepted exact hash.

    accepted_sha is the operator's explicit Verifier-accepted identity, not an
    inferred verdict. Preserve the independent receipt bytes; do not pretend a
    Markdown keyword search constitutes independent review.
    """
    data, manifest_ref = bound_bytes({"path": str(manifest_path), "sha256": accepted_sha})
    manifest = json.loads(data)
    if (manifest.get("schema") != "mimo.heldout/v1" or manifest.get("successor_version") not in {"v7", "v7.1"}
            or manifest.get("scoring_allowed") is not True or manifest.get("freeze_ready") is not True
            or manifest.get("pending_gates") != []
            or str((manifest.get("independent_verifier") or {}).get("status")).lower() != "pass"):
        raise ValueError("v7 producer release and independent Verifier PASS are required")
    canonical_task = INVENTORY["canonical_task"]
    tasks = manifest["heldout"]
    if (len(tasks) != 33 or len(set(tasks)) != 33
            or sorted(canonical_task(task) for task in tasks) != inventory["frozen_task_ids"]):
        raise ValueError("v7 membership differs from the authorized original33")
    if INVENTORY["bare_sha"](manifest["frozen_sources"]["heldout.json"]["sha256"]) != INVENTORY["FROZEN_SHA"]:
        raise ValueError("v7 release does not bind the original frozen artifact")
    for reference in manifest["sources"]:
        bound_bytes(reference)
    verifier_ref = file_ref(verifier_receipt)
    source_parents = {row["task_id"]: row["source_package"]["sha256"] for row in inventory["rows"]
                      if row["population"] == "official"}
    graders: dict[str, cf.Grader] = {}
    for row in manifest["tasks"]:
        task = canonical_task(row["task_id"])
        if task in graders or task not in inventory["frozen_task_ids"]:
            raise ValueError("duplicate or unauthorized selected v7 task")
        if row["frozen_parent"]["package_digest"] != source_parents[task]:
            raise ValueError("v7 frozen parent differs from the native source package")
        target = row["evaluation_target"]
        package = Path(target["package_path"]).resolve(strict=True)
        actual_task, digest = INVENTORY["_package_identity"](package)
        if (actual_task != task or digest != target["package_digest"]
                or harbor_task_digest(package) != target["harbor_digest"]):
            raise ValueError("selected v7 package identity changed")
        lineage = target["lineage"]
        newest = lineage["records_newest_first"][0]
        record_bytes, record_ref = bound_bytes(newest["record"])
        from evallab.task_variants import VariantRecord

        record = VariantRecord.model_validate_json(record_bytes)
        if (record.variant_digest != digest or canonical_task(record.task_name) != task
                or record.variant_harbor_digest != target["harbor_digest"]
                or record.transform != newest["transform"] or newest["package_digest"] != digest
                or newest["harbor_digest"] != target["harbor_digest"]
                or INVENTORY["bare_sha"](record_ref["sha256"]) != INVENTORY["bare_sha"](lineage["record_sha256"])):
            raise ValueError("selected v7 grader record does not bind its package")
        graders[task] = cf.Grader(task, record.transform, digest, package, record_ref["path"])
    if set(graders) != set(inventory["frozen_task_ids"]):
        raise ValueError("v7 must select exactly one grader for each frozen task")
    cf.canonical_graders(list(graders.values()))
    return {
        "schema": "heldout-v7.admission/v1", "scoring_allowed": True,
        "manifest": manifest_ref, "verifier_receipt": verifier_ref,
        "operator_attested_verifier_accepted_sha256": manifest_ref["sha256"],
        "acceptance_basis": "Explicit independently accepted exact hash plus the producer's released PASS; receipt bytes retained, not natural-language parsed.",
        "graders": {task: grader.to_json() for task, grader in sorted(graders.items())},
    }, graders


class JournalGate(CloudSpendGate):
    """Reserve before dispatch; failures and process restarts never refund spend.

    With an admitted ``retry_grant``, the journal must still begin with the
    exact pinned original49 prefix, and every suffix row must be a
    provenance-marked retry of an authorized cell under this grant identity —
    once per entry, never across a restart, and only while the grant deadline is
    live at the actual charge. Grant-free replay admits provenance-marked
    duplicates only for the pinned four cells, at most once each, after the
    exact original49 prefix, and under a single grant identity.
    """

    def __init__(
        self, path: Path, usd_per_execution: float, *, cap_usd: float, approval: str,
        grant: dict[str, Any] | None = None, clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(approval, str):
            raise ValueError("a cumulative reservation journal requires its recorded approval")
        cap, unit = _money(cap_usd, "cap_usd"), _money(usd_per_execution, "usd_per_execution")
        super().__init__(approval, float(cap), float(unit))
        self.path = path
        self.context = threading.local()
        self.reserved_uids: set[tuple[str, str]] = set()
        self.reserved_keys: set[tuple[str, str, str | None]] = set()
        self.grant_remaining: set[tuple[str, str, str]] = set()
        self._replayed_retries: set[tuple[str, str, str | None]] = set()
        self._suffix_identity: str | None = None
        self._grant_id: str | None = None
        self._deadline: datetime | None = None
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        if grant is not None:
            prefix = grant.get("prefix") if isinstance(grant, dict) else None
            if (not isinstance(grant, dict) or grant.get("schema") != "heldout-v7.retry-grant/v1"
                    or not isinstance(grant.get("grant_id"), str) or not grant["grant_id"]
                    or not isinstance(grant.get("expires_at"), datetime)
                    or grant["expires_at"].tzinfo is None or grant["expires_at"].utcoffset() is None
                    or grant.get("cells") != RETRY_GRANT_CELLS
                    or prefix != RETRY_GRANT_PREFIX):
                raise ValueError("a retry grant must be the normalized explicit grant")
            self.grant_remaining = set(grant["cells"])
            self._grant_id = grant["grant_id"]
            self._deadline = grant["expires_at"]
        if grant is not None and not path.is_file():
            raise ValueError("a granted reservation journal must exist with its immutable prefix")
        if path.exists():
            raw = path.read_bytes()
            if grant is not None:
                prefix = grant["prefix"]
                head = raw[: prefix["size_bytes"]]
                if (len(raw) < prefix["size_bytes"] or INVENTORY["sha"](head) != prefix["sha256"]
                        or len(head.decode("utf-8").splitlines()) != prefix["rows"]):
                    raise ValueError("the reservation journal does not start with the granted immutable prefix")
                lines = head.decode("utf-8").splitlines() + raw[prefix["size_bytes"]:].decode("utf-8").splitlines()
                prefix_rows = prefix["rows"]
            else:
                lines = raw.decode("utf-8").splitlines()
                prefix_rows = len(lines)
            prefix_keys: set[tuple[str, str, str | None]] = set()
            for index, line in enumerate(lines):
                entry = json.loads(line)
                uid = tuple(entry["source_uid"])
                digest = entry.get("grader_digest")
                key = (*uid, digest)
                if (len(uid) != 2
                        or digest is not None and "sha256:" + INVENTORY["bare_sha"](digest) != digest
                        or entry["execution"] != self.executions + 1
                        or _money(entry["usd_per_execution"], "journal usd_per_execution") != unit
                        or entry["ledger_tag"] != "2026-10-17-v7"
                        or _money(entry["reserved_usd"], "journal reserved_usd") != Decimal(str(round((self.executions + 1) * self.usd_per_execution, 8)))):
                    raise ValueError("invalid cumulative reservation journal")
                provenance = entry.get("retry_grant")
                if key in self.reserved_keys:
                    try:
                        identity = isinstance(provenance, str) and str(UUID(provenance)) == provenance
                    except (ValueError, AttributeError, TypeError):
                        identity = False
                    if grant is not None:
                        # Sealed prefix history never carries grant provenance; a
                        # marked suffix row replays only a still-unconsumed
                        # granted retry under this exact grant identity.
                        if (index < prefix_rows or not identity or provenance != self._grant_id
                                or key not in self.grant_remaining):
                            raise ValueError("invalid cumulative reservation journal")
                        self.grant_remaining.discard(key)
                    # Grant-free replay keeps default refusal except the pinned
                    # four cells' single marked retries after the exact
                    # original49 prefix, all under one grant identity.
                    elif (index < RETRY_GRANT_PREFIX["rows"] or not identity
                            or key not in RETRY_GRANT_CELLS or key in self._replayed_retries
                            or (self._suffix_identity is not None
                                and provenance != self._suffix_identity)):
                        raise ValueError("invalid cumulative reservation journal")
                    else:
                        self._suffix_identity = provenance
                        self._replayed_retries.add(key)
                # Provenance marks only consumed granted retries; under a grant
                # every suffix row must be one of them.
                elif provenance is not None or grant is not None and index >= prefix_rows:
                    raise ValueError("invalid cumulative reservation journal")
                self.reserved_uids.add(uid)
                self.reserved_keys.add(key)
                if index < prefix_rows:
                    prefix_keys.add(key)
                self.executions += 1
            if grant is not None and not grant["cells"] <= prefix_keys:
                raise ValueError("granted retry cells are not original prefix reservations")
            if grant is None and self._replayed_retries:
                head = raw[: RETRY_GRANT_PREFIX["size_bytes"]]
                if (len(raw) < RETRY_GRANT_PREFIX["size_bytes"]
                        or INVENTORY["sha"](head) != RETRY_GRANT_PREFIX["sha256"]
                        or len(head.decode("utf-8").splitlines()) != RETRY_GRANT_PREFIX["rows"]):
                    raise ValueError("invalid cumulative reservation journal")
        if self.executions * self.usd_per_execution > self.cap_usd + 1e-9:
            raise ValueError("prior reservations exceed the lane cap")

    def is_reserved(self, uid: tuple[str, str], digest: str | None) -> bool:
        # An unconsumed granted retry is not masked by its original reservation.
        if digest is not None and (*uid, digest) in self.grant_remaining:
            return False
        if digest is None:
            return uid in self.reserved_uids
        # Legacy source-only reservations are never silently reassigned or refunded.
        return (*uid, None) in self.reserved_keys or (*uid, digest) in self.reserved_keys

    def charge(self) -> None:
        uid = tuple(self.context.source_uid)
        digest = getattr(self.context, "grader_digest", None)
        if digest is not None:
            INVENTORY["bare_sha"](digest)
        with self._lock:
            reserved = (self.executions + 1) * self.usd_per_execution
            if reserved > self.cap_usd + 1e-9:
                raise SpendCapReached("the cumulative heldout-v7 reservation cap is exhausted")
            key = (*uid, digest)
            if self.is_reserved(uid, digest):
                raise SpendCapReached("source already reserved; no implicit retry or refund")
            granted = key in self.grant_remaining
            if granted:
                instant = self._clock()
                if (not isinstance(instant, datetime) or instant.tzinfo is None
                        or instant.utcoffset() is None or self._deadline <= instant):
                    raise SpendCapReached("the explicit retry grant expired before this reservation")
            elif self._grant_id is not None:
                raise SpendCapReached("the retry grant admits only its four authorized original cells")
            entry = {
                "ledger_tag": "2026-10-17-v7", "source_uid": list(uid),
                "execution": self.executions + 1, "usd_per_execution": self.usd_per_execution,
                "reserved_usd": round(reserved, 8),
            }
            if digest is not None:
                entry["grader_digest"] = digest
            if granted:
                entry["retry_grant"] = self._grant_id
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, sort_keys=True, allow_nan=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            self.reserved_uids.add(uid)
            self.reserved_keys.add(key)
            self.grant_remaining.discard(key)
            self.executions += 1


def _retained_observation(path: Path) -> dict[str, Any]:
    reference = json.loads(path.with_name(path.stem + "-ref.json").read_bytes())
    if Path(reference["path"]).resolve() != path.resolve():
        raise ValueError("retained observation pointer changed")
    data, _ = bound_bytes(reference)
    observation = json.loads(data)
    for reference in observation["native_evidence"].get("files", {}).values():
        bound_bytes(reference)
    return observation


def _case_paths(case: Path, key: tuple[str, str, str | None], grant: dict[str, Any] | None) -> tuple[Path, Path]:
    """A case's effective retained observation and the path a new observation takes.

    A granted cell whose original observation already exists writes its one
    authorized retry as ``observation.retry-<grant_id>.json``; existing bytes are
    never edited. A retained retry observation supersedes the original.
    """
    # Hash-pointer ``*-ref.json`` companions are never observations themselves.
    retries = sorted(
        path for path in case.glob("observation.retry-*.json") if not path.name.endswith("-ref.json")
    )
    if len(retries) > 1:
        raise ValueError("a source cell carries multiple retained retry observations")
    base = case / "observation.json"
    retained = retries[0] if retries else base
    granted = grant is not None and key in grant["cells"]
    fresh = case / f"observation.retry-{grant['grant_id']}.json" if granted and base.exists() else base
    return retained, fresh


def _masked_by_grant(observation: dict[str, Any], key: tuple[str, str, str | None], grant: dict[str, Any] | None) -> bool:
    """A granted cell's stale unmeasured original observation never blocks its authorized retry."""
    return (grant is not None and key in grant["cells"]
            and observation["v7_outcome"] is None and observation.get("granted_retry") is None)


def execute(
    *, inventory_path: Path, manifest_path: Path, accepted_sha: str | None, verifier_receipt: Path | None,
    rate_evidence: Path, usd_per_execution: float, work: Path, output: Path,
    repo_root: Path, workers: int = 4, staged_plan_path: Path | None = None, reuse_work: Path | None = None,
    sandbox_timeout_seconds: int = 300, budget_envelope: Path | None = None,
) -> dict[str, Any]:
    """Run the complete bounded inventory. No source/graded row is silently omitted."""
    inventory_ref = file_ref(inventory_path)
    inventory = json.loads(inventory_path.read_bytes())
    for reference in inventory["sources"].values():
        bound_bytes(reference)
    reconstructed = INVENTORY["inventory"](Path(inventory["sources"]["configuration"]["path"]))
    if {**inventory, "created": None} != {**reconstructed, "created": None}:
        raise ValueError("inventory cells differ from their bound canonical/native source evidence")
    staged = runpy.run_path(str(HERE / "staged.py"))
    plan = None
    if staged_plan_path is not None:
        plan, graders = staged["load_plan"](staged_plan_path, inventory)
        if (Path(plan["sources"]["manifest"]["path"]).resolve() != manifest_path.resolve()
                or plan["budget"]["usd_per_execution"] != usd_per_execution
                or file_ref(rate_evidence) != bound_bytes(plan["sources"]["rate_evidence"])[1]):
            raise ValueError("staged execution changes its bound manifest or reservation basis")
        admission, targets = plan["admission"], plan["selected_packages"]
        sandbox_timeout_seconds = plan["budget"]["sandbox_timeout_seconds"]
    else:
        if accepted_sha is None or verifier_receipt is None:
            raise ValueError("canonical run requires an independently accepted exact manifest")
        admission, graders = admit(inventory, manifest_path, accepted_sha, verifier_receipt)
        targets = staged["canonical_bindings"](manifest_path)
    # Validate the entire raw map before any paid call, including all exclusions.
    REPORT["_validate"](inventory, [], admission)
    if workers < 1 or workers > 4:
        raise ValueError("workers must be between 1 and 4")
    if type(sandbox_timeout_seconds) is not int or sandbox_timeout_seconds < 1:
        raise ValueError("an explicit positive integer Sandbox TTL is required")
    rate_ref = file_ref(rate_evidence)
    work, output, repo_root = work.resolve(), output.resolve(), repo_root.resolve()
    if output.exists() or output.is_relative_to(work) or work.is_relative_to(output):
        raise ValueError("report output must be new and disjoint from campaign work")
    immutable = [Path(row["source_trial_dir"]).parent for row in inventory["rows"]]
    immutable.extend(Path(row["source_package"]["path"]) for row in inventory["rows"])
    immutable.extend(grader.package for grader in graders.values())
    for target in (work, output):
        if any(target.is_relative_to(path) or path.is_relative_to(target) for path in immutable):
            raise ValueError("campaign output overlaps immutable input evidence")
    budget_root = Path(inventory["sources"]["configuration"]["path"]).resolve().parent
    journal = budget_root / "classifiers-v7-reservations.jsonl"
    budget_ref = None
    grant = None
    if plan is None:
        if budget_envelope is None:
            raise ValueError("canonical run requires an admitted parent budget envelope")
        envelope, budget_ref = checked_budget(budget_envelope, journal=journal, usd_per_execution=usd_per_execution)
        cap_usd = float(_money(envelope["heldout_regrade"]["cap_usd"], "heldout_regrade.cap_usd"))
        approval = envelope["approval"]
        grant = retry_grant(envelope, journal=journal, usd_per_execution=usd_per_execution)
    else:
        cap_usd, approval = plan["budget"]["cap_usd"], plan["budget"]["prior"]["approval"]
    try:
        replay_and_collect = runpy.run_path(str(HERE / "native.py"))["replay_and_collect"]
        from evallab import harbor_modal
        from evallab.schemas import ExperimentMatrix

        if (getattr(harbor_modal, "NATIVE_SINGLE_CREATE_SUPPORTED", False) is not True
                or "modal_sandbox_create_max_attempts" not in ExperimentMatrix.model_fields):
            raise ValueError("heldout regrades require the integrated typed single-create Modal runtime before reserving spend")
    except ImportError as exc:
        raise ValueError("install the pinned Harbor/Modal native runtime before v7 execution") from exc
    work.mkdir(parents=True, exist_ok=True)
    with (budget_root / "classifiers-v7-ledger.lock").open("a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        header = {
            "schema": "heldout-v7.campaign/v1", "ledger_tag": "2026-10-17-v7",
            "inventory": inventory_ref, "admission": admission,
            "rate_evidence": rate_ref, "usd_per_execution": usd_per_execution, "cap_usd": cap_usd,
            "budget_envelope": budget_ref,
            "modal_policy": {"app": APP, "resource_policy": "limit", "sandbox_timeout_seconds": sandbox_timeout_seconds,
                             "sandbox_create_max_attempts": 1},
            "measurement_mode": "staged" if plan is not None else "canonical",
            "retry_grant": ({
                "grant_id": grant["grant_id"], "expires_at": grant["expires_at"].isoformat(),
                "cells": [{"source_uid": [job, trial], "grader_digest": digest}
                          for job, trial, digest in sorted(grant["cells"])],
            } if grant is not None else None),
            "canonical_scoring_allowed": plan is None, "citable": plan is None,
            "staged_plan": file_ref(staged_plan_path) if staged_plan_path is not None else None,
            "reuse_work": str(reuse_work.resolve()) if reuse_work is not None else None,
            "consumer_code": {name: file_ref(HERE / name) for name in ("regrade.py", "campaign.py", "native.py", "report.py", "staged.py")},
            "reservation_caveat": "Cumulative estimates, not settled provider bills; retain all failures and interrupted reservations.",
        }
        header_path = work / "campaign.json"
        if header_path.exists():
            if json.loads(header_path.read_bytes()) != header:
                raise ValueError("resume changes the admitted inventory, graders or budget basis")
        else:
            create_json(header_path, header)
        gate = JournalGate(journal, usd_per_execution, cap_usd=cap_usd, approval=approval, grant=grant)
        cases = work / "cases"
        cases.mkdir(exist_ok=True)
        def case_path(source: dict[str, Any]) -> Path:
            key = "\n".join([*source["source_uid"], targets[source["task_id"]]["package_digest"]])
            return cases / hashlib.sha256(key.encode()).hexdigest()

        cached: dict[tuple[str, str], dict[str, Any]] = (
            staged["retained_observations"](reuse_work.resolve(), inventory, targets) if reuse_work is not None else {}
        )
        if grant is not None:
            for source in inventory["rows"]:
                uid = tuple(source["source_uid"])
                key = (*uid, targets[source["task_id"]]["package_digest"])
                if uid in cached and _masked_by_grant(cached[uid], key, grant):
                    del cached[uid]
        for source in inventory["rows"]:
            uid = tuple(source["source_uid"])
            target = targets[source["task_id"]]
            key = (*uid, target["package_digest"])
            retained_path, _ = _case_paths(case_path(source), key, grant)
            if retained_path.exists():
                retained = _retained_observation(retained_path)
                if retained["source_uid"] != list(uid) or not staged["same_measurement"](source, retained, target):
                    raise ValueError("retained observation source identity changed")
                staged["qualify_retained"](source, retained, target)
                if _masked_by_grant(retained, key, grant):
                    continue  # An explicitly granted cell retries past its stale unmeasured original.
                cached[uid] = retained
        needed = sum(
            tuple(source["source_uid"]) not in cached and source["diff"] is not None
            and not (plan is not None and not targets[source["task_id"]]["changed_from_v6"])
            and not gate.is_reserved(tuple(source["source_uid"]), targets[source["task_id"]]["package_digest"])
            for source in inventory["rows"]
        )
        if (gate.executions + needed) * usd_per_execution > cap_usd + 1e-9:
            raise SpendCapReached("the complete selected cohort does not fit the remaining admitted cumulative heldout cap; no new dispatch")

        def one(source: dict[str, Any]) -> dict[str, Any]:
            uid = tuple(source["source_uid"])
            target = targets[source["task_id"]]
            case = case_path(source)
            case.mkdir(exist_ok=True)
            key = (*uid, target["package_digest"])
            retained_path, saved = _case_paths(case, key, grant)
            if uid in cached:
                observation = cached[uid]
                if not retained_path.exists():
                    reference = create_json(retained_path, observation)
                    create_json(retained_path.with_name(retained_path.stem + "-ref.json"), reference)
                return observation
            if plan is not None and not target["changed_from_v6"]:
                observation = staged["inherit"](source, target)
            elif gate.is_reserved(uid, target["package_digest"]):
                # Never silently spend again after a crash in the dispatch window.
                observation = {
                    "source_uid": list(uid), "v7_outcome": None,
                    "v7_error": "interrupted_after_reservation_requires_native_evidence_reconciliation",
                    "replayed": None, "native_evidence": {"files": {}, "case_dir": str(case)},
                }
            else:
                gate.context.source_uid = uid
                gate.context.grader_digest = target["package_digest"]

                def dispatch(matrix: Any, *, root: Path) -> Any:
                    configured = matrix.model_copy(update={
                        "modal_app_name": APP, "modal_resource_policy": "limit",
                        "modal_sandbox_timeout_seconds": sandbox_timeout_seconds,
                        "modal_sandbox_create_max_attempts": 1,
                    })
                    create_json(case / "dispatch.json", configured.model_dump(mode="json"))
                    return runner.run_matrix(configured, root=root)

                observation = replay_and_collect(
                    source, graders[source["task_id"]], repo_root=repo_root, work=case / "replay",
                    spend_gate=gate, matrix_runner=dispatch,
                )
                observation["measurement_origin"] = (
                    "new_native_model_free_regrade"
                    if (observation.get("native_evidence") or {}).get("dispatched") is True
                    else "not_dispatched"
                )
            if grant is not None and key in grant["cells"]:
                observation["granted_retry"] = {"grant_id": grant["grant_id"]}
            observation["measurement_binding"] = staged["measurement_binding"](source, target)
            reference = create_json(saved, observation)
            create_json(saved.with_name(saved.stem + "-ref.json"), reference)
            return observation

        previous_executions = gate.executions
        with ThreadPoolExecutor(max_workers=workers) as pool:
            observations = list(pool.map(one, inventory["rows"]))
        report = REPORT["build_report"](inventory, observations, admission)
        report["campaign"] = {**header, "spend": gate.to_json(), "journal": file_ref(gate.path) if gate.path.exists() else None}
        report["campaign"]["this_invocation"] = {
            "new_reservations": gate.executions - previous_executions,
            "new_native_dispatches": sum(
                tuple(row["source_uid"]) not in cached and row["native_evidence"].get("dispatched") is True
                for row in observations
            ),
            "reused_observations": len(cached),
            "pricing_basis": "resource_bound_plus_build_buffer",
            "all_in_invoice_hard_bound_proven": False,
        }
        result = staged["publish"](report, output, staged=plan is not None)
        result["spend"] = gate.to_json()
        return result
