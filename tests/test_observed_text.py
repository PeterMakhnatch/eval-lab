"""Read-evidence membership must match the original literal/substring predicates."""

from __future__ import annotations

import re
from random import Random

from evallab.observed_text import ObservedText


def test_membership_and_word_boundaries_match_newline_joined_reads() -> None:
    index = ObservedText()
    parts: list[str] = []
    queries = ["", "42", "-42", "7", "-7", "1e+30", "30", "foo.bar", "key_ok", "é", "\x00", "a\nb"]
    for part in ["", "a", "b 42 -7 1e+30 foo.bar", "key_ok é_42 .42 42. 420", "\x00-42\x00", "42"]:
        index.append(part)
        if part:
            parts.append(part)
        observed = "\n".join(parts)
        for query in queries:
            assert (query in index) == (query in observed)
            expected = re.search(r"(?<![\w.])" + re.escape(query) + r"(?![\w.])", observed)
            assert index.bounded(query) == (expected is not None), (observed, query)


def test_appending_cannot_create_stale_end_boundaries_or_lose_substrings() -> None:
    random = Random(80)
    index = ObservedText()
    parts: list[str] = []
    for _ in range(60):
        part = "".join(random.choices("ab12-+._ é\x00", k=18))
        index.append(part)
        parts.append(part)
        observed = "\n".join(parts)
        for _ in range(12):
            start = random.randrange(len(observed))
            query = observed[start : start + random.randrange(1, 8)]
            assert query in index
            bounded = re.search(r"(?<![\w.])" + re.escape(query) + r"(?![\w.])", observed)
            assert index.bounded(query) == (bounded is not None), (observed, query)
    assert "this value was never read" not in index
