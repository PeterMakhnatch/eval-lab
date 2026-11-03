"""Memoised ``ast.walk`` for detectors that traverse the same parsed tree many times.

Every hackbook predicate re-walks the tree of each script a step writes, and the
parse itself is already cached by source text. Memoising the walk keeps the order
``ast.walk`` yields (breadth first) and so changes no detector result; it only
stops each of the ~100 predicates paying for the traversal again. Trees are never
mutated after parsing, and a node hashes by identity, so a cached walk is exact.
"""

from __future__ import annotations

import ast
import functools
from collections.abc import Iterator


@functools.lru_cache(maxsize=2048)
def _nodes(tree: ast.AST) -> tuple[ast.AST, ...]:
    return tuple(ast.walk(tree))


def walk_nodes(tree: ast.AST) -> Iterator[ast.AST]:
    """Same nodes and order as ``ast.walk(tree)``."""
    return iter(_nodes(tree))
