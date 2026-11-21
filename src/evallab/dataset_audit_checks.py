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
from evallab.registry import task_directory_digest
from evallab.task_lint import lint_task

PATCH_LINES_SCOPE = "added-patch-lines"
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
    try:
        raw = path.read_bytes()
    except OSError:
        return "", b""
    return raw.decode("utf-8", errors="replace"), raw


def parse_test_patch(patch_text: str) -> list[tuple[str, bool, list[str]]]:
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
    return path.rsplit("/", 1)[-1] in harness_basenames


def _flag_unstated_literal(
    testcode: list[str], instruction: str
) -> tuple[int | None, str]:
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
    return [
        {
            "rule": finding.rule,
            "severity": finding.severity,
            "path": finding.path,
            "message": finding.message,
        }
        for finding in lint_task(task_dir)
    ]


HACK_SCAN_SCHEMA = "evallab.reward_hack.scan/v1"

HACK_SCAN_NOTE = (
    "descriptive V1-V8 claims joined from evallab hack scan; "
    "absent findings are not a certificate and never keep/fix/discard"
)

HACK_SCAN_LIMITS_FALLBACK = (
    "static inspection only: each finding is a claim with file evidence, never proof "
    "of exploitability, and an absent finding is not a security certificate"
)


def _scan_package_for_static(task_dir: Path):
    from evallab.reward_hack import scan_package

    return scan_package(task_dir)


def _hack_scan_fact(task_dir: Path) -> dict[str, Any]:
    try:
        scan = _scan_package_for_static(task_dir)
    except Exception as exc:  # noqa: BLE001 - recorded, never raised
        return {
            "available": False,
            "schema": HACK_SCAN_SCHEMA,
            "package_digest": None,
            "harbor_digest": None,
            "recon": {},
            "findings": [],
            "limits": HACK_SCAN_LIMITS_FALLBACK,
            "error": f"{type(exc).__name__}: {exc}",
            "descriptive_note": HACK_SCAN_NOTE,
        }
    try:
        findings = [
            {
                "flaw_class": finding.flaw_class,
                "severity": finding.severity,
                "title": finding.title,
                "evidence": finding.evidence,
                "detail": finding.detail,
            }
            for finding in scan.findings
        ]
        return {
            "available": True,
            "schema": scan.schema_,
            "package_digest": scan.package_digest,
            "harbor_digest": scan.harbor_digest,
            "recon": dict(scan.recon),
            "findings": findings,
            "limits": scan.limits,
            "error": None,
            "descriptive_note": HACK_SCAN_NOTE,
        }
    except Exception as exc:  # noqa: BLE001 - recorded, never raised
        return {
            "available": False,
            "schema": HACK_SCAN_SCHEMA,
            "package_digest": None,
            "harbor_digest": None,
            "recon": {},
            "findings": [],
            "limits": HACK_SCAN_LIMITS_FALLBACK,
            "error": f"{type(exc).__name__}: {exc}",
            "descriptive_note": HACK_SCAN_NOTE,
        }


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
    hack_scan: dict[str, Any] | None = None,
) -> AuditObservation:
    facts: dict[str, Any] = {
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
    }
    if hack_scan is not None:
        facts["hack_scan"] = hack_scan
    return AuditObservation(
        stage="static",
        status="recorded",
        facts=facts,
        sources=tuple(sources),
        reason=reason,
    )


def static_observation(
    task: AuditTask,
    *,
    added_patch: Path | None = None,
    harness_basenames: Collection[str] = (),
) -> AuditObservation:
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
                    hack_scan=_hack_scan_fact(task_dir),
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
                hack_scan=_hack_scan_fact(task_dir),
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
            "hack_scan": _hack_scan_fact(task_dir),
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


PROBE_MARK = "LEAK_PROBE_V1"


def build_leak_probe_script() -> str:
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


_REPO_LINE_RE = re.compile(
    r"^LEAK_PROBE_V1 repo path_b64=(\S+) base=(\S+) beyond_base=(\S+) "
    r"unreachable=(\S+) refs=(\S+)(?: note=(\S+))?"
)
_REPO_LIKE_RE = re.compile(r"^LEAK_PROBE_V1 repo\s")


def _decode_probe_path(value: str) -> str | None:
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


