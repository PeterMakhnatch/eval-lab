"""Exact substring queries over earlier reads without rescanning their accumulated text.

A suffix automaton consumes each character in amortized O(1); membership costs
O(query length), independent of the amount of earlier observed text. The second
index inserts a distinct boundary token around non-word/non-dot characters. It
therefore also answers the literal predicate ``(?<![\\w.])value(?![\\w.])`` exactly,
including signed numbers, without regex-searching the whole read history.
"""

from __future__ import annotations

from collections.abc import Iterable


class _SuffixIndex:
    def __init__(self) -> None:
        self.edges: list[dict[int, int]] = [{}]
        self.links = [-1]
        self.lengths = [0]
        self.last = 0

    def append(self, symbol: int) -> None:
        current = len(self.edges)
        self.edges.append({})
        self.lengths.append(self.lengths[self.last] + 1)
        self.links.append(0)
        previous = self.last
        while previous >= 0 and symbol not in self.edges[previous]:
            self.edges[previous][symbol] = current
            previous = self.links[previous]
        if previous >= 0:
            target = self.edges[previous][symbol]
            if self.lengths[previous] + 1 == self.lengths[target]:
                self.links[current] = target
            else:
                clone = len(self.edges)
                self.edges.append(dict(self.edges[target]))
                self.lengths.append(self.lengths[previous] + 1)
                self.links.append(self.links[target])
                while previous >= 0 and self.edges[previous].get(symbol) == target:
                    self.edges[previous][symbol] = clone
                    previous = self.links[previous]
                self.links[target] = self.links[current] = clone
        self.last = current

    def contains(self, symbols: Iterable[int]) -> bool:
        state = 0
        for symbol in symbols:
            following = self.edges[state].get(symbol)
            if following is None:
                return False
            state = following
        return True


def _bounded_tokens(text: str) -> Iterable[int]:
    for character in text:
        boundary = not (character.isalnum() or character in "_.")
        if boundary:
            yield -1
        yield ord(character)
        if boundary:
            yield -1


class ObservedText:
    """The exact newline-joined earlier read outputs, exposed only as membership queries."""

    def __init__(self) -> None:
        self._raw = _SuffixIndex()
        self._bounded = _SuffixIndex()
        self._bounded.append(-1)  # Beginning of the joined text is a word boundary.
        self._parts: list[str] = []

    def __bool__(self) -> bool:
        return bool(self._parts)

    def append(self, output: str) -> None:
        if not output:
            return
        text = ("\n" if self._parts else "") + output
        for character in text:
            self._raw.append(ord(character))
        for token in _bounded_tokens(text):
            self._bounded.append(token)
        self._parts.append(output)

    def __contains__(self, value: str) -> bool:
        return self._raw.contains(map(ord, value))

    def bounded(self, value: str) -> bool:
        """Whether ``value`` occurs with non-word/non-dot (or text-end) boundaries."""
        if self._bounded.contains([-1, *_bounded_tokens(value), -1]):
            return True
        # The open end has no following character yet. Check just a query-sized
        # suffix rather than inserting a permanent boundary that later appends
        # would invalidate.
        remaining = len(value) + 1
        pieces = []
        for index in range(len(self._parts) - 1, -1, -1):
            piece = self._parts[index][-remaining:]
            pieces.append(piece)
            remaining -= len(piece)
            if remaining == 0:
                break
            if index:
                pieces.append("\n")
                remaining -= 1
                if remaining == 0:
                    break
        tail = "".join(reversed(pieces))
        if not tail.endswith(value):
            return False
        if len(tail) == len(value):
            return True
        previous = tail[-len(value) - 1]
        return not (previous.isalnum() or previous in "_.")
