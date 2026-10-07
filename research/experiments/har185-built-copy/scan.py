"""HAR-185 built-copy second-leak scan.

Containment question per task image: does the image contain an
installed/built copy of the task's own project (site-packages,
build/lib, egg-info/dist-info, wheels/sdists, pip caches, .tox/.nox
envs, vendored copies) that is newer than, or differs from, the base
working tree in the fix region? ``strip-future-history`` (HAR-177)
does not cover this second leak class.

Containment rule (binding for this module): report identifying
metadata only -- paths, file sizes, SHA-256 hashes, and parsed
Name/Version lines from packaging metadata. NEVER quote file
contents, code, or diffs. The registry fetch stream-reads every
layer once but hashes only candidate files; all bytes are discarded.
Only literal Name/Version packaging assignments are parsed; dynamic
expressions are not evaluated or retained as version metadata.

Method, cheapest first: ``mirror.gcr.io`` pull-through for Docker Hub
manifests/blobs, otherwise Docker Hub anonymous, otherwise the row
is ``unscanned`` with the blocker recorded. No ``docker pull`` of
full images anywhere.

Usage (local validation, $0)::

    python scan.py validate --snapshot <hf-store> --workers 2
    python scan.py sample --reuse-sample \\
        research/experiments/har177-leak-scan/sample100.csv \\
        --snapshot <hf-store> --variants <variants> \\
        --out built_copy.csv --mode local

"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import os
import random
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime

from packaging.version import InvalidVersion, Version

REPO = "xiaomimimo/mimo-v2.6-rl-oss"
MIRROR = "https://mirror.gcr.io"
DOCKERHUB = "https://registry-1.docker.io"
DOCKER_TOKEN = "https://auth.docker.io/token"

MANIFEST_ACCEPT = (
    "application/vnd.docker.distribution.manifest.v2+json, "
    "application/vnd.oci.image.manifest.v1+json"
)

DIGEST_RE = re.compile(r"@sha256:([0-9a-f]{64})")
CWD_RE = re.compile(r"^CWD=(\S+)", re.MULTILINE)
WORKDIR_RE = re.compile(r'^workdir\s*=\s*"([^"]+)"', re.MULTILINE)

#: Cap on hashed candidate bytes per image (discard-only accounting past it).
MAX_HASH_BYTES = 8 * 1024**3
#: Cap on recorded overlay paths per image (name index only).
MAX_PATHS = 300_000
#: Cap on hashed worktree .py files per image (CPU bound).
MAX_WORKTREE_HASHED = 2_000
#: How many built-copy paths land in the CSV column (paths only).
CSV_PATH_LIMIT = 25
#: Bytes read from a packaging-metadata file to parse Name/Version lines.
META_READ_LIMIT = 16 * 1024
ARCHIVE_BYTES_LIMIT = 64 * 1024**2

CSV_COLUMNS = (
    "task_id",
    "run",
    "ledger_digest",
    "image_digest",
    "project",
    "project_basis",
    "has_built_copy",
    "needs_repair",
    "fix_differs",
    "comparison_status",
    "fix_files",
    "built_copy_kinds",
    "built_copy_paths",
    "n_built_files",
    "base_version",
    "installed_version",
    "version_cmp",
    "method",
    "error",
    "comparisons",
    "scanned_at",
)

#: Known positives the method must reproduce before any sweep.
#: 001269: /testbed/build/lib held the fixed module (responses).
#: 002308: site-packages pre_commit copy used after pip download failed.
KNOWN_POSITIVES = ("001269", "002308")
KNOWN_KIND_HINT = {"001269": "build-lib", "002308": "site-packages"}
KNOWN_PROJECT = {"001269": "responses", "002308": "pre_commit"}

#: test.patch files that carry no fix-region signal.
INFRA_FILES = frozenset({"test_commands.json", "mimo_test_command.sh"})

#: Tar members that are never evidence (git history is HAR-177's beat).
SKIP_COMPONENTS = frozenset({".git"})

#: Third-party test-only imports that never name the task's project.
TEST_ONLY_IMPORTS = frozenset({"pytest", "_pytest", "mock", "nose", "hypothesis"})

PY_SUFFIX = ".py"

#: Evidence kinds that count as a built/installed copy outside the sources.
BUILT_KINDS = frozenset(
    {
        "site-packages",
        "dist-packages",
        "build-lib",
        "dist-info",
        "egg-info",
        "wheel",
        "sdist",
        "pip-cache",
        "tox-nox",
        "vendored",
        "pth-egglink",
    }
)


# --------------------------------------------------------------------------
# Pure helpers (no network, no subprocess): unit-tested.
# --------------------------------------------------------------------------


def parse_image_digest(dockerfile_text: str) -> str | None:
    """Image digest named by a task ``environment/Dockerfile``."""
    match = DIGEST_RE.search(dockerfile_text)
    return f"sha256:{match.group(1)}" if match else None


def parse_setup_cwd(setup_text: str) -> str:
    """Working directory the setup script checks out (default ``/testbed``)."""
    match = CWD_RE.search(setup_text)
    return match.group(1) if match else "/testbed"


def parse_task_workdir(task_toml_text: str) -> str | None:
    """``[environment] workdir`` from a task.toml, if present."""
    match = WORKDIR_RE.search(task_toml_text)
    return match.group(1) if match else None


def parse_manifest(body: bytes) -> dict:
    """``(config, layers)`` from a manifest document; raises when layerless."""
    doc = json.loads(body)
    layers = [entry["digest"] for entry in doc.get("layers", [])]
    if not layers:
        raise ValueError("manifest has no layers")
    return {"config": doc.get("config", {}).get("digest", ""), "layers": layers}


def sanitize_cell(text: str, limit: int = 500) -> str:
    """One-line, separator-safe CSV cell (paths/versions only, never content)."""
    flat = " ".join(str(text).split())
    flat = flat.replace(";", ",").replace("|", "/")
    return flat[:limit]


def verdict(has_copy: bool | None) -> tuple[str, str]:
    """``(has_built_copy, needs_repair-base)`` CSV values; repair refined later."""
    if has_copy is None:
        return "unscanned", "false"
    return ("yes", "false") if has_copy else ("no", "false")


def build_row(
    *,
    task_id: str,
    run: str,
    ledger_digest: str,
    image_digest: str,
    project: str = "",
    project_basis: str = "",
    has_copy: bool | None = None,
    needs_repair: bool = False,
    fix_differs: str = "unverifiable",
    fix_files: list[str] | None = None,
    kinds: list[str] | None = None,
    paths: list[str] | None = None,
    n_built: int = 0,
    base_version: str = "",
    installed_version: str = "",
    version_cmp: str = "unverifiable",
    method: str = "",
    error: str = "",
) -> dict:
    """One CSV row from the built-copy analysis (paths/versions/hashes only)."""
    if error:
        copy, _ = verdict(None)
        repair = "false"
        differs = "unverifiable"
    else:
        copy, _ = verdict(has_copy)
        repair = "true" if needs_repair else "false"
        differs = fix_differs
    status = ("confirmed" if repair == "true"
              else "clean" if differs == "no" and version_cmp in ("same", "older")
              else "unverifiable")
    shown = (paths or [])[:CSV_PATH_LIMIT]
    return {
        "task_id": task_id,
        "run": run,
        "ledger_digest": ledger_digest,
        "image_digest": image_digest,
        "project": project,
        "project_basis": project_basis,
        "has_built_copy": copy,
        "needs_repair": repair,
        "fix_differs": differs,
        "comparison_status": status,
        "fix_files": ";".join(sanitize_cell(p, 200) for p in (fix_files or [])),
        "built_copy_kinds": ";".join(sorted(set(kinds or []))),
        "built_copy_paths": ";".join(sanitize_cell(p) for p in shown),
        "n_built_files": str(n_built),
        "base_version": sanitize_cell(base_version, 64),
        "installed_version": sanitize_cell(installed_version, 64),
        "version_cmp": version_cmp,
        "method": method,
        "error": error,
        "scanned_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------
# Fix-region parsing from the task package (tests/test.patch).
# --------------------------------------------------------------------------


def parse_patch_paths(patch_text: str) -> list[str]:
    """``b/``-side paths from ``+++`` lines (excludes /dev/null)."""
    paths: list[str] = []
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            path = line[len("+++ b/") :].strip()
            if path and path != "/dev/null" and path not in paths:
                paths.append(path)
    return paths


def is_test_path(path: str) -> bool:
    """A test file by location or basename convention."""
    comps = path.split("/")
    if any(part in ("tests", "test", "testing") for part in comps[:-1]):
        return True
    base = comps[-1]
    stem = base[: -len(PY_SUFFIX)] if base.endswith(PY_SUFFIX) else base
    return stem.startswith("test_") or stem.endswith("_test") or stem == "conftest"


def split_patch_paths(paths: list[str]) -> tuple[list[str], list[str]]:
    """``(source_paths, test_paths)``; infra files are dropped entirely."""
    source: list[str] = []
    tests: list[str] = []
    for path in paths:
        if path.split("/")[-1] in INFRA_FILES:
            continue
        (tests if is_test_path(path) else source).append(path)
    return source, tests


IMPORT_RE = re.compile(r"^\+(?:from\s+([\w.]+)\s+import|import\s+([\w., ]+))")


def imports_from_patch(patch_text: str) -> list[str]:
    """Top-level imported names from added patch lines (deduplicated)."""
    found: list[str] = []
    for line in patch_text.splitlines():
        match = IMPORT_RE.match(line)
        if not match:
            continue
        raw = match.group(1) or match.group(2) or ""
        for part in raw.split(","):
            top = part.strip().split(".")[0].strip()
            if top and top.isidentifier() and top not in found:
                found.append(top)
    return found


def stdlib_top_names() -> set[str]:
    """Interpreter stdlib top-level names (empty set when unavailable)."""
    return set(getattr(sys, "stdlib_module_names", ()))


def implied_fix_names(test_paths: list[str]) -> tuple[list[str], list[str]]:
    """``(basenames, dirnames)`` of candidate fixed modules from test names.

    ``hook_impl_test.py`` implies basename ``hook_impl.py``;
    ``test_responses.py`` implies basename ``responses.py`` plus the
    package dirname ``responses`` (for single-level ``<pkg>/__init__.py``
    layouts, matched one level deep only).
    """
    basenames: list[str] = []
    dirnames: list[str] = []
    for path in test_paths:
        base = path.split("/")[-1]
        if not base.endswith(PY_SUFFIX):
            continue
        stem = base[: -len(PY_SUFFIX)]
        if stem.startswith("test_"):
            rest = stem[len("test_") :]
            for cand in (rest + PY_SUFFIX, rest):
                target = basenames if cand.endswith(PY_SUFFIX) else dirnames
                if cand and cand not in target:
                    target.append(cand)
        elif stem.endswith("_test"):
            cand = stem[: -len("_test")] + PY_SUFFIX
            if cand not in basenames:
                basenames.append(cand)
    return basenames, dirnames


def norm_variant(name: str) -> str:
    """Comparable form of a project name (``pre-commit`` ~ ``pre_commit``)."""
    return re.sub(r"[-_.]+", "", name).lower()


def name_variants(name: str) -> set[str]:
    """Path spellings a project name may take (``-``/``_`` variants)."""
    lowered = name.lower()
    return {lowered, lowered.replace("-", "_"), lowered.replace("_", "-")}


def test_dir_package(test_paths: list[str]) -> str:
    """Owning top-level package of the test files (``<pkg>/tests/...``)."""
    for path in test_paths:
        comps = path.split("/")
        if len(comps) > 2 and comps[0] not in ("tests", "test", "testing"):
            return comps[0]
    return ""


def guess_project(
    source_paths: list[str],
    test_paths: list[str],
    added_imports: list[str],
) -> tuple[str, str]:
    """``(project_hint, basis)`` for the task's own project (may be unknown).

    Preference: explicit source paths, then the test-dir package confirmed
    by an added import, then the test-dir package alone, then the first
    non-test-only third-party import. Empty name means unresolvable from
    the package alone; the image evidence refines it later.
    """
    stdlib = stdlib_top_names()
    if source_paths:
        comps = source_paths[0].split("/")
        if comps[0] == "src" and len(comps) > 2:
            return comps[1], "patch-src-layout"
        if comps[0]:
            return comps[0].removesuffix(".py"), "patch-source-path"
    test_pkg = test_dir_package(test_paths)
    third_party = [
        name
        for name in added_imports
        if name not in stdlib and name not in TEST_ONLY_IMPORTS
    ]
    if test_pkg:
        if norm_variant(test_pkg) in {norm_variant(name) for name in third_party}:
            return test_pkg, "test-dir+import"
        return test_pkg, "test-dir-package"
    if third_party:
        return third_party[0], "patch-import"
    return "", "unknown"


# --------------------------------------------------------------------------
# Tar-member classification (names only; content never retained).
# --------------------------------------------------------------------------


def clean_member_name(name: str) -> str:
    """Tar member name without ``./`` or leading-``/`` prefixes."""
    clean = name
    while clean.startswith("./"):
        clean = clean[len("./") :]
    return clean.lstrip("/")


def is_build_lib(comps: list[str]) -> bool:
    """A ``build/lib*`` artefact directory anywhere in the components."""
    return any(
        part == "build" and i + 1 < len(comps) and comps[i + 1].startswith("lib")
        for i, part in enumerate(comps)
    )


def component_matches_project(component: str, variants: set[str]) -> bool:
    """A path component naming the project (dist-info version suffixes fold)."""
    lowered = component.lower().removeprefix("__editable__").lstrip("._")
    for suffix in (".dist-info", ".egg-info", ".egg-link", ".egg", ".pth", ".py"):
        lowered = lowered.removesuffix(suffix)
    for variant in variants:
        if lowered == variant:
            return True
        # The suffix must start a version, not another distribution name:
        # requests-futures is not an installed copy of requests.
        if re.match(re.escape(variant) + r"[-_.]v?\d", lowered):
            return True
    return False


def path_matches_project(clean: str, variants: set[str]) -> bool:
    """Any component of the path names the project."""
    if not variants:
        return False
    return any(component_matches_project(part, variants) for part in clean.split("/"))


def archive_names_project(clean: str, dirnames: set[str]) -> bool:
    """A wheel/sdist filename containing a candidate package dirname."""
    base = clean.split("/")[-1].lower()
    if not base.endswith((".whl", ".tar.gz", ".tgz", ".zip")):
        return False
    return any(dirname.lower().replace("_", "-") in base.replace("_", "-") for dirname in dirnames)


def classify_member(
    clean: str,
    *,
    repo_root: str,
    variants: set[str],
    fix_basenames: set[str],
    fix_dirnames: set[str],
) -> set[str]:
    """Classify candidate paths; project identity is checked again at reduction."""
    comps = clean.split("/")
    if not clean or any(part in SKIP_COMPONENTS for part in comps):
        return set()
    base = comps[-1]
    lowered = base.lower()
    root = repo_root.strip("/") + "/"
    in_tree = clean.startswith(root)
    rel = clean[len(root):].split("/") if in_tree else []
    project_hit = path_matches_project(clean, variants)
    fix_hit = base in fix_basenames
    candidate = project_hit or fix_hit or bool(fix_dirnames & set(comps))
    kinds: set[str] = set()
    for kind in ("site-packages", "dist-packages"):
        if kind in comps and candidate:
            kinds.add(kind)
    if is_build_lib(comps) and candidate:
        kinds.add("build-lib")
    if any(part in (".tox", ".nox") for part in comps) and candidate:
        kinds.add("tox-nox")
    if any(part in ("vendor", "vendored", "third_party", "thirdparty", "_vendor")
           for part in comps) and candidate:
        kinds.add("vendored")
    if project_hit and (lowered.endswith((".egg-link", ".pth"))
                        or lowered.startswith("__editable__")):
        kinds.add("pth-egglink")
    if lowered.endswith(".whl"):
        kinds.add("wheel")
    if lowered.endswith((".tar.gz", ".tgz", ".zip")):
        kinds.add("sdist")
    if ("pip" in comps and any(part in ("http", "http-v2", "wheels", "cache")
                               for part in comps)
            and (lowered.endswith(".body") or kinds & {"wheel", "sdist"} or project_hit)):
        kinds.add("pip-cache")
    if lowered.endswith((".dist-info", ".egg-info")):
        kinds.add("dist-info" if lowered.endswith(".dist-info") else "egg-info")
    if lowered in ("metadata", "pkg-info") and len(comps) > 1:
        parent = comps[-2].lower()
        if parent.endswith((".dist-info", ".egg-info")):
            kinds.add("dist-info" if parent.endswith(".dist-info") else "egg-info")
    if in_tree:
        if base.endswith(PY_SUFFIX) and not kinds and (
            fix_hit or (len(rel) == 2 and rel[0] in fix_dirnames)
        ):
            kinds.add("worktree-fix")
        if len(rel) == 1 and base in ("setup.py", "setup.cfg", "pyproject.toml", "PKG-INFO"):
            kinds.add("worktree-pkgmeta")
    return kinds


# --------------------------------------------------------------------------
# Version parsing (Name/Version lines and version assignments only).
# --------------------------------------------------------------------------


def version_from_metadata(text: str) -> tuple[str, str]:
    """``(name, version)`` from METADATA/PKG-INFO text (first hit each)."""
    name = ""
    version = ""
    for line in text.splitlines():
        lower = line.lower()
        if lower.startswith("name:") and not name:
            name = line.split(":", 1)[1].strip()
        elif lower.startswith("version:") and not version:
            version = line.split(":", 1)[1].strip()
        if name and version:
            break
    return name, version


VERSION_ASSIGN_RE = re.compile(r"""^[^#\n]*?\bversion\s*=\s*['"]?([^'"\s#;,]+)['"]?""", re.MULTILINE)
NAME_ASSIGN_RE = re.compile(
    r"""^\s*name\s*=\s*(?P<quote>['"]?)(?P<name>[A-Za-z0-9_.-]+)(?P=quote)\s*(?:,|$)""",
    re.MULTILINE,
)


def version_from_packaging_file(filename: str, text: str) -> str:
    """Version from a setup.cfg/pyproject/setup.py/PKG-INFO/METADATA snippet."""
    base = filename.split("/")[-1]
    if base in ("PKG-INFO", "METADATA"):
        return version_from_metadata(text)[1]
    match = VERSION_ASSIGN_RE.search(text)
    if not match:
        return ""
    candidate = match.group(1).strip()
    try:
        Version(candidate)
    except InvalidVersion:
        return ""
    return candidate


def compare_versions(left: str, right: str) -> str:
    """Compare public PEP 440 releases; local/build labels do not prove recency."""
    if not left or not right:
        return "unverifiable"
    try:
        installed, base = Version(Version(left).public), Version(Version(right).public)
    except InvalidVersion:
        return "unverifiable"
    if installed > base:
        return "newer"
    if installed < base:
        return "older"
    return "same"


# --------------------------------------------------------------------------
# Registry client (mirror.gcr.io first, Docker Hub anonymous fallback).
# --------------------------------------------------------------------------


@dataclass
class Registry:
    """Minimal pull-through client that streams blobs without materializing."""

    repo: str = REPO
    mirror: str = MIRROR
    timeout: int = 60
    tries: int = 3
    _hub_token: str | None = field(default=None, repr=False)

    def _get(self, url: str, headers: dict | None = None) -> tuple[bytes, int]:
        last: Exception | None = None
        for attempt in range(self.tries):
            try:
                req = urllib.request.Request(url, headers=headers or {})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read(), resp.status
            except Exception as exc:
                last = exc
                time.sleep(2**attempt)
        raise RuntimeError(f"GET {url} failed after {self.tries} tries: {last}")

    def manifest(self, digest: str) -> tuple[dict, str]:
        """Manifest document; returns ``(parsed, method)``."""
        path = f"/v2/{self.repo}/manifests/{digest}"
        try:
            body, _ = self._get(self.mirror + path, {"Accept": MANIFEST_ACCEPT})
            return parse_manifest(body), "mirror.gcr.io-stream"
        except Exception as mirror_err:
            token = self._hub_auth()
            body, _ = self._get(
                DOCKERHUB + path,
                {"Accept": MANIFEST_ACCEPT, "Authorization": f"Bearer {token}"},
            )
            return parse_manifest(body), f"dockerhub-stream (mirror failed: {mirror_err})"

    def _hub_auth(self) -> str:
        if self._hub_token is None:
            scope = f"repository:{self.repo}:pull"
            url = f"{DOCKER_TOKEN}?service=registry.docker.io&scope={scope}"
            body, _ = self._get(url)
            self._hub_token = json.loads(body)["token"]
        return self._hub_token

    def open_blob(self, digest: str) -> tuple:
        """Open a blob stream; the caller reads and closes it."""
        last: Exception | None = None
        for attempt in range(self.tries):
            try:
                url = f"{self.mirror}/v2/{self.repo}/blobs/{digest}"
                req = urllib.request.Request(url)
                return urllib.request.urlopen(req, timeout=self.timeout), ("mirror.gcr.io-stream")
            except Exception as exc:
                last = exc
                time.sleep(2**attempt)
        try:
            token = self._hub_auth()
            url = f"{DOCKERHUB}/v2/{self.repo}/blobs/{digest}"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            return urllib.request.urlopen(req, timeout=self.timeout), (
                f"dockerhub-stream (mirror failed: {last})"
            )
        except Exception as exc:
            raise RuntimeError(
                f"blob {digest}: mirror failed ({last}); hub failed ({exc})"
            ) from exc


# --------------------------------------------------------------------------
# Layer streaming: name index plus hashes of candidate files only.
# --------------------------------------------------------------------------


def apply_whiteout(overlay: dict, clean: str) -> bool:
    """Apply an OCI ``.wh.`` whiteout marker; True when it consumed the entry."""
    comps = clean.split("/")
    base = comps[-1]
    if base == ".wh..wh..opq":
        prefix = "/".join(comps[:-1]) + "/"
        for key in [key for key in overlay if key.startswith(prefix)]:
            del overlay[key]
        return True
    if base.startswith(".wh."):
        target = "/".join(comps[:-1] + [base[len(".wh.") :]])
        for key in [key for key in overlay if key == target or key.startswith((target + "/", target + "!/"))]:
            del overlay[key]
        return True
    return False


@dataclass
class StreamState:
    """Accumulator for one image's streamed layers (no contents retained)."""

    repo_root: str
    variants: set[str]
    fix_paths: set[str]
    fix_basenames: set[str]
    fix_dirnames: set[str]
    overlay: dict = field(default_factory=dict)
    worktree_py_index: list = field(default_factory=list)
    dists: list = field(default_factory=list)
    base_meta: list = field(default_factory=list)
    hash_bytes: int = 0
    worktree_hashed: int = 0
    truncated: bool = False


def _hash_bytes(handle: io.BufferedIOBase, limit: int) -> tuple[str, int, bool]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = handle.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            return "", size, True
        digest.update(chunk)
    return digest.hexdigest(), size, False


def _read_limited(handle: io.BufferedIOBase) -> bytes:
    return handle.read(META_READ_LIMIT + 1)[:META_READ_LIMIT]


def _wants_py_hash(clean: str, kinds: set[str]) -> bool:
    if not clean.endswith(PY_SUFFIX):
        return False
    if "worktree-fix" in kinds:
        return True
    return bool(kinds & {"site-packages", "dist-packages", "build-lib", "tox-nox", "vendored"})


def _wants_worktree_project_hash(clean: str, state: StreamState) -> bool:
    if state.worktree_hashed >= MAX_WORKTREE_HASHED or not state.variants:
        return False
    root = state.repo_root.strip("/") + "/"
    if not clean.startswith(root) or not clean.endswith(PY_SUFFIX):
        return False
    top = clean[len(root):].split("/")[0].lower().replace("-", "_")
    variant_tops = {variant.lower().replace("-", "_") for variant in state.variants}
    return top in variant_tops


def _record_meta(
    state: StreamState, clean: str, kinds: set[str], raw: bytes, *, in_tree: bool
) -> None:
    text = raw.decode("utf-8", errors="replace")
    base = clean.split("/")[-1]
    name, version = version_from_metadata(text) if base in ("METADATA", "PKG-INFO") else ("", "")
    if base in ("setup.py", "setup.cfg", "pyproject.toml") and in_tree:
        version = version_from_packaging_file(base, text)
        name_match = NAME_ASSIGN_RE.search(text)
        name = (name_match["name"] if name_match
                and (base == "setup.cfg" or name_match["quote"]) else "")
    if name or version:
        meta = {"path": clean, "name": name, "version": version}
        state.overlay.setdefault(clean, {})["metadata"] = meta
        target = state.base_meta if in_tree and "worktree-pkgmeta" in kinds else state.dists
        target[:] = [item for item in target if item["path"] != clean]
        target.append(meta)


def scan_archive(handle, outer: str, kinds: set[str], state: StreamState, size: int) -> None:
    """Inspect bounded wheel/sdist/cache archives, retaining only hashes/metadata."""
    if size > ARCHIVE_BYTES_LIMIT:
        state.truncated = True
        return
    with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as spool:
        while chunk := handle.read(1024 * 1024):
            spool.write(chunk)
        spool.seek(0)
        magic = spool.read(4)
        spool.seek(0)

        def record(inner, content, inner_size):
            name = clean_member_name(inner)
            if ".." in name.split("/") or ".git" in name.split("/"):
                return
            path = outer + "!/" + name
            base = name.split("/")[-1]
            if base in ("METADATA", "PKG-INFO"):
                raw = _read_limited(content)
                _record_meta(state, path, kinds, raw, in_tree=False)
                state.overlay.setdefault(path, {}).update(kinds=sorted(kinds), size=inner_size)
            elif name.endswith(PY_SUFFIX) and (
                base in state.fix_basenames or path_matches_project(name, state.variants)
                or bool(state.fix_dirnames & set(name.split("/")))
            ):
                sha, consumed, cut = _hash_bytes(content, MAX_HASH_BYTES - state.hash_bytes)
                state.hash_bytes += consumed
                state.truncated |= cut
                if not cut:
                    state.overlay[path] = {"kinds": sorted(kinds), "size": inner_size, "sha256": sha}

        try:
            if magic.startswith(b"PK"):
                archive_kinds = (kinds - {"sdist"}) | {"wheel"}
                kinds = archive_kinds
                with zipfile.ZipFile(spool) as archive:
                    for member in archive.infolist():
                        if member.is_dir() or member.file_size > ARCHIVE_BYTES_LIMIT:
                            continue
                        with archive.open(member) as content:
                            record(member.filename, content, member.file_size)
            elif magic.startswith(b"\x1f\x8b"):
                kinds = (kinds - {"wheel"}) | {"sdist"}
                with tarfile.open(fileobj=spool, mode="r|gz") as archive:
                    for member in archive:
                        if member.isfile():
                            content = archive.extractfile(member)
                            if content is not None:
                                record(member.name, content, member.size)
        except (zipfile.BadZipFile, tarfile.TarError, EOFError):
            state.truncated = True


def scan_layer_stream(stream: io.RawIOBase | io.BufferedIOBase, state: StreamState) -> None:
    """Fold one gzip layer into ``state``; records paths/hashes/versions only."""
    with gzip.GzipFile(fileobj=stream) as gunzip, tarfile.open(
        fileobj=gunzip, mode="r|*"
    ) as tar:
        for member in tar:
            clean = clean_member_name(member.name)
            if not clean or ".." in clean.split("/"):
                continue
            if apply_whiteout(state.overlay, clean):
                state.dists[:] = [item for item in state.dists if item["path"] in state.overlay]
                state.base_meta[:] = [item for item in state.base_meta if item["path"] in state.overlay]
                continue
            if member.isdir():
                continue
            if len(state.overlay) >= MAX_PATHS:
                state.truncated = True
                continue
            kinds = classify_member(
                clean,
                repo_root=state.repo_root,
                variants=state.variants,
                fix_basenames=state.fix_basenames | state.fix_paths,
                fix_dirnames=state.fix_dirnames,
            )
            root = state.repo_root.strip("/") + "/"
            in_tree = clean.startswith(root)
            if in_tree and clean.endswith(PY_SUFFIX) and len(state.worktree_py_index) < MAX_PATHS:
                state.worktree_py_index.append(clean[len(root):])
            want_meta = bool(kinds & {"dist-info", "egg-info", "worktree-pkgmeta"})
            want_py = _wants_py_hash(clean, kinds)
            want_project = not kinds and _wants_worktree_project_hash(clean, state)
            if not (kinds or want_project):
                continue
            if not member.isfile():
                continue
            for old in [path for path in state.overlay if path.startswith(clean + "!/")]:
                del state.overlay[old]
            if state.hash_bytes >= MAX_HASH_BYTES:
                state.truncated = True
                if kinds:
                    entry = state.overlay.get(clean, {})
                    entry["kinds"] = sorted(set(entry.get("kinds", [])) | kinds)
                    state.overlay[clean] = entry
                continue
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            if kinds & {"wheel", "sdist"} or (
                "pip-cache" in kinds and clean.endswith(".body")
            ):
                scan_archive(extracted, clean, kinds, state, member.size)
            elif want_meta:
                raw = _read_limited(extracted)
                state.hash_bytes += len(raw)
                _record_meta(state, clean, kinds, raw, in_tree=in_tree)
                entry = state.overlay.get(clean, {})
                entry["kinds"] = sorted(set(entry.get("kinds", [])) | kinds)
                entry["size"] = member.size
                state.overlay[clean] = entry
            elif want_py or want_project:
                remaining = MAX_HASH_BYTES - state.hash_bytes
                sha, size, cut = _hash_bytes(extracted, remaining)
                state.hash_bytes += size
                if cut:
                    state.truncated = True
                    continue
                entry = state.overlay.get(clean, {})
                entry["kinds"] = sorted(
                    set(entry.get("kinds", [])) | (kinds or {"worktree-project"})
                )
                entry["size"] = size
                entry["sha256"] = sha
                state.overlay[clean] = entry
                if want_project:
                    state.worktree_hashed += 1
            elif kinds:
                entry = state.overlay.get(clean, {})
                entry["kinds"] = sorted(set(entry.get("kinds", [])) | kinds)
                entry["size"] = member.size
                state.overlay[clean] = entry


# --------------------------------------------------------------------------
# Per-image analysis (paths/hashes/versions to a CSV row).
# --------------------------------------------------------------------------


def resolve_project(
    hint: str, hint_basis: str, state: StreamState
) -> tuple[str, str]:
    """Project name from the hint refined by image evidence.

    The hint wins when a worktree fix file or an installed dist-info
    confirms it; otherwise the worktree fix-file owners or the single
    fix-relevant dist-info name it; else unknown.
    """
    fix_tops: list[str] = []
    for path, entry in state.overlay.items():
        if "worktree-fix" not in entry.get("kinds", []):
            continue
        root = state.repo_root.strip("/") + "/"
        rel = path[len(root):].split("/") if path.startswith(root) else []
        if len(rel) > 1 and not set(entry.get("kinds", [])) & BUILT_KINDS:
            top = rel[1] if rel[0] == "src" and len(rel) > 2 else rel[0]
            if top not in fix_tops:
                fix_tops.append(top)
    dist_names: list[str] = []
    for dist in state.dists:
        name = dist.get("name", "")
        if name and name not in dist_names:
            dist_names.append(name)
    base_names = {meta["name"] for meta in state.base_meta if meta.get("name")}
    if hint:
        hint_norm = norm_variant(hint)
        if hint in fix_tops or hint_norm in {norm_variant(top) for top in fix_tops}:
            return hint, f"{hint_basis}+worktree" if hint_basis else "worktree-fix-dir"
        if len(base_names) == 1:
            return next(iter(base_names)), "root-packaging-name"
        if hint_basis in ("patch-source-path", "patch-src-layout", "test-dir+import"):
            return hint, hint_basis
        if fix_tops and len(fix_tops) == 1:
            return fix_tops[0], "worktree-fix-dir"
        fix_dist = [
            name
            for name in dist_names
            if norm_variant(name) in {norm_variant(top) for top in fix_tops}
        ]
        if fix_dist:
            return fix_dist[0], "dist-info+worktree"
        return "", "unknown"
    if len(fix_tops) == 1:
        fix_norm = norm_variant(fix_tops[0])
        for name in dist_names:
            if norm_variant(name) == fix_norm:
                return name, "dist-info+worktree"
        return fix_tops[0], "worktree-fix-dir"
    for name in dist_names:
        if norm_variant(name) in {norm_variant(top) for top in fix_tops}:
            return name, "dist-info+worktree"
    if len(dist_names) == 1 and fix_tops:
        return dist_names[0], "dist-info"
    if len(base_names) == 1:
        return next(iter(base_names)), "root-packaging-name"
    return "", "unknown"


def summarize_state(
    state: StreamState,
    *,
    task_id: str,
    run: str,
    ledger_digest: str,
    image_digest: str,
    hint: str,
    hint_basis: str,
    method: str,
) -> dict:
    """Reduce one image's stream state to a CSV-ready row dict."""
    project, basis = resolve_project(hint, hint_basis, state)
    variants = name_variants(project) if project else set()
    built = {
        path: entry
        for path, entry in state.overlay.items()
        if set(entry.get("kinds", [])) & BUILT_KINDS
        and bool(variants) and path_matches_project(path, variants)
    }
    fix_entries = {
        path: entry
        for path, entry in state.overlay.items()
        if "worktree-fix" in entry.get("kinds", [])
        and path.startswith(state.repo_root.strip("/") + "/")
    }
    # Base = worktree sources that are not themselves built artefacts: a
    # build/lib copy carrying worktree-fix kind must never seed the base.
    base_entries = {
        path: entry
        for path, entry in fix_entries.items()
        if not (set(entry.get("kinds", [])) & BUILT_KINDS)
    }
    fix_files = sorted(
        path[len(state.repo_root.strip("/")) + 1:] for path in base_entries
    )
    comparisons: list[dict] = []
    for path, entry in built.items():
        if not path.endswith(PY_SUFFIX) or not entry.get("sha256"):
            continue
        # Full package-relative suffix avoids collisions between __init__.py
        # or identically named modules in different packages.
        matches = [
            (base_path, base_entry) for base_path, base_entry in base_entries.items()
            if base_entry.get("sha256")
            and path.endswith("/" + base_path[len(state.repo_root.strip("/")) + 1:].removeprefix("src/"))
        ]
        if len(matches) != 1:
            continue
        base_path, base_entry = matches[0]
        comparisons.append({
            "base_path": base_path, "copy_path": path,
            "base_sha256": base_entry["sha256"], "copy_sha256": entry["sha256"],
            "differs": entry["sha256"] != base_entry["sha256"],
        })
    fix_differs = ("yes" if any(item["differs"] for item in comparisons)
                   else "no" if comparisons else "unverifiable")
    kinds = sorted(
        {
            kind
            for entry in built.values()
            for kind in entry.get("kinds", [])
            if kind in BUILT_KINDS
        }
    )
    ordered_paths = sorted(built, key=lambda p: (not any(c["copy_path"] == p for c in comparisons), p.count("/"), p))
    matching_dists = [dist for dist in state.dists if project
                      and norm_variant(dist.get("name", "")) == norm_variant(project)
                      and dist["path"] in built]
    versions = {dist.get("version", "") for dist in matching_dists} - {""}
    base_versions = {item.get("version", "") for item in state.base_meta} - {""}
    installed_version = next(iter(versions)) if len(versions) == 1 else ""
    base_version = next(iter(base_versions)) if len(base_versions) == 1 else ""
    version_cmp = compare_versions(installed_version, base_version)
    has_copy: bool | None = bool(built) if project else None
    needs_repair = bool(built) and (fix_differs == "yes" or version_cmp == "newer")
    methods = [method]
    if state.truncated:
        methods.append("truncated-index")
    result = build_row(
        task_id=task_id,
        run=run,
        ledger_digest=ledger_digest,
        image_digest=image_digest,
        project=project,
        project_basis=basis,
        has_copy=has_copy,
        needs_repair=needs_repair,
        fix_differs=fix_differs,
        fix_files=fix_files,
        kinds=kinds,
        paths=ordered_paths,
        n_built=len(built),
        base_version=base_version,
        installed_version=installed_version,
        version_cmp=version_cmp,
        method="+".join(methods),
        error="",
    )
    result["comparisons"] = json.dumps(comparisons, separators=(",", ":"))
    if state.truncated and result["comparison_status"] == "clean":
        result["comparison_status"] = "unverifiable"
    return result




# --------------------------------------------------------------------------
# Per-task scan.
# --------------------------------------------------------------------------


def read_text(path: str) -> str:
    with open(path, errors="replace") as handle:
        return handle.read()


def find_snapshot_task(hf_store: str, task_id: str) -> str | None:
    """Snapshot task dir for ``task_id`` across all pulls in the HF store."""
    try:
        pulls = sorted(
            entry
            for entry in os.listdir(hf_store)
            if os.path.isdir(os.path.join(hf_store, entry))
        )
    except OSError:
        return None
    for pull in pulls:
        candidate = os.path.join(hf_store, pull, "tasks", task_id)
        if os.path.isdir(candidate):
            return candidate
    return None


def resolve_package(row: dict, *, snapshot_dir: str, variants_dir: str) -> str | None:
    """Local package dir for a ledger row (snapshot task or variant package)."""
    if row["run"] == "original":
        return find_snapshot_task(snapshot_dir, row["task_id"])
    short = row.get("run_digest", "").removeprefix("sha256:")[:12]
    if not short:
        return None
    candidate = os.path.join(variants_dir, f"mimo-v2.6-rl__{row['task_id']}", short)
    return candidate if os.path.isdir(candidate) else None


def task_fix_spec(package: str) -> dict:
    """Fix-region spec from a task package (test.patch + workdir + digest)."""
    try:
        patch_text = read_text(os.path.join(package, "tests", "test.patch"))
    except OSError as exc:
        return {"error": f"no test.patch: {exc}"}
    paths = parse_patch_paths(patch_text)
    source_paths, test_paths = split_patch_paths(paths)
    added_imports = imports_from_patch(patch_text)
    project, basis = guess_project(source_paths, test_paths, added_imports)
    fix_base, fix_dirs = implied_fix_names(test_paths)
    fix_basenames = {path.split("/")[-1] for path in source_paths} | set(fix_base)
    try:
        workdir = parse_task_workdir(read_text(os.path.join(package, "task.toml")))
    except OSError:
        workdir = None
    if workdir:
        prefer_root = workdir.lstrip("/")
    else:
        try:
            setup_text = read_text(os.path.join(package, "environment", "setup", "setup.sh"))
            prefer_root = parse_setup_cwd(setup_text).lstrip("/")
        except OSError:
            prefer_root = "testbed"
    try:
        dockerfile = read_text(os.path.join(package, "environment", "Dockerfile"))
        image = parse_image_digest(dockerfile)
    except OSError as exc:
        return {"error": f"no package Dockerfile: {exc}"}
    return {
        "project": project,
        "project_basis": basis,
        "source_paths": source_paths,
        "fix_basenames": sorted(fix_basenames),
        "fix_dirnames": sorted(set(fix_dirs)),
        "prefer_root": prefer_root,
        "image": image,
        "error": "" if image else "Dockerfile names no digest",
    }


def scan_image(
    *,
    task_id: str,
    run: str,
    ledger_digest: str,
    fix_spec: dict,
    image_digest: str,
    registry: Registry,
) -> dict:
    """Scan one image by digest; always returns a CSV-ready row dict.

    Needs only the public registry; no container or paid worker is launched.
    Only paths, sizes, hashes, and Name/Version lines are recorded; source
    contents are never retained as evidence.
    """
    started = time.monotonic()
    project_hint = fix_spec.get("project", "")
    hint_basis = fix_spec.get("project_basis", "")
    variants = name_variants(project_hint) if project_hint else set()
    state = StreamState(
        repo_root=fix_spec.get("prefer_root", "testbed") or "testbed",
        variants=variants,
        fix_paths=set(fix_spec.get("source_paths", [])),
        fix_basenames=set(fix_spec.get("fix_basenames", [])),
        fix_dirnames=set(fix_spec.get("fix_dirnames", [])),
    )
    methods: list[str] = []
    try:
        manifest, manifest_method = registry.manifest(image_digest)
        methods.append(manifest_method)
        for layer in manifest["layers"]:
            resp, blob_method = registry.open_blob(layer)
            try:
                scan_layer_stream(resp, state)
            finally:
                resp.close()
            if blob_method not in methods:
                methods.append(blob_method)
    except Exception as exc:
        result = build_row(
            task_id=task_id,
            run=run,
            ledger_digest=ledger_digest,
            image_digest=image_digest,
            project=project_hint,
            project_basis=hint_basis,
            method="+".join(methods),
            error=f"{type(exc).__name__}: {str(exc)[:200]}",
        )
        result["_elapsed_s"] = round(time.monotonic() - started, 1)
        return result
    result = summarize_state(
        state,
        task_id=task_id,
        run=run,
        ledger_digest=ledger_digest,
        image_digest=image_digest,
        hint=project_hint,
        hint_basis=hint_basis,
        method="+".join(methods),
    )
    result["_elapsed_s"] = round(time.monotonic() - started, 1)
    return result


def scan_task(
    row: dict,
    *,
    snapshot_dir: str,
    variants_dir: str,
    registry: Registry,
) -> dict:
    """Scan one ledger row's image; always returns a CSV-ready row dict."""
    package = resolve_package(row, snapshot_dir=snapshot_dir, variants_dir=variants_dir)
    if package is None:
        return build_row(
            task_id=row["task_id"],
            run=row["run"],
            ledger_digest=row.get("run_digest", row.get("ledger_digest", "")),
            image_digest=row.get("image_digest", ""),
            method="",
            error="no local task package (snapshot/variant dir missing)",
        )
    spec = task_fix_spec(package)
    if spec.get("error"):
        return build_row(
            task_id=row["task_id"],
            run=row["run"],
            ledger_digest=row.get("run_digest", row.get("ledger_digest", "")),
            image_digest=row.get("image_digest", ""),
            method="",
            error=spec["error"],
        )
    image = row.get("image_digest") or spec.get("image")
    if not image:
        return build_row(
            task_id=row["task_id"],
            run=row["run"],
            ledger_digest=row.get("run_digest", row.get("ledger_digest", "")),
            image_digest="",
            project=spec.get("project", ""),
            project_basis=spec.get("project_basis", ""),
            method="",
            error="no image digest (row and Dockerfile both empty)",
        )
    return scan_image(
        task_id=row["task_id"],
        run=row["run"],
        ledger_digest=row.get("run_digest", row.get("ledger_digest", "")),
        fix_spec=spec,
        image_digest=image,
        registry=registry,
    )


def load_usable_rows(ledger_path: str) -> list[dict]:
    with open(ledger_path, newline="") as handle:
        return [row for row in csv.DictReader(handle) if row["status"] == "usable"]


def write_csv(path: str, rows: list[dict]) -> None:
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in CSV_COLUMNS})


def append_spend_log(path: str, event: dict) -> None:
    event = {"at": datetime.now(UTC).isoformat(timespec="seconds"), **event}
    with open(path, "a") as handle:
        handle.write(json.dumps(event) + "\n")


# --------------------------------------------------------------------------
# Random-sample sweep: prevalence with a Wilson bound.
# --------------------------------------------------------------------------


def draw_sample(task_ids: list[str], n: int, seed: int) -> list[str]:
    """Seeded random sample of task ids, returned in sorted order."""
    rng = random.Random(seed)
    if n > len(task_ids):
        raise ValueError(f"sample {n} exceeds population {len(task_ids)}")
    return sorted(rng.sample(sorted(task_ids), n))


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for k successes in n trials."""
    if n <= 0:
        return (0.0, 0.0)
    if not 0 <= k <= n:
        raise ValueError(f"k={k} outside [0, {n}]")
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def load_reuse(paths: list[str]) -> dict[str, dict]:
    """Prior scan rows keyed by task_id (later files win; unscanned never reused)."""
    reused: dict[str, dict] = {}
    for path in paths:
        with open(path, newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("has_built_copy") in ("yes", "no"):
                    reused[row["task_id"]] = row
    return reused


def load_sample_rows(sample_csv: str) -> list[dict]:
    """Task rows from a prior sample CSV (HAR-177 sample100.csv reuse)."""
    with open(sample_csv, newline="") as handle:
        rows = []
        for row in csv.DictReader(handle):
            rows.append(
                {
                    "task_id": row["task_id"],
                    "run": row.get("run", "original"),
                    "run_digest": row.get("ledger_digest", ""),
                    "image_digest": row.get("image_digest", ""),
                    "status": "usable",
                }
            )
    return rows


def prevalence(rows: list[dict]) -> dict:
    """Confirmed-difference detection rate over the precommitted sample.

    Unknown comparisons remain in the denominator: this is a lower-bound
    detection rate, not an assertion that unknown images are leak-free.
    """
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["has_built_copy"]] = counts.get(row["has_built_copy"], 0) + 1
    scanned = counts.get("yes", 0) + counts.get("no", 0)
    confirmed = sum(row.get("needs_repair") == "true" for row in rows)
    lo, hi = wilson(confirmed, len(rows))
    return {"counts": counts, "scanned": scanned, "yes": confirmed,
            "copies": counts.get("yes", 0), "n": len(rows), "lo": lo, "hi": hi}


# --------------------------------------------------------------------------
# Known-positive validation: stop and report on any disagreement.
# --------------------------------------------------------------------------


def validate_result(short_id: str, result: dict) -> list[str]:
    """Disagreements between a scan result and the known-positive facts."""
    problems: list[str] = []
    if result["has_built_copy"] != "yes":
        problems.append(
            f"{short_id}: expected has_built_copy=yes, "
            f"got {result['has_built_copy']} ({result['error']})"
        )
        return problems
    hint = KNOWN_KIND_HINT[short_id]
    if hint not in result["built_copy_kinds"].split(";"):
        problems.append(
            f"{short_id}: expected built-copy kind {hint!r}, "
            f"got kinds={result['built_copy_kinds']!r} "
            f"paths={result['built_copy_paths'][:300]!r}"
        )
    want_project = KNOWN_PROJECT[short_id]
    if norm_variant(result["project"]) != norm_variant(want_project):
        problems.append(
            f"{short_id}: expected project {want_project!r}, got {result['project']!r} "
            f"(basis={result['project_basis']!r})"
        )
    if result["fix_differs"] != "yes":
        problems.append(
            f"{short_id}: expected fix_differs=yes (built copy holds the fixed "
            f"module), got {result['fix_differs']!r} fix_files={result['fix_files'][:200]!r}"
        )
    return problems


def run_validation(
    *,
    snapshot_dir: str,
    registry: Registry,
    workers: int = 2,
    out_csv: str | None = None,
) -> tuple[list[dict], list[str]]:
    """Scan the 2 known-positive original images; return ``(rows, problems)``."""
    rows = [
        {
            "task_id": f"format-code-task-{short}",
            "run": "original",
            "run_digest": "",
            "image_digest": "",
            "status": "usable",
        }
        for short in KNOWN_POSITIVES
    ]
    results = _scan_concurrent(
        rows,
        snapshot_dir=snapshot_dir,
        variants_dir="",
        registry=registry,
        workers=workers,
    )
    problems: list[str] = []
    for row in results:
        short = row["task_id"].removeprefix("format-code-task-")
        problems.extend(validate_result(short, row))
    if out_csv is not None:
        write_csv(out_csv, results)
    return results, problems


def _scan_concurrent(
    rows: list[dict],
    *,
    snapshot_dir: str,
    variants_dir: str,
    registry: Registry,
    workers: int,
    checkpoint: str | None = None,
) -> list[dict]:
    def one(row: dict) -> dict:
        return scan_task(
            row,
            snapshot_dir=snapshot_dir,
            variants_dir=variants_dir,
            registry=registry,
        )

    ordered: list[dict | None] = [None] * len(rows)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_idx = {pool.submit(one, row): idx for idx, row in enumerate(rows)}
        for future in as_completed(future_to_idx):
            ordered[future_to_idx[future]] = future.result()
            if checkpoint:
                write_csv(checkpoint, [result for result in ordered if result is not None])
    return [row for row in ordered if row is not None]


MODAL_CPU = 0.25
MODAL_MEMORY_GIB = 0.5
MODAL_TIMEOUT = 660
MODAL_RATE = MODAL_CPU * 0.0000131 + MODAL_MEMORY_GIB * 0.00000222
MODAL_RESERVE_USD = 0.06  # setup/egress, above HAR-177's posted byte-ratio cost


def modal_ceiling(n: int) -> float:
    """Reserved-resource ceiling including 60s startup/teardown per input."""
    return n * (MODAL_TIMEOUT + 60) * MODAL_RATE + MODAL_RESERVE_USD


def modal_billing_rows(app_id: str) -> list[dict]:
    """Read-only billing receipt; never confuse another app's spend with ours."""
    result = subprocess.run(
        ["modal", "billing", "report", "--for", "today", "--show-resources", "--json"],
        check=True, capture_output=True, text=True, timeout=120,
    )
    return [row for row in json.loads(result.stdout) if row["object_id"] == app_id]


def run_modal_batch(rows: list[dict], args: argparse.Namespace) -> list[dict]:
    """One parent-authorized app: five-image pilot then the unchanged sample."""
    import modal

    cap = args.approved_cap_usd
    ceiling = modal_ceiling(len(rows))
    if not 0 < cap <= 0.4 or ceiling > cap:
        raise ValueError(f"Modal ceiling ${ceiling:.5f} exceeds authorized ${cap:.2f}")
    payloads = []
    for row in rows:
        package = resolve_package(row, snapshot_dir=args.snapshot, variants_dir=args.variants)
        if package is None:
            raise ValueError(f"missing local package: {row['task_id']}")
        spec = task_fix_spec(package)
        if spec.get("error"):
            raise ValueError(f"{row['task_id']}: {spec['error']}")
        payloads.append({"row": row, "spec": spec})
    app = modal.App("har185-built-copy")
    image = (modal.Image.debian_slim(python_version="3.12")
             .pip_install("packaging==26.3")
             .add_local_file(os.path.abspath(__file__), "/root/scan.py"))

    @app.function(
        image=image, cpu=(MODAL_CPU, MODAL_CPU), memory=(512, 512),
        timeout=MODAL_TIMEOUT, serialized=True, max_containers=32,
        scaledown_window=2, retries=0,
    )
    def scan_one(payload):
        sys.path.insert(0, "/root")
        import scan as worker
        row = payload["row"]
        return worker.scan_image(
            task_id=row["task_id"], run=row["run"],
            ledger_digest=row.get("run_digest", ""),
            fix_spec=payload["spec"], image_digest=row["image_digest"],
            registry=worker.Registry(),
        )

    results: list[dict] = []
    app_id = ""

    def consume(batch):
        for payload, result in zip(
            batch, scan_one.map(batch, return_exceptions=True), strict=True
        ):
            if isinstance(result, BaseException):
                row = payload["row"]
                result = build_row(
                    task_id=row["task_id"], run=row["run"],
                    ledger_digest=row.get("run_digest", ""), image_digest=row["image_digest"],
                    project=payload["spec"]["project"], method="modal+mirror.gcr.io-stream",
                    error=f"{type(result).__name__}: remote input failed or timed out",
                )
            results.append(result)
            write_csv(args.out, results)

    with app.run():
        app_id = app.app_id
        append_spend_log(args.spend_log, {
            "event": "modal_start", "app_id": app_id, "n": len(rows),
            "authorized_usd": cap, "resource_ceiling_usd": ceiling,
            "cpu_limit": MODAL_CPU, "memory_limit_mib": 512,
            "input_timeout_s": MODAL_TIMEOUT, "reserve_usd": MODAL_RESERVE_USD,
            "price_source": "https://modal.com/pricing",
        })
        pilot_n = min(5, len(payloads))
        consume(payloads[:pilot_n])
        pilot_seconds = sum(float(result.get("_elapsed_s", MODAL_TIMEOUT)) for result in results)
        projection = pilot_seconds / pilot_n * len(rows) * MODAL_RATE + MODAL_RESERVE_USD
        billing = modal_billing_rows(app_id)
        posted = sum(float(row["cost"]) for row in billing)
        append_spend_log(args.spend_log, {
            "event": "modal_pilot", "app_id": app_id, "n": pilot_n,
            "projection_usd": projection, "posted_usd": posted, "billing_rows": billing,
            "billing_status": "posted usage may lag; resource ceiling also enforced",
        })
        if projection > cap or posted > cap:
            append_spend_log(args.spend_log, {"event": "STOP_projection_over_cap", "app_id": app_id})
            raise RuntimeError("pilot projection exceeds authorized cap; no remaining inputs launched")
        consume(payloads[pilot_n:])
    billing = modal_billing_rows(app_id)
    append_spend_log(args.spend_log, {
        "event": "modal_stop", "app_id": app_id, "n": len(results), "billing_rows": billing,
        "posted_usd": sum(float(row["cost"]) for row in billing),
        "billing_status": "reconcile delayed rows before final receipt",
    })
    return results


# --------------------------------------------------------------------------
# CLI.
# --------------------------------------------------------------------------


def run_sample(args: argparse.Namespace, registry: Registry) -> int:
    """Sample sweep: reuse the HAR-177 task list or draw fresh, scan, report."""
    if args.reuse_sample:
        rows = load_sample_rows(args.reuse_sample)
        seed_note = f"reuse-sample={args.reuse_sample} seed=177100"
    else:
        usable = load_usable_rows(args.ledger)
        by_id = {row["task_id"]: row for row in usable}
        drawn = draw_sample(list(by_id), args.n, args.seed)
        rows = [by_id[tid] for tid in drawn]
        seed_note = f"seed={args.seed}"
        append_spend_log(
            args.spend_log,
            {
                "event": "sample_draw",
                "n": args.n,
                "seed": args.seed,
                "population": len(by_id),
                "tasks": drawn,
            },
        )
    if args.tasks:
        wanted = set(args.tasks.split(","))
        rows = [row for row in rows if row["task_id"] in wanted]
    reuse = load_reuse([path for path in args.reuse.split(",") if path]) if args.reuse else {}
    reused = {row["task_id"]: reuse[row["task_id"]] for row in rows if row["task_id"] in reuse}
    need = [row for row in rows if row["task_id"] not in reused]
    started = time.monotonic()
    append_spend_log(
        args.spend_log,
        {"event": "sample_start", "reused": len(reused), "to_scan": len(need),
         "mode": args.mode, "sample_sha256": hashlib.sha256(
             read_text(args.reuse_sample).encode()).hexdigest() if args.reuse_sample else "",
         "seed": 177100 if args.reuse_sample else args.seed},
    )
    results: list[dict] = list(reused.values())
    if need and args.mode == "modal":
        results += run_modal_batch(need, args)
    elif need:
        results += _scan_concurrent(
            need,
            snapshot_dir=args.snapshot,
            variants_dir=args.variants,
            registry=registry,
            workers=args.workers,
            checkpoint=args.out,
        )
    results.sort(key=lambda row: row["task_id"])
    write_csv(args.out, results)
    report = prevalence(results)
    print(
        f"sample n={len(results)} {seed_note} "
        f"confirmed={report['yes']}/{report['n']} "
        f"detection_rate={report['yes'] / report['n'] if report['n'] else 0:.3f} "
        f"wilson95=({report['lo']:.3f},{report['hi']:.3f}) "
        f"counts={report['counts']} reused={len(reused)}"
    )
    append_spend_log(args.spend_log, {
        "event": "sample_stop", "n": len(results), "confirmed": report["yes"],
        "seconds": round(time.monotonic() - started, 1), "mode": args.mode,
        "modal_spend_usd": 0 if args.mode == "local" else "see app-specific billing rows",
        "daytona_spend_usd": 0, "model_spend_usd": 0,
        "local_spend_usd": 0,
    })
    return 0


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--snapshot", required=True, help="HF task-store dir")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--spend-log", default="spend.log")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    valid = sub.add_parser("validate", help="scan the 2 known-positive images")
    _common(valid)
    valid.add_argument("--out-csv", default=None)

    scan = sub.add_parser("scan", help="scan ledger rows to a CSV")
    _common(scan)
    scan.add_argument("--ledger", required=True)
    scan.add_argument("--variants", required=True)
    scan.add_argument("--out", required=True)
    scan.add_argument("--tasks", default="", help="comma-separated task_ids")
    scan.add_argument("--limit", type=int, default=0)
    scan.add_argument("--mode", choices=("local",), default="local")

    sample = sub.add_parser("sample", help="sample sweep with Wilson prevalence")
    _common(sample)
    sample.add_argument("--ledger", default="", help="ledger for fresh draws")
    sample.add_argument("--variants", default="")
    sample.add_argument("--out", required=True)
    sample.add_argument("--reuse-sample", default="", help="prior sample CSV to reuse")
    sample.add_argument("--tasks", default="", help="comma-separated task_ids to keep")
    sample.add_argument("--n", type=int, default=100)
    sample.add_argument("--seed", type=int, default=177100)
    sample.add_argument("--reuse", default="", help="comma-separated prior CSVs to reuse")
    sample.add_argument("--mode", choices=("local", "modal"), default="local")
    sample.add_argument("--approved-cap-usd", type=float, default=0.0,
                        help="requires explicit parent approval; never self-authorize")

    args = parser.parse_args(argv)
    registry = Registry()

    if args.command == "validate":
        started = time.monotonic()
        append_spend_log(
            args.spend_log,
            {"event": "validation_start", "tasks": list(KNOWN_POSITIVES), "mode": "local"},
        )
        results, problems = run_validation(
            snapshot_dir=args.snapshot,
            registry=registry,
            workers=args.workers,
            out_csv=args.out_csv,
        )
        append_spend_log(
            args.spend_log,
            {
                "event": "validation_stop",
                "seconds": round(time.monotonic() - started, 1),
                "problems": len(problems),
                "modal_spend_usd": 0,
            },
        )
        for row in results:
            print(
                f"{row['task_id']} {row['has_built_copy']} "
                f"project={row['project']}({row['project_basis']}) "
                f"kinds={row['built_copy_kinds']} "
                f"fix_differs={row['fix_differs']} "
                f"{row.get('_elapsed_s', '?')}s {row['method']} {row['error']}"
            )
            print(f"  fix_files={row['fix_files'][:220]}")
            print(f"  paths={row['built_copy_paths'][:320]}")
        if problems:
            print("\nVALIDATION DISAGREEMENTS (stop and report):")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        print("\nvalidation: both known positives agree")
        return 0

    if args.command == "sample":
        if not args.reuse_sample and not args.ledger:
            parser.error("sample needs --reuse-sample or --ledger")
        return run_sample(args, registry)

    rows = load_usable_rows(args.ledger)
    if args.tasks:
        wanted = set(args.tasks.split(","))
        rows = [row for row in rows if row["task_id"] in wanted]
    if args.limit:
        rows = rows[: args.limit]
    append_spend_log(
        args.spend_log,
        {"event": "scan_start", "tasks": len(rows), "mode": args.mode},
    )
    started = time.monotonic()
    results = _scan_concurrent(
        rows,
        snapshot_dir=args.snapshot,
        variants_dir=args.variants,
        registry=registry,
        workers=args.workers,
    )
    write_csv(args.out, results)
    counts: dict[str, int] = {}
    for row in results:
        counts[row["has_built_copy"]] = counts.get(row["has_built_copy"], 0) + 1
    print(f"rows={len(results)} {counts}")
    append_spend_log(
        args.spend_log,
        {
            "event": "scan_stop",
            "seconds": round(time.monotonic() - started, 1),
            "modal_spend_usd": 0, "local_spend_usd": 0,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
