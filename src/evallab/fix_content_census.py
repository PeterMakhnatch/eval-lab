"""Fix-content census for MiMo code tasks (Vals routes V2/V3/V4/V5/E1).

For one task image the census recovers the reference fix commit from the
leaked pre-setup git history (HAR-191 ``research/experiments/leak-oracle``
extractor logic: ``oracle_sweep.csv`` fix commits re-verified against the
image's own objects, full extractor run for tasks without a sweep record),
derives the post-fix blob SHAs and distinctive added lines for the changed
non-test files, then searches the whole container filesystem outside the
repo's ``.git`` for those contents: site-packages, build/dist/egg-info,
pip/npm/yarn/pnpm/go/cargo/maven/gradle caches, /tmp, /root, /opt. It also
checks ``.git`` for recoverable fix objects (V2/V4) and mtime clustering of
the fix-touched files (V3).

Two modes per task:

* ``published``: the setup the ledger runs (Python) or the snapshot adapter
  setup (other languages) -- the published FineEnvs-equivalent setup.
* ``clean``: the clean chain composed textually from the snapshot root
  setup (strip-future-history@1, purge-installed-copies@1 where applicable,
  purge-build-caches@2, mtime-normalize@1) with the same ``build_setup_sh``
  functions the derives use, so the bytes match a derived chain.

Any location found in ``clean`` mode is an open leak. Standalone probes
default to local Docker with ``--network none``. :func:`run_probe` also
supports ``backend="modal"`` for paid, known-patch census phases, using the
same pinned image, staged :data:`PROBE_SH`, setup payload, workdir and root.
The census forwards the package network contract via ``egress_lock``;
recovery/image archaeology stays Docker-only. Failed setup or incomplete
content scans never establish zero surviving locations.

Container work happens in :data:`PROBE_SH` (Docker: staged via bind mount,
results in a second mount; Modal: the same stage uploaded, outputs
downloaded); everything else here is host-side orchestration plus
pure helpers covered by unit tests.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Row fields for the census CSV.
CSV_FIELDS = (
    "task_id",
    "language",
    "image12",
    "mode",
    "setup_rc",
    "ready",
    "fix_sha",
    "fix_source",
    "fix_present_pre",
    "fix_present_post",
    "rev_count",
    "fsck_unreachable",
    "branches",
    "tags",
    "stash",
    "patterns",
    "patterns_raw",
    "hits_total",
    "hits_by_route",
    "blob_matches",
    "mtime_signal",
    "mtime_detail",
    "caches",
    "open_leak",
    "scan_rc",
    "scan_complete",
    "probe_rc",
    "notes",
)

#: Added lines this long (stripped) count as searchable patterns in the probe.
#: Floor 20 (not 15): 15-char lines coincide across unrelated packages
#: (measured: ``aliases: Set[str]`` in pydantic vs a pip-audit fix).
PATTERN_MIN_LEN = 20

#: Added lines this long (stripped) count as distinctive for verdicts.
DISTINCTIVE_MIN_LEN = 20

_DIFF_GIT_RE = re.compile(r"^diff --git a/(.*) b/(.*)$")

#: Test-path markers mirroring the HAR-191 extractor's TEST_PATH_RES
#: (research/experiments/leak-oracle/extract.py): paths a reference fix never
#: touches by definition.
_TEST_RES = (
    re.compile(r"(^|/)(tests?|testing|test_|_test|spec|specs|e2e)(/|$)"),
    re.compile(r"\.(spec|test)\.[a-z]+$"),
    re.compile(r"conftest\.py$"),
    re.compile(r"mimo_test_command\.sh$"),
    re.compile(r"test_commands\.json$"),
    re.compile(r"test\.patch$"),
)

_HARNESS_BASENAMES = frozenset(
    {"test_commands.json", "mimo_test_command.sh", "test.patch", "test_command.sh"}
)


def is_test_path(path: str) -> bool:
    """Whether ``path`` is a test/harness file (never a fix-content carrier)."""
    base = path.rsplit("/", 1)[-1]
    if base in _HARNESS_BASENAMES:
        return True
    return any(rx.search(path) for rx in _TEST_RES)


def non_test_files(files: list[str]) -> list[str]:
    """Repo-relative non-test files, deduplicated and sorted."""
    return sorted({f for f in files if f and not is_test_path(f)})


def parse_diff_added_lines(patch_text: str) -> dict[str, list[str]]:
    """Added lines per b-side file from a unified diff (``+`` prefix stripped)."""
    current: str | None = None
    added: dict[str, list[str]] = {}
    for line in patch_text.splitlines():
        m = _DIFF_GIT_RE.match(line)
        if m:
            current = m.group(2)
            added.setdefault(current, [])
            continue
        if current is None or line.startswith("+++"):
            continue
        if line.startswith("+"):
            added[current].append(line[1:])
    return added


def distinctive_added_lines(
    patch_text: str,
    *,
    min_len: int = DISTINCTIVE_MIN_LEN,
    max_lines: int = 80,
) -> list[str]:
    """Distinctive added lines from the diff's non-test files.

    Long added lines only (short lines match everywhere), deduplicated,
    order-preserving, capped so the container-side ``grep -F -f`` stays fast.
    """
    per_file = parse_diff_added_lines(patch_text)
    out: list[str] = []
    seen: set[str] = set()
    for path in sorted(per_file):
        if is_test_path(path):
            continue
        for line in per_file[path]:
            stripped = line.strip()
            if len(stripped) < min_len or stripped in seen:
                continue
            seen.add(stripped)
            out.append(stripped)
            if len(out) >= max_lines:
                return out
    return out


def classify_location(path: str, workdir: str) -> str:
    """Route bucket for a container path holding fix content."""
    git_dir = workdir.rstrip("/") + "/.git/"
    if path == workdir.rstrip("/") + "/.git" or path.startswith(git_dir):
        return "git-objects"
    low = path.lower()
    if ".cache/pip" in low or "/pip/http" in low:
        return "pip-cache"
    if "_cacache" in low:
        return "npm-cache"
    if ".cache/yarn" in low or "/yarn/" in low:
        return "yarn-cache"
    if "pnpm-store" in low or "pnpm/store" in low:
        return "pnpm-store"
    if "pkg/mod" in low:
        return "go-mod-cache"
    if "go-build" in low:
        return "go-build-cache"
    if "cargo/registry" in low:
        return "cargo-registry"
    if ".m2/repository" in low:
        return "maven-repo"
    if ".gradle" in low:
        return "gradle-cache"
    if "site-packages" in low or "dist-packages" in low:
        return "site-packages"
    if low.startswith("/tmp/"):
        return "tmp"
    if path == workdir.rstrip("/") or path.startswith(workdir.rstrip("/") + "/"):
        rest = path[len(workdir.rstrip("/") + "/") :]
        first = rest.split("/", 1)[0]
        if first in ("build", "dist") or first.endswith(".egg-info"):
            return "installed-copies"
        if first == "target":
            return "target-dir"
        if first == "node_modules":
            return "node-modules"
        return "worktree"
    if low.startswith(("/root/", "/opt/", "/usr/local/", "/var/", "/home/")):
        return "system-dirs"
    return "other"


def summarize_hits(
    hits: list[dict[str, Any]], workdir: str
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Attach route buckets; return (counts-by-route, annotated hits)."""
    counts: dict[str, int] = {}
    for hit in hits:
        route = classify_location(hit["path"], workdir)
        hit["route"] = route
        counts[route] = counts.get(route, 0) + 1
    return counts, hits


#: Awk program selecting searchable patterns from a fix diff: stripped added
#: lines (length >= ``PATTERN_MIN_LEN``) from NON-TEST files only.
#: Test-path classification mirrors :func:`is_test_path`. Staged as
#: ``patterns.awk`` by :func:`stage_probe` and run by the probe;
#: unit-tested via bash.
_PATTERNS_AWK_TEMPLATE = r"""
/^\+\+\+ / { f = $2; sub(/^b\//, "", f); test = (f ~ /(^|\/)(tests?|testing|test_|_test|spec|specs|e2e)(\/|$)/ || f ~ /\.(spec|test)\.[A-Za-z]+$/ || f ~ /(^|\/)(conftest\.py|mimo_test_command\.sh|test_commands\.json|test\.patch|test_command\.sh)$/); next }
/^--- / { next }
test { next }
/^\+/ { line = substr($0, 2); gsub(/^[ \t]+|[ \t]+$/, "", line); if (length(line) >= @@MIN@@) print line }
"""
PATTERNS_AWK = _PATTERNS_AWK_TEMPLATE.replace("@@MIN@@", str(PATTERN_MIN_LEN))

PROBE_SH = """\
#!/bin/bash
# fix-content-census probe: inspect the fix pre-setup, run the staged setup,
# then scan the filesystem for post-fix content. Results in /census-out.
WORKDIR="$1"
FIX="$2"
STAGE=/census-stage
OUT=/census-out
mkdir -p "$OUT"
printf '%s' "$WORKDIR" > "$OUT/workdir"
printf '%s' "$FIX" > "$OUT/fix"
# ---- stage 0: reference fix from the leaked pre-setup history, or ----
# ---- precomputed patterns from a known reference patch (no in-image fix) ----
if [ -f "$STAGE/patterns.pre" ]; then
  cp "$STAGE/patterns.pre" "$OUT/patterns_raw.txt"
  { cat "$STAGE/fix_source" 2>/dev/null || echo unknown-patch; } > "$OUT/fix_source"
  echo precomputed > "$OUT/fix_present_pre"
  BASE=$(git -C "$WORKDIR" rev-parse HEAD 2>/dev/null || true)
  printf '%s' "$BASE" > "$OUT/base"
  : > "$OUT/blobs.txt"
  : > "$OUT/fix.diff"
  cp "$STAGE/files.pre" "$OUT/changed.txt" 2>/dev/null || : > "$OUT/changed.txt"
else
  BASE=$(git -C "$WORKDIR" rev-parse HEAD 2>/dev/null || true)
  printf '%s' "$BASE" > "$OUT/base"
  if git -C "$WORKDIR" cat-file -t "$FIX" >/dev/null 2>&1; then echo yes > "$OUT/fix_present_pre"; else echo no > "$OUT/fix_present_pre"; fi
  git -C "$WORKDIR" diff "$FIX^" "$FIX" --name-only 2>/dev/null > "$OUT/changed.txt" || true
  git -C "$WORKDIR" diff "$FIX^" "$FIX" 2>/dev/null > "$OUT/fix.diff" || true
  : > "$OUT/blobs.txt"
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    _sha=$(git -C "$WORKDIR" rev-parse "$FIX:$f" 2>/dev/null || true)
    [ -n "$_sha" ] && printf '%s\t%s\n' "$_sha" "$f" >> "$OUT/blobs.txt"
  done < "$OUT/changed.txt"
  awk -f "$STAGE/patterns.awk" "$OUT/fix.diff" 2>/dev/null | sort -u > "$OUT/patterns_raw.txt" || true
fi
# Distinctive patterns: added lines absent from the base tree. A line the
# base already contains is not fix content, no matter where else it appears.
# Batched: one git-grep lists every raw pattern occurring at base (-o prints
# only the matched part, i.e. the pattern itself), then exact-subtract.
# Applies to both recovered and precomputed raw patterns.
: > "$OUT/patterns.txt"
if [ -s "$OUT/patterns_raw.txt" ] && [ -n "$BASE" ]; then
  git -C "$WORKDIR" grep -ohF -f "$OUT/patterns_raw.txt" "$BASE" -- . 2>/dev/null | sort -u > "$OUT/base_hits.txt" || true
  grep -vFxf "$OUT/base_hits.txt" "$OUT/patterns_raw.txt" > "$OUT/patterns.txt" || true
fi
wc -l < "$OUT/patterns_raw.txt" | tr -d ' ' > "$OUT/pattern_raw_count"
wc -l < "$OUT/patterns.txt" | tr -d ' ' > "$OUT/pattern_count"
# ---- staged setup (published or clean chain) ----
mkdir -p /var/lib/mimo
cp -r "$STAGE/setup/." /var/lib/mimo/ 2>/dev/null
bash /var/lib/mimo/setup.sh > "$OUT/setup.log" 2>&1
printf '%s' "$?" > "$OUT/setup_rc"
if [ -f /var/lib/mimo/ready ]; then echo yes > "$OUT/ready"; else echo no > "$OUT/ready"; fi
# ---- stage 2: git recoverability (V2/V4) ----
if git -C "$WORKDIR" cat-file -t "$FIX" >/dev/null 2>&1; then echo yes > "$OUT/fix_present_post"; else echo no > "$OUT/fix_present_post"; fi
if [ -d /var/lib/mimo/git-hidden ]; then echo yes > "$OUT/git_hidden"; else echo no > "$OUT/git_hidden"; fi
if [ -d "$WORKDIR/.git" ]; then echo yes > "$OUT/git_present"; else echo no > "$OUT/git_present"; fi
git -C "$WORKDIR" rev-list --all --count 2>/dev/null > "$OUT/rev_count" || echo ? > "$OUT/rev_count"
git -C "$WORKDIR" branch -a 2>/dev/null > "$OUT/branches.txt" || true
git -C "$WORKDIR" tag 2>/dev/null > "$OUT/tags.txt" || true
git -C "$WORKDIR" stash list 2>/dev/null > "$OUT/stash.txt" || true
_to=""; command -v timeout >/dev/null 2>&1 && _to="timeout 240"
$_to git -C "$WORKDIR" fsck --unreachable --no-reflogs 2>/dev/null | wc -l | tr -d ' ' > "$OUT/fsck_unreachable" || echo ? > "$OUT/fsck_unreachable"
# ---- stage 2: filesystem content scan outside .git and the census mounts ----
: > "$OUT/hits.txt"
printf '%s' no-patterns > "$OUT/scan_rc"
if [ -s "$OUT/patterns.txt" ]; then
  _gto=""; command -v timeout >/dev/null 2>&1 && _gto="timeout 1200"
  $_gto grep -rlF -f "$OUT/patterns.txt" --exclude-dir=proc --exclude-dir=sys --exclude-dir=dev --exclude-dir=.git --exclude-dir=census-stage --exclude-dir=census-out / 2>"$OUT/scan-stderr.txt" > "$OUT/hits.txt"
  printf '%s' "$?" > "$OUT/scan_rc"
fi
: > "$OUT/hit_detail.txt"
while IFS= read -r p; do
  [ -n "$p" ] || continue
  _c=$(grep -cF -f "$OUT/patterns.txt" "$p" 2>/dev/null || true)
  _s=$(sha256sum "$p" 2>/dev/null || shasum -a 256 "$p" 2>/dev/null || echo "nosum $p")
  printf '%s\t%s\t%s\n' "$p" "$_c" "$_s" >> "$OUT/hit_detail.txt"
done < "$OUT/hits.txt"
# ---- stage 2: mtime clustering of the fix-touched files (V3) ----
: > "$OUT/fix_mtimes.txt"
while IFS= read -r f; do
  [ -n "$f" ] || continue
  if [ -e "$WORKDIR/$f" ]; then
    _m=$(stat -c '%Y' "$WORKDIR/$f" 2>/dev/null || stat -f '%m' "$WORKDIR/$f" 2>/dev/null || echo ?)
    printf '%s\t%s\n' "$_m" "$f" >> "$OUT/fix_mtimes.txt"
  fi
done < "$OUT/changed.txt"
find "$WORKDIR" -path "$WORKDIR/.git" -prune -o -type f -printf '%T@\n' 2>/dev/null | sort -u | wc -l | tr -d ' ' > "$OUT/worktree_mtimes" || echo ? > "$OUT/worktree_mtimes"
# ---- stage 2: cache inventory (existence; content verdicts come from hits) ----
{
  for _d in "$HOME/.cache/pip" "$HOME/.npm" "$HOME/.cache/yarn" "$HOME/.local/share/pnpm/store" "$HOME/go/pkg/mod" "$HOME/.cache/go-build" "$HOME/.cargo/registry" "$HOME/.m2/repository" "$HOME/.gradle" /tmp /opt; do
    [ -e "$_d" ] && echo "present $_d" || echo "absent $_d"
  done
  if [ "$HOME" != "/root" ]; then
    for _d in /root/.cache/pip /root/.npm /root/.cache/yarn /root/.local/share/pnpm/store /root/go/pkg/mod /root/.cache/go-build /root/.cargo/registry /root/.m2/repository /root/.gradle; do
      [ -e "$_d" ] && echo "present $_d" || echo "absent $_d"
    done
  fi
  if command -v go >/dev/null 2>&1; then echo "gomodcache $(go env GOMODCACHE 2>/dev/null || echo ?)"; echo "gocache $(go env GOCACHE 2>/dev/null || echo ?)"; fi
} > "$OUT/caches.txt"
tail -n 20 "$OUT/setup.log" > "$OUT/setup_tail.txt" 2>/dev/null || true
"""


def _rmtree(path: Path) -> None:
    """Remove a tree that may hold read-only or container-written files."""
    subprocess.run(["chmod", "-R", "u+w", str(path)], capture_output=True)
    shutil.rmtree(path, ignore_errors=True)


def stage_probe(
    stage_dir: Path,
    setup_source: Path,
    setup_sh: bytes | None = None,
    *,
    precomputed: Mapping[str, Any] | None = None,
) -> None:
    """Write the probe stage dir: probe.sh plus the setup payload to run.

    ``precomputed`` seeds the probe from a known reference patch instead of
    in-image git archaeology: ``{"patterns": [...], "files": [...],
    "fix_source": str}`` stages ``patterns.pre``/``files.pre``/``fix_source``
    for the probe's stage-0 branch (base-tree subtraction still applies).
    """
    stage = Path(stage_dir)
    setup = stage / "setup"
    _rmtree(stage)
    shutil.copytree(setup_source, setup)
    if setup_sh is not None:
        target = setup / "setup.sh"
        with contextlib.suppress(OSError):
            os.chmod(target, 0o644)
        target.write_bytes(setup_sh)
    (stage / "probe.sh").write_text(PROBE_SH, encoding="utf-8")
    (stage / "patterns.awk").write_text(PATTERNS_AWK, encoding="utf-8")
    if precomputed is not None:
        patterns = [str(line) for line in precomputed.get("patterns", []) if str(line).strip()]
        files = [str(path) for path in precomputed.get("files", []) if str(path).strip()]
        (stage / "patterns.pre").write_text("\n".join(patterns) + "\n", encoding="utf-8")
        (stage / "files.pre").write_text("\n".join(files) + "\n", encoding="utf-8")
        (stage / "fix_source").write_text(
            str(precomputed.get("fix_source", "unknown-patch")), encoding="utf-8"
        )


def compose_clean_setup(
    root_setup_sh: str, *, with_purge: bool, caches_version: int = 2
) -> tuple[str, list[str]]:
    """Clean-chain setup.sh composed with the real transform functions.

    Order: strip-future-history@1, purge-installed-copies@1 (Python tasks
    where it applies), purge-build-caches@2/@3, mtime-normalize@1. Returns
    (setup text, applied transform ids).
    """
    from evallab import mtime_normalize, purge_build_caches, strip_future_history
    from evallab.purge_installed_copies import TRANSFORM_ID as PURGE_ID
    from evallab.purge_installed_copies import build_setup_sh as purge_setup
    from evallab.task_variants import VariantInvalid

    if caches_version not in (2, 3):
        raise ValueError(f"caches_version must be 2 or 3, got {caches_version}")
    applied: list[str] = []
    text = strip_future_history.build_setup_sh(root_setup_sh)
    applied.append(strip_future_history.TRANSFORM_ID)
    if with_purge:
        try:
            text = purge_setup(text)
            applied.append(PURGE_ID)
        except VariantInvalid:
            applied.append(PURGE_ID + "(present)")
    if caches_version == 3:
        text = purge_build_caches.build_setup_sh_v3(text)
        applied.append(purge_build_caches.TRANSFORM_ID_V3)
    else:
        text = purge_build_caches.build_setup_sh_v2(text)
        applied.append(purge_build_caches.TRANSFORM_ID_V2)
    text = mtime_normalize.build_setup_sh(text)
    applied.append(mtime_normalize.TRANSFORM_ID)
    return text, applied


def run_probe(
    image: str,
    workdir: str,
    fix_sha: str,
    stage_dir: Path | str,
    out_dir: Path | str,
    *,
    timeout: int = 2700,
    backend: str = "docker",
    egress_lock: bool = True,
) -> None:
    """Run the census probe in a container (root; network per ``egress_lock``).

    ``backend="docker"`` (default) uses local Docker, ``--network none`` when
    ``egress_lock`` is true (default; existing behavior unchanged).
    ``backend="modal"`` replays the same staged probe in a Modal sandbox on
    the same pinned image: same ``probe.sh`` + setup payload, same workdir
    and fix arguments, root, ``block_network=egress_lock``, billed to the
    named census app with a ``probe-runtime.json`` lifecycle receipt. The
    census runner passes ``egress_lock`` from the task package network
    contract (``network_mode == "none"``), so published/clean setups match
    package semantics. The sandbox is terminated on success and on error; a
    failed termination is recorded and raised (possible continued billing),
    never suppressed. SDK failures raise explicitly and never fabricate
    result files.
    """
    if backend not in ("docker", "modal"):
        raise ValueError(f"backend must be 'docker' or 'modal', got {backend!r}")
    out = Path(out_dir)
    _rmtree(out)
    out.mkdir(parents=True)
    if backend == "modal":
        _run_probe_modal(
            image,
            workdir,
            fix_sha,
            stage_dir,
            out,
            timeout=timeout,
            egress_lock=egress_lock,
        )
        return
    cmd = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        *(["--network", "none"] if egress_lock else []),
        "--user",
        "root",
        "-v",
        f"{Path(stage_dir)}:/census-stage:ro",
        "-v",
        f"{out}:/census-out",
        image,
        "bash",
        "/census-stage/probe.sh",
        workdir,
        fix_sha,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    (out / "docker_rc").write_text(str(proc.returncode), encoding="utf-8")
    if proc.stdout:
        (out / "docker_stdout.txt").write_text(proc.stdout[-20000:], encoding="utf-8")
    if proc.stderr:
        (out / "docker_stderr.txt").write_text(proc.stderr[-20000:], encoding="utf-8")


#: Modal app for fix-content-census probes. A looked-up named app (not an
#: ephemeral ``App(name=...)``) so slice spend attributes to the exact billed
#: app; approved by Main for exact slice billing.
_MODAL_APP_NAME = "mimo-clean-census-fix"

#: Headroom added to the Modal sandbox lifetime past the probe ``timeout`` so
#: stage upload and output download are not censored by a probe that runs the
#: full bound.
_MODAL_LIFETIME_HEADROOM_S = 300


def _modal_text(value: bytes | str) -> str:
    """Decode a Modal exec stream chunk (text mode returns ``str``)."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _modal_exec(
    sandbox: Any, argv: list[str], *, workdir: str, timeout: int
) -> tuple[int, str, str]:
    """Run one sandbox command; return (rc, stdout, stderr) as text."""
    proc = sandbox.exec(*argv, workdir=workdir, timeout=timeout)
    stdout = _modal_text(proc.stdout.read())
    stderr = _modal_text(proc.stderr.read())
    return proc.wait(), stdout, stderr


def _run_probe_modal(
    image: str,
    workdir: str,
    fix_sha: str,
    stage_dir: Path | str,
    out: Path,
    *,
    timeout: int,
    egress_lock: bool = True,
) -> None:
    """Replay the staged probe in a Modal sandbox (root).

    Same pinned image (``linux/amd64``), same ``/census-stage`` payload as the
    Docker path, same ``probe.sh`` arguments. Network follows ``egress_lock``
    (``block_network``), honouring the task package network contract like the
    Docker path. Probe outputs are downloaded from ``/census-out``
    byte-for-byte so :func:`collect_result` consumes real equivalent files,
    and ``probe-runtime.json`` records the billed app, sandbox id, and UTC
    lifecycle for spend attribution. A nonzero probe rc is recorded as
    evidence (like the Docker path), never raised; orchestration failures
    raise explicitly.
    """
    try:
        import modal
    except ImportError as exc:
        raise RuntimeError(
            "run_probe(backend='modal') requires the Modal SDK "
            "(e.g. 'uv run --with modal==1.6.1 ...'); not installed here"
        ) from exc
    stage = Path(stage_dir)
    app = modal.App.lookup(_MODAL_APP_NAME, create_if_missing=True)
    modal_image = modal.Image.from_registry(image, platform="linux/amd64")
    started = datetime.now(UTC)
    sandbox = modal.Sandbox.create(
        "sleep",
        str(timeout + _MODAL_LIFETIME_HEADROOM_S),
        app=app,
        image=modal_image,
        workdir=workdir,
        block_network=egress_lock,
        timeout=timeout + _MODAL_LIFETIME_HEADROOM_S,
    )
    failure: BaseException | None = None
    try:
        payloads = [
            (f"/census-stage/{path.relative_to(stage).as_posix()}", path.read_bytes())
            for path in sorted(stage.rglob("*"))
            if not path.is_dir()
        ]
        parents = sorted({remote.rpartition("/")[0] for remote, _ in payloads})
        rc, _, mkdir_err = _modal_exec(
            sandbox,
            ["mkdir", "-p", "/census-stage", "/census-out", *parents],
            workdir=workdir,
            timeout=120,
        )
        if rc != 0:
            raise RuntimeError(f"modal stage mkdir failed (rc={rc}): {mkdir_err[-2000:]}")
        for remote, data in payloads:
            with sandbox.open(remote, "wb") as handle:
                handle.write(data)
        rc, stdout, stderr = _modal_exec(
            sandbox,
            ["bash", "/census-stage/probe.sh", workdir, fix_sha],
            workdir=workdir,
            timeout=timeout,
        )
        (out / "modal_rc").write_text(str(rc), encoding="utf-8")
        if stdout:
            (out / "modal_stdout.txt").write_text(stdout[-20000:], encoding="utf-8")
        if stderr:
            (out / "modal_stderr.txt").write_text(stderr[-20000:], encoding="utf-8")
        rc, listing, list_err = _modal_exec(
            sandbox,
            ["find", "/census-out", "-type", "f", "-print"],
            workdir=workdir,
            timeout=120,
        )
        if rc != 0:
            raise RuntimeError(f"modal probe output listing failed (rc={rc}): {list_err[-2000:]}")
        for line in listing.splitlines():
            remote = line.strip()
            if not remote.startswith("/census-out/") or remote == "/census-out/":
                continue
            dest = out / remote[len("/census-out/") :]
            with sandbox.open(remote, "rb") as handle:
                data = handle.read()
            if isinstance(data, str):
                data = data.encode("utf-8")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        ended = datetime.now(UTC)
        (out / "probe-runtime.json").write_text(
            json.dumps(
                {
                    "backend": "modal",
                    "app_name": _MODAL_APP_NAME,
                    "sandbox_id": getattr(sandbox, "object_id", "?"),
                    "image": image,
                    "workdir": workdir,
                    "started_at": started.isoformat(),
                    "ended_at": ended.isoformat(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        try:
            sandbox.terminate()
        except Exception as term_exc:
            # A failed termination may mean continued billing: always retain
            # the evidence; raise only when no probe failure is already
            # propagating, so the original error is never hidden.
            (out / "modal_terminate_error.txt").write_text(
                f"{type(term_exc).__name__}: {term_exc}\n", encoding="utf-8"
            )
            if failure is None:
                raise


def _read(path: Path, default: str = "?") -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return default


def _read_lines(path: Path) -> list[str]:
    try:
        return [
            line.rstrip("\n")
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        ]
    except OSError:
        return []


def collect_result(
    task_id: str, language: str, image12: str, mode: str, out_dir: Path | str
) -> dict[str, Any]:
    """Assemble one census row from probe outputs (host-side verdicts)."""
    out = Path(out_dir)
    workdir = _read(out / "workdir", "")
    fix_sha = _read(out / "fix", "")
    blobs: dict[str, str] = {}
    for line in _read_lines(out / "blobs.txt"):
        sha, _, path = line.partition("\t")
        if sha and path:
            blobs[path] = sha
    blob_shas = set(blobs.values())
    hits: list[dict[str, Any]] = []
    for line in _read_lines(out / "hit_detail.txt"):
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        path, count, sumline = parts[0], parts[1], " ".join(parts[2:])
        try:
            n = int(count.strip())
        except ValueError:
            n = 0
        sha = sumline.split()[0] if sumline.split() else ""
        hits.append(
            {
                "path": path,
                "matched_lines": n,
                "sha256": "" if sha in ("", "nosum") else sha,
                "blob_match": bool(sha) and sha in blob_shas,
            }
        )
    counts, hits = summarize_hits(hits, workdir)
    blob_matches = [h["path"] for h in hits if h["blob_match"]]
    fix_present_post = _read(out / "fix_present_post")
    ready = _read(out / "ready")
    # An open leak is recoverable fix content, not a failed setup: setup
    # failures are applicability findings and ride in notes.
    open_leak = fix_present_post == "yes" or bool(hits)
    mtimes = _read_lines(out / "fix_mtimes.txt")
    fix_epochs = sorted({line.partition("\t")[0] for line in mtimes} - {"", "?"})
    worktree_mtimes = _read(out / "worktree_mtimes")
    mtime_signal = len(fix_epochs) == 1 and len(mtimes) >= 2 and worktree_mtimes not in ("?", "1")
    notes: list[str] = []
    fix_pre = _read(out / "fix_present_pre")
    if fix_pre == "precomputed":
        notes.append(
            f"patterns from known patch ({_read(out / 'fix_source')}); no in-image fix commit"
        )
    elif fix_pre != "yes":
        notes.append("fix absent pre-setup; content scan has no oracle")
    if _read(out / "setup_rc") != "0":
        notes.append(f"setup rc={_read(out / 'setup_rc')}")
    if ready != "yes":
        notes.append("ready sentinel missing")
    # Runner evidence: whichever backend ran the probe records its rc; a Modal
    # run has no docker_rc, so only present files are judged (a missing
    # docker_rc on a Modal run is expected, not a finding).
    rc_names = [name for name in ("docker_rc", "modal_rc") if (out / name).exists()]
    for rc_name in rc_names or ["docker_rc"]:
        if _read(out / rc_name) != "0":
            notes.append(f"{rc_name} rc={_read(out / rc_name)}")
    scan_rc = _read(out / "scan_rc")
    scan_complete = scan_rc in ("0", "1", "no-patterns")
    if not scan_complete:
        notes.append(f"content scan incomplete rc={scan_rc}")
    probe_rc = _read(out / rc_names[0]) if len(rc_names) == 1 else "unknown"
    if _read(out / "git_hidden") == "yes":
        notes.append("setup hid .git (git-hidden); post git checks are blind")
    if _read(out / "git_present") == "no":
        notes.append("no worktree .git post-setup")
    return {
        "task_id": task_id,
        "language": language,
        "image12": image12,
        "mode": mode,
        "setup_rc": _read(out / "setup_rc"),
        "ready": ready,
        "scan_rc": scan_rc,
        "scan_complete": scan_complete,
        "probe_rc": probe_rc,
        "fix_sha": fix_sha,
        "fix_source": _read(out / "fix_source", ""),
        "fix_present_pre": _read(out / "fix_present_pre"),
        "fix_present_post": fix_present_post,
        "rev_count": _read(out / "rev_count"),
        "fsck_unreachable": _read(out / "fsck_unreachable"),
        "branches": len(_read_lines(out / "branches.txt")),
        "tags": len(_read_lines(out / "tags.txt")),
        "stash": len(_read_lines(out / "stash.txt")),
        "patterns": _read(out / "pattern_count"),
        "patterns_raw": _read(out / "pattern_raw_count"),
        "hits_total": len(hits),
        "hits_by_route": json.dumps(counts, sort_keys=True),
        "blob_matches": json.dumps(sorted(blob_matches)),
        "mtime_signal": "yes" if mtime_signal else "no",
        "mtime_detail": f"fix_epochs={','.join(fix_epochs) or '?'} "
        f"fix_files={len(mtimes)} worktree_distinct={worktree_mtimes}",
        "caches": json.dumps(
            [c for c in _read_lines(out / "caches.txt") if c.startswith("present")]
        ),
        "open_leak": "yes" if open_leak else "no",
        "notes": "; ".join(notes),
        "hits": hits,
    }


def leak_oracle_extract(
    task_dir: Path | str, git_dir: Path | str, out_dir: Path | str
) -> dict[str, Any]:
    """Run the HAR-191 extractor (research/experiments/leak-oracle/extract.py).

    ``git_dir`` is a host-side copy of the image's ``.git`` (via
    ``docker create`` + ``docker cp``); ``task_dir`` is the task package.
    Returns the evidence dict (``status`` ok/ok-divergent on success, with
    ``fix`` and a ``solution.patch`` in ``out_dir``).
    """
    repo_root = Path(__file__).resolve().parents[2]
    extractor = repo_root / "research" / "experiments" / "leak-oracle" / "extract.py"
    spec = importlib.util.spec_from_file_location("leak_oracle_extract", extractor)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load extractor: {extractor}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["leak_oracle_extract"] = module
    spec.loader.exec_module(module)
    return module.extract(Path(task_dir), str(git_dir), Path(out_dir))


def copy_git_from_image(image: str, workdir: str, dest: Path) -> bool:
    """Copy ``<workdir>/.git`` out of a created (never run) container."""
    _rmtree(dest)
    dest.mkdir(parents=True)
    create = subprocess.run(
        ["docker", "create", "--platform", "linux/amd64", image, "/bin/true"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if create.returncode != 0:
        return False
    cid = create.stdout.strip()
    try:
        cp = subprocess.run(
            ["docker", "cp", f"{cid}:{workdir.rstrip('/')}/.git", str(dest / ".git")],
            capture_output=True,
            text=True,
            timeout=1200,
        )
        return cp.returncode == 0
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=120)


def write_csv(rows: list[dict[str, Any]], path: Path | str) -> None:
    """Write census rows (drop the nested ``hits`` payload)."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in CSV_FIELDS})


def main(argv: list[str] | None = None) -> int:
    """$0 census driver: probe one task/mode or summarize result dirs."""
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_probe = sub.add_parser("probe", help="run one container probe")
    p_probe.add_argument("--image", required=True)
    p_probe.add_argument("--workdir", required=True)
    p_probe.add_argument("--fix", required=True)
    p_probe.add_argument("--setup-source", required=True)
    p_probe.add_argument("--setup-sh", default=None)
    p_probe.add_argument("--stage-dir", required=True)
    p_probe.add_argument("--out-dir", required=True)
    p_sum = sub.add_parser("summarize", help="assemble rows from probe out dirs")
    p_sum.add_argument("--results", required=True)
    p_sum.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "probe":
        setup_sh = Path(args.setup_sh).read_bytes() if args.setup_sh else None
        stage_probe(Path(args.stage_dir), Path(args.setup_source), setup_sh)
        run_probe(args.image, args.workdir, args.fix, args.stage_dir, args.out_dir)
        return 0
    results = Path(args.results)
    rows: list[dict[str, Any]] = []
    for task_dir in sorted(results.iterdir()):
        if not task_dir.is_dir():
            continue
        for mode_dir in sorted(task_dir.iterdir()):
            if not mode_dir.is_dir():
                continue
            meta: dict[str, Any] = {}
            meta_path = task_dir / f"{mode_dir.name}.meta.json"
            if meta_path.is_file():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            row = collect_result(
                meta.get("task_id", task_dir.name),
                meta.get("language", "?"),
                meta.get("image12", "?"),
                mode_dir.name,
                mode_dir,
            )
            row["fix_source"] = meta.get("fix_source", "")
            rows.append(row)
    write_csv(rows, Path(args.out))
    print(f"summarized {len(rows)} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def recover_fix_lite(git_dir: Path | str, base: str, test_files: list[str]) -> dict[str, Any]:
    """Fallback fix recovery for histories the HAR-191 extractor refuses.

    Same core signal as extractor S1 (before-blob continuity: the first
    future toucher of a task test file keeps the base blob) plus a non-test
    file change in the same commit, without the keyword/divergence scoring.
    Returns ``{"sha": ...}`` or ``{"status": ..., "rationale": ...}``.
    Host-side, plain ``git`` CLI only.
    """
    git = str(git_dir)

    def run(*args: str) -> str:
        proc = subprocess.run(
            ["git", "--git-dir", git, *args],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {proc.stderr[:200]}")
        return proc.stdout

    try:
        base_blobs: dict[str, str] = {}
        for f in test_files:
            try:
                out = run("rev-parse", f"{base}:{f}").strip()
            except RuntimeError:
                continue
            if re.fullmatch(r"[0-9a-f]{40}", out):
                base_blobs[f] = out
        if not base_blobs:
            return {"status": "no-test-blobs", "rationale": "no task test file in base"}
        log = run("log", "--all", "--format=%H %P %aI", "--name-only", "--", *test_files)
    except RuntimeError as exc:
        return {"status": "git-error", "rationale": str(exc)}
    candidates: list[tuple[str, str]] = []
    header: list[str] | None = None
    names: list[str] = []
    records: list[tuple[list[str], list[str]]] = []
    for line in log.split("\n"):
        if re.match(r"^[0-9a-f]{40}( |$)", line):
            if header is not None:
                records.append((header, names))
            header = line.split(" ")
            names = []
        elif line.strip():
            if header is not None:
                names.append(line.strip())
    if header is not None:
        records.append((header, names))
    for toks, files in records:
        if len(toks) < 3 or not re.fullmatch(r"[0-9a-f]{40}", toks[0]):
            continue
        sha, date = toks[0], toks[-1]
        parents = " ".join(toks[1:-1])
        parent = parents.split()[0] if parents.split() else ""
        touched = [p for p in files if p in base_blobs]
        if not touched or not parent:
            continue
        try:
            continuous = [
                f for f in touched if run("rev-parse", f"{parent}:{f}").strip() == base_blobs[f]
            ]
            changed = run("diff", "--name-only", parent, sha).split()
            non_test = non_test_files(changed)
        except RuntimeError:
            continue
        if continuous and non_test:
            candidates.append((sha, date))
    if not candidates:
        return {"status": "no-candidate", "rationale": "no continuous test+source toucher"}
    candidates.sort(key=lambda c: c[1])
    return {"sha": candidates[0][0], "method": "lite-s1"}


__all__ = [
    "CSV_FIELDS",
    "DISTINCTIVE_MIN_LEN",
    "PATTERN_MIN_LEN",
    "PATTERNS_AWK",
    "PROBE_SH",
    "classify_location",
    "collect_result",
    "compose_clean_setup",
    "copy_git_from_image",
    "distinctive_added_lines",
    "is_test_path",
    "leak_oracle_extract",
    "main",
    "non_test_files",
    "parse_diff_added_lines",
    "recover_fix_lite",
    "run_probe",
    "stage_probe",
    "summarize_hits",
    "write_csv",
]
