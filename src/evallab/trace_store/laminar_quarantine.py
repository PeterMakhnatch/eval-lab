"""Laminar traces that Eval Lab must never upload to, read from, or score again.

These 108 Laminar trace ids are our own control-journal copies, uploaded by mistake
(journal records projected as actor history). Laminar cannot delete them: the owner's
normal-session DELETE (UI/API, project ``a5586b6d``) answers HTTP 500 ``DELETE query is
not allowed for table default.spans because it has projections and setting
lightweight_mutation_projection_mode is not set`` (2026-10-28; nothing was deleted). The
trace ids are deterministic, so a later upload of the corrected actor history would land
on the same id and merge with the wrong spans. They are therefore quarantined here instead.

The list holds ids only (``uuid5`` of the cloud-DB trace id; no content, no labels). It
is the ``trace_ids`` of the owner-action packet ``laminar-p1-owner-action-20261028.json``
and equals the 108 ``present`` ids of the live read-only inventory taken 2026-10-28.

Every Laminar path goes through :func:`is_quarantined`: uploads refuse
(:func:`reject_quarantined`); fetches, queries, imports, exports and readers skip and count
(:func:`skip_quarantined`). Laminar Signal filters have no trace-id predicate (columns:
span names, status, tags, token count), and these copies carry no opt-in tag, so Signals
exclude them only through the required opt-in tag filter;
:func:`assert_not_signal_selectable` verifies that no copy carries that tag.

This module is intentionally independent of the Laminar client: the single exception is a
lazy import of the two id derivations, so ``evallab.laminar`` may import it.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable, Iterable
from functools import lru_cache
from typing import Any

QUARANTINED_REMOTE_IDS: frozenset[str] = frozenset({
    "02bfa31e-4b13-5829-ac7d-84f80054c3b0",
    "044a0c62-6f9d-55b9-adc8-1aa215df44ff",
    "06f8d32e-d79d-5cac-9ce9-293d81088949",
    "0756d308-ecd9-5726-afc2-f9543b36a4ca",
    "0b8773f5-450d-50c4-b756-c05a723bb10b",
    "0c7311c1-2d90-5c93-b3e6-43ba3a81e849",
    "0eab6417-37c3-5e54-911b-4beb9678477d",
    "12767795-8106-5ffe-959b-9928ab3294b7",
    "1e56fc30-cfbf-5d2d-8074-6b063351a10b",
    "22a7ae06-48c4-5301-9aef-1872ac228ee6",
    "23fac816-f9ff-5396-9722-fe70328d5c31",
    "250e7455-7820-5800-b7d9-c7d5d2916e5b",
    "26afaca5-913d-5610-b130-59504a766656",
    "26d1264d-1df7-5bb1-8487-0301411f2162",
    "2b00f4b0-024c-5ef1-a75f-f82432d39c46",
    "2df52b20-1d47-5e30-a562-34afb398e849",
    "32fd197a-a822-55dc-867b-6216ec07cba5",
    "33d5e1c1-3484-5689-a8b6-1e2200ecec64",
    "36ac9e76-ccb2-5310-8c62-a0891f7e0285",
    "37df3048-9884-5067-a3f2-b3d3b15dd162",
    "3d7462f9-17b5-56de-95f3-fed184170287",
    "3e29100d-2886-5788-895d-504e6330ecfa",
    "3ea0eda5-5b2f-5b14-9892-227d31558ea3",
    "427d6b6d-9bf6-5a83-a947-80e6809a303a",
    "4856a504-bb87-56b6-9ed3-dfba9079c1c1",
    "4e665f58-ebc8-50a7-8330-b8ff2497828e",
    "504680c9-db4a-58c4-9cee-c480da0e0013",
    "51289454-c17b-5978-afea-c6456d4ed999",
    "51ff8858-a67f-530e-a55e-2bc772f04f0c",
    "5582e19e-6eeb-57f2-8ccc-0f1d259e2fed",
    "558f3cfc-a630-5a0f-9fdd-21f5fe4eac50",
    "5694d746-df88-5c8c-818f-14c58534ddf4",
    "5a7e520a-90e4-50dd-9a44-f338745a6ae6",
    "5c4d2380-a943-57bd-a27b-0071e8203727",
    "5e2baef7-0e40-587d-86a3-26b8c04e8c60",
    "5fb30fef-4e7d-5209-a163-7bde3d308b12",
    "612ae51d-3a7a-53f0-9aa0-428d2179635e",
    "6534f962-07bf-5244-bcde-5c4c0ad4114d",
    "654eebec-aa98-5759-a74d-e1be1798a438",
    "660226ec-6910-58f7-882e-5ef9f956ae4e",
    "6613e087-9d7b-5782-a37b-05dbaa809e16",
    "6a1242c0-6563-5d19-9026-733c464024f6",
    "6d44a781-dc50-519f-9ec5-acf2a1c1cda1",
    "6f741539-3961-5d81-bcbb-58501ee25609",
    "72824a9f-1f59-5130-8225-d91f3e076ff3",
    "73288946-4acd-522a-9cc7-5856f9223a7f",
    "780f1973-ae12-57fc-ab27-5945ecea8d7e",
    "7c38f953-949a-542c-ae92-419866c73157",
    "813dee42-e276-513c-8fd1-e77af515c574",
    "831c84af-b2ee-5e8b-ade9-ebc423b7d745",
    "861cfbd8-8181-5db2-9f9d-0a2af37db924",
    "8697825d-b343-5770-a36a-ffc5d2683c90",
    "87ee6085-c7b9-5a35-ab13-a7cadf8dbe6d",
    "890af719-3e1d-5aa4-9b36-bc423f709896",
    "964c14c1-82aa-5d66-a577-6c7461dec133",
    "993eba8e-38f5-5c28-8d47-d201438f53db",
    "9b2af3e8-e4a9-5834-95f8-d135aa290bc7",
    "9c25ce75-2f88-5a4d-8b82-4fc1e3c5ba97",
    "9c5173a7-acfd-5cc4-93aa-160a17cd1ef1",
    "9c5da0aa-2b12-5ba2-bd06-b1651b6bf299",
    "9fe69d4d-553d-59b1-b560-54c5f4733fa2",
    "a06e26a4-f5f0-5bd9-9f31-613f2e8b95ca",
    "a2c58fbd-22b2-526b-a1c0-ace5ee99f265",
    "a4ace286-28ed-5162-b52b-1e4404b82ac0",
    "a5ab3953-e623-5fc7-9908-d1ad14d7df8b",
    "a7603089-7720-5c71-a73c-5dc65e9363cc",
    "a7c2596d-ef69-589a-980a-6b3996f8f17f",
    "ab7bd5f6-8bd5-55f0-846b-3745981e525a",
    "ac0aae9b-1b6b-505d-b797-0cac52bab6d0",
    "b4646ed4-f190-54c3-954c-d2b1232420c1",
    "b6a2bb39-bce3-52a2-ab04-da3d1a468343",
    "b6deeb7c-eb30-5bdb-a87d-ee4afd3b36c6",
    "b8774337-bc5b-5f1d-a88d-483152607894",
    "bbe7023a-d2f9-54ae-a20f-df557cab3d9c",
    "bc731c8f-7347-5cc4-9a64-aad2f59436f6",
    "be0e5adc-8906-5f79-ab3c-0aefb1aff2c1",
    "be5840ea-2259-5ccc-962a-36c8edc071ef",
    "bf602c8c-66ae-5616-9842-729b552aba66",
    "c088d10f-52ae-5cf8-86ff-a0db1aeac9fb",
    "c0ed001d-3064-56b3-9640-3e5e06ced62f",
    "c4ea51a8-0586-577c-b829-6a44cac6d8fa",
    "c500b761-eea7-5688-8ae4-a44be2c98af3",
    "ca44c771-4f4c-573d-868c-be38b99db847",
    "ca54ecb7-4621-5368-97a3-19d7d81036fb",
    "cac016d0-dcc6-524d-9eca-0d5d9c5125c6",
    "ce16a94b-07e0-5b6f-9fe1-3bc69930653f",
    "d24590eb-6638-5dbf-8e5c-145916fb0537",
    "d6500f8e-4016-52ce-b8f9-3df35e2ba3c4",
    "d9eb4bd4-e484-5025-8fe9-ab5dae1c8249",
    "dc52ddf8-d4c9-5af7-a37b-bdee90ff4fd2",
    "dcf0f72f-ed3c-5c4d-9c02-32993d26f6d2",
    "dfdd3a45-e8f0-58c2-bfba-854a07802298",
    "e4a6006a-9630-5184-adc7-16de26332e0b",
    "e672e316-ac80-5428-8e38-8c420a72825f",
    "e964fda7-9832-50fc-9e40-3e006629641b",
    "eb4d7c8a-03bc-5edf-9b8f-ac656971947f",
    "ec5e3205-c86a-51da-92e1-0e8fd07b1095",
    "ed0b87a7-07eb-586b-822b-da55af4fff7e",
    "f1a0317d-9468-57ec-ba7c-5ded6855e9e3",
    "f27efcf7-3a33-5571-b46a-e2331215f337",
    "f57a1890-a713-584f-8e96-10fec79ec292",
    "f5bacb8b-c6dd-5ff1-9d00-a863f291e6b4",
    "f65f40ed-7e53-5ea1-866f-d96b4a3bcd04",
    "f84fc545-3c99-5a2f-a650-b524bf9e4781",
    "f86ea9e0-cd42-5083-8d42-ae5dc589011b",
    "fab7a1ee-e73c-5d32-9f75-21078ef31ab3",
    "fb31eed7-fba9-590e-8663-b4c2d8207082",
    "feb184e0-d86c-5a26-a9b7-0e619d2753ae",
})


class QuarantinedTrace(ValueError):
    """The trace is a quarantined Laminar copy; nothing about it may reach Laminar."""

    def __init__(self, identifier: str) -> None:
        self.identifier = identifier
        super().__init__(f"Trace is quarantined in Laminar (undeletable copy): {identifier}")


@lru_cache(maxsize=65536)
def is_quarantined(identifier: str) -> bool:
    """Whether ``identifier`` names a quarantined Laminar trace.

    ``identifier`` may be the Laminar trace id (dashed UUID or 32-hex OTLP form), the cloud-DB
    trace id the trace store derives it from (``trace_store.core.trace_uuid``), or a Harbor
    trial name the watch exporter derives it from (``evallab.laminar.laminar_trace_uuid``).
    """
    from evallab.laminar import laminar_trace_uuid
    from evallab.trace_store.core import trace_uuid

    text = identifier.strip()
    candidates = {text.lower(), trace_uuid(text), laminar_trace_uuid(text)}
    with contextlib.suppress(ValueError):
        candidates.add(str(uuid.UUID(text)))
    return not candidates.isdisjoint(QUARANTINED_REMOTE_IDS)


def reject_quarantined(identifier: str) -> None:
    """Refuse an upload: raise :class:`QuarantinedTrace` for a quarantined trace."""
    if is_quarantined(identifier):
        raise QuarantinedTrace(identifier)


def skip_quarantined[T](
    items: Iterable[T], key: Callable[[T], str | None],
) -> tuple[list[T], list[T]]:
    """``(kept, skipped)``: split off items whose ``key`` is quarantined; ``None`` keys stay.

    Callers count the skips with ``len(skipped)``.
    """
    kept: list[T] = []
    skipped: list[T] = []
    for item in items:
        identifier = key(item)
        if identifier is not None and is_quarantined(identifier):
            skipped.append(item)
        else:
            kept.append(item)
    return kept, skipped


def assert_not_signal_selectable(
    run_sql: Callable[[str, dict[str, Any]], list[dict[str, Any]]], *, opt_in_tag: str,
) -> None:
    """Refuse when a quarantined copy carries the tag a Signal filter requires.

    A Laminar Signal filter cannot name a trace id, so the opt-in tag is the only thing keeping
    these copies out of a Signal. ``run_sql(query, parameters)`` is a read-only project SQL call.
    """
    rows = run_sql(
        "SELECT DISTINCT trace_id FROM spans"
        " WHERE trace_id IN {ids:Array(UUID)} AND has(tags, {tag:String})",
        {"ids": sorted(QUARANTINED_REMOTE_IDS), "tag": opt_in_tag},
    )
    if rows:
        raise QuarantinedTrace(f"{len(rows)} quarantined trace(s) carry the Signal opt-in tag")
