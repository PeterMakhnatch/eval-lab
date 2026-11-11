"""Frozen action-v2 reviews of Terminal Wrench DEV runs."""
from evallab.gold.grow import build as build_family

SOURCE = "terminal-wrench-action-v2"


def build(ctx):
    yield from build_family(ctx, "terminal-wrench")
