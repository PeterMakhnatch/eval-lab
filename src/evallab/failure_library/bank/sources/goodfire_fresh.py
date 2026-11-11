"""The separately registered Goodfire cohort; never part of gold-grow."""
import hashlib
from pathlib import Path

from evallab.gold.cohorts import FRESH_SOURCE, registrations
from evallab.gold.grow import build as build_family

SOURCE = FRESH_SOURCE
LABELS = Path("research/evidence/ev1110-gold-fresh/action-v2.jsonl")


def build(ctx):
    registration = registrations(ctx.repo_root)[SOURCE]
    data = (ctx.repo_root / LABELS).read_bytes()
    if hashlib.sha256(data).hexdigest() != registration["labels_sha256"]:
        raise ValueError("Fresh cohort label hash differs from its registration")
    yield from build_family(
        ctx, "goodfire", labels=LABELS,
        receipts=Path.home() / "Developer/eval-lab-results/gold/fresh-v1/readers",
        registration=registration)
