"""Failure-fact critic for the open QA harness (spec item 8, G9/G12).

Reads retained ``qa env audit`` sessions, attributes each session that did not end in a
confirmed finding to a harness-level failure class from recorded facts only, clusters the
sessions by cause, and *proposes* one-line facts for a human to accept into
``qa/facts/<family>.md``. No model runs; nothing is written to ``facts/`` automatically.

Failure classes (recorded status and observations, never model text):

* ``parse``       - the session stopped on ``invalid_action``.
* ``driver_error``- ``model_error``, ``harness_error`` or ``quota_stop``.
* ``no_finish``   - a time, token or tool budget ran out before a final.
* ``unmeasured``  - the session finished but every runtime-tool call was unmeasured.
* ``source_only`` - the session finished without trying execution.

``facts_for`` is the interface the lead packet (spec 4c) uses to load the accepted facts for a
task; the failed tasks of a cluster are listed DEV-only (split guard) so a fact can be tested by
re-running exactly the tasks it was written for.

    evallab qa env critic --audit-root DIR --out DIR [--min-sessions 2]
"""

from __future__ import annotations



FAILURE_CLASSES = ("parse", "driver_error", "no_finish", "unmeasured", "source_only")
#: Fact lines are ``- <fact> -- evidence: <where it was measured>``; facts name their family.






#: Causes that come from the host or the harness, not from the task: a prompt fact cannot fix them.












# -- accepted facts --------------------------------------------------------------------






# -- CLI -------------------------------------------------------------------------------




