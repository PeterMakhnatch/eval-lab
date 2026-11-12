"""Codex native rollout reader: child agent threads Harbor's converter drops.

Harbor 0.21's codex converter (``harbor/agents/installed/codex.py``,
``_convert_events_to_trajectory``) converts exactly one rollout file -- the
lexically greatest ``rollout-*.jsonl`` in the most recent
``agent/sessions/<YYYY>/<MM>/<DD>/`` directory -- into the ATIF trajectory,
and it never emits ``subagent_trajectory_ref`` entries: a ``spawn_agent`` tool
call converts like any other function call. Child threads therefore vanish
from the trajectory, and the run report can only say ``delegations_only``.

This module re-reads the retained native rollouts so those children (and
their token spend) become visible again. Record shapes below are verified
against real retained rollouts (codex CLI 0.147.0, e.g.
``runs/canary-event-summary-codex-20260815/.../agent/sessions/2026/08/15/
rollout-*.jsonl``; re-verified against CLI 0.162.1 on 2026-10-13 — same
``session_meta`` / ``turn_context`` / ``response_item`` / ``event_msg``
``token_count`` (``info.total_token_usage`` + ``last_token_usage``) and
``rate_limits`` (``used_percent`` / ``window_minutes`` / ``resets_at``)
shapes; the newer ``world_state`` and ``token_usage_record`` line types are
tolerated (ignored, never an error)), Harbor 0.21's converter semantics, and
the codex source (openai/codex commit 25270df2615eb4da5b9d4a9a392226933fb096c5,
2026-09-26, shallow clone at /Users/petermakhnatch/Developer/lab-extras/upstream/codex):

- one JSON object per line; line ``type`` is ``session_meta``,
  ``response_item``, ``event_msg``, ``turn_context`` (or ``world_state``);
- ``session_meta.payload`` carries the thread id (``id``; ``session_id`` is
  the *root* thread's id), ``cli_version``, ``originator``, ``source`` and
  ``thread_source``;
- ``source`` serializes codex's ``SessionSource`` enum
  (``codex-rs/protocol/src/protocol.rs``): top-level threads are plain
  strings (``"exec"``, ``"cli"``, ...); a spawned child is
  ``{"subagent": {"thread_spawn": {"parent_thread_id": "<uuid>",
  "depth": N, "agent_path": ..., "agent_nickname": ...,
  "agent_role": ...}}}`` (externally tagged serde; verified wire example in
  ``codex-rs/cli/src/doctor/thread_inventory.rs::source_category``).
  ``thread_source`` is ``"user"`` for top-level threads, ``"subagent"`` for
  spawned children (``ThreadSource``, same protocol file);
- a file counts as a child thread ONLY for a ``thread_spawn`` source with a
  ``parent_thread_id`` -- the real parent edge (``SessionSource::
  parent_thread_id``, used by ``core/src/hook_runtime.rs`` for
  SubagentStop). Any other extra rollout (retry, second ``codex exec``,
  resume, review/compact threads) is reported under ``other_threads``,
  never as delegated spend;
- tool calls are ``response_item`` payloads of type ``function_call`` /
  ``custom_tool_call`` paired with ``function_call_output`` /
  ``custom_tool_call_output`` by ``call_id``;
- every model API call is closed by an ``event_msg`` of type
  ``token_count`` whose ``info.last_token_usage`` meters that call and
  ``info.total_token_usage`` snapshots the thread's cumulative totals
  (``input_tokens``, ``cached_input_tokens``, ``output_tokens``,
  ``reasoning_output_tokens``); the payload carries no cost field;
- ``turn_context.payload.model`` names the thread's model;
- child threads persist as sibling ``rollout-<timestamp>-<thread-id>.jsonl``
  files in the same session date directory (the filename pattern is
  Harbor's ``_ROLLOUT_FILENAME_RE``).

Rules this reader keeps:

- Deterministic: sorted file order, no wall clock, no network, no model
  calls. Files stream line by line; only short spawn-related outputs are
  retained (capped), so large ``exec`` outputs never inflate memory.
- Missing data stays ``None`` with a reason -- never a fabricated zero. In
  particular ``cost_usd`` is always ``None``: codex ``token_count`` events
  carry no cost, and inventing one from a price table is another slice's
  job (HAR-76 item 3).
- Child token totals come from the cumulative ``total_token_usage``
  snapshots (maximum), never from summing per-call ``last_token_usage``:
  summing would double-count retried or re-reported calls. The caller
  (``run_report._subagents``) reports child tokens per child only; parent
  run totals are untouched, so nothing is double-counted into the run.
- Parent linkage is the ``parent_thread_id`` edge from the child's own
  ``session_meta``. The ``spawn_agent``-call linkage (``spawn_call_id``) is
  kept ONLY for the source-proven shape: V1 spawn outputs are
  ``{"agent_id": "<child thread id>", "nickname": ...}``
  (``core/src/tools/handlers/multi_agents/spawn.rs``); V2 outputs carry
  only ``task_name``/``nickname``
  (``core/src/tools/handlers/multi_agents_v2/spawn.rs``) and can never
  link, so such children keep ``spawn_call_id None`` rather than a guess.
"""

from __future__ import annotations

import re

#: Native codex rollout filename pattern (Harbor 0.21 ``_ROLLOUT_FILENAME_RE``).
ROLLOUT_FILENAME_RE = re.compile(r"^rollout-(\d{4})-(\d{2})-(\d{2})T\S+\.jsonl$")

#: Where Harbor retains codex rollouts inside a trial directory.

#: Cap on retained output text per tool call (spawn outputs are short status
#: payloads; this keeps large ``exec`` outputs out of memory).


