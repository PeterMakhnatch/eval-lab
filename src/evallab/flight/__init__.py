"""Flight recorder: invisible per-trial kernel/egress/model timeline.

The observer runs in a separate privileged container in the Docker host PID
namespace. It reads the trial through ``/proc/<pid>/root`` and kernel
tracepoints; it writes only to a host directory mounted into the observer.
The trial container receives no mount, file, env var, process, or network
change on the locked tier.
"""

from __future__ import annotations

FLIGHT_SCHEMA_VERSION = 1
