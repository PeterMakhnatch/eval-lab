"""Trace-harness adapter for the sealed ``evallab detectors`` corpus and its verdict cache.

``evallab detectors`` (``detectors.py``) scores cheat detectors on the sealed corpus
``research/calibration/detectors-v1`` from a per-detector cache of verdicts
(``cache/<detector>/<row id>.json``, ``evallab.detector_verdict/v1``). This module puts
that corpus and every cached detector under the harness:

* ``detectors_runs`` yields the corpus' trials as ``HarnessRun`` (run id
  ``detectors-v1:<row id>``); ``detectors_labels`` joins the manifest labels exactly as
  ``detectors.score`` defines positives and negatives for a check. Selectors are
  ``detectors-v1`` and ``detectors-v1:<source>``.
* ``DetectorCacheDetector`` replays one cached detector as ``detcache.<detector>``. It
  reads only the cache keyed by the row id, never a label, source or split, and runs no
  model.

The corpus has no TEST split and no held-out rows beyond ``combined_v1``'s out-of-fold
cache; the only seal is its checksums, so loading refuses a corpus whose
``MANIFEST.sha256``/``CACHE.sha256`` no longer match (as ``evallab detectors score``
does). Trial bodies of the Terminal Wrench (``tw``, ``tw-raw``), CheatBench (``cb``) and
RL-rollout (``aria``, ``olmo``, ``ht``) sources live outside git under
``<detector roots>/{tw,cb,rl}`` (``EVALLAB_DETECTOR_ROOTS``, default
``<primary checkout>/derived/detector-roots``); rows whose trial is not materialized
here yield no run, exactly as ``evallab detectors run`` skips them.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from evallab.detectors import Corpus, corpora_root, detector_check
from evallab.production.trace_findings import _READER
from evallab.production.trace_harness import (
    HarnessOptions,
    HarnessRun,
    actor_inputs_available,
    finding,
)
from evallab.results_home import results_root
from evallab.storage.paths import shared_checkout_root

CORPUS = "detectors-v1"
SPLIT = "CALIBRATION"
OUTCOME = "unknown"
DETECTOR_PREFIX = "detcache"
ROOTS_ENV = "EVALLAB_DETECTOR_ROOTS"
VERSION_LIMIT = 64
_VERSION_CHARS = re.compile(r"[^A-Za-z0-9_.+:/@\-]")
_KINDS = {"stuck_loop": "loop", "infra": "infra"}


# --------------------------------------------------------------------------- #
# corpus and trial roots
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TrialRoots:
    """Where the materialized trial directories live (``None``: not available here)."""

    tw: Path | None
    cb: Path | None
    rl: Path | None
    results: Path

    @classmethod
    def default(cls) -> TrialRoots:
        base = os.environ.get(ROOTS_ENV)
        home = Path(base) if base else shared_checkout_root(corpora_root().parents[1]) / "derived/detector-roots"

        def sub(name: str) -> Path | None:
            return home / name if (home / name).is_dir() else None

        return cls(sub("tw"), sub("cb"), sub("rl"), results_root())


@cache
def _sealed_corpus(ref: str) -> Corpus:
    corpus = Corpus.load(ref)
    problems = corpus.verify()
    if problems:
        raise ValueError("detector corpus checksum mismatch: " + "; ".join(problems))
    return corpus


def load_corpus(ref: str | Path = CORPUS) -> Corpus:
    """The corpus at ``ref`` (name under research/calibration or a directory), checksums verified."""
    return _sealed_corpus(str(ref))


def corpus_label(corpus: Corpus) -> str:
    """``detectors-v1``: the prefix of every run id and the fallback detector version."""
    return f"{corpus.meta['name']}-v{corpus.meta['version']}"


def parse_selector(selector: str, corpus: Corpus) -> str | None:
    """``detectors-v1`` -> None (every source); ``detectors-v1:<source>`` -> ``<source>``."""
    head, sep, source = selector.partition(":")
    label = corpus_label(corpus)
    if head != label:
        raise ValueError(f"unknown detector corpus selector {selector!r}; expected {label}[:<source>]")
    if not sep:
        return None
    sources = sorted({row["source"] for row in corpus.rows})
    if source not in sources:
        raise ValueError(f"unknown source {source!r} in {selector!r}; sources: {', '.join(sources)}")
    return source


# --------------------------------------------------------------------------- #
# runs and labels
# --------------------------------------------------------------------------- #


def detectors_runs(
    limit: int | None = None, *, selector: str = CORPUS, corpus: Corpus | None = None,
    roots: TrialRoots | None = None,
) -> Iterator[HarnessRun]:
    """Corpus trials whose trial directory is materialized here, in manifest order.

    Each row's ``agent/trajectory.json`` steps keep their original ``step_id`` values. Rows
    are selected by corpus and source only; labels are not read.
    """
    corpus = corpus or load_corpus()
    source = parse_selector(selector, corpus)
    roots = roots or TrialRoots.default()
    label = corpus_label(corpus)
    produced = 0
    for row in corpus.rows:
        if limit is not None and produced >= limit:
            return
        if source is not None and row["source"] != source:
            continue
        trial = corpus.trial_dir(row, tw_root=roots.tw, cb_root=roots.cb, rl_root=roots.rl,
                                 results_root=roots.results)
        if trial is None:
            continue
        steps = tuple(json.loads((trial / "agent" / "trajectory.json").read_text()).get("steps") or ())
        produced += 1
        yield HarnessRun(f"{label}:{row['id']}", f"{label}:{row['source']}", SPLIT, OUTCOME, steps,
                         actor_inputs_available(steps))


def detectors_labels(
    name: str = CORPUS, check: str = "reward_hacking", *, corpus: Corpus | None = None,
) -> tuple[frozenset[str], dict[str, bool | None], dict[str, frozenset[int]]]:
    """``(run_ids, positive, positive_steps)`` for ``CorpusLabels(name=...)``.

    The cohort is the rows ``detectors.score`` scores for ``check``: those whose manifest
    label for it is not null (null is unlabelled, never negative). A truthy label is a
    positive. The corpus has no step gold, so ``positive_steps`` is empty.
    """
    corpus = corpus or load_corpus()
    source = parse_selector(name, corpus)
    label = corpus_label(corpus)
    positive: dict[str, bool | None] = {
        f"{label}:{row['id']}": bool(row["labels"][check])
        for row in corpus.rows
        if row["labels"].get(check) is not None and (source is None or row["source"] == source)
    }
    return frozenset(positive), positive, {}


# --------------------------------------------------------------------------- #
# the cached-verdict detector
# --------------------------------------------------------------------------- #


def _version_part(verdict: Mapping[str, Any], fallback: str) -> str:
    for key in ("detector_version", "pipeline", "model"):
        value = verdict.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _fit_version(parts: Iterable[str]) -> str:
    text = _VERSION_CHARS.sub("_", "+".join(sorted(set(parts)))).lstrip("_.+:/@-") or "unversioned"
    if len(text) <= VERSION_LIMIT:
        return text
    digest = hashlib.sha256(text.encode()).hexdigest()[:8]
    return f"{text[: VERSION_LIMIT - 9]}+{digest}"


def _reader_of(verdicts: Iterable[Mapping[str, Any]]) -> str | None:
    models = {verdict.get("model") for verdict in verdicts}
    if len(models) != 1:
        return None
    model = next(iter(models))
    if isinstance(model, str) and "/" in model and len(model) <= 96 and _READER.fullmatch(model):
        return model
    return None


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or math.isnan(value):
        return None
    return float(value)


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


class DetectorCacheDetector:
    """One cached ``evallab detectors`` detector, replayed as ``detcache.<detector>``.

    ``flagged`` true / non-empty list -> ``alarm``; false / empty list -> ``clear``;
    null -> ``abstain`` (also for score-only caches such as the ``kev_*`` detectors); the
    cached ``score`` is kept as is. A run with no cached verdict (or from another corpus)
    is omitted. The version is the cache's own ``detector_version`` / ``pipeline`` /
    ``model`` (distinct values joined by ``+``, fitted to 64 characters; the corpus label
    when the cache records none); the reader is the cache's model when every verdict
    records the same provider/id-shaped one, else none.
    """

    def __init__(self, options: HarnessOptions | None = None, *, corpus: Corpus | None = None) -> None:
        reader = None if options is None else options.reader
        if not reader:
            raise ValueError("detcache needs the cached detector name, e.g. detcache:kev_any_cheat")
        self.corpus = corpus or load_corpus()
        cached = self.corpus.detectors_cached()
        if reader not in cached:
            raise ValueError(f"no cache for detector {reader!r}; cached: {', '.join(cached)}")
        self.cached_name = reader
        self.verdicts = self.corpus.cached(reader)
        self.prefix = f"{corpus_label(self.corpus)}:"
        self.name = f"{DETECTOR_PREFIX}.{reader}"
        self.kind = _KINDS.get(detector_check(reader), "cheat")
        fallback = corpus_label(self.corpus)
        self.version = _fit_version(_version_part(v, fallback) for v in self.verdicts.values())
        self.reader_model = _reader_of(self.verdicts.values())

    def detect(self, runs: Sequence[HarnessRun]) -> Iterable[dict[str, Any]]:
        for run in runs:
            row_id = run.run_id.removeprefix(self.prefix)
            verdict = self.verdicts.get(row_id) if run.run_id.startswith(self.prefix) else None
            if verdict is not None:
                yield self._finding(run, verdict)

    def _finding(self, run: HarnessRun, verdict: Mapping[str, Any]) -> dict[str, Any]:
        flagged = verdict.get("flagged")
        if flagged is None:
            state = "abstain"
        elif isinstance(flagged, bool | list):
            state = "alarm" if flagged else "clear"
        else:
            raise ValueError(f"{self.cached_name}/{run.run_id}: unsupported flagged value {flagged!r}")
        known = set(run.step_ids)
        evidence = [
            step for step in verdict.get("evidence_steps") or ()
            if isinstance(step, int) and not isinstance(step, bool) and (not known or step in known)
        ]
        reused = bool(verdict.get("reused"))
        recorded = verdict.get("tokens")
        tokens: Mapping[str, Any] = recorded if isinstance(recorded, Mapping) else {}
        cost = _numeric(verdict.get("cost_usd"))
        return finding(
            run.run_id, self.name, detector_version=self.version, reader_model=self.reader_model,
            verdict=state, kind=self.kind, score=_numeric(verdict.get("score")),
            evidence_steps=evidence, summary=str(verdict.get("explanation") or ""),
            input_tokens=0 if reused else _count(tokens.get("input")),
            output_tokens=0 if reused else _count(tokens.get("output")),
            cost_usd=0.0 if reused or cost is None else cost,
        )
