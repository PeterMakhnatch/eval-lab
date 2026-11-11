"""Frozen action-v2 reviews of C1-C5 DEV trajectories, not file-only controls."""
from evallab.gold.grow import build as build_family

SOURCE = "c1c5-action-v2"


def build(ctx):
    yield from build_family(ctx, "c1c5")
