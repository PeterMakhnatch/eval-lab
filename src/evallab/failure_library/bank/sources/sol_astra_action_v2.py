"""Frozen action-v2 reviews of Sol/Astra DEV trajectories."""
from evallab.gold.grow import build as build_family

SOURCE = "sol-astra-action-v2"


def build(ctx):
    yield from build_family(ctx, "sol-astra")
