"""Cyber task-ledger decisions: keep / exclude_train / discard per task.

Inputs are the vendored ``raimondasl/agentleak`` ID lists (see
``research/experiments/cyber-task-ledger/inputs/SOURCES.md``) joined against
the pinned ``FineEnvs/MiMo-V2.6-RL-harbor-cyber`` snapshot. All functions
here are pure over parsed structures; file IO lives in
``research/experiments/cyber-task-ledger/build.py``.

Precedence: suspect-discard > CyberGym/SEC-bench exclusion > duplicate-discard
> keep. A duplicate of an excluded task can never train (it is the same bug),
so twins of excluded tasks are ``discard``, not ``keep``.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Expected shapes of the pinned upstream inputs; a truncated or re-pinned
#: file fails the build instead of silently changing verdicts.
EXPECTED_OVERLAP_TASKS = 278
EXPECTED_SUSPECT_TASKS = 26
EXPECTED_DUP_PAIRS = 164
EXPECTED_SECBENCH_TASKS = 24
EXPECTED_RELATED_TASKS = 19

#: Crash-evidence values that corroborate a shared bug (agentleak
#: ``crash_check`` column). ``no_match`` and ``names_fuzzer_entry_point``
#: do not corroborate.
STRONG_CRASH = frozenset({"function_and_type", "function_only", "file_only"})


@dataclass(frozen=True)
class OverlapEntry:
    canonical_bug_id: str
    cybergym_ids: tuple[str, ...]
    match_type: str


@dataclass(frozen=True)
class DupPair:
    canonical_bug_id: str
    first: str
    second: str
    identical: bool


@dataclass(frozen=True)
class RelatedEntry:
    mimo_bug: str
    cybergym_task_id: str
    same_fix_commit: bool


@dataclass(frozen=True)
class SecbenchEntry:
    secbench_id: str
    split: str
    id_match: str
    same_fix: bool
    crash_check: str


@dataclass
class LedgerRow:
    task_id: str
    verdict: str = "keep"
    reason: str = ""
    evidence: str = ""
    split_group: str = ""
    project: str = ""
    dup_twin: str = ""
    variant_record: str = ""


def split_group_for(project: str) -> str:
    """Family key: ARVO project. Duplicate groups are project-pure, so twins
    never straddle a split drawn on this key."""
    return f"cyber:{project}"


def secbench_is_strong(rows: list[SecbenchEntry]) -> bool:
    """A SEC-bench overlap excludes from train when the crash evidence
    corroborates a shared bug AND the pair is linked by issue ID or fix
    commit. ``no_match`` crashes stay keep-with-flag."""
    for row in rows:
        if row.crash_check not in STRONG_CRASH:
            continue
        if row.same_fix or row.id_match.strip() in ("plain", "translated"):
            return True
    return False


def _overlap_reason(entry: OverlapEntry) -> str:
    gym = ",".join(entry.cybergym_ids)
    return (
        "exclude_train: shares CyberGym test-set bug "
        f"{entry.canonical_bug_id} ({entry.match_type} match to {gym}); "
        "train-on-test risk, eval-only"
    )


def _secbench_reason(rows: list[SecbenchEntry]) -> str:
    descs = []
    for row in sorted(rows, key=lambda r: r.secbench_id):
        link = "same fix commit" if row.same_fix else f"{row.id_match} issue ID"
        descs.append(f"{row.secbench_id} ({row.split}, {link}, {row.crash_check})")
    return (
        "exclude_train: shares SEC-bench bug " + "; ".join(descs) + "; "
        "train-on-test risk for SEC-bench, eval-only"
    )


def _dup_reason(twin: str, twin_verdict: str, identical: bool, canonical: str) -> str:
    prompt = "identical prompt" if identical else "differing prompt"
    if twin_verdict == "keep":
        return (
            f"discard: duplicate of {twin} ({prompt}, same bug {canonical}); "
            f"kept {twin}"
        )
    return (
        f"discard: duplicate of {twin} ({prompt}, same bug {canonical}); "
        f"twin is {twin_verdict}, so this copy cannot train either"
    )


def decide_verdicts(
    *,
    task_ids: list[str],
    projects: dict[str, str],
    instruction_sha: dict[str, str],
    overlap: dict[str, OverlapEntry],
    suspects: set[str],
    dup_pairs: list[DupPair],
    related: dict[str, list[RelatedEntry]],
    secbench: dict[str, list[SecbenchEntry]],
    clean: set[str],
    fix_binary: set[str],
) -> dict[str, LedgerRow]:
    """Decide one row per task. Raises ``ValueError`` on any invariant breach."""
    known = set(task_ids)
    _check_closed_world(known, overlap, suspects, dup_pairs, related, secbench)

    rows = {
        task: LedgerRow(
            task_id=task,
            project=projects[task],
            split_group=split_group_for(projects[task]),
        )
        for task in task_ids
    }
    twin_of: dict[str, str] = {}
    for pair in dup_pairs:
        twin_of[pair.first] = pair.second
        twin_of[pair.second] = pair.first
    for task, twin in twin_of.items():
        rows[task].dup_twin = twin

    def note(task: str, text: str) -> None:
        if text not in rows[task].reason:
            if rows[task].reason:
                rows[task].reason += "; " + text
            else:
                rows[task].reason = text

    # 1. Bogus targets.
    for task in sorted(suspects):
        rows[task].verdict = "discard"
        rows[task].reason = (
            "discard: bogus crash target (ABRT in LLVMFuzzerInitialize, "
            "a fuzzer entry point, not a real crash)"
        )
        rows[task].evidence = f"inputs/mimo_suspect_specs.csv:mimo_instance_id={task}"

    # 2. CyberGym test-set overlap.
    for task in sorted(overlap):
        entry = overlap[task]
        ev = f"inputs/overlap.csv:canonical_bug_id={entry.canonical_bug_id}"
        if rows[task].verdict == "discard":
            note(task, f"also shares CyberGym test-set bug {entry.canonical_bug_id}")
            rows[task].evidence += ";" + ev
        else:
            rows[task].verdict = "exclude_train"
            rows[task].reason = _overlap_reason(entry)
            rows[task].evidence = ev

    # 3. SEC-bench overlap with corroborating crash evidence.
    weak_secbench: set[str] = set()
    for task in sorted(secbench):
        entries = secbench[task]
        ev = ";".join(
            f"inputs/mimo_secbench.csv:secbench_instance_id={r.secbench_id}"
            for r in sorted(entries, key=lambda r: r.secbench_id)
        )
        if secbench_is_strong(entries):
            if rows[task].verdict == "keep":
                rows[task].verdict = "exclude_train"
                rows[task].reason = _secbench_reason(entries)
                rows[task].evidence = ev
            else:
                note(task, f"also shares SEC-bench bug ({ev})")
                rows[task].evidence += ";" + ev
        else:
            weak_secbench.add(task)

    # 4. Old/new-ID duplicate pairs: keep one, discard the other.
    # A suspect twin is bogus, not the same bug: with differing prompts the
    # surviving member names the real crash site and is kept (the 42494585
    # pair); with identical prompts it shares the bogus spec and goes too.
    kept_despite_twin: dict[str, str] = {}
    for pair in dup_pairs:
        members = (pair.first, pair.second)
        decided = [m for m in members if rows[m].verdict != "keep"]
        undecided = [m for m in members if rows[m].verdict == "keep"]
        ev = f"inputs/mimo_duplicates.csv:canonical_bug_id={pair.canonical_bug_id}"
        if not undecided:
            for m in members:
                rows[m].evidence += ";" + ev if rows[m].evidence else ev
        elif len(undecided) == 1:
            loser, twin = undecided[0], decided[0]
            if twin in suspects and not pair.identical:
                kept_despite_twin[loser] = twin
                rows[loser].evidence = ev
            else:
                rows[loser].verdict = "discard"
                rows[loser].reason = _dup_reason(
                    twin, rows[twin].verdict, pair.identical, pair.canonical_bug_id
                )
                rows[loser].evidence = ev
        else:
            in_clean = [m for m in members if m in clean]
            if len(in_clean) != 1:
                raise ValueError(
                    f"duplicate pair {pair.canonical_bug_id} has {len(in_clean)} "
                    "members in the clean list, want exactly 1"
                )
            keeper = in_clean[0]
            loser = pair.second if keeper == pair.first else pair.first
            rows[loser].verdict = "discard"
            rows[loser].reason = _dup_reason(
                keeper, "keep", pair.identical, pair.canonical_bug_id
            )
            rows[loser].evidence = ev
    # 5. Keep the rest, flagging related-bug and unverified SEC-bench links.
    for task in sorted(rows):
        row = rows[task]
        if row.verdict != "keep":
            continue
        flags: list[str] = []
        evs: list[str] = [f"inputs/mimo_cyber_clean_ids.txt:mimo_instance_id={task}"]
        if task in kept_despite_twin:
            twin = kept_despite_twin[task]
            flags.append(
                f"paired with bogus suspect spec {twin} under a differing prompt; "
                "this member names the real crash site"
            )
            evs.insert(0, row.evidence)
        if task in related:
            gym = ",".join(sorted({r.cybergym_task_id for r in related[task]}))
            flags.append(
                "related: same crash signature as CyberGym "
                f"{gym} with a different fix commit (related bug, not the same bug)"
            )
            bugs = ",".join(sorted({r.mimo_bug for r in related[task]}))
            evs.append(f"inputs/related_bugs.csv:mimo_bug={bugs}")
        if task in weak_secbench:
            ids = ",".join(sorted({r.secbench_id for r in secbench[task]}))
            flags.append(
                "secbench: same-fix claim unverified "
                f"(crash no_match in {ids}); kept with flag"
            )
            evs.extend(
                f"inputs/mimo_secbench.csv:secbench_instance_id={r.secbench_id}"
                for r in sorted(secbench[task], key=lambda r: r.secbench_id)
            )
        base = (
            "keep: no CyberGym/SEC-bench overlap, not a suspect spec, not a duplicate"
        )
        row.reason = base if not flags else base + "; " + "; ".join(flags)
        row.evidence = ";".join(evs)

    _check_invariants(
        rows, dup_pairs, instruction_sha, clean, secbench, fix_binary, suspects
    )
    return rows


def _check_closed_world(
    known: set[str],
    overlap: dict[str, OverlapEntry],
    suspects: set[str],
    dup_pairs: list[DupPair],
    related: dict[str, list[RelatedEntry]],
    secbench: dict[str, list[SecbenchEntry]],
) -> None:
    if len(overlap) != EXPECTED_OVERLAP_TASKS:
        raise ValueError(f"overlap tasks {len(overlap)}, want {EXPECTED_OVERLAP_TASKS}")
    if len(suspects) != EXPECTED_SUSPECT_TASKS:
        raise ValueError(f"suspects {len(suspects)}, want {EXPECTED_SUSPECT_TASKS}")
    if len(dup_pairs) != EXPECTED_DUP_PAIRS:
        raise ValueError(f"dup pairs {len(dup_pairs)}, want {EXPECTED_DUP_PAIRS}")
    if len(secbench) != EXPECTED_SECBENCH_TASKS:
        raise ValueError(f"secbench tasks {len(secbench)}, want {EXPECTED_SECBENCH_TASKS}")
    if len(related) != EXPECTED_RELATED_TASKS:
        raise ValueError(f"related tasks {len(related)}, want {EXPECTED_RELATED_TASKS}")
    referenced: set[str] = set(overlap) | set(suspects) | set(secbench) | set(related)
    seen: set[str] = set()
    for pair in dup_pairs:
        for member in (pair.first, pair.second):
            if member in seen:
                raise ValueError(f"duplicate pair member in two pairs: {member}")
            seen.add(member)
        referenced.update((pair.first, pair.second))
    unknown = referenced - known
    if unknown:
        raise ValueError(f"upstream IDs missing locally: {sorted(unknown)}")


def _check_invariants(
    rows: dict[str, LedgerRow],
    dup_pairs: list[DupPair],
    instruction_sha: dict[str, str],
    clean: set[str],
    secbench: dict[str, list[SecbenchEntry]],
    fix_binary: set[str],
    suspects: set[str],
) -> None:
    # Duplicate prompts: identical pairs share bytes, the odd pair differs,
    # and the odd pair never keeps its suspect member.
    for pair in dup_pairs:
        same = instruction_sha[pair.first] == instruction_sha[pair.second]
        if pair.identical and not same:
            raise ValueError(
                f"pair {pair.canonical_bug_id} listed identical but prompts differ"
            )
        if not pair.identical and same:
            raise ValueError(
                f"pair {pair.canonical_bug_id} listed non-identical but prompts match"
            )
        if rows[pair.first].split_group != rows[pair.second].split_group:
            raise ValueError(
                f"pair {pair.canonical_bug_id} straddles split groups: "
                f"{rows[pair.first].split_group} vs {rows[pair.second].split_group}"
            )
        if not pair.identical:
            for member in (pair.first, pair.second):
                if rows[member].verdict == "keep" and member in suspects:
                    raise ValueError(
                        f"non-identical pair {pair.canonical_bug_id} keeps "
                        f"suspect member {member}"
                    )
    # Keep set reproduces the upstream clean list minus corroborated
    # SEC-bench overlaps (the ledger's one deliberate delta).
    strong_excluded = {task for task in secbench if secbench_is_strong(secbench[task])}
    want_keep = clean - {t for t in strong_excluded if t in clean}
    got_keep = {t for t, r in rows.items() if r.verdict == "keep"}
    if got_keep != want_keep:
        raise ValueError(
            "keep set differs from clean-minus-secbench: "
            f"extra={sorted(got_keep - want_keep)} missing={sorted(want_keep - got_keep)}"
        )
    # No kept task ships a fixed build (all fix_binary tasks are excluded).
    kept_fix = got_keep & fix_binary
    if kept_fix:
        raise ValueError(f"kept tasks with fix_binary: {sorted(kept_fix)}")


__all__ = [
    "EXPECTED_DUP_PAIRS",
    "EXPECTED_OVERLAP_TASKS",
    "EXPECTED_RELATED_TASKS",
    "EXPECTED_SECBENCH_TASKS",
    "EXPECTED_SUSPECT_TASKS",
    "STRONG_CRASH",
    "DupPair",
    "LedgerRow",
    "OverlapEntry",
    "RelatedEntry",
    "SecbenchEntry",
    "decide_verdicts",
    "secbench_is_strong",
    "split_group_for",
]
