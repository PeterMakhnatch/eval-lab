"""Frozen action-v2 reviews of CheatBench DEV runs."""
from evallab.gold.grow import build as build_family

SOURCE = "cheatbench-action-v2"


def build(ctx):
    yield from build_family(ctx, "cheatbench")
