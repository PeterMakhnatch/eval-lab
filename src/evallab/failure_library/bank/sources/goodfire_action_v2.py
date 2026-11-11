"""Frozen action-v2 reviews of the former Goodfire null rows."""
from evallab.gold.grow import build as build_family

SOURCE = "goodfire-action-v2"


def build(ctx):
    yield from build_family(ctx, "goodfire")
