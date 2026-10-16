"""Modal verifier environment for paid snapshot regrades (``harbor trial regrade``).

``harbor trial regrade`` takes ``--env`` but no ``--environment-kwarg``, so the
two settings every lab Modal run passes as kwargs
(:func:`evallab.execution_contracts.build_command`) are fixed by this class:

- ``persistent_env`` PATH pin (:data:`evallab.execution_contracts.MODAL_PATH_PIN`),
  so the verifier's setup runs under the image's /usr/bin interpreter, as on Docker;
- a bounded sandbox lifetime. Harbor's default is a billable 24 hours, which a
  dead controller would otherwise leave running.

Import through ``evallab.harbor_modal:RegradeModalEnvironment``; the regrade
path admits it only under a :class:`evallab.regrade.CloudSpendGate`.
"""

from __future__ import annotations

from typing import Any

from harbor.environments.modal import ModalEnvironment  # ty: ignore[unresolved-import]

from evallab.execution_contracts import MODAL_PATH_PIN

#: Longest a regrade's verifier sandbox may live: the MiMo verifier timeout
#: (2,100 s) plus artifact upload and teardown margin.
REGRADE_SANDBOX_TIMEOUT_SECS = 3600


class RegradeModalEnvironment(ModalEnvironment):
    """Harbor's native Modal environment with the lab's PATH pin and lifetime bound."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["sandbox_timeout_secs"] = min(
            int(kwargs.get("sandbox_timeout_secs", REGRADE_SANDBOX_TIMEOUT_SECS)),
            REGRADE_SANDBOX_TIMEOUT_SECS,
        )
        kwargs["persistent_env"] = {**(kwargs.get("persistent_env") or {}), "PATH": MODAL_PATH_PIN}
        super().__init__(*args, **kwargs)
