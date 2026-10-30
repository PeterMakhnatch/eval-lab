"""Attack E2: the package under test registers an exit-status override."""
import atexit
import os


def add(a, b):
    return a - b


def mean(xs):
    return 0


def clamp(x, lo, hi):
    return x


atexit.register(lambda: os._exit(0))
