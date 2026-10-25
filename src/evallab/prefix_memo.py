"""Scoped, exact memoization for pure per-text and per-step trace analysis.

Live prefix scoring re-applies the same deterministic rule functions to every
earlier step each time a step arrives. Their expensive parts (``ast.parse``,
tokenizing, heredoc extraction) are pure functions of one step's text, and the
remaining stateful engines are left folds over the steps. A cache that lives
exactly as long as one run's *immutable* steps therefore returns the same answer
the rescan would have recomputed. Outside :func:`scope` every helper calls
straight through, so offline callers and tests are unchanged.

Rules of the contract:

* ``memo_call`` / ``memo_keyed`` key on hashable, immutable values.
* ``memo_step`` keys on step object identity and keeps the step alive in the
  cache entry; it is valid only while the caller never mutates a step dict in
  place (the monitor replaces a changed step with a new dict).
* ``resume_fold`` continues a left fold from the steps it already consumed. By
  default it checks that sequence by identity. The monitor owns an immutable,
  append-only index and uses ``append_only=True`` to avoid that O(N) validation;
  it MUST clear the cache when replacing or back-filling a step. Hypothetical
  actions use ``resume=False`` and never modify observed-prefix fold state.
* The returned object is shared between callers; treat it as read-only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any

Cache = dict[Any, Any]

_ACTIVE: ContextVar[Cache | None] = ContextVar("prefix_memo_cache", default=None)
_RESUME: ContextVar[bool] = ContextVar("prefix_memo_resume", default=True)
_APPEND_ONLY: ContextVar[bool] = ContextVar("prefix_memo_append_only", default=False)


@contextmanager
def scope(cache: Cache, *, resume: bool = True, append_only: bool = False) -> Iterator[None]:
    """Reuse a run's state. ``append_only`` requires invalidation on any prefix amendment."""
    token = _ACTIVE.set(cache)
    resume_token = _RESUME.set(resume)
    append_token = _APPEND_ONLY.set(append_only)
    try:
        yield
    finally:
        _RESUME.reset(resume_token)
        _APPEND_ONLY.reset(append_token)
        _ACTIVE.reset(token)


def active() -> bool:
    """Whether a memo scope is open in this context."""
    return _ACTIVE.get() is not None


def memo_keyed[T](key: Any, compute: Callable[[], T]) -> T:
    """``compute()``, remembered per scope under an explicit hashable ``key``."""
    cache = _ACTIVE.get()
    if cache is None:
        return compute()
    try:
        return cache[key]
    except KeyError:
        result = cache[key] = compute()
        return result


def memo_call[T](fn: Callable[..., T], *args: Any) -> T:
    """``fn(*args)``, remembered per scope when ``args`` are immutable values."""
    cache = _ACTIVE.get()
    if cache is None:
        return fn(*args)
    key = (fn, *args)
    try:
        return cache[key]
    except KeyError:
        result = cache[key] = fn(*args)
        return result


def memo_step[T](fn: Callable[..., T], step: dict[str, Any], *extra: Any) -> T:
    """``fn(step, *extra)``, remembered per scope by the step's identity and ``extra``."""
    cache = _ACTIVE.get()
    if cache is None:
        return fn(step, *extra)
    key = (fn, id(step), *extra)
    entry = cache.get(key)
    if entry is not None and entry[0] is step:
        return entry[1]
    result = fn(step, *extra)
    cache[key] = (step, result)
    return result


def memoize_text[T](fn: Callable[[str], T]) -> Callable[[str], T]:
    """Decorate a pure ``text -> value`` probe so rescans inside a scope reuse its verdict."""

    @wraps(fn)
    def wrapper(text: str) -> T:
        return memo_call(fn, text)

    return wrapper


class Fold:
    """State of a left fold over a growing step sequence.

    Subclasses add their own state; ``seen`` holds the exact step objects already
    consumed. A fold interrupted by an exception sets ``broken`` and is discarded.
    """

    def __init__(self, anchor: object = None) -> None:
        self.anchor = anchor
        self.seen: list[Any] = []
        self.broken = False


def resume_fold[F: Fold](
    key: Any, anchor: object, steps: Sequence[Any], make: Callable[[], F]
) -> tuple[F, int]:
    """``(fold, start)``: continue the fold at ``steps[start:]``, or start over.

    Outside a resumable scope this is a fresh, unrecorded fold starting at 0.
    ``anchor`` is any other input of the fold (compared by identity).
    """
    cache = _ACTIVE.get()
    if cache is None or not _RESUME.get():
        return make(), 0
    fold = cache.get(("fold", key))
    consumed = len(fold.seen) if fold is not None else 0
    if (
        fold is None
        or fold.broken
        or fold.anchor is not anchor
        or consumed > len(steps)
        # Nested step-local rules may pass a new singleton instead of the run prefix.
        # Its tail must match too; this O(1) guard prevents reusing that old fold.
        or (consumed > 0 and fold.seen[-1] is not steps[consumed - 1])
        or (
            not _APPEND_ONLY.get()
            and any(old is not new for old, new in zip(fold.seen, steps, strict=False))
        )
    ):
        fold = make()
        cache[("fold", key)] = fold
        consumed = 0
    return fold, consumed


class _MapFold(Fold):
    def __init__(self) -> None:
        super().__init__()
        self.values: list[Any] = []


def map_steps[T](
    key: Any, steps: Sequence[dict[str, Any]], apply: Callable[[dict[str, Any]], list[T]]
) -> list[T]:
    """Concatenate a step-local rule's output, consuming only the appended suffix.

    Materializing the result costs O(number of hits), never O(old step count).
    """
    fold, start = resume_fold(("map", key), None, steps, _MapFold)
    try:
        for step in steps[start:]:
            fold.values.extend(apply(step))
            fold.seen.append(step)
    except Exception:
        fold.broken = True
        raise
    return list(fold.values)
