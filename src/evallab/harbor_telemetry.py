"""Opt every Harbor child process out of Harbor's PostHog usage telemetry.

Harbor sends ``harbor.job_finished`` (job id, agents, models, token usage, cost,
duration and reward) unless ``HARBOR_TELEMETRY=off``
(https://docs.harborframework.com/telemetry/telemetry.md). Every eval-lab path
that starts a ``harbor`` child builds its environment through this module or
through ``execution_contracts.subscription_environment``, which uses it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

HARBOR_TELEMETRY_ENV = "HARBOR_TELEMETRY"
HARBOR_TELEMETRY_OFF = "off"


def harbor_child_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy of ``environment`` (default: ``os.environ``) with Harbor telemetry off."""
    child = dict(os.environ if environment is None else environment)
    child[HARBOR_TELEMETRY_ENV] = HARBOR_TELEMETRY_OFF
    return child
