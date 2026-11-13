"""Public-corpus admission and the durable production work journal.

Construct ``CorpusRegistry(repo_root, frozen_eval_token=...)`` from committed local
indices, then share ``ContinuousWorkQueue(database, registry=registry)`` across
workers. A database is either a SQLite path/``sqlite:///`` URL or a PostgreSQL
``postgresql://`` DSN. Use a dedicated production database (or PostgreSQL schema).
No corpus downloads, model calls, or dispatch happen on construction.

Every event and its outbox entry commit together; qualifying results and their
chained jobs commit in that same transaction. ``events`` is append-only, including
at the database boundary. Global sequence allocation is serialized until commit,
so a durable consumer cannot skip a slower, uncommitted event. Outbox publication
is at-least-once: publish the envelope using event_id as the broker deduplication
key, then call ``mark_published``. Publication and job acknowledgement are separate.

Jobs require registry-resolved dataset_id/item_id. Caller-provided split labels
never authorize admission. TEST (including MiMo's permanently held-out tasks) is
sealed unless a token configured on the registry is explicitly passed to that
operation. Tokens are not persisted and are not inherited by later operations.
Leases require heartbeat renewal and are fenced by unguessable lease tokens;
expired workers cannot acknowledge jobs reclaimed by another worker.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Systems that own leased jobs. Only these may enqueue ``*_task`` work.
DATASETS = ("tb4-audit-v1", "mimo-defect-map-v1", "cheatbench-actions-v1", "hacktrace-act-v1")
_BARE_NUMBER = re.compile(r"[0-9]+")


class SplitLockError(ValueError):
    """A sealed corpus item was not explicitly authorized for frozen evaluation."""



@dataclass(frozen=True)
class CorpusItem:
    dataset_id: str
    item_id: str
    split: str
    task_id: str | None
    task_digest: str | None
    source_path: Path
    metadata: dict[str, Any]
    source_record_sha256: str


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _identity(value: str) -> str:
    return value.strip().casefold()


def _split(value: Any) -> str:
    value = _identity(_text(value, "split"))
    if value in ("test", "heldout"):
        return "test"
    if value == "dev":
        return "dev"
    raise ValueError(f"unsupported corpus split: {value!r}")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def source_record_digest(record: Any) -> str:
    """Hash a source record using the queue's canonical JSON encoding."""
    return "sha256:" + hashlib.sha256(_json(record).encode()).hexdigest()



def _jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{number}: corpus row must be an object")
            yield row


class CorpusRegistry:
    """Index the four public local corpora without opening task/answer bytes.

    TB4 task records and finding records are both addressable by their native
    identifiers. MiMo records use ``record_id`` (task plus historical package
    digest), not a mutable latest-version alias. Canonical split assignments can
    only tighten a local row's split, never turn TEST back into DEV. The TEST lock
    follows task names/digests across corpus records as well as individual ids.

    Only the four canonical corpus keys feed that global lock: assignments under
    secondary key ids tighten solely their own ``(key_id, item_id)`` rows.
    """

    def __init__(
        self, repo_root: str | Path | None = None, *, frozen_eval_token: str | None = None
    ) -> None:
        self.repo_root = (
            Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[3]
        )
        self._token = (
            None if frozen_eval_token is None else _text(frozen_eval_token, "frozen_eval_token")
        )
        self._items: dict[tuple[str, str], CorpusItem] = {}
        self._sealed_names: set[str] = set()
        self._sealed_digests: set[str] = set()
        self._sealed_ids: set[str] = set()
        self._assignment_names: dict[tuple[str, str], str] = {}
        self._assigned_test_names: set[str] = set()
        self._assigned_test_ids: set[str] = set()
        # Split of each canonical index task name, TEST winning across records.
        self._index_names: dict[tuple[str, str], str] = {}
        keys = self.repo_root / "research/answer-keys"
        assignments: dict[tuple[str, str], str] = {}
        assignment_path = keys / "splits/assignments.csv"
        self.split_assignments_digest: str | None = None
        if assignment_path.is_file():
            raw_assignments = assignment_path.read_bytes()
            self.split_assignments_digest = (
                "sha256:" + hashlib.sha256(raw_assignments).hexdigest()
            )
            with io.StringIO(raw_assignments.decode("utf-8"), newline="") as stream:
                reader = csv.DictReader(stream, strict=True)
                if reader.fieldnames is not None and (
                    not {"key_id", "item_id", "split"}.issubset(reader.fieldnames)
                    or len(set(reader.fieldnames)) != len(reader.fieldnames)
                ):
                    raise ValueError("split assignments have an invalid header")
                for row in reader:
                    if None in row:
                        raise ValueError("split assignments have extra row fields")
                    key_id = _identity(_text(row.get("key_id"), "assignment key_id"))
                    item_id = _identity(_text(row.get("item_id"), "assignment item_id"))
                    key = key_id, item_id
                    split = _split(row.get("split"))
                    assignments[key] = "test" if "test" in (split, assignments.get(key)) else split
                    if row.get("task"):
                        name = self.task_name(row["task"])
                        if not name:
                            raise ValueError("split assignments have an empty task name")
                        name_key = key_id, name
                        self._assignment_names[name_key] = (
                            "test" if "test" in (split, self._assignment_names.get(name_key)) else split
                        )
                    if split == "test":
                        self._assigned_test_ids.add(item_id)
                        if row.get("task"):
                            self._assigned_test_names.add(self.task_name(row["task"]))
                    # Secondary assignments do not become global queue seals.
                    if split == "test" and row.get("task") and key_id in DATASETS:
                        self._sealed_names.add(self.task_name(row["task"]))
        self._assignments = assignments
        self._assignment_keys = frozenset(dataset for dataset, _ in assignments)
        for dataset in ("tb4-audit-v1", "cheatbench-actions-v1", "hacktrace-act-v1"):
            for row in _jsonl(keys / dataset / "items.jsonl"):
                self._add(dataset, row, keys / dataset / "items.jsonl", assignments)
        task_index = keys / "tb4-audit-v1/tasks.jsonl"
        for row in _jsonl(task_index):
            source_row = row
            subject = row.get("subject") or {}
            row = {**row, "item_id": subject.get("task_id")}
            self._add(
                "tb4-audit-v1", row, task_index, assignments, source_record=source_row
            )
        inventory_path = self.repo_root / "research/experiments/mimo-defect-map-v1/inventory.json"
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        if inventory.get("dataset_id") != "mimo-defect-map-v1":
            raise ValueError("MiMo inventory dataset_id does not match mimo-defect-map-v1")
        heldout = {
            self.task_name(task)
            for task in inventory["heldout_task_ids_excluded_from_training_all_versions"]
        }
        self._sealed_names.update(heldout)
        for row in inventory["tasks"]:
            source_row = row
            row = {
                **row,
                "item_id": row["record_id"],
                "split": "test"
                if self.task_name(row["task_id"]) in heldout
                or row.get("heldout_excluded_from_training_all_versions")
                else "dev",
                "subject": {"task_id": row["task_id"], "task_digest": row.get("package_digest")},
            }
            self._add(
                "mimo-defect-map-v1", row, inventory_path, assignments, source_record=source_row
            )

    @staticmethod
    def task_name(task: str) -> str:
        """The normalized task name, without namespace prefixes or version suffixes."""
        return _identity(task.rsplit("/", 1)[-1].split("@", 1)[0])

    def _add(
        self,
        dataset: str,
        row: dict[str, Any],
        source: Path,
        assignments: Mapping[tuple[str, str], str],
        *,
        source_record: dict[str, Any] | None = None,
    ) -> None:
        item_id = _text(row.get("item_id"), "item_id")
        if _identity(_text(row.get("key_id", dataset), "key_id")) != _identity(dataset):
            raise ValueError(f"corpus key_id does not match {dataset}")
        key = _identity(dataset), _identity(item_id)
        split = _split(row.get("split", assignments.get(key)))
        if assignments.get(key) == "test":
            split = "test"
        subject = row.get("subject") or {}
        task_id, digest = subject.get("task_id"), subject.get("task_digest")
        record = row if source_record is None else source_record
        record_digest = source_record_digest(record)
        item = CorpusItem(dataset, item_id, split, task_id, digest, source, row, record_digest)
        if key in self._items:
            raise ValueError(f"duplicate corpus item: {dataset}/{item_id}")
        self._items[key] = item
        if task_id:
            name_key = key[0], self.task_name(task_id)
            self._index_names[name_key] = (
                "test" if "test" in (split, self._index_names.get(name_key)) else split
            )
        if split == "test":
            self._sealed_ids.add(_identity(item_id))
            if task_id:
                self._sealed_names.add(self.task_name(task_id))
            if digest:
                self._sealed_digests.add(_identity(digest))

    def _effective_split(self, item: CorpusItem) -> str:
        if (
            item.split == "test"
            or self.sealed_identity(
                item_id=item.item_id, task_id=item.task_id, task_digest=item.task_digest,
            ) is not None
        ):
            return "test"
        return "dev"

    def sealed_identity(
        self,
        *,
        item_id: str | None = None,
        task_id: str | None = None,
        task_digest: str | None = None,
    ) -> str | None:
        """The seal a bare identity hits (``item_id``/``task name``/``task digest``).

        Item ids, canonical task names, and task digests a canonical TEST row
        sealed stay sealed wherever they reappear, including inside external
        registrations and nested provenance. ``None`` means unsealed.
        """
        if item_id is not None and _identity(item_id) in self._sealed_ids:
            return "item_id"
        if task_id is not None and self.task_name(task_id) in self._sealed_names:
            return "task name"
        if task_digest is not None and _identity(task_digest) in self._sealed_digests:
            return "task digest"
        return None

    def sealed_identifiers(self) -> frozenset[str]:
        """Text seals: canonical identities and secondary TEST task names, not item labels.

        Bare numeric item ids (row indices such as CheatBench's ``44``) are not
        distinctive text: as substrings they match inside digests, counts, and
        line numbers. They stay sealed as exact structured identities through
        :meth:`sealed_identity` and per-key assignments, never as text seals.
        """
        return frozenset(
            value for value in (
                self._sealed_ids | self._sealed_names | self._sealed_digests
                | self._assigned_test_names
            )
            if not _BARE_NUMBER.fullmatch(value)
        )

    def has_assignment_authority(self) -> bool:
        """Whether a validated, nonempty split ledger was loaded into this registry."""
        digest = getattr(self, "split_assignments_digest", None)
        return (
            isinstance(digest, str)
            and digest.startswith("sha256:")
            and len(digest) == 71
            and all(char in "0123456789abcdef" for char in digest[7:])
            and bool(getattr(self, "_assignments", None))
        )

    def has_split_assignments(self, key_id: str) -> bool:
        """Whether this key has authoritative per-item split assignments."""
        return _identity(key_id) in self._assignment_keys

    def assigned_split(
        self, key_id: str, *, item_id: str | None = None, task_id: str | None = None,
    ) -> str | None:
        """Resolve assignment identities without opening task or answer content.

        Secondary keys retain their own split locks, not global seals. TEST wins
        across item-id/name aliases; ``None`` means no explicit assignment and
        must not authorize a DEV row for a key with split assignments. A
        canonical key with no ledger rows (MiMo) resolves through its own
        identity index: the record id or normalized task name.
        """
        key_id = _identity(key_id)
        item_id = None if item_id is None else _identity(item_id)
        task_id = None if task_id is None else _identity(task_id)
        candidates = [
            self._assignments.get((key_id, item_id)) if item_id is not None else None,
            self._assignments.get((key_id, task_id)) if task_id is not None else None,
            self._assignments.get((key_id, self.task_name(task_id)))
            if task_id is not None else None,
            self._assignment_names.get((key_id, self.task_name(task_id)))
            if task_id is not None else None,
        ]
        if key_id in DATASETS and key_id not in self._assignment_keys:
            indexed = self._items.get((key_id, item_id)) if item_id is not None else None
            candidates += [
                None if indexed is None else self._effective_split(indexed),
                self._index_names.get((key_id, self.task_name(task_id)))
                if task_id is not None else None,
            ]
        if "test" in candidates:
            return "test"
        return "dev" if "dev" in candidates else None

    def assigned_test_anywhere(
        self, *, item_id: str | None = None, task_id: str | None = None,
    ) -> bool:
        """Whether any assignment key marks this item or canonical task TEST.

        Retrieval uses the stricter cross-key guard even where queue admission
        retains a secondary key's local split contract.
        """
        return (
            item_id is not None and _identity(item_id) in self._assigned_test_ids
        ) or (
            task_id is not None
            and (
                _identity(task_id) in self._assigned_test_ids
                or self.task_name(task_id) in self._assigned_test_ids
                or self.task_name(task_id) in self._assigned_test_names
            )
        )

    def get(self, dataset_id: str, item_id: str) -> CorpusItem:
        """Resolve native identifiers, raising KeyError for unregistered input."""
        item = self._items[_identity(dataset_id), _identity(item_id)]
        # Defensive copies keep callers from changing future admission metadata.
        return CorpusItem(
            item.dataset_id,
            item.item_id,
            self._effective_split(item),
            item.task_id,
            item.task_digest,
            item.source_path,
            json.loads(_json(item.metadata)),
            item.source_record_sha256,
        )

    def items(
        self, *, dataset_id: str | None = None, split: str | None = None
    ) -> tuple[CorpusItem, ...]:
        split = None if split is None else _split(split)
        dataset_id = None if dataset_id is None else _identity(dataset_id)
        return tuple(
            self.get(dataset, item_id)
            for (dataset, item_id), item in self._items.items()
            if (dataset_id is None or dataset == dataset_id)
            and (split is None or self._effective_split(item) == split)
        )

    def admit(
        self, dataset_id: str, item_id: str, *, frozen_eval_token: str | None = None
    ) -> CorpusItem:
        item = self.get(dataset_id, item_id)
        if item.split == "test" and not (
            self._token is not None
            and isinstance(frozen_eval_token, str)
            and hmac.compare_digest(self._token.encode(), frozen_eval_token.encode())
        ):
            raise SplitLockError(
                f"TEST split is sealed: {dataset_id}/{item_id}; explicit frozen-eval token required"
            )
        return item



