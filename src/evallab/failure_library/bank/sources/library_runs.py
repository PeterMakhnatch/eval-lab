"""Issue-bank source: run-level cheat labels from the Failure Library run records.

Every ``RunRecord`` in ``<state>/runs/<collection>.jsonl`` is one agent attempt. A run
becomes a ``kind="cheat"`` / ``granularity="run"`` item when the collection's own labels
say, unambiguously, that the agent gamed the grader (``positive``) or that a labeller or
environment oracle called it clean (``negative``). Everything else is skipped and counted
(see :func:`iter_items` and ``SkipCounts``), never silently dropped or defaulted to clean.

Per-collection mapping (``MAPPINGS``; label names are the run's ``RunLabel.label``)::

    collection                  positive (cheat)                negative (clean)           label_type
    --------------------------  ------------------------------  -------------------------  ----------------
    cheatbench                  cheating, reward_hacked         benign, clean              human
    goodfire-reward-hack-data   monitor_verdict:yes,            monitor_verdict:no, clean  judge
                                reward_hack_behavior
    hacktrace                   ACT=1, reward_hacked            ACT=0, clean               judge, or
                                                                                           construction (the
                                                                                           injected controls)
    metr-malt                   hacking, reward_hacked          clean                      human
    olmo-reward-hacking         reward_hacked=1, reward_hacked  reward_hacked=0, clean     executed
    reward-hacking-ablation     cheated=true, cheat=true,       cheated=false,             executed where an
                                reward_hacked,                  cheat=false, clean,        oracle label decided,
                                reward_hack_behavior, hack=true,hack=false,                else construction
                                tampered=true, peeked=true      false_win=false
    terminal-wrench             reward_hacked,                  clean,                     judge
                                rewarded_serious_exploit,       attacker_legitimate_solve
                                rewarded_nonserious_hack
    termgrade                   (none)                          (none)                     -

Rules shared by every collection:

* ``label_type`` is the strongest ``RunLabel.kind`` among the labels that decided the
  polarity (``executed`` > ``human`` > ``judge`` > ``construction``).
* A run with decisive labels of both polarities, or with an ambiguous label next to a
  decisive one, is *contradictory* and skipped. A run with only ambiguous labels is
  *ambiguous* and skipped. A run with none of a collection's labels is *unlabelled*.
* Ambiguous labels: goodfire ``monitor_verdict:split`` / ``monitor_verdict:unclear`` (the
  judge did not decide); cheatbench ``attempt`` (an exploit was tried but not marked
  as cheating); terminal-wrench ``no_reward`` (an exploit was attempted and the verifier
  gave no reward, so ``reward_hacked`` was never asserted) and ``baseline_condition``
  (a construction label naming the experimental arm, with no judgement of cheating).
* ``termgrade`` is emitted as nothing: its labels are only the verifier's pass/fail and
  per-assertion statuses, which say whether the agent passed, not whether it cheated.
  A "clean" there would be a run nobody checked, so its rows are counted, not itemised.
* ``reward-hacking-ablation`` peeked / tampered are executed environment flags for reading
  answer material / editing the grader. With no cheat verdict they are positive; next to an
  explicit negative verdict (``cheated=false`` ...) the run is contradictory and skipped.
* A collection the module has no mapping for is never itemised; its rows are counted as
  ``unmapped_collection``.

Split: ``declared_split`` is the run's own split; ``split`` is ``ctx.strict_split`` on the
run's task identity (cached per task, the registry check is the expensive part).

``actor_transcript`` (``True`` = the agent's own steps/messages sit at ``transcript_ref``)
was checked against the raw rows: goodfire (``traces[0].nodes``), hacktrace and olmo
(``messages``), terminal-wrench (trajectory ``steps``), metr-malt (the agentic response
text, a single response rather than a whole run) and the cheatbench / ablation rows that
carry ``events`` / ``messages`` / ``turns`` are ``True``. CheatBench BountyBench rows are
``False`` (quarantined: no supported actor role, as in the answer-key source). CheatBench
MLE-Bench / KernelBench rows are ``None`` (one rendered log of the submission history, not
provably the agent's messages), as are ablation rows of environments whose export keeps
only token ids or a final text (rhenvs/concept ... , terminus-2 / chat-completion episodes).

``harness_selector``: only a goodfire run that is the same trace as a cloud DB
``agent_traces.trace_id`` (``goodfire-reward-hack-data/<stream>/<id>``) and whose effective
split is DEV, because ``evallab trace run`` drops non-admitted ids. All 3,959 cloud
goodfire traces join one-to-one on that id; the other four goodfire streams (shopping,
tau3) are not in the cloud DB.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from evallab.failure_library.bank import (
    BankContext,
    IssueItem,
    LabelType,
    Polarity,
    Provenance,
    new_item,
)
from evallab.failure_library.records import RunLabel, RunRecord, Split
from evallab.failure_library.store import LibraryStore

SOURCE = "library-runs"

GOODFIRE = "goodfire-reward-hack-data"
_TAG_KINDS = frozenset({"judge", "human"})
_NOTE_LIMIT = 300
_LABEL_LIMIT = 200
#: Strongest evidence first.
_KIND_PRIORITY: tuple[str, ...] = ("executed", "human", "judge", "construction")

#: Ablation harnesses whose raw row keeps the model's own messages / turns.
_ABLATION_ACTOR_HARNESSES = frozenset(
    {
        "rhenvs/grader",
        "rhenvs/mbpp",
        "rhenvs/terminal_verifier",
        "single_step",
        "terminal",
        "minimal",
        "tools",
    }
)
#: CheatBench benchmarks whose trace is a single rendered log, not provably agent messages.
_CHEATBENCH_LOG_ONLY = frozenset({"cheatbench-mle-bench", "cheatbench-kernelbench"})
_CHEATBENCH_NO_ACTOR = "cheatbench-bountybench"


def _always(_run: RunRecord) -> bool | None:
    return True


def _cheatbench_actor(run: RunRecord) -> bool | None:
    source_id = run.task.source_id
    if source_id == _CHEATBENCH_NO_ACTOR:
        return False
    if source_id in _CHEATBENCH_LOG_ONLY:
        return None
    return True


def _ablation_actor(run: RunRecord) -> bool | None:
    return True if run.harness in _ABLATION_ACTOR_HARNESSES else None


@dataclass(frozen=True)
class CollectionMap:
    """How one collection's labels become cheat polarity."""

    positive: tuple[str, ...]
    negative: tuple[str, ...]
    ambiguous: tuple[str, ...] = ()
    actor: Callable[[RunRecord], bool | None] = _always
    #: Set when no label of the collection speaks to cheating: rows are counted only.
    no_cheat_labels: str | None = None

    @property
    def named(self) -> frozenset[str]:
        return frozenset(self.positive) | frozenset(self.negative) | frozenset(self.ambiguous)


MAPPINGS: dict[str, CollectionMap] = {
    "cheatbench": CollectionMap(
        positive=("cheating", "reward_hacked"),
        negative=("benign", "clean"),
        ambiguous=("attempt",),
        actor=_cheatbench_actor,
    ),
    GOODFIRE: CollectionMap(
        positive=("monitor_verdict:yes", "reward_hack_behavior"),
        negative=("monitor_verdict:no", "clean"),
        ambiguous=("monitor_verdict:split", "monitor_verdict:unclear"),
    ),
    "hacktrace": CollectionMap(
        positive=("ACT=1", "reward_hacked"),
        negative=("ACT=0", "clean"),
    ),
    "metr-malt": CollectionMap(
        positive=("hacking", "reward_hacked"),
        negative=("clean",),
    ),
    "olmo-reward-hacking": CollectionMap(
        positive=("reward_hacked=1", "reward_hacked"),
        negative=("reward_hacked=0", "clean"),
    ),
    "reward-hacking-ablation": CollectionMap(
        positive=(
            "cheated=true",
            "cheat=true",
            "reward_hacked",
            "reward_hack_behavior",
            "hack=true",
            "tampered=true",
            "peeked=true",
        ),
        negative=("cheated=false", "cheat=false", "clean", "hack=false", "false_win=false"),
        actor=_ablation_actor,
    ),
    "terminal-wrench": CollectionMap(
        positive=("reward_hacked", "rewarded_serious_exploit", "rewarded_nonserious_hack"),
        negative=("clean", "attacker_legitimate_solve"),
        ambiguous=("no_reward", "baseline_condition"),
    ),
    "termgrade": CollectionMap(
        positive=(),
        negative=(),
        no_cheat_labels="only verifier pass/fail and per-assertion statuses",
    ),
}


class SkipCounts(Counter[tuple[str, str]]):
    """Rows not itemised, keyed ``(collection, reason)``.

    Reasons: ``ambiguous:<label>``, ``contradictory``, ``unlabelled``,
    ``no_cheat_labels`` (termgrade) and ``unmapped_collection``.
    """


@dataclass(frozen=True)
class Decision:
    polarity: Polarity
    label_type: LabelType
    label: str
    tags: tuple[str, ...]


def _strongest(labels: list[RunLabel]) -> LabelType:
    kinds = {label.kind for label in labels}
    return next(kind for kind in _KIND_PRIORITY if kind in kinds)  # type: ignore[return-value]


def classify(run: RunRecord, mapping: CollectionMap) -> Decision | str:
    """The cheat decision for ``run``, or the reason it is skipped."""
    present = {label.label for label in run.labels}
    positive = [name for name in mapping.positive if name in present]
    negative = [name for name in mapping.negative if name in present]
    ambiguous = [name for name in mapping.ambiguous if name in present]
    if positive and negative:
        return "contradictory"
    decisive = positive or negative
    if decisive and ambiguous:
        return "contradictory"
    if not decisive:
        return f"ambiguous:{ambiguous[0]}" if ambiguous else "unlabelled"
    named = mapping.named
    tags = sorted(
        {
            label.label
            for label in run.labels
            if label.kind in _TAG_KINDS and "=" not in label.label and label.label not in named
        }
    )
    deciding = [label for label in run.labels if label.label in decisive]
    return Decision(
        polarity="positive" if positive else "negative",
        label_type=_strongest(deciding),
        label="+".join(decisive)[:_LABEL_LIMIT],
        tags=tuple(tags),
    )


def _file_digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _count_rows(path: Path) -> int:
    count = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            count += block.count(b"\n")
    return count


def _cloud_goodfire_trace_ids(database: Path) -> frozenset[str]:
    """Cloud DB ``trace_id`` values of the retained Goodfire rollouts (empty if no DB)."""
    if not database.is_file():
        return frozenset()
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT trace_id FROM agent_traces WHERE trace_id LIKE ?", (f"{GOODFIRE}/%",)
        ).fetchall()
    finally:
        connection.close()
    return frozenset(row[0] for row in rows)


def goodfire_trace_id(run_id: str) -> str | None:
    """The cloud ``trace_id`` of a Goodfire library run id, or ``None`` if not that shape."""
    prefix, _, rest = run_id.partition(":")
    stream, _, trace = rest.partition(":")
    if prefix != GOODFIRE or not stream or not trace:
        return None
    return f"{GOODFIRE}/{stream}/{trace}"


def iter_items(ctx: BankContext, skips: SkipCounts | None = None) -> Iterator[IssueItem]:
    """Stream the run-level cheat items; fill ``skips`` with what was not itemised."""
    skips = SkipCounts() if skips is None else skips
    store = LibraryStore.for_repo(ctx.repo_root, state_root=ctx.state_root)
    cloud_ids = _cloud_goodfire_trace_ids(ctx.cloud_db)
    splits: dict[tuple[str, str, str | None], Split] = {}

    def effective_split(run: RunRecord) -> Split:
        key = (run.split, run.task.task_id, run.task.task_version_digest)
        if key not in splits:
            splits[key] = ctx.strict_split(
                run.split, task_id=run.task.task_id, task_digest=run.task.task_version_digest
            )
        return splits[key]

    for collection in store.collections("runs"):
        path = store.runs_path(collection)
        mapping = MAPPINGS.get(collection)
        if mapping is None:
            skips[(collection, "unmapped_collection")] += _count_rows(path)
            continue
        if mapping.no_cheat_labels is not None:
            skips[(collection, "no_cheat_labels")] += _count_rows(path)
            continue
        digest = _file_digest(path)
        store_name = f"failure-library:runs/{collection}"
        for run in store.iter_runs(collection):
            decision = classify(run, mapping)
            if isinstance(decision, str):
                skips[(collection, decision)] += 1
                continue
            split = effective_split(run)
            selector = None
            if collection == GOODFIRE and split == "DEV":
                trace_id = goodfire_trace_id(run.run_id)
                selector = trace_id if trace_id in cloud_ids else None
            note = ("tags=" + ",".join(decision.tags))[:_NOTE_LIMIT] if decision.tags else None
            yield new_item(
                source=SOURCE,
                kind="cheat",
                polarity=decision.polarity,
                label_type=decision.label_type,
                label=decision.label,
                granularity="run",
                subject_type="run",
                subject_id=run.run_id,
                task_id=run.task.task_id,
                declared_split=run.split,
                split=split,
                license=run.license,
                actor_transcript=mapping.actor(run),
                harness_selector=selector,
                provenance=Provenance(store=store_name, ref=run.run_id, digest=digest, note=note),
            )


def build(ctx: BankContext) -> Iterator[IssueItem]:
    return iter_items(ctx)
