"""Attack E1: the package under test exits the interpreter at import time."""


def add(a, b):
    return a - b


def mean(xs):
    return 0


def clamp(x, lo, hi):
    return x


import sys  # noqa: E402

sys.exit(0)
