"""Pure classifiers for the non-Python code-task census (no Docker, no network).

Every function here is deterministic and operates on in-memory text, so it is
unit-testable without images. The corpus runner lives in ``census.py``.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------- #
# Grading-download detectors: unconditional vs conditional fetches
# --------------------------------------------------------------------------- #

#: (tool-label, regex) for fetches that need the network at grade time.
#: Mirrors ``task_lint._NETWORK_INSTALL`` (pip/npm/go-install/curl) and extends
#: it to the non-Python graders named in the assignment: npm/go-mod/maven/
#: composer/bundler/cargo, plus the surrounding ecosystems (apt, julia, dart).
_DOWNLOAD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pip", re.compile(r"\bpip(?:3)?\s+install\b")),
    ("npm", re.compile(r"\bnpm\s+(?:install|ci|update)\b")),
    ("yarn/pnpm", re.compile(r"\b(?:yarn(?:\s+install)?|pnpm\s+install)\b")),
    ("go-mod", re.compile(r"\bgo\s+mod\s+download\b")),
    ("go-install", re.compile(r"\bgo\s+install\b")),
    ("go-get", re.compile(r"\bgo\s+get\b")),
    ("maven", re.compile(r"\bmvn\b")),
    ("gradle", re.compile(r"\bgradle\b")),
    ("composer", re.compile(r"\bcomposer\s+install\b")),
    ("bundler", re.compile(r"\bbundle\s+install\b")),
    ("cargo", re.compile(r"\bcargo\s+(?:build|test|fetch|update)\b")),
    ("apt", re.compile(r"\bapt-get\s+install\b")),
    ("curl-fetch", re.compile(r"\bcurl\s+[^\n]*?https?://")),
    ("julia-pkg", re.compile(r"Pkg\.(?:instantiate|add|update|build)")),
    ("dart-pub", re.compile(r"\bdart\s+pub\s+get\b")),
)

#: Line-level markers showing the fetch only runs when a cache is absent
#: (offline-safe when the image bake populated the cache).
_CONDITIONAL_MARKERS = (
    "if ",
    "[ !",
    "[ -",
    "[[ ",
    "|| true",
    "|| exit 0",
    "command -v",
    "which ",
)

#: Offline flags that make an otherwise-networked invocation hermetic.
_OFFLINE_FLAGS = (
    "--offline",
    "--frozen",
    "-o ",  # mvn -o (offline); matched as a token below
    "GOPROXY=off",
    "GOFLAGS=-mod=vendor",
    "--mod=vendor",
    "-mod=vendor",
    "--prefer-offline",
)


def _line_is_conditional(line: str) -> bool:
    stripped = line.strip()
    if any(marker in line for marker in _CONDITIONAL_MARKERS):
        return True
    # A shell guard continuing from the previous line (… && … / … || …).
    return bool(stripped.startswith(("&&", "||")))


def _line_is_offline(line: str) -> bool:
    tokens = line.replace("=", " ").split()
    if any(flag in line for flag in ("--offline", "--frozen", "GOPROXY=off", "--prefer-offline")):
        return True
    if "-mod=vendor" in line or "--mod=vendor" in line:
        return True
    # mvn -o as its own token (avoid matching "-o <file>" in cc commands).
    return bool("mvn" in line and "-o" in tokens)


def _deprefixed(line: str) -> str:
    """Strip one unified-diff ``+`` prefix so patch-embedded shell parses."""
    stripped = line.strip()
    if stripped.startswith("+") and not stripped.startswith("+++"):
        return stripped[1:].strip()
    return stripped


_IF_OPEN = re.compile(r"(?<![\w])if(?![\w]).*\bthen\s*$|(?<![\w])if(?![\w]).*;\s*then(\s|;|$)")


def classify_downloads(shell_text: str) -> list[dict[str, str]]:
    """Find network fetches in grading shell text.

    Returns one row per match: ``tool``, ``line`` (stripped), and ``mode`` —
    ``unconditional`` (breaks under the egress lock), ``conditional`` (guarded
    by a cache check: a same-line ``if``/``||``/``command -v`` marker or an
    enclosing multi-line ``if…fi`` block such as
    ``if [ ! -x node_modules/.bin/tap ]; then`` / ``npm install`` / ``fi`` —
    offline-safe when baked), or ``offline-flag`` (invocation carries a
    hermetic flag such as ``GOPROXY=off`` / ``mvn -o``).
    """
    rows: list[dict[str, str]] = []
    if_depth = 0
    for raw_line in shell_text.splitlines():
        body = _deprefixed(raw_line)
        if not body or body.startswith("#"):
            continue
        # A new embedded script or diff file resets guard context: an unclosed
        # guard in one blob must not mark the next blob conditional.
        if body.startswith("#!") or body.startswith("diff --git"):
            if_depth = 0
            continue
        if body == "then" or _IF_OPEN.search(body):
            if_depth += 1
        if body == "fi" or body.startswith("fi ") or body.startswith("fi;"):
            if_depth = max(0, if_depth - 1)
            continue
        for tool, pattern in _DOWNLOAD_PATTERNS:
            if not pattern.search(body):
                continue
            if _line_is_offline(body):
                mode = "offline-flag"
            elif if_depth > 0 or _line_is_conditional(body):
                mode = "conditional"
            else:
                mode = "unconditional"
            rows.append({"tool": tool, "mode": mode, "line": body[:220]})
    return rows


# --------------------------------------------------------------------------- #
# Rust toolchain-PATH heuristic
# --------------------------------------------------------------------------- #

_CARGO_USE = re.compile(r"\bcargo\b")


def rust_path_status(grading_text: str, setup_text: str) -> str:
    """Static verdict for the Rust PATH-missing class (cf. 000681 MEASURED).

    ``ok`` — grading does not invoke cargo, or setup puts a cargo bin dir on
    PATH (``cargo/bin`` reference, rustup init, or ``CARGO_HOME``/``RUSTUP_HOME``
    wiring). ``path-missing-candidate`` — cargo is invoked at grade time but
    nothing in setup references a cargo location. ``not-rust`` — no cargo use.
    """
    if not _CARGO_USE.search(grading_text):
        return "not-rust"
    setup_has_cargo = (
        "cargo/bin" in setup_text
        or "rustup" in setup_text
        or "CARGO_HOME" in setup_text
        or "RUSTUP_HOME" in setup_text
        or ".cargo/env" in setup_text
    )
    return "ok" if setup_has_cargo else "path-missing-candidate"


# --------------------------------------------------------------------------- #
# Unknown-130 runner buckets
# --------------------------------------------------------------------------- #

#: (bucket, regex) over the grading text. Order matters: first match wins, so
#: specific runners precede the generic lint/python fallbacks. ``usercase``
#: keys on the scenario-test marker (CWD /workspace/repo tasks whose command
#: execs the bundled usecase script); ``shell-compare`` keys on bespoke bash
#: graders (``|| fail "<label>…`` output/file comparisons with no named
#: runner) and sits just above ``custom``.
_UNKNOWN_RUNNERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("julia", re.compile(r"\bjulia\b")),
    ("forge", re.compile(r"\bforge\s+test\b")),
    ("wake", re.compile(r"\bwake\b")),
    ("nim", re.compile(r"\bnim\s+(?:c|r|compile|run|test)\b")),
    ("mocha", re.compile(r"\bmocha\b")),
    ("jest", re.compile(r"\bjest\b")),
    ("vitest", re.compile(r"\bvitest\b")),
    ("tap", re.compile(r"\btap\b")),
    ("ava", re.compile(r"\bava\b")),
    ("tape", re.compile(r"\btape\b")),
    ("jasmine", re.compile(r"\bjasmine\b")),
    ("karma", re.compile(r"\bkarma\b")),
    ("nodeunit", re.compile(r"\bnodeunit\b")),
    ("tsx", re.compile(r"\b(?:tsx|ts-node)\b")),
    ("cargo-test", re.compile(r"\bcargo\s+(?:test|t)\b")),
    ("ansible-test", re.compile(r"\bansible-test\b")),
    ("shelltest", re.compile(r"\bshelltest\b")),
    ("npm-run", re.compile(r"\bnpm\s+run\b")),
    ("usercase", re.compile(r"usercase-test-coderl|usecase\.sh")),
    ("lint-mypy", re.compile(r"\bmypy\b")),
    ("lint-ruff", re.compile(r"\bruff\b")),
    ("lint-eslint", re.compile(r"\beslint\b")),
    ("lint-tsc", re.compile(r"\btsc\b")),
    ("lint-tsd", re.compile(r"\btsd\b")),
    ("pytest", re.compile(r"\bpytest\b")),
    ("python", re.compile(r"\bpython[23]?\b")),
    ("sbt", re.compile(r"\bsbt\b")),
    ("gradle-runner", re.compile(r"\bgradle\w*\b")),
    ("dart", re.compile(r"\bdart\b")),
    ("bats", re.compile(r"\bbats\b")),
    ("make", re.compile(r"\b(?:make|cmake|ctest)\b")),
    ("go-test", re.compile(r"\bgo\s+(?:test|build|run)\b")),
    ("node", re.compile(r"\bnode\b")),
    ("solidity-other", re.compile(r"\b(?:solc|hardhat|anvil|cast|truffle|brownie|ape)\b")),
    ("shell-compare", re.compile(r'\|\| fail "')),
)


def bucket_unknown_runner(grading_text: str) -> str:
    """Assign an Unknown-category task to one per-runner bucket."""
    for bucket, pattern in _UNKNOWN_RUNNERS:
        if pattern.search(grading_text):
            return bucket
    return "custom"


__all__ = [
    "bucket_unknown_runner",
    "classify_downloads",
    "rust_path_status",
]
