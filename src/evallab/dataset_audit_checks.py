"""Generic $0 static and mechanical leak stages for dataset audit (HAR-195).

This module owns three dataset-neutral entry points built on the frozen
:mod:`evallab.dataset_audit_contracts` types:

- :func:`static_observation` reads a task package directory and records the
  seven descriptive lexical flags ported from the MiMo static-audit prototype
  (``/tmp/static_audit.py`` rules a..g). The rules are re-implemented here as
  import-safe, input-explicit helpers: no hard-coded manifest, no dataset
  sniffing, no built-in harness list. Patch-line scope and harness
  exclusions arrive as explicit caller inputs; the default scope reads the
  package's actual test source. Byte counts are honest, and every fact is
  bound to the sha256 of the inputs actually read.
- :func:`prepare_leak_probe` derives a read-only image-inspection variant of
  a task package (``solution/solve.sh`` replacement only) following the
  HAR-161 ``probe-image-checks@1`` pattern. The parent alone executes the
  returned probe task through the Executor as a local, model-free job.
- :func:`leak_observation` reads a finished probe job directory and reduces
  its ``LEAK_PROBE_V1`` lines to coverage/refs/unreachable evidence using
  HAR-177 semantics (future history iff commits beyond base exist on any ref
  or as unreachable objects). The verifier reward is never read.

Scientific boundaries preserved throughout:

- Static flags are descriptive diagnostics, never quality verdicts; generic
  :func:`evallab.task_lint.lint_task` findings are attached as diagnostics,
  not keep/fix/discard predictions.
- Missing or incomplete evidence stays ``unknown`` (``None`` flags), never
  clean. Absence of a probe output is ``unavailable``, not negative proof.
- Nothing here executes a task, contacts the network/registry, spends,
  launches services, or mutates the original package. The probe script
  itself is metadata-only git reads plus its own log writes.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Collection
from pathlib import Path
from typing import Any

from evallab.dataset_audit_contracts import (
    AuditObservation,
    AuditSource,
    AuditTask,
)
from evallab.registry import harbor_task_digest, task_directory_digest
from evallab.task_lint import lint_task
from evallab.task_variants import (
    VariantExistsError,
    VariantRecord,
    derive_task,
    materialize,
    resolve_record,
)

# ---------------------------------------------------------------------------
# Static stage: ported /tmp/static_audit.py rules (descriptive, lexical).
# ---------------------------------------------------------------------------

#: Scope label when the corpus is the added lines of an explicitly supplied
#: patch file (the caller also supplies that dataset's harness exclusions).
PATCH_LINES_SCOPE = "added-patch-lines"
#: Scope label when the corpus is the actual test source of a Harbor package.
#: Deliberately different from the patch-lines label.
HARBOR_TEST_SCOPE = "harbor-test-source"

STR_RE = re.compile(r"""('''|\"\"\"|'|")(.+?)\1""")
PY_ASSERT_ANY = re.compile(
    r"\bassert\w*|EXPECT_\w*|expect\s*\(|pytest\.raises|assertRaises|fail\s*\(|"
    r"\beq_\b|\bok_\b|\bfail_\w+|\bmust_\w+|\bcheck_\w+|\bverify\w*",
    re.I,
)
NUM_RE = re.compile(r"\b\d+\.\d+|\b\d{3,}\b")
PY_ASSERT = re.compile(r"assert|match\s*=|raises\s*\(|expect\s*\(", re.I)
SHELL_CHECK = re.compile(
    r"exit\s+[1-9]|(?<![\w-])grep(?![\w-])|(?<![\w-])diff(?![\w-])|\[\s|"
    r"^\s*test\s|command\s+-v|\bFAIL\b|die\s|fatal\s",
    re.I,
)
CODELIKE = re.compile(
    r"\bdef \b|\bclass \b|\bimport \b|self\.|\(.*\)|lambda\b|\breturn\b|=>|function\b"
)

NET_PATS = [
    r"\brequests\b",
    r"\burllib\b",
    r"\bhttp\.client\b",
    r"\bhttp\.server\b",
    r"\baiohttp\b",
    r"\bhttpx\b",
    r"\bsocket\b",
    r"\bpip\s+install\b",
    r"\bpip\s+download\b",
    r"\bgit\s+clone\b",
    r"https?://",
    r"\bftp://",
    r"\bresponses\b",
    r"\bhttpretty\b",
    r"\burlopen\b",
    r"\burlretrieve\b",
    r"(?<![\w-])curl(?![\w-])",
    r"(?<![\w-])wget(?![\w-])",
]
LOCALHOST = re.compile(
    r"127\.0\.0\.1|localhost|0\.0\.0\.0|example\.com|\.example\b|"
    r"testserver|::1|httpbin|mock",
    re.I,
)

RAND_RE = re.compile(r"\brandom\b", re.I)
SEED_RE = re.compile(
    r"\bseed\b|RandomState\s*\(|default_rng\s*\(|np\.random\.seed|random\.seed",
    re.I,
)
TIME_RE = re.compile(
    r"time\.sleep|time\.time|datetime\.now|datetime\.today|datetime\.utcnow|"
    r"monotonic|perf_counter|clock_gettime|Date\.now|System\.currentTimeMillis",
    re.I,
)
ENT_RE = re.compile(
    r"os\.urandom|uuid[145]?\s*\(|uuid[145]?\.\w+|secrets\.|/dev/urandom|"
    r"crypto\.random",
    re.I,
)
SETITER_RE = re.compile(
    r"list\(\s*set\(|tuple\(\s*set\(|str\(\s*set\(|\.join\(\s*set\(|"
    r"join\(\s*\w*[Ss]et\b"
)
# NOTE: /tmp/static_audit.py defines WINPATH_RE twice; the second (broader)
# assignment is the effective rule and is reproduced here.
ABSPATH_RE = re.compile(
    r"/((?:home|root|testbed|workspace|tmp|var|usr|opt|etc|data|mnt|private|Users)/\S*)"
)
HARNESS_PREFIX = re.compile(r"^/(tests|testbed|workspace|tmp|var/folders|proc|dev)(/|$)")
VAR_LIB_LOG = re.compile(r"^/(var/lib/mimo|logs)(/|$)")
USER_RE = re.compile(
    r"os\.environ\[\s*.(USER|USERNAME|LOGNAME|HOME|HOSTNAME).\]|"
    r"getpass\.getuser|pwd\.getpwuid|os\.getlogin|useradd\s+\w+|"
    r"chown\s+\S+|sudo\s+-u\s+\w+",
    re.I,
)
GPU_RE = re.compile(
    r"\bcupy\b|torch\.cuda|cudnn|nvidia|\bGPU\b|is_gpu_available|"
    r"tf\.test\.is_gpu|cuda\.",
    re.I,
)

COMMENT_DEF = re.compile(r"^\s*(#|//|import\s|from\s|def\s|class\s|--)")
TMP_RE = re.compile(
    r"tmp_path|tmpdir|tmp_dir|temp_dir|tempdir|TMPDIR|tempfile|mkdtemp|"
    r"mkstemp|TemporaryDirector|TEMPDIR|MemoryFile|io\.BytesIO|io\.StringIO",
    re.I,
)
FILEREF_RES = [
    re.compile(r"""open\s*\(\s*['"]([^'"]+)['"]"""),
    re.compile(r"""Path\s*\(\s*['"]([^'"]+)['"]"""),
    re.compile(
        r"""(?:read_csv|read_json|read_excel|loadtxt|genfromtxt|imread|imopen|"""
        r"""fromfile|np\.load|pd\.read_\w+)\s*\(\s*['"]([^'"]+)['"]"""
    ),
    re.compile(
        r"""['"]([^'"]*\.(?:csv|json|jsonl|xml|yaml|yml|txt|dat|npy|npz|png|jpg|"""
        r"""jpeg|wav|mp3|pkl|pickle|h5|hdf5|db|sqlite|tsv|parquet|pt|pth|onnx|"""
        r"""tif|tiff)[^'"]*)['"]"""
    ),
]
TOOL_RE = re.compile(
    r"\bcargo\s+(test|build|run)|rustc\b|\bnpm\s+(test|run|install)|"
    r"yarn\s+(test|install)|npx\b|\bgo\s+(test|build|run|vet)\b|\bmvn\s|"
    r"gradle\s+(test|build|check)|\bdotnet\s+(test|build)|\bjulia\b|"
    r"\bRscript\b|\bruby\s+\S+\.rb|\bphp\s+\S+\.php|\bperl\s+\S+\.pl",
    re.I,
)
TESTFUNC_RE = re.compile(
    r"\s*(def\s+test\w*|async\s+def\s+test\w*|test\s*\(|it\s*\()"
)


def _sha256_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _sha256_file(path: Path) -> str | None:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError:
        return None


def _read_text_file(path: Path) -> tuple[str, bytes]:
    """(decoded text, raw bytes) of a file; missing files give ("", b"")."""
    try:
        raw = path.read_bytes()
    except OSError:
        return "", b""
    return raw.decode("utf-8", errors="replace"), raw


def parse_test_patch(patch_text: str) -> list[tuple[str, bool, list[str]]]:
    """Split a unified test patch into ``(path, is_new, added_lines)``.

    Pure helper ported from the static-audit prototype: harness files are
    included here and filtered by the caller.
    """
    sections: list[tuple[str, bool, list[str]]] = []
    cur: str | None = None
    is_new = False
    buf: list[str] = []
    for line in patch_text.splitlines():
        match = re.match(r"^diff --git a/\S+ b/(\S+)$", line)
        if match:
            if cur is not None:
                sections.append((cur, is_new, buf))
            cur, is_new, buf = match.group(1), False, []
            continue
        if cur is None:
            continue
        if line.startswith("new file"):
            is_new = True
            continue
        if line.startswith("+") and not line.startswith("+++"):
            buf.append(line[1:])
    if cur is not None:
        sections.append((cur, is_new, buf))
    return sections


def is_harness_path(path: str, harness_basenames: Collection[str]) -> bool:
    """Whether a patch path names a caller-supplied harness file (out of scope)."""
    return path.rsplit("/", 1)[-1] in harness_basenames


def _flag_unstated_literal(
    testcode: list[str], instruction: str
) -> tuple[int | None, str]:
    """(a) assertion/check-line literals absent from the instruction."""
    if not instruction:
        return None, "instruction unavailable; unstated-literal scan needs it"
    ilow = instruction.lower()
    unstated: list[str] = []
    codelike_n = 0
    for line in testcode:
        if not (PY_ASSERT.search(line) or SHELL_CHECK.search(line)):
            continue
        if line.lstrip().startswith("#"):
            continue
        for match in STR_RE.finditer(line):
            lit = match.group(2)
            if len(lit) < 4:
                continue
            if lit in instruction or lit.lower() in ilow:
                continue
            unstated.append(lit)
            if CODELIKE.search(lit):
                codelike_n += 1
        for match in NUM_RE.finditer(line):
            lit = match.group(0)
            if lit in instruction:
                continue
            unstated.append(lit)
    seen: set[str] = set()
    uniq: list[str] = []
    for item in unstated:
        if item not in seen:
            seen.add(item)
            uniq.append(item)
    if not uniq:
        return 0, ""
    examples = [u[:60] for u in uniq[:2]]
    return 1, f"n={len(uniq)} codelike={codelike_n} ex={examples}"


def _flag_network(testcode: list[str]) -> tuple[int, str]:
    """(b) network-pattern mentions, localhost-only vs external-or-mixed."""
    hits: list[str] = []
    for line in testcode:
        for pattern in NET_PATS:
            if re.search(pattern, line, re.I):
                hits.append(line.strip()[:160])
                break
    if not hits:
        return 0, ""
    kind = (
        "localhost-only"
        if all(LOCALHOST.search(hit) for hit in hits)
        else "external-or-mixed"
    )
    return 1, f"[{kind}] n={len(hits)} ex={hits[0][:120]}"


def _flag_nondeterminism(corpus: str) -> tuple[int, str]:
    """(c) unseeded randomness, wall-clock, entropy, set-iteration order."""
    found: list[str] = []
    if RAND_RE.search(corpus) and not SEED_RE.search(corpus):
        found.append("unseeded-random")
    match = TIME_RE.search(corpus)
    if match:
        found.append("time:" + match.group(0))
    match = ENT_RE.search(corpus)
    if match:
        found.append("entropy:" + match.group(0))
    match = SETITER_RE.search(corpus)
    if match:
        found.append("set-iteration:" + match.group(0))
    if not found:
        return 0, ""
    return 1, "; ".join(item[:100] for item in found[:3])


def _flag_env_coupled(testcode: list[str]) -> tuple[int, str]:
    """(d) absolute host paths, user identity, or GPU mentions in tests."""
    paths: list[str] = []
    for line in testcode:
        stripped = line.strip()
        if stripped.startswith("#!"):
            continue
        for match in ABSPATH_RE.finditer(line):
            candidate = "/" + match.group(1).rstrip("\"':;,)\\]")
            if HARNESS_PREFIX.match(candidate) or VAR_LIB_LOG.match(candidate):
                continue
            if candidate not in paths:
                paths.append(candidate)
    users = [line.strip()[:120] for line in testcode if USER_RE.search(line)]
    gpus = [line.strip()[:120] for line in testcode if GPU_RE.search(line)]
    if not (paths or users or gpus):
        return 0, ""
    parts: list[str] = []
    if paths:
        parts.append(f"paths={paths[0][:80]}(+{len(paths) - 1})")
    if users:
        parts.append(f"user={users[0][:80]}")
    if gpus:
        parts.append(f"gpu={gpus[0][:80]}")
    return 1, "; ".join(parts[:3])


def _flag_tiny_suite(
    testcode: list[str], sh_lines: list[str], has_mod: bool
) -> tuple[int, str]:
    """(e) fewer than two assertion/shell-check lines, no modified files."""
    n_py = sum(
        1
        for line in testcode
        if not COMMENT_DEF.match(line) and PY_ASSERT_ANY.search(line)
    )
    n_sh = sum(1 for line in sh_lines if SHELL_CHECK.search(line))
    n_testf = sum(1 for line in testcode if TESTFUNC_RE.match(line))
    flag = 1 if (not has_mod and (n_py + n_sh) < 2) else 0
    evidence = (
        f"pyasserts={n_py} shchecks={n_sh} testfuncs={n_testf} "
        f"modifies_existing={int(has_mod)}"
    )
    return flag, evidence


def _flag_repo_file_read(
    testcode: list[str], known_basenames: set[str]
) -> tuple[int, str]:
    """(f) file/data references not supplied by the analyzed scope."""
    repo_dep: list[str] = []
    abs_read: list[str] = []
    for line in testcode:
        if TMP_RE.search(line):
            continue
        for regex in FILEREF_RES:
            for match in regex.finditer(line):
                ref = match.group(1)
                if ref.startswith(("http://", "https://", "ftp://")):
                    continue
                if ref.startswith("/"):
                    cleaned = ref.rstrip("\"':;,)\\]")
                    if HARNESS_PREFIX.match(cleaned) or VAR_LIB_LOG.match(cleaned):
                        continue
                    if cleaned not in abs_read:
                        abs_read.append(cleaned)
                    continue
                if ref.rsplit("/", 1)[-1] in known_basenames:
                    continue
                if TMP_RE.search(ref):
                    continue
                if ref not in repo_dep:
                    repo_dep.append(ref)
    if not (repo_dep or abs_read):
        return 0, ""
    evidence = ""
    if repo_dep:
        evidence += f"repo-dependent={[r[:70] for r in repo_dep[:2]]} n={len(repo_dep)}"
    if abs_read:
        evidence += ("; " if evidence else "") + f"abs-read={[r[:70] for r in abs_read[:2]]}"
    return 1, evidence


def _flag_polyglot_toolchain(sh_corpus: list[str]) -> tuple[int, str]:
    """(g) non-Python toolchain mentions in shell test lines."""
    tools: list[str] = []
    for line in sh_corpus:
        if line.strip().startswith("export ") and "_OPTS" in line:
            continue
        match = TOOL_RE.search(line)
        if match:
            tools.append(f"{match.group(0).strip()}:{line.strip()[:110]}")
    if not tools:
        return 0, ""
    return 1, tools[0][:140]


def _collect_harbor_test_source(
    task_dir: Path,
) -> tuple[dict[str, bytes], list[str], list[str]]:
    """Actual test source of an ordinary Harbor package (no patch scope).

    Returns ``(files_by_relpath, testcode_lines, sh_lines)``. Binary or
    unreadable files are skipped; shell lines come from ``*.sh`` test files.
    """
    tests_dir = task_dir / "tests"
    files: dict[str, bytes] = {}
    testcode: list[str] = []
    sh_lines: list[str] = []
    if not tests_dir.is_dir():
        return files, testcode, sh_lines
    for path in sorted(p for p in tests_dir.rglob("*") if p.is_file()):
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            continue
        rel = path.relative_to(task_dir).as_posix()
        files[rel] = raw
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        testcode.extend(lines)
        if path.suffix == ".sh":
            sh_lines.extend(lines)
    return files, testcode, sh_lines


def _task_lint_findings(task_dir: Path) -> list[dict[str, Any]]:
    """Generic task_lint findings as descriptive diagnostics (never verdicts)."""
    return [
        {
            "rule": finding.rule,
            "severity": finding.severity,
            "path": finding.path,
            "message": finding.message,
        }
        for finding in lint_task(task_dir)
    ]


def _unknown_static_observation(
    *,
    task_id: str,
    coverage: str,
    scope: dict[str, Any],
    sources: list[AuditSource],
    instruction_raw: bytes,
    instruction_sha: str | None,
    corpus_sha: str | None,
    corpus_bytes: int,
    lint_findings: list[dict[str, Any]],
    reason: str,
) -> AuditObservation:
    """A recorded observation with every flag unknown (never clean)."""
    return AuditObservation(
        stage="static",
        status="recorded",
        facts={
            "coverage": coverage,
            "scope": scope,
            "a_unstated_literal": None,
            "b_network": None,
            "c_nondeterminism": None,
            "d_env_coupled": None,
            "e_tiny_suite": None,
            "f_repo_file_read": None,
            "g_polyglot_toolchain": None,
            "ev_a": "",
            "ev_b": "",
            "ev_c": "",
            "ev_d": "",
            "ev_e": "",
            "ev_f": "",
            "ev_g": "",
            "instr_bytes": len(instruction_raw),
            "corpus_bytes": corpus_bytes,
            "corpus_lines": 0,
            "instruction_sha256": instruction_sha,
            "corpus_sha256": corpus_sha,
            "task_lint": lint_findings,
            "quality_note": (
                "descriptive lexical flags only; not validity proof "
                "and not a keep/fix/discard prediction"
            ),
        },
        sources=tuple(sources),
        reason=reason,
    )


def static_observation(
    task: AuditTask,
    *,
    added_patch: Path | None = None,
    harness_basenames: Collection[str] = (),
) -> AuditObservation:
    """Collect static descriptors for one task package (read-only, $0).

    The default scope analyzes the package's actual ``tests/`` source with
    coverage ``harbor-test-source``. When the caller supplies ``added_patch``
    (a unified patch whose added lines are the hidden tests), the corpus is
    those added lines minus files in ``harness_basenames``, with coverage
    ``added-patch-lines``. An explicitly requested patch that is missing
    never falls back to ``tests/``: its flags stay unknown.

    Every fact is bound to the sha256 of the inputs read; byte counts are
    honest ``len(bytes)``. Unsupported or incomplete slices are ``None``
    (unknown), never clean. The observation status is ``recorded``: fresh
    local facts, no execution.
    """
    if task.path is None:
        return AuditObservation(
            stage="static",
            status="unavailable",
            facts={"coverage": "unknown", "has_future_history": None},
            sources=(),
            reason=f"{task.task_id}: task package path is unset",
        )
    task_dir = Path(task.path)
    if not task_dir.is_dir():
        return AuditObservation(
            stage="static",
            status="unavailable",
            facts={"coverage": "unknown", "has_future_history": None},
            sources=(),
            reason=f"{task.task_id}: task package directory is missing: {task_dir}",
        )

    try:
        instruction_path = task_dir / "instruction.md"
        task_toml_path = task_dir / "task.toml"
        patch_path = Path(added_patch) if added_patch is not None else None

        instruction_text, instruction_raw = _read_text_file(instruction_path)
        instruction_sha = (
            _sha256_bytes(instruction_raw)
            if instruction_raw or instruction_path.is_file()
            else None
        )
        task_toml_sha = _sha256_file(task_toml_path)

        sources: list[AuditSource] = []
        sources.append(
            AuditSource(
                path=str(instruction_path),
                sha256=instruction_sha,
                available=instruction_path.is_file(),
                binding="instruction.md",
            )
        )
        sources.append(
            AuditSource(
                path=str(task_toml_path),
                sha256=task_toml_sha,
                available=task_toml_path.is_file(),
                binding="task.toml",
            )
        )

        if patch_path is not None:
            coverage = PATCH_LINES_SCOPE
            if not patch_path.is_file():
                # Explicitly requested but absent: unknown, never tests/ fallback.
                sources.append(
                    AuditSource(
                        path=str(patch_path),
                        sha256=None,
                        available=False,
                        binding="added patch",
                    )
                )
                return _unknown_static_observation(
                    task_id=task.task_id,
                    coverage=coverage,
                    scope={"added_patch": str(patch_path), "added_lines": 0},
                    sources=sources,
                    instruction_raw=instruction_raw,
                    instruction_sha=instruction_sha,
                    corpus_sha=None,
                    corpus_bytes=0,
                    lint_findings=_task_lint_findings(task_dir),
                    reason=(
                        f"{task.task_id}: explicit added patch is missing: "
                        f"{patch_path}; flags unknown"
                    ),
                )
            patch_text, patch_raw = _read_text_file(patch_path)
            patch_sha = _sha256_bytes(patch_raw)
            sources.append(
                AuditSource(
                    path=str(patch_path),
                    sha256=patch_sha,
                    available=True,
                    binding="added patch",
                )
            )
            sections = parse_test_patch(patch_text)
            real = [
                (path, is_new, lines)
                for (path, is_new, lines) in sections
                if not is_harness_path(path, harness_basenames)
            ]
            testcode = [line for (_, _, lines) in real for line in lines]
            sh_lines = [
                line
                for (path, _, lines) in real
                if path.endswith(".sh")
                for line in lines
            ]
            harness_sh = [
                line
                for (path, _, lines) in sections
                if is_harness_path(path, harness_basenames) and path.endswith(".sh")
                for line in lines
            ]
            known_basenames = {path.rsplit("/", 1)[-1] for (path, _, _) in sections}
            has_mod = any(not is_new for (_, is_new, _) in real)
            scope_detail: dict[str, Any] = {
                "patch_files": len(sections),
                "non_harness_files": len(real),
                "added_lines": len(testcode),
            }
            sh_corpus = sh_lines + harness_sh
            corpus_input_sha = patch_sha
            corpus_input_bytes = len(patch_raw)
        else:
            coverage = HARBOR_TEST_SCOPE
            files, testcode, sh_lines = _collect_harbor_test_source(task_dir)
            for rel in sorted(files):
                sources.append(
                    AuditSource(
                        path=str(task_dir / rel),
                        sha256=_sha256_bytes(files[rel]),
                        available=True,
                        binding=rel,
                    )
                )
            known_basenames = {rel.rsplit("/", 1)[-1] for rel in files}
            # No patch scope exists here: nothing was "modified"; the visible
            # suite is the whole corpus. Recorded explicitly, not inferred.
            has_mod = False
            scope_detail = {
                "test_files": sorted(files),
                "test_lines": len(testcode),
                "modifies_existing": 0,
                "scope_note": (
                    "no added patch supplied; analyzed actual tests/ source, "
                    "not added-patch lines"
                ),
            }
            sh_corpus = list(sh_lines)
            corpus_input_sha = (
                _sha256_bytes(b"\x00".join(files[rel] for rel in sorted(files)))
                if files
                else None
            )
            corpus_input_bytes = sum(len(raw) for raw in files.values())

        lint_findings = _task_lint_findings(task_dir)

        if not testcode:
            return _unknown_static_observation(
                task_id=task.task_id,
                coverage=coverage,
                scope=scope_detail,
                sources=sources,
                instruction_raw=instruction_raw,
                instruction_sha=instruction_sha,
                corpus_sha=corpus_input_sha,
                corpus_bytes=corpus_input_bytes,
                lint_findings=lint_findings,
                reason=(
                    f"{task.task_id}: empty test corpus under {coverage}; "
                    "flags unknown"
                ),
            )

        corpus_text = "\n".join(testcode)
        fa, eva = _flag_unstated_literal(testcode, instruction_text)
        fb, evb = _flag_network(testcode)
        fc, evc = _flag_nondeterminism(corpus_text)
        fd, evd = _flag_env_coupled(testcode)
        fe, eve = _flag_tiny_suite(testcode, sh_lines, has_mod)
        ff, evf = _flag_repo_file_read(testcode, known_basenames)
        fg, evg = _flag_polyglot_toolchain(sh_corpus)

        package_digest_actual = task_directory_digest(task_dir)
        package_digest_match: bool | None = (
            package_digest_actual == task.package_digest
            if task.package_digest is not None
            else None
        )

        facts: dict[str, Any] = {
            "coverage": coverage,
            "scope": scope_detail,
            "a_unstated_literal": None if fa is None else bool(fa),
            "b_network": bool(fb),
            "c_nondeterminism": bool(fc),
            "d_env_coupled": bool(fd),
            "e_tiny_suite": bool(fe),
            "f_repo_file_read": bool(ff),
            "g_polyglot_toolchain": bool(fg),
            "ev_a": eva,
            "ev_b": evb,
            "ev_c": evc,
            "ev_d": evd,
            "ev_e": eve,
            "ev_f": evf,
            "ev_g": evg,
            "instr_bytes": len(instruction_raw),
            "corpus_bytes": corpus_input_bytes,
            "corpus_lines": len(testcode),
            "instruction_sha256": instruction_sha,
            "corpus_sha256": corpus_input_sha,
            "package_digest_actual": package_digest_actual,
            "package_digest_match": package_digest_match,
            "task_lint": lint_findings,
            "quality_note": (
                "descriptive lexical flags only; static flags never determine "
                "quality and are not a keep/fix/discard prediction"
            ),
        }
        reasons: list[str] = []
        if not instruction_raw:
            reasons.append("instruction.md missing or empty; a_unstated_literal unknown")
        return AuditObservation(
            stage="static",
            status="recorded",
            facts=facts,
            sources=tuple(sources),
            reason="; ".join(reasons) if reasons else None,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        return AuditObservation(
            stage="static",
            status="failed",
            facts={"coverage": "unknown", "has_future_history": None},
            sources=(),
            reason=f"{task.task_id}: static read failed: {exc}",
        )


PROBE_TRANSFORM = "probe-image-checks@1"

PROBE_MARK = "LEAK_PROBE_V1"


def build_leak_probe_script() -> str:
    """Inspect candidate checkouts without changing their files or refs.

    Findings are scoped to discovered repositories and their initial HEAD.
    Failed commands remain unknown rather than becoming zero-count evidence.
    """
    return r'''#!/bin/bash
set -u -o pipefail
export GIT_OPTIONAL_LOCKS=0
PROBE="LEAK_PROBE_V1"
OUT_DIR="${AUDIT_LEAK_OUT_DIR:-/tmp/audit-leak-probe}"
mkdir -p "$OUT_DIR" || exit 1
OUT="$OUT_DIR/leak-probe.log"
: > "$OUT" || exit 1
log() { printf '%s\n' "$*"; printf '%s\n' "$*" >> "$OUT"; }
log "$PROBE start"
if ! command -v git >/dev/null 2>&1; then
  log "$PROBE missing_git git binary not on PATH"
  log "$PROBE done repos=0"
  exit 0
fi
g() {
  if command -v timeout >/dev/null 2>&1; then timeout 120 git "$@";
  else git "$@"; fi
}
CANDIDATES=("$PWD")
add_candidate() {
  [ -n "$1" ] || return
  for existing in "${CANDIDATES[@]}"; do
    [ "$existing" != "$1" ] || return
  done
  CANDIDATES+=("$1")
}
for d in /testbed /workspace/repo /workspace /app /root/repo "$HOME/repo"; do
  add_candidate "$d"
done
if [ -n "${AUDIT_LEAK_EXTRA_ROOTS:-}" ]; then
  while IFS= read -r d; do add_candidate "$d"; done <<< "$AUDIT_LEAK_EXTRA_ROOTS"
fi
found=0
for c in "${CANDIDATES[@]}"; do
  [ -e "$c/.git" ] || continue
  if command -v base64 >/dev/null 2>&1; then
    enc=$(printf '%s' "$c" | base64 | tr -d ' \t\r\n')
  else enc=""; fi
  [ -n "$enc" ] || enc="unknown"
  if ! base=$(g -C "$c" rev-parse --verify HEAD 2>/dev/null) ||
     ! [[ "$base" =~ ^[0-9a-f]{40}$|^[0-9a-f]{64}$ ]]; then
    log "$PROBE repo path_b64=$enc base=unknown beyond_base=unknown unreachable=unknown refs=unknown note=incomplete-base"
    found=$((found + 1))
    continue
  fi
  if beyond=$(g -C "$c" rev-list --all --not "$base" --count 2>/dev/null); then
    case "$beyond" in ''|*[!0-9]*) beyond="unknown" ;; esac
  else beyond="unknown"; fi
  refs="unknown"
  if ref_lines=$(g -C "$c" for-each-ref --format='%(refname)' 2>/dev/null); then
    refs=0
    while IFS= read -r line; do
      [ -z "$line" ] || refs=$((refs + 1))
    done <<< "$ref_lines"
  fi
  unreachable="unknown"
  if fsck_lines=$(g -C "$c" fsck --unreachable --no-reflogs 2>/dev/null); then
    unreachable=0
    while IFS= read -r line; do
      case "$line" in
        "unreachable commit "*|"dangling commit "*) unreachable=$((unreachable + 1)) ;;
      esac
    done <<< "$fsck_lines"
  fi
  log "$PROBE repo path_b64=$enc base=$base beyond_base=$beyond unreachable=$unreachable refs=$refs"
  found=$((found + 1))
done
if [ "$found" -eq 0 ]; then
  log "$PROBE no_repository candidates_searched=${#CANDIDATES[@]}"
else
  log "$PROBE done repos=$found"
fi
'''


def _probe_task_slug(task_name: str) -> str:
    return task_name.strip("/").replace("/", "__") or "task"


def _find_existing_probe_record(
    *,
    task_name: str,
    parent_digest: str,
    probe_sha256: str,
    repo_root: Path,
    records_dir: Path,
) -> VariantRecord | None:
    """Locate an already-derived identical probe record (idempotent retry)."""
    slug = _probe_task_slug(task_name)
    candidates = sorted((repo_root / records_dir).glob(f"{slug}/*.json"))
    for path in candidates:
        try:
            record = resolve_record(path, repo_root=repo_root, records_dir=records_dir)
        except (OSError, ValueError):
            continue
        if record.transform != PROBE_TRANSFORM:
            continue
        if record.parent.digest != parent_digest:
            continue
        if len(record.files) != 1 or record.files[0].path != "solution/solve.sh":
            continue
        if record.files[0].after_sha256 != probe_sha256:
            continue
        return record
    return None


def prepare_leak_probe(
    task: AuditTask,
    *,
    repo_root: Path,
    records_dir: Path | str = "library/task-variants",
    variants_root: Path | None = None,
) -> AuditTask:
    """Derive the read-only leak-inspection variant of one task package.

    Only ``solution/solve.sh`` changes (replaced by
    :func:`build_leak_probe_script`); environment, verifier, instruction,
    and task metadata are byte-identical to the parent. Lineage follows the
    canonical :func:`evallab.task_variants.derive_task` store, so the
    original package is never mutated and the probe carries an explicit
    ``probe-image-checks@1`` record. The caller (parent) alone executes the
    returned probe task through the Executor as a local, model-free job;
    this function performs no execution, network, or provider calls.
    """
    if task.path is None:
        raise ValueError(f"{task.task_id}: task package path is unset")
    parent_dir = Path(task.path)
    if not parent_dir.is_dir():
        raise ValueError(
            f"{task.task_id}: task package directory is missing: {parent_dir}"
        )
    if not (parent_dir / "task.toml").is_file():
        raise ValueError(f"{task.task_id}: not a Harbor task package: {parent_dir}")

    root = Path(repo_root)
    records = Path(records_dir)
    script = build_leak_probe_script()
    script_bytes = script.encode("utf-8")
    probe_sha = _sha256_bytes(script_bytes)

    parent_digest = task_directory_digest(parent_dir)
    parent_harbor = harbor_task_digest(parent_dir)
    if parent_digest != task.package_digest or parent_harbor != task.harbor_digest:
        raise ValueError("the probe parent no longer matches the selected audit package")

    parent_source: dict[str, Any] = (
        dict(task.parent_source) if task.parent_source else {}
    )
    if not parent_source:
        parent_source = {"kind": "local", "path": str(parent_dir)}

    try:
        record = derive_task(
            parent_dir,
            changes={"solution/solve.sh": script_bytes},
            transform=PROBE_TRANSFORM,
            rationale=(
                "HAR-195 audit leak probe: the probe solution runs read-only "
                "image inspection (git history past base, unreachable objects) "
                "before grading; task unchanged"
            ),
            created_by="har195-audit",
            inputs={
                "probe": "image-inspection",
                "parent_package_digest": parent_digest,
                "parent_harbor_digest": parent_harbor,
            },
            parent_source=parent_source,
            repo_root=root,
            records_dir=records,
            variants_root=variants_root,
        )
    except VariantExistsError:
        record = _find_existing_probe_record(
            task_name=task.task_name,
            parent_digest=parent_digest,
            probe_sha256=probe_sha,
            repo_root=root,
            records_dir=records,
        )
        if record is None:
            raise

    package_dir = materialize(
        record,
        parent_dir,
        repo_root=root,
        records_dir=records,
        variants_root=variants_root,
    )
    lineage: dict[str, Any] = dict(task.parent_source) if task.parent_source else {}
    lineage.update(
        {
            "probe_transform": record.transform,
            "probe_parent_digest": record.parent.digest,
            "probe_variant_digest": record.variant_digest,
            "probe_record": str(
                Path(records)
                / _probe_task_slug(record.task_name)
                / f"{record.variant_digest.split(':', 1)[1][:12]}.json"
            ),
        }
    )
    return AuditTask(
        dataset_id=task.dataset_id,
        task_id=task.task_id,
        task_name=task.task_name,
        path=package_dir,
        package_digest=record.variant_digest,
        harbor_digest=record.variant_harbor_digest,
        aliases=task.aliases,
        source_uri=task.source_uri,
        revision=task.revision,
        parent_source=lineage,
    )


_REPO_LINE_RE = re.compile(
    r"^LEAK_PROBE_V1 repo path_b64=(\S+) base=(\S+) beyond_base=(\S+) "
    r"unreachable=(\S+) refs=(\S+)(?: note=(\S+))?"
)
_REPO_LIKE_RE = re.compile(r"^LEAK_PROBE_V1 repo\s")


def _decode_probe_path(value: str) -> str | None:
    """Repository path from the probe's base64 field (None when unusable)."""
    if value == "unknown":
        return None
    try:
        return base64.b64decode(value, validate=True).decode("utf-8")
    except ValueError:
        return None
_DONE_RE = re.compile(r"^LEAK_PROBE_V1 done repos=(\d+)")
_NO_REPO_RE = re.compile(r"^LEAK_PROBE_V1 no_repository\b(.*)$")
_MISSING_GIT_RE = re.compile(r"^LEAK_PROBE_V1 missing_git\b(.*)$")
_INCOMPLETE_RE = re.compile(r"^LEAK_PROBE_V1 incomplete_scan\b(.*)$")


def parse_probe_output(text: str) -> dict[str, Any]:
    """Reduce raw oracle text to probe evidence (pure; never touches reward).

    Returns ``repos`` (per-repository dicts), ``done``/``no_repository``/
    ``missing_git`` markers, and whether any ``LEAK_PROBE_V1`` line exists.
    Repository paths arrive base64-encoded (``path_b64``) so spaces survive
    the line format; undecodable paths stay ``None`` and make their
    repository incomplete, and ``repo_like`` keeps malformed repo lines from
    reading as no-repository. Numeric fields stay ``int``; tool/parse
    failures stay ``"unknown"``.
    """
    repos: list[dict[str, Any]] = []
    done: int | None = None
    no_repository = False
    missing_git = False
    incomplete_scan = False
    seen_probe = False
    repo_like = False
    for line in text.splitlines():
        if PROBE_MARK not in line:
            continue
        seen_probe = True
        stripped = line.strip()
        if _REPO_LIKE_RE.match(stripped):
            repo_like = True
        match = _REPO_LINE_RE.match(stripped)
        if match:
            encoded, base, beyond, unreachable, refs, note = match.groups()
            path = _decode_probe_path(encoded)

            def _num(value: str) -> int | str:
                return int(value) if value.isdigit() else "unknown"

            repos.append(
                {
                    "path": path,
                    "base": None if base == "unknown" else base,
                    "beyond_base_refs": _num(beyond),
                    "unreachable_commits": _num(unreachable),
                    "refs": _num(refs),
                    "note": note,
                    "complete": path is not None
                    and all(
                        value.isdigit() for value in (beyond, unreachable, refs)
                    )
                    and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base) is not None,
                }
            )
            continue
        if _REPO_LIKE_RE.match(stripped):
            incomplete_scan = True
        match = _DONE_RE.match(stripped)
        if match:
            done = int(match.group(1))
            continue
        if _NO_REPO_RE.match(stripped):
            no_repository = True
            continue
        if _MISSING_GIT_RE.match(stripped):
            missing_git = True
            continue
        if _INCOMPLETE_RE.match(stripped):
            incomplete_scan = True
            continue
    return {
        "repos": repos,
        "done": done,
        "no_repository": no_repository,
        "missing_git": missing_git,
        "incomplete_scan": incomplete_scan,
        "seen_probe": seen_probe,
        "repo_like": repo_like,
    }




def leak_observation(task: AuditTask, job_dir: Path) -> AuditObservation:
    """Read a finished probe job into HAR-177 leak evidence (read-only, $0).

    The verifier reward is never consulted: only ``LEAK_PROBE_V1`` lines in
    the trial's oracle output determine the facts. Missing output, a missing
    git binary, an unscanned repository, or incomplete numeric fields all
    yield ``has_future_history=None`` (unknown) with an explicit coverage
    label - never a clean verdict. A fully scanned repository with zero
    beyond-base refs and zero unreachable commits is the only clean case.
    """
    from evallab.dataset_audit_sources import file_source
    from evallab.evidence.facts import _task_digest
    from evallab.results import load_job

    job = Path(job_dir)
    if not job.is_dir():
        return AuditObservation(
            stage="leak",
            status="unavailable",
            facts={
                "coverage": "missing-job",
                "has_future_history": None,
                "output_present": False,
            },
            sources=(),
            reason=f"{task.task_id}: probe job directory is missing: {job}",
        )
    try:
        native = load_job(job)
        if len(native.trials) != 1:
            raise ValueError("a probe must have exactly one native trial")
        native_trial = native.trials[0]
    except (OSError, ValueError, TypeError) as exc:
        return AuditObservation(
            stage="leak", status="unavailable",
            facts={"coverage": "unbound-native-job", "has_future_history": None,
                   "output_present": False},
            sources=(file_source(job / "result.json", binding="native-job"),),
            reason=f"{task.task_id}: no complete native probe identity: {exc}",
        )
    trial = native_trial.path
    native_sources = (
        file_source(job / "result.json", binding="native-job"),
        file_source(trial / "result.json", binding="native-trial"),
        file_source(trial / "lock.json", binding="native-task-lock"),
    )
    digest = _task_digest(native_trial)
    if digest is not None and re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        digest = "sha256:" + digest.lower()
    config = native_trial.config or native_trial.result.get("config") or {}
    agent = config.get("agent") if isinstance(config, dict) else None
    agent_name = agent.get("name") if isinstance(agent, dict) else None
    if task.harbor_digest is None or digest != task.harbor_digest or agent_name != "oracle":
        return AuditObservation(
            stage="leak", status="failed",
            facts={"coverage": "native-identity-mismatch", "has_future_history": None,
                   "observed_harbor_digest": digest, "observed_agent": agent_name},
            sources=native_sources,
            reason="probe output does not bind to the selected native oracle package",
        )
    oracle_path = trial / "agent" / "oracle.txt"
    if not oracle_path.is_file():
        return AuditObservation(
            stage="leak",
            status="unavailable",
            facts={
                "coverage": "missing-output",
                "has_future_history": None,
                "output_present": False,
                "trial": trial.name,
            },
            sources=(),
            reason=(
                f"{task.task_id}: probe oracle output is missing: {oracle_path}; "
                "not clean"
            ),
        )

    try:
        raw = oracle_path.read_bytes()
    except OSError as exc:
        return AuditObservation(
            stage="leak",
            status="failed",
            facts={
                "coverage": "unreadable-output",
                "has_future_history": None,
                "output_present": True,
                "trial": trial.name,
            },
            sources=(),
            reason=f"{task.task_id}: probe oracle output unreadable: {exc}",
        )
    output_sha = _sha256_bytes(raw)
    text = raw.decode("utf-8", errors="replace")
    parsed = parse_probe_output(text)
    sources = (
        AuditSource(
            path=str(oracle_path),
            sha256=output_sha,
            available=True,
            binding="probe oracle output",
        ),
        *native_sources,
    )

    base_facts: dict[str, Any] = {
        "probe": PROBE_TRANSFORM,
        "probe_package_digest": task.package_digest,
        "probe_harbor_digest": digest,
        "job_path": str(job),
        "output_present": True,
        "output_sha256": output_sha,
        "trial": trial.name,
        "reward_interpreted": False,
    }

    if not parsed["seen_probe"]:
        return AuditObservation(
            stage="leak",
            status="failed",
            facts={
                **base_facts,
                "coverage": "probe-not-run",
                "has_future_history": None,
                "repos": [],
            },
            sources=sources,
            reason=(
                f"{task.task_id}: oracle output has no {PROBE_MARK} lines; "
                "the probe did not run here, not clean"
            ),
        )

    if parsed["missing_git"]:
        return AuditObservation(
            stage="leak",
            status="executed",
            facts={
                **base_facts,
                "coverage": "missing-git",
                "has_future_history": None,
                "git_present": False,
                "repos": [],
            },
            sources=sources,
            reason=f"{task.task_id}: git binary missing in the probe image; unknown",
        )

    if parsed["no_repository"] or (not parsed["repos"] and not parsed["repo_like"]):
        return AuditObservation(
            stage="leak",
            status="executed",
            facts={
                **base_facts,
                "coverage": "no-repository",
                "has_future_history": None,
                "git_present": True,
                "repos": [],
            },
            sources=sources,
            reason=f"{task.task_id}: probe found no git repository; unknown",
        )

    repos = parsed["repos"]
    if parsed["repo_like"] and not repos:
        return AuditObservation(
            stage="leak",
            status="executed",
            facts={
                **base_facts,
                "coverage": "incomplete-scan",
                "has_future_history": None,
                "git_present": True,
                "repos": [],
                "beyond_base_refs": None,
                "unreachable_commits": None,
                "base_commit": None,
            },
            sources=sources,
            reason=(
                f"{task.task_id}: probe repo lines matched no known shape; unknown"
            ),
        )

    numeric = [repo for repo in repos if repo["complete"]]
    if (
        not numeric or len(numeric) != len(repos) or parsed["incomplete_scan"]
        or parsed["done"] != len(repos)
    ):
        return AuditObservation(
            stage="leak",
            status="executed",
            facts={
                **base_facts,
                "coverage": "incomplete-scan",
                "has_future_history": None,
                "git_present": True,
                "repos": repos,
                "beyond_base_refs": None,
                "unreachable_commits": None,
                "base_commit": next(
                    (repo["base"] for repo in repos if repo["base"]), None
                ),
            },
            sources=sources,
            reason=(
                f"{task.task_id}: probe scan incomplete "
                f"({len(numeric)}/{len(repos)} repositories complete); unknown"
            ),
        )

    beyond_total = sum(repo["beyond_base_refs"] for repo in numeric)
    unreachable_total = sum(repo["unreachable_commits"] for repo in numeric)
    # HAR-177 semantics: future history iff refs beyond base OR unreachable.
    has_future = bool(beyond_total > 0 or unreachable_total > 0)
    return AuditObservation(
        stage="leak",
        status="executed",
        facts={
            **base_facts,
            "coverage": "probe-image-inspection",
            "has_future_history": has_future,
            "git_present": True,
            "repos": repos,
            "beyond_base_refs": beyond_total,
            "unreachable_commits": unreachable_total,
            "base_commit": numeric[0]["base"],
        },
        sources=sources,
        reason=None,
    )
