"""Recorded QA failure classes for native audit facts."""

from __future__ import annotations

FAILURE_CLASSES = ("parse", "driver_error", "no_finish", "unmeasured", "source_only")
#: Fact lines are ``- <fact> -- evidence: <where it was measured>``; facts name their family.


#: Causes that come from the host or the harness, not from the task: a prompt fact cannot fix them.


# -- accepted facts --------------------------------------------------------------------


# -- CLI -------------------------------------------------------------------------------


