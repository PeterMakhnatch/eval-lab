"""Gate blank, off-brief and horizontally overflowing rendered webdev pages.

Consume Playwright's rendered DOM, including visible image alt text, rather
than static HTML (which would reject React/Vue shells). Failing gates score
zero; passing gates still require the vision judge. Missing measurements
are infrastructure masks, never permission to bypass a gate.

Keyword presence is a weak necessary condition, not a completeness check.
Oracle-pair validation of false rejections remains staged.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Any

from evallab.task_variants import VariantInvalid, VariantRecord, derive_task

#: Transform id recorded in lineage.
TRANSFORM_ID = "webdev-structural-gate@1"

#: Marker comment identifying the inserted change (also the idempotence guard).
MARKER = "webdev-structural-gate@1"

#: Package-relative paths this transform rewrites.
GRADE_REL = "tests/grade.py"
GRADE_JSON_REL = "tests/grade.json"
SHOT_REL = "tests/webdev/shot.py"

MIN_DOM_CHARS = 1
OVERFLOW_TOLERANCE_PX = 1
MAX_KEYWORDS = 8

#: Tokens too generic to work as content keywords.
STOPWORDS = frozenset({
    "page", "pages", "website", "site", "design", "responsive", "mobile",
    "desktop", "section", "sections", "button", "buttons", "link", "links",
    "header", "footer", "content", "layout", "style", "modern", "clean",
    "simple", "brief", "landing", "static", "esto", "esta", "para", "com",
    "uma", "los", "las", "una", "del", "con", "por", "como", "mais",
    "index", "html", "text", "image", "images", "mock", "mocked", "mockup",
    "visual", "medium", "stack", "css", "conteúdo", "create", "build",
    "criar", "inclui", "include", "including", "use", "using", "must",
})

_QUOTED_RE = re.compile(r"""[«»“”"'‘’]\s*([^«»“”"'‘’]{3,60}?)\s*[«»“”"'‘’]""")
_CAPSEQ_RE = re.compile(r"[A-ZÀ-Þ][\w\-À-ÿ]{2,}(?:\s+[A-ZÀ-Þ][\w\-À-ÿ]{2,}){0,2}")
_LONGTOK_RE = re.compile(r"[A-Za-zÀ-ÿ0-9_#@][A-Za-zÀ-ÿ0-9_\-#@.]{5,}")


def extract_keywords(query: str, limit: int = MAX_KEYWORDS) -> list[str]:
    """Deterministic per-task gate keywords from the full brief.

    Quoted spans first (explicit names), then capitalized sequences
    (proper nouns), then long distinctive tokens. Generic web words,
    short tokens and pure numbers are dropped. Order is stable:
    first-seen wins, duplicates fold case-insensitively.
    """
    ranked: list[str] = []

    def _add(term: str) -> None:
        term = " ".join(term.split())
        if len(term) < 3 or term.replace(".", "").isdigit():
            return
        low = term.lower()
        if low in STOPWORDS:
            return
        if any(low == seen.lower() for seen in ranked):
            return
        ranked.append(term)

    for m in _QUOTED_RE.finditer(query):
        _add(m.group(1))
    for m in _CAPSEQ_RE.finditer(query):
        if len(ranked) >= limit:
            break
        _add(m.group(0))
    for m in _LONGTOK_RE.finditer(query):
        if len(ranked) >= limit:
            break
        tok = m.group(0).strip(".-_#@")
        if len(tok) < 6 or tok.lower() in STOPWORDS:
            continue
        if not re.search(r"[A-Za-zÀ-ÿ]", tok):
            continue
        _add(tok)
    return ranked[:limit]


#: Insertion point in grade.py (kept verbatim by the W1/W2 transforms).
_GATE_ANCHOR = '(V / "screenshot.jpg").write_bytes(jpg)'

_GATE_BLOCK = """\
(V / "screenshot.jpg").write_bytes(jpg)
# webdev-structural-gate@1: necessary conditions, not a replacement judge.
_gate_layout = {}
for _line in (shot["output"] or "").splitlines():
    if _line.startswith("LAYOUT:"):
        try:
            _gate_layout = json.loads(_line[len("LAYOUT:"):])
        except (ValueError, TypeError):
            pass
        break
if (not isinstance(_gate_layout, dict) or _gate_layout.get("error")
        or not isinstance(_gate_layout.get("text"), str)
        or not isinstance(_gate_layout.get("scroll_w"), (int, float))
        or not isinstance(_gate_layout.get("client_w"), (int, float))
        or _gate_layout["client_w"] <= 0):
    done(None, "Structural measurements unavailable; not scored.")
_gate_text = " ".join(_gate_layout["text"].split())
_gate_kw = cfg.get("gate_keywords") or []
_gate_fails = []
if not _gate_text:
    _gate_fails.append("blank rendered DOM")
if _gate_kw and not any(_kw.casefold() in _gate_text.casefold() for _kw in _gate_kw):
    _gate_fails.append("no brief-keyword DOM presence")
if _gate_layout["scroll_w"] > _gate_layout["client_w"] + 1:
    _gate_fails.append("horizontal overflow")
_gate_report = {"passed": not _gate_fails, "failed": _gate_fails,
                "dom_chars": len(_gate_text), "keywords": _gate_kw,
                "layout": _gate_layout}
(V / "structural_gates.json").write_text(json.dumps(_gate_report, indent=1))
if _gate_fails:
    done(0.0, "Structural gates failed: " + "; ".join(_gate_fails),
         structural_gates=_gate_fails)"""

_SHOT_PNG_ANCHOR = '    png = pg.screenshot(full_page=True, type="jpeg", quality=75)'
_SHOT_PNG_NEW = """    png = pg.screenshot(full_page=True, type="jpeg", quality=75)
    try:
        _layout = pg.evaluate("() => ({scroll_w: document.documentElement.scrollWidth, client_w: document.documentElement.clientWidth, text: (document.body?.innerText || '') + ' ' + Array.from(document.images).filter(i => {const r = i.getBoundingClientRect(); const s = getComputedStyle(i); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';}).map(i => i.alt).join(' ')})")
    except Exception as _le:
        _layout = {"error": str(_le)[:100]}"""
_SHOT_WRITE_ANCHOR = '\nsys.stdout.write("RENDER_ENV:" + json.dumps(render_env) + "\\n")'
_SHOT_WRITE_NEW = """\nsys.stdout.write("LAYOUT:" + json.dumps(_layout) + "\\n")
sys.stdout.write("RENDER_ENV:" + json.dumps(render_env) + "\\n")"""


def build_grade_py(parent_grade_py: str) -> str:
    """Parent ``grade.py`` plus the gate block. Idempotent."""
    if MARKER in parent_grade_py:
        return parent_grade_py
    if parent_grade_py.count(_GATE_ANCHOR) != 1:
        raise VariantInvalid(
            "grade.py has no unique screenshot anchor; refusing structural-gate patch"
        )
    return parent_grade_py.replace(_GATE_ANCHOR, _GATE_BLOCK, 1)


def build_shot_py(parent_shot_py: str) -> str:
    """Parent ``shot.py`` plus the LAYOUT emission. Idempotent."""
    if MARKER in parent_shot_py:
        return parent_shot_py
    if parent_shot_py.count(_SHOT_PNG_ANCHOR) != 1:
        raise VariantInvalid(
            "shot.py has no unique screenshot anchor; refusing layout emission patch"
        )
    if parent_shot_py.count(_SHOT_WRITE_ANCHOR) != 1:
        raise VariantInvalid(
            "shot.py has no unique write anchor; refusing layout emission patch"
        )
    text = parent_shot_py.replace(_SHOT_PNG_ANCHOR, _SHOT_PNG_NEW, 1)
    marker_note = '"""In-pod full-page screenshot pipeline: playwright inside the agent\'s own sandbox.'
    if marker_note in text:
        text = text.replace(
            marker_note,
            marker_note + "\n\nwebdev-structural-gate@1 additionally reports LAYOUT "
            "(scrollWidth/clientWidth); parsers ignore the extra line.",
            1,
        )
    return text.replace(_SHOT_WRITE_ANCHOR, _SHOT_WRITE_NEW, 1)


def build_grade_json(parent_grade_json: str, query: str) -> str:
    """Parent ``grade.json`` plus ``gate_keywords`` from the full brief."""
    try:
        cfg = json.loads(parent_grade_json)
    except Exception as exc:
        raise VariantInvalid(f"parent grade.json is not JSON: {exc}") from exc
    if not isinstance(cfg, dict) or "cwd" not in cfg or "query" not in cfg:
        raise VariantInvalid("parent grade.json lacks cwd/query")
    keywords = extract_keywords(query)
    if cfg.get("gate_keywords") == keywords:
        return parent_grade_json
    cfg["gate_keywords"] = keywords
    return json.dumps(cfg, indent=1, ensure_ascii=False) + "\n"


def _read_parent(parent_dir: Path) -> tuple[str, str, str, str, str]:
    try:
        task_name = tomllib.loads((parent_dir / "task.toml").read_text(encoding="utf-8"))["task"]["name"]
    except Exception as exc:
        raise VariantInvalid(f"parent task.toml is missing or invalid: {exc}") from exc
    try:
        grade_py = (parent_dir / GRADE_REL).read_text(encoding="utf-8")
        grade_json = (parent_dir / GRADE_JSON_REL).read_text(encoding="utf-8")
        shot_py = (parent_dir / SHOT_REL).read_text(encoding="utf-8")
    except OSError as exc:
        raise VariantInvalid(f"parent grader file is missing: {exc}") from exc
    if not task_name:
        raise VariantInvalid("parent task.toml names no task")
    try:
        query = json.loads(grade_json)["query"]
    except Exception as exc:
        raise VariantInvalid(f"parent grade.json is not JSON: {exc}") from exc
    return task_name, grade_py, grade_json, shot_py, query


def build_changes(
    parent_dir: Path | str,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """Build the ``derive_task`` changes mapping plus lineage inputs."""
    parent = Path(parent_dir)
    task_name, grade_py, grade_json, shot_py, query = _read_parent(parent)
    new_py = build_grade_py(grade_py)
    new_shot = build_shot_py(shot_py)
    new_json = build_grade_json(grade_json, query)
    if new_py == grade_py and new_shot == shot_py and new_json == grade_json:
        raise VariantInvalid("parent already carries webdev-structural-gate@1")
    changes: dict[str, bytes | None] = {}
    if new_py != grade_py:
        changes[GRADE_REL] = new_py.encode("utf-8")
    if new_shot != shot_py:
        changes[SHOT_REL] = new_shot.encode("utf-8")
    if new_json != grade_json:
        changes[GRADE_JSON_REL] = new_json.encode("utf-8")
    inputs: dict[str, Any] = {
        "parent_task": task_name,
        "gate_keywords": extract_keywords(query),
        "min_dom_chars": MIN_DOM_CHARS,
        "overflow_tolerance_px": OVERFLOW_TOLERANCE_PX,
        "grade_before_sha256": f"sha256:{hashlib.sha256(grade_py.encode()).hexdigest()}",
        "grade_after_sha256": f"sha256:{hashlib.sha256(new_py.encode()).hexdigest()}",
    }
    return changes, inputs


def derive_webdev_structural_gate(
    parent_dir: Path | str,
    *,
    rationale: str = (
        "Gate trivial, off-brief and overflowing pages to a scored 0 before "
        "the vision call, cutting judge spend and the blank-page lottery. "
        "Passing pages reach the judge unchanged."
    ),
    created_by: str = "webdev-structural-gate",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive the ``webdev-structural-gate@1`` variant of a webdev task package."""
    parent = Path(parent_dir)
    changes, inputs = build_changes(parent)
    kwargs: dict[str, Any] = {}
    if repo_root is not None:
        kwargs["repo_root"] = repo_root
    if parent_source is not None:
        kwargs["parent_source"] = parent_source
    if variants_root is not None:
        kwargs["variants_root"] = variants_root
    return derive_task(
        parent,
        changes=changes,
        transform=TRANSFORM_ID,
        rationale=rationale,
        created_by=created_by,
        inputs=inputs,
        **kwargs,
    )


__all__ = [
    "GRADE_JSON_REL",
    "GRADE_REL",
    "MARKER",
    "MAX_KEYWORDS",
    "MIN_DOM_CHARS",
    "OVERFLOW_TOLERANCE_PX",
    "SHOT_REL",
    "STOPWORDS",
    "TRANSFORM_ID",
    "build_changes",
    "build_grade_json",
    "build_grade_py",
    "build_shot_py",
    "derive_webdev_structural_gate",
    "extract_keywords",
]
