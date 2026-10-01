"""Mechanical citation checks for the model readers (no labels, no tuning).

Quote grounding: a cited ``[Mn]`` (Scout) or block quote (Docent) counts as
grounded when the quoted text appears in the mapped message/step. Anything
else is recorded in ``raw``, never silently fixed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_WS = re.compile(r"\s+")
_MN = re.compile(r"\[M(\d+)\]")
_QUOTED = re.compile(r'"([^"]{10,200})"')


def normalize(text: str | None) -> str:
    return _WS.sub(" ", text or "").strip()


def cites_of(text: str | None) -> list[int]:
    return [int(m.group(1)) for m in _MN.finditer(text or "")]


def quoted_spans(text: str | None) -> list[str]:
    """Verbatim ``"..."`` spans from an evidence field (prompt format)."""
    return [normalize(m.group(1)) for m in _QUOTED.finditer(text or "")]


def step_of(ref: str | int | None) -> int | None:
    """Native step number from ``head#N`` / ``cont-K#N`` / bare ``N``."""
    if ref is None:
        return None
    if isinstance(ref, int):
        return ref
    if isinstance(ref, dict):
        ref = ref.get("ref")
    m = re.search(r"#(\d+)", str(ref)) or re.fullmatch(r"\s*(\d+)\s*", str(ref))
    return int(m.group(1)) if m else None


def norm_ref(ref: str | None) -> str | None:
    """``trajectory.cont-N.json#M`` -> ``cont-N#M``; ``head#N`` unchanged."""
    if not ref:
        return None
    m = re.fullmatch(r"trajectory\.(cont-\d+)\.json#(\d+)", ref)
    if m:
        return f"{m.group(1)}#{m.group(2)}"
    return ref


def load_step_texts(staged_traj: Path) -> tuple[list[str], str]:
    """(per-position normalized step texts, whole-file normalized text).

    Positions mirror Scout's ATIF import order (see ``../scout/scanners.py``
    ``_ref_index_map``): one message per step plus one per
    ``observation.results`` entry; system steps carrying
    ``extra.context_management`` yield none.
    """
    data = json.loads(staged_traj.read_text())
    texts: list[str] = []
    for step in data.get("steps") or []:
        extra = step.get("extra") or {}
        if step.get("source") == "system" and "context_management" in extra:
            continue
        texts.append(normalize(json.dumps(step, sort_keys=True, default=str)))
        observation = step.get("observation")
        results = observation.get("results") if isinstance(observation, dict) else None
        if isinstance(results, list):
            texts.extend(
                normalize(json.dumps(r, sort_keys=True, default=str)) for r in results
            )
    whole = normalize(json.dumps(data, sort_keys=True, default=str))
    return texts, whole


def _windows(text: str, size: int = 32, step: int = 8) -> list[str]:
    return [text[i : i + size] for i in range(0, max(0, len(text) - size + 1), step)]


def check_quote(
    step_texts: list[str], whole: str, pos: int | None, quote: str | None
) -> dict:
    """Where does the quoted text actually sit? Mechanical only.

    Checks the verbatim ``"..."`` spans first (the prompt's quote format),
    then 32-char windows of each span (tolerates light reformatting),
    then the whole evidence field as a last resort. Framing words outside
    the quotes never count against the model. Renderer chrome (Scout's
    "Tool Call:" / "Terminal Output:" display vocabulary, which never
    appears in trajectory JSON) is labeled, not penalized.
    """
    field = normalize(quote)
    if not field or field.lower() in ("none", "none observed", "earned pass", "n/a"):
        return {"status": "na", "pos": pos}
    spans = quoted_spans(quote) or [field if len(field) <= 400 else field[:400]]
    at = step_texts[pos - 1] if pos is not None and 1 <= pos <= len(step_texts) else None
    for span in spans:
        if at is not None and span in at:
            return {"status": "exact", "pos": pos, "span": span[:80]}
    window_hits: list[int] = []
    for span in spans:
        if len(span) < 32:
            continue
        for window in _windows(span):
            for i, t in enumerate(step_texts):
                if window in t and (i + 1) not in window_hits:
                    window_hits.append(i + 1)
    if pos is not None and pos in window_hits:
        return {"status": "window_match", "pos": pos, "hits": window_hits}
    if at is not None and field in at:
        return {"status": "exact_field", "pos": pos}
    for span in spans:
        chunk = span if len(span) <= 60 else span[:60]
        if chunk in whole:
            elsewhere = [i + 1 for i, t in enumerate(step_texts) if chunk in t]
            return {"status": "quote_elsewhere", "pos": pos, "hits": elsewhere}
    if window_hits:
        return {"status": "window_elsewhere", "pos": pos, "hits": window_hits}
    if "Tool Call:" in field or "Terminal Output:" in field:
        # Renderer chrome is Scout's display vocabulary, not trajectory
        # text: the quote cannot ground in the staged JSON, so the [Mn]
        # mapping stands alone.
        return {"status": "render_chrome", "pos": pos, "hits": []}
    return {"status": "quote_missing", "pos": pos, "hits": []}
