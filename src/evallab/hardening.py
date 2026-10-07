"""Hardening transforms for MiMo task packages (HAR-194).

Each package transform is digest-bound through ``derive_task`` and fail-closed:
a failed check stops setup, with no hide-only or network-open fallback.

* ``strip-future-history@1`` removes future git objects (HAR-177).
* ``purge-installed-copies@1`` removes ``build/``, project egg-info and
  site-packages copies, then reinstalls the base tree editable and offline.
  Confirmed targets are 001269 and 002308. HAR-185 confirmed 0 of 100 sampled
  images, so this is not applied to the rest of the corpus.
* ``separate-verifier@1`` runs hidden tests in a second container where Harbor
  0.24 supports it (HAR-169: oracle pass and nop fail on 001269, 000905, 002391).

A verifier that passes only with egress open is not a package transform.
Allow-listing the verifier is rejected: Harbor drops the no-network overlay
for that container, so hidden tests would sit on a network. A recorded-response
mock is rejected: no response bodies were captured, and the known case is live
market data. The default is discard.
"""

from __future__ import annotations

from evallab.purge_installed_copies import TRANSFORM_ID as PURGE_ID
from evallab.purge_installed_copies import derive_purge_installed_copies
from evallab.separate_verifier import TRANSFORM_ID as SEPARATE_ID
from evallab.separate_verifier import derive_separate_verifier
from evallab.strip_future_history import TRANSFORM_ID as STRIP_ID
from evallab.strip_future_history import derive_strip_future_history

TRANSFORMS = (STRIP_ID, PURGE_ID, SEPARATE_ID)
NETWORK_LABEL = "oracle:fail-network"
NETWORK_DEFAULT = "discard"
#: Installed-copy leaks confirmed by reading the image, not by a sample guess.
CONFIRMED_PURGE = (
    "format-code-task-001269",
    "format-code-task-002308",
)

__all__ = [
    "CONFIRMED_PURGE",
    "NETWORK_DEFAULT",
    "NETWORK_LABEL",
    "PURGE_ID",
    "SEPARATE_ID",
    "STRIP_ID",
    "TRANSFORMS",
    "derive_purge_installed_copies",
    "derive_separate_verifier",
    "derive_strip_future_history",
    "network_discard_reason",
]


def network_discard_reason(evidence: str) -> str:
    """One-line discard reason. The evidence path is the deciding input."""
    if not evidence or evidence.startswith("http"):
        raise ValueError(f"network discard needs a local evidence path, got {evidence!r}")
    return (
        f"verifier passes only with egress open ({evidence}:{NETWORK_LABEL}); "
        "allow-list rejected because a networked verifier can exfiltrate hidden inputs; "
        "recorded mock rejected because no response bodies were captured"
    )
