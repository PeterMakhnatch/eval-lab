"""Hardening transforms for MiMo task packages (HAR-194).

Each package transform is digest-bound through ``derive_task`` and fail-closed:
a failed check stops setup, with no hide-only or network-open fallback.

* ``strip-future-history@1`` removes future git objects (HAR-177).
* ``purge-installed-copies@1`` removes ``build/``, project egg-info and
  site-packages copies, then reinstalls the base tree editable and offline.
  Confirmed targets are 001269 and 002308. HAR-185 confirmed 0 of 100 sampled
  images, so this is not applied to the rest of the corpus.
* ``purge-build-caches@1`` removes regenerable build/module caches
  (``__pycache__``, ``.pytest_cache``, mypy/ruff/hypothesis caches, node
  ``.cache``/``.vitest``) that can carry the fixed tree (Vals rung 5).
  Dependency dirs and project copies stay: offline graders need the former
  and the latter belong to ``purge-installed-copies@1``.
* ``purge-build-caches@2`` is the language-aware superset (vals-routes-v2):
  the @1 sweep plus this project's own entries in shared caches (pip wheels,
  go module/build cache, cargo target/registry, maven artifacts, gradle
  project cache, npm/yarn/pnpm package entries), each fail-closed. It
  supersedes @1 and refuses @1 parents; chain adoption is the clean-set
  owner's call.
* ``purge-build-caches@3`` adds node gitignored build outputs (``lib/``,
  ``dist/``, ``build/``, ``out/``): delete when grading tests import from
  ``src/``, else rebuild from the base tree via the package build/compile
  script and fail setup when neither is safe. It supersedes @2 and refuses
  @1/@2 parents; the clean chain is strip → purge → @3 → mtime.
* ``separate-verifier@1`` runs hidden tests in a second container where Harbor
  0.24 supports it (HAR-169: oracle pass and nop fail on 001269, 000905, 002391).
  @1 restores the agent's workspace, ``.git`` and base sha into the verifier,
  so container-root tamper (tracked conftest, sitecustomize, PATH shadowing,
  background writers, in-source exit/pytest hooks) still grades 1.
* ``separate-verifier@2`` ("patch-only verifier") grades only the agent's
  repo-file patch: the verifier reruns the bundled clean setup itself,
  computes BASE there, diffs with its own git under the BASE tree's ignore
  rules, drops test-infra paths, gates tamper signatures, then applies the
  hidden tests and grades with a structured junit check.
* ``terminal-guard-extend@1`` snapshots the terminal interpreter hook surface
  (``sitecustomize`` / ``usercustomize`` / ``*.pth`` / ``pytest11`` entry
  points) at end of setup, quarantines additions and fails closed on anchor
  tamper at grade time, scrubs hook-loading env, pins pytest to the
  setup-time plugins with autoload off, and runs the guard itself without
  site imports (candidate-0109: planted skip-all plugin flipped reward 0 to
  1; no terminal grader needs a third-party plugin, so honest grading is
  unchanged).
* ``general-pinned-backup@1`` takes consistent pre-agent SQLite backups;
  50 tasks contain references; the retained census measures item-level effects.
* ``general-strict-answer@1`` keeps a missing ``answer.md`` missing.
* ``general-nop-zero-weight@1`` removes reward credit from measured pristine
  rule passes while retaining execution and failure reporting.
* ``webdev-temp0-pin@1`` binds temperature/model/provider/revision and masks
  unversioned judge deployments or replies. A version-aware endpoint is staged.
* ``webdev-structural-gate@1`` rejects blank/off-brief/overflowing rendered DOM;
  missing browser measurements mask. Oracle false-rejection validation is staged.
* ``webdev-brief-explicit@1`` sends full long briefs with hash/length provenance.

A verifier that passes only with egress open is not a package transform.
Allow-listing the verifier is rejected: Harbor drops the no-network overlay
for that container, so hidden tests would sit on a network. A recorded-response
mock is rejected: no response bodies were captured, and the known case is live
market data. The default is discard.
"""

from __future__ import annotations

from evallab.general_nop_gate import TRANSFORM_ID as GENERAL_NOP_GATE_ID
from evallab.general_nop_gate import derive_general_nop_gate
from evallab.general_pinned_backup import TRANSFORM_ID as GENERAL_PINNED_BACKUP_ID
from evallab.general_pinned_backup import derive_general_pinned_backup
from evallab.general_strict_answer import TRANSFORM_ID as GENERAL_STRICT_ANSWER_ID
from evallab.general_strict_answer import derive_general_strict_answer
from evallab.mtime_normalize import TRANSFORM_ID as MTIME_ID
from evallab.mtime_normalize import derive_mtime_normalize
from evallab.purge_build_caches import TRANSFORM_ID as CACHE_ID
from evallab.purge_build_caches import TRANSFORM_ID_V2 as CACHE_V2_ID
from evallab.purge_build_caches import TRANSFORM_ID_V3 as CACHE_V3_ID
from evallab.purge_build_caches import (
    derive_purge_build_caches,
    derive_purge_build_caches_v2,
    derive_purge_build_caches_v3,
)
from evallab.purge_installed_copies import TRANSFORM_ID as PURGE_ID
from evallab.purge_installed_copies import derive_purge_installed_copies
from evallab.separate_verifier import TRANSFORM_ID as SEPARATE_ID
from evallab.separate_verifier import TRANSFORM_ID_V2 as SEPARATE_V2_ID
from evallab.separate_verifier import derive_separate_verifier, derive_separate_verifier_v2
from evallab.strip_future_history import TRANSFORM_ID as STRIP_ID
from evallab.strip_future_history import derive_strip_future_history
from evallab.terminal_guard import TRANSFORM_ID as TERMINAL_GUARD_ID
from evallab.terminal_guard import derive_terminal_guard, derive_terminal_prefetch
from evallab.webdev_brief_explicit import TRANSFORM_ID as WEBDEV_BRIEF_EXPLICIT_ID
from evallab.webdev_brief_explicit import derive_webdev_brief_explicit
from evallab.webdev_structural_gate import TRANSFORM_ID as WEBDEV_STRUCTURAL_GATE_ID
from evallab.webdev_structural_gate import derive_webdev_structural_gate
from evallab.webdev_temp0_pin import TRANSFORM_ID as WEBDEV_TEMP0_PIN_ID
from evallab.webdev_temp0_pin import derive_webdev_temp0_pin

TRANSFORMS = (
    STRIP_ID,
    PURGE_ID,
    CACHE_ID,
    CACHE_V2_ID,
    CACHE_V3_ID,
    SEPARATE_ID,
    SEPARATE_V2_ID,
    MTIME_ID,
    TERMINAL_GUARD_ID,
    GENERAL_PINNED_BACKUP_ID,
    GENERAL_STRICT_ANSWER_ID,
    GENERAL_NOP_GATE_ID,
    WEBDEV_TEMP0_PIN_ID,
    WEBDEV_STRUCTURAL_GATE_ID,
    WEBDEV_BRIEF_EXPLICIT_ID,
)
NETWORK_LABEL = "oracle:fail-network"
NETWORK_DEFAULT = "discard"
#: Installed-copy leaks confirmed by reading the image, not by a sample guess.
CONFIRMED_PURGE = (
    "format-code-task-001269",
    "format-code-task-002308",
)

__all__ = [
    "CACHE_ID",
    "CACHE_V2_ID",
    "CACHE_V3_ID",
    "CONFIRMED_PURGE",
    "GENERAL_NOP_GATE_ID",
    "GENERAL_PINNED_BACKUP_ID",
    "GENERAL_STRICT_ANSWER_ID",
    "MTIME_ID",
    "NETWORK_DEFAULT",
    "NETWORK_LABEL",
    "PURGE_ID",
    "SEPARATE_ID",
    "SEPARATE_V2_ID",
    "STRIP_ID",
    "TERMINAL_GUARD_ID",
    "TRANSFORMS",
    "WEBDEV_BRIEF_EXPLICIT_ID",
    "WEBDEV_STRUCTURAL_GATE_ID",
    "WEBDEV_TEMP0_PIN_ID",
    "derive_general_nop_gate",
    "derive_general_pinned_backup",
    "derive_general_strict_answer",
    "derive_mtime_normalize",
    "derive_purge_build_caches",
    "derive_purge_build_caches_v2",
    "derive_purge_build_caches_v3",
    "derive_purge_installed_copies",
    "derive_separate_verifier",
    "derive_separate_verifier_v2",
    "derive_strip_future_history",
    "derive_terminal_guard",
    "derive_terminal_prefetch",
    "derive_webdev_brief_explicit",
    "derive_webdev_structural_gate",
    "derive_webdev_temp0_pin",
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
