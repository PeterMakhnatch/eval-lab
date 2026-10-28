"""Shared corpus admission, durable production workers, live control plane, and outbox relay."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from evallab.production.asset_exporter import (
        AssetExportError,
        VerifiedAssetExporter,
        export_training_bundle,
    )
    from evallab.production.cloud_trace_db import (
        CloudTraceRetriever,
        RetrievedExample,
        RetrievedTraceExample,
        admitted_cloud_task_ids,
        format_retrieval_prompt_block,
        protected_task_ids,
    )
    from evallab.production.control_plane import (
        app_from_env,
        create_app,
    )
    from evallab.production.corpus_queue import (
        ContinuousWorkQueue,
        CorpusRegistry,
    )
    from evallab.production.monitor_worker import (
        MisalignmentLiveWorker,
    )
    from evallab.production.outbox_relay import (
        DrainReport,
        HttpGatewayTransport,
        LocalJetStreamAdapter,
        NatsTcpTransport,
        OutboxRelay,
        OutboxRelayError,
        PermanentPublishError,
        PoisonQuarantine,
        PoisonRecord,
        PubAck,
        StoredMessage,
        TransientBrokerError,
    )
    from evallab.production.seeker_worker import (
        RewardSeekerLiveWorker,
        build_hackbook_probe_candidate,
        hackbook_probe_template_catalog,
    )
    from evallab.production.supervisor import (
        ProductionSupervisor,
    )
    from evallab.production.tq_worker import (
        TaskQualityLiveWorker,
    )

# Resolved on first attribute access (PEP 562), so importing one ``evallab.production.*``
# submodule does not import the whole worker stack (about half a second per process).
_EXPORTS = {
    "AssetExportError": "evallab.production.asset_exporter",
    "CloudTraceRetriever": "evallab.production.cloud_trace_db",
    "ContinuousWorkQueue": "evallab.production.corpus_queue",
    "CorpusRegistry": "evallab.production.corpus_queue",
    "DrainReport": "evallab.production.outbox_relay",
    "HttpGatewayTransport": "evallab.production.outbox_relay",
    "LocalJetStreamAdapter": "evallab.production.outbox_relay",
    "MisalignmentLiveWorker": "evallab.production.monitor_worker",
    "NatsTcpTransport": "evallab.production.outbox_relay",
    "OutboxRelay": "evallab.production.outbox_relay",
    "OutboxRelayError": "evallab.production.outbox_relay",
    "PermanentPublishError": "evallab.production.outbox_relay",
    "PoisonQuarantine": "evallab.production.outbox_relay",
    "PoisonRecord": "evallab.production.outbox_relay",
    "ProductionSupervisor": "evallab.production.supervisor",
    "PubAck": "evallab.production.outbox_relay",
    "RetrievedExample": "evallab.production.cloud_trace_db",
    "RetrievedTraceExample": "evallab.production.cloud_trace_db",
    "RewardSeekerLiveWorker": "evallab.production.seeker_worker",
    "StoredMessage": "evallab.production.outbox_relay",
    "TaskQualityLiveWorker": "evallab.production.tq_worker",
    "TransientBrokerError": "evallab.production.outbox_relay",
    "VerifiedAssetExporter": "evallab.production.asset_exporter",
    "admitted_cloud_task_ids": "evallab.production.cloud_trace_db",
    "app_from_env": "evallab.production.control_plane",
    "build_hackbook_probe_candidate": "evallab.production.seeker_worker",
    "create_app": "evallab.production.control_plane",
    "export_training_bundle": "evallab.production.asset_exporter",
    "format_retrieval_prompt_block": "evallab.production.cloud_trace_db",
    "hackbook_probe_template_catalog": "evallab.production.seeker_worker",
    "protected_task_ids": "evallab.production.cloud_trace_db",
}

__all__ = [
    "AssetExportError",
    "CloudTraceRetriever",
    "ContinuousWorkQueue",
    "CorpusRegistry",
    "DrainReport",
    "HttpGatewayTransport",
    "LocalJetStreamAdapter",
    "MisalignmentLiveWorker",
    "NatsTcpTransport",
    "OutboxRelay",
    "OutboxRelayError",
    "PermanentPublishError",
    "PoisonQuarantine",
    "PoisonRecord",
    "ProductionSupervisor",
    "PubAck",
    "RetrievedExample",
    "RetrievedTraceExample",
    "RewardSeekerLiveWorker",
    "StoredMessage",
    "TaskQualityLiveWorker",
    "TransientBrokerError",
    "VerifiedAssetExporter",
    "admitted_cloud_task_ids",
    "app_from_env",
    "build_hackbook_probe_candidate",
    "create_app",
    "export_training_bundle",
    "format_retrieval_prompt_block",
    "hackbook_probe_template_catalog",
    "protected_task_ids",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_EXPORTS})
