import importlib.util
import random
import sys
from collections import Counter
from copy import deepcopy
from functools import cache
from itertools import combinations
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from muster.lab.load import module  # noqa: E402
from muster.tasks.inquiry.zoo import parse_reply  # noqa: E402

SEED = "inquiry-v2.5"
FACETS = ("plain", "month", "part", "range", "silent", "mixed")
ARMS = ("probe", "fields", "flagged", "solo")
COUNT = 10
QUESTIONS = 13
CASES = 8
SITES = ("north", "south", "east", "west")
HOLDERS = ("p2", "p3")
MAX_BASE_TRIES = 20_000
MAX_MASK_TRIES = 20_000
MAX_BOOK_COHORT_TRIES = 2_000_000
BOOK_CANDIDATES = 128
ROOT = Path(__file__).resolve().parent
KIT = ROOT.parent / "_kit"
SPEC = importlib.util.spec_from_file_location("inquiry_build", KIT / "build.py")
assert SPEC is not None and SPEC.loader is not None
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)
WORLD = module(ROOT / "template/world.py", "inquiry_instance_world", siblings=(KIT,))


def conditions():
    return [f"{facet}-{arm}" for facet in FACETS for arm in ARMS]


def rng(*keys):
    return random.Random("/".join(map(str, (SEED, *keys))))


def subsets(parts):
    return [list(subset) for size in range(1, len(parts)) for subset in combinations(parts, size)]


def split_total(randomizer, total, parts, selected, selected_value, factor):
    assert total > selected_value >= factor
    assert total % factor == selected_value % factor == 0
    others = [part for part in parts if part != selected]
    remaining = (total - selected_value) // factor
    assert remaining >= len(others)
    cuts = [0, *sorted(randomizer.sample(range(1, remaining), len(others) - 1)), remaining]
    shares = {part: (right - left) * factor for part, left, right in zip(
        others, cuts[:-1], cuts[1:], strict=True)}
    shares[selected] = selected_value
    assert sum(shares.values()) == total and min(shares.values()) > 0
    return shares


def total(data, site, month, parts=None):
    scope = {"site": site, "month": month, "parts": parts if parts is not None else data["sites"][site]["parts"]}
    return WORLD.kilogram_total(data, scope)


def wrong_figures(data, case):
    site, month = case["site"], case["month"]
    correct = total(data, site, month)
    parts = data["sites"][site]["parts"]
    a, b = data["range_offsets"][site][month - 1]
    return {0, correct // data["kg_per_crate"],
            *(total(data, site, other) for other in WORLD.MONTHS if other != month),
            correct - a, correct + b, correct + (b - a) // 2,
            *(total(data, site, m, subset) for m in WORLD.MONTHS for subset in subsets(parts))}


@cache
def layout(index):
    randomizer = rng("layout", index)
    holder_order = randomizer.sample(SITES, len(SITES))
    sites, cases, off_months = {}, [], {}
    for site in SITES:
        parts = list("ABC"[:randomizer.choice((2, 3))])
        sites[site] = {"parts": parts, "holder": HOLDERS[holder_order.index(site) // 2]}
        months = randomizer.sample(WORLD.MONTHS, 2)
        other = randomizer.sample([m for m in WORLD.MONTHS if m not in months], 2)
        cases.extend({"site": site, "month": month, "off_month": off}
                     for month, off in zip(months, other, strict=True))
    randomizer.shuffle(cases)
    cases = [{"id": f"c{position}", **case} for position, case in enumerate(cases, 1)]
    for case in cases:
        off_months[case["id"]] = case.pop("off_month")
    partial_parts = {case["id"]: randomizer.choice(sites[case["site"]]["parts"]) for case in cases}
    return {"id": f"i{index:02d}", "sites": sites, "cases": cases, "off_months": off_months,
            "partial_parts": partial_parts, "kg_per_crate": randomizer.choice((4, 5, 8)),
            "question_budget": QUESTIONS, "file_budget": CASES, "real": ["p1"],
            "tokens": {"p1": BUILD.token(SEED, index, "p1")}}


def grid(factor, low, high):
    points = range(((low + factor - 1) // factor) * factor, high + 1, factor)
    assert len(points) > 0
    return points


def apparent_amount(randomizer, factor):
    return randomizer.choice(grid(factor, 500, 20_000))


@cache
def rank_strata(kind):
    ranks = list(range(5)) * 2
    rng("rank", kind).shuffle(ranks)
    return ranks


def mixed_amounts(randomizer, index, mask, factor):
    for _ in range(MAX_BASE_TRIES):
        amounts = [apparent_amount(randomizer, factor) for _ in mask]
        if len(set(amounts)) != CASES:
            continue
        for kind in ("crate", "chain"):
            eligible = sorted(amounts[p] for p, label in enumerate(mask) if label in ("plain", kind))
            if eligible.index(amounts[mask.index(kind)]) != rank_strata(kind)[index - 1]:
                break
        else:
            return amounts
    raise AssertionError("no stratified mixed amounts")


def case_totals(randomizer, data, index):
    mask = fault_masks("mixed")[index - 1]
    factor = data["kg_per_crate"]
    amounts = mixed_amounts(randomizer, index, mask, factor)
    totals, selected = {}, {}
    for case, kind, amount in zip(data["cases"], mask, amounts, strict=True):
        site, month, cid = case["site"], case["month"], case["id"]
        a, _ = data["range_offsets"][site][month - 1]
        if kind == "crate":
            value = amount * factor
        elif kind == "chain":
            value = apparent_amount(randomizer, factor * factor)
            totals[site, data["off_months"][cid]] = amount
        elif kind == "range":
            value = amount + a
        elif kind == "part":
            remaining_parts = len(data["sites"][site]["parts"]) - 1
            value = amount + randomizer.choice(grid(factor, factor * remaining_parts, amount // 5))
            selected[site, month] = amount
        else:
            value = amount
        totals[site, month] = value
    return totals, selected


def candidate(randomizer, index):
    data = layout(index).copy()
    factor = data["kg_per_crate"]
    offsets = {site: [randomizer.sample(grid(factor, 10, 90), 2) for _ in WORLD.MONTHS]
               for site in SITES}
    data["range_offsets"] = offsets
    totals, selected = case_totals(randomizer, data, index)
    books = {}
    for site in SITES:
        parts = data["sites"][site]["parts"]
        book = {part: [] for part in parts}
        for month in WORLD.MONTHS:
            value = totals.get((site, month), apparent_amount(randomizer, factor))
            case = next((c for c in data["cases"] if c["site"] == site and c["month"] == month), None)
            part = data["partial_parts"][case["id"]] if case else randomizer.choice(parts)
            share = selected.get((site, month))
            if share is None:
                low = value // 10 if value > 20_000 else value * 7 // 10
                share = randomizer.choice(grid(factor, low, value - factor * (len(parts) - 1)))
            for name, number in split_total(randomizer, value, parts, part, share, factor).items():
                book[name].append(number)
        books[site] = book
    return {**data, "books": books}


def book_candidates(index):
    randomizer = rng("base", index)
    rows = []
    for _ in range(MAX_BASE_TRIES):
        data = candidate(randomizer, index)
        signature = part_rank_signature(data, index)
        if signature is None:
            continue
        totals = [total(data, case["site"], case["month"]) for case in data["cases"]]
        if len(set(totals)) != CASES:
            continue
        if any(value in wrong_figures(data, case) for value, case in zip(totals, data["cases"], strict=True)):
            continue
        rows.append((data, signature))
        if len(rows) == BOOK_CANDIDATES:
            return rows
    raise AssertionError(f"no valid book pool for {index}")


def square_balance(rows):
    assert len(rows) == COUNT
    for facet in ("month", "part", "range", "mixed"):
        counts = Counter()
        for index, (data, _) in enumerate(rows, 1):
            faults = dict(zip((case["id"] for case in data["cases"]),
                              fault_masks(facet)[index - 1], strict=True))
            view = {**data, "faults": faults}
            factor = data["kg_per_crate"]
            for case in data["cases"]:
                question = WORLD.case_question(view, case)
                scope = WORLD.first_scope(view, question)
                assert scope is not None
                figure = WORLD.figure_for(view, scope)
                amount = figure if scope["exact"] else figure["low"]
                key = "off" if faults[case["id"]] != "plain" else "exact"
                counts[key] += 1
                counts[key, "square"] += amount % (factor * factor) == 0
        off, exact = counts["off"], counts["exact"]
        assert off + exact == COUNT * CASES
        difference = abs(counts["off", "square"] * exact - counts["exact", "square"] * off)
        if 100 * difference > 15 * off * exact:
            return False
    return True


@cache
def base_rows():
    randomizer = rng("books-cohort")
    pools = [book_candidates(index) for index in range(1, COUNT + 1)]
    for _ in range(MAX_BOOK_COHORT_TRIES):
        rows = [randomizer.choice(pool) for pool in pools]
        signatures = [signature for _, signature in rows]
        ranks = Counter(rank for ranked, _ in signatures for rank in ranked)
        if not all(ranks[rank] in (3, 4) for rank in range(CASES)):
            continue
        if not 14 <= sum(below for _, below in signatures) <= 16:
            continue
        if not square_balance(rows):
            continue
        for index, (data, signature) in enumerate(rows, 1):
            validate_base(data)
            assert part_rank_signature(data, index) == signature
        return tuple(data for data, _ in rows)
    raise AssertionError("no balanced book cohort")


def base(index):
    assert 1 <= index <= COUNT
    return base_rows()[index - 1]


def validate_base(data):
    assert set(data["sites"]) == set(SITES) == set(data["books"])
    assert Counter(row["holder"] for row in data["sites"].values()) == dict.fromkeys(HOLDERS, 2)
    assert data["kg_per_crate"] in (4, 5, 8)
    assert len(data["cases"]) == CASES == len({case["id"] for case in data["cases"]})
    assert Counter(case["site"] for case in data["cases"]) == dict.fromkeys(SITES, 2)
    totals = set()
    for site, row in data["sites"].items():
        parts = row["parts"]
        assert len(parts) in (2, 3) and len(set(parts)) == len(parts)
        assert set(parts) == set(data["books"][site])
        for values in data["books"][site].values():
            assert len(values) == len(WORLD.MONTHS)
            assert all(0 < value <= 160_000 and value % data["kg_per_crate"] == 0
                       for value in values)
        cases = [case for case in data["cases"] if case["site"] == site]
        months = {case["month"] for case in cases}
        off_months = {data["off_months"][case["id"]] for case in cases}
        assert len(months) == len(off_months) == 2
        assert months | off_months <= set(WORLD.MONTHS) and not months & off_months
        for month in WORLD.MONTHS:
            a, b = data["range_offsets"][site][month - 1]
            current = total(data, site, month)
            assert 10 <= a <= 90 and 10 <= b <= 90 and a != b
            assert a % data["kg_per_crate"] == b % data["kg_per_crate"] == 0
            assert 0 < current - a < current < current + b
        for case in cases:
            value = total(data, site, case["month"])
            assert value not in totals and value not in wrong_figures(data, case)
            totals.add(value)
    assert len(totals) == CASES


def has_both_holders(index, positions):
    data = layout(index)
    return {data["sites"][data["cases"][position]["site"]]["holder"] for position in positions} == set(HOLDERS)

def mixed_masks(randomizer):
    kinds = ("part", "chain", "range", "crate")
    for _ in range(MAX_MASK_TRIES):
        rows = [None] * COUNT
        indices = randomizer.sample(range(COUNT), COUNT)
        for left, right in zip(indices[::2], indices[1::2], strict=True):
            positions = randomizer.sample(range(CASES), CASES)
            for index, chosen in ((left, positions[:4]), (right, positions[4:])):
                row = ["plain"] * CASES
                for p, kind in zip(chosen, randomizer.sample(kinds, len(kinds)), strict=True):
                    row[p] = kind
                rows[index] = tuple(row)
        if not all(has_both_holders(index, [p for p, kind in enumerate(row) if kind != "plain"])
                   for index, row in enumerate(rows, 1)):
            continue
        if not all(sum(row[p] == kind for row in rows) <= 3
                   for p in range(CASES) for kind in kinds):
            continue
        assert all(sum(row[p] != "plain" for row in rows) == 5 for p in range(CASES))
        return tuple(rows)
    raise AssertionError("no balanced mixed masks")



@cache
def fault_masks(facet):
    if facet == "plain":
        return tuple(("plain",) * CASES for _ in range(COUNT))
    randomizer = rng("faults", facet)
    if facet == "mixed":
        return mixed_masks(randomizer)
    labels = [facet] * 3 + ["plain"] * 5
    for _ in range(MAX_MASK_TRIES):
        rows = []
        for index in range(1, COUNT + 1):
            for _ in range(MAX_MASK_TRIES):
                row = tuple(randomizer.sample(labels, len(labels)))
                if has_both_holders(index, [p for p, label in enumerate(row) if label != "plain"]):
                    rows.append(row)
                    break
            else:
                raise AssertionError("no mask with both holders")
        balance = [sum(row[position] != "plain" for row in rows) for position in range(CASES)]
        if all(count in (3, 4) for count in balance):
            assert all(Counter(row) == Counter(labels) for row in rows)
            return tuple(rows)
    raise AssertionError(f"no balanced masks for {facet}")


def part_signature(data, mask, order):
    ranks = tuple(rank for rank, p in enumerate(order) if mask[p] == "part")
    seen = set()
    below_peer = 0
    for p in order:
        site = data["cases"][p]["site"]
        if site not in seen and mask[p] == "part":
            below_peer += 1
        seen.add(site)
    return ranks, below_peer




def part_rank_signature(data, index):
    mask = fault_masks("part")[index - 1]
    amounts = []
    for case, kind in zip(data["cases"], mask, strict=True):
        site, month = case["site"], case["month"]
        parts = [data["partial_parts"][case["id"]]] if kind == "part" else None
        amounts.append(total(data, site, month, parts))
    if len(set(amounts)) != CASES:
        return None
    order = sorted(range(CASES), key=amounts.__getitem__)
    return part_signature(data, mask, order)


def validate_scope(data, scope):
    figure = WORLD.figure_for(data, scope)
    amounts = (figure,) if scope["exact"] else (figure["low"], figure["high"])
    assert all(amount % data["kg_per_crate"] == 0 for amount in amounts)
    expected = WORLD.public_scope(scope, figure)
    assert parse_reply(expected) == expected
    for form in range(WORLD.FORMS):
        assert parse_reply({"text": WORLD.render(scope, figure, form)}) == expected


def validate_correction(data, question, scope, fields, *, chain_pending):
    corrected = WORLD.corrected_scope(data, question, scope, fields, chain_pending=chain_pending)
    expected = set(WORLD.differences(data, question, scope)) - set(fields)
    cid, kind = WORLD.case_kind(data, question)
    if chain_pending and kind == "chain" and "month" in fields:
        extra = next((field for field in data["chain_order"] if field not in fields), None)
        if extra is not None:
            expected.add(extra)
            validate_chain_figure(data, question, cid, corrected, extra)
    assert set(WORLD.differences(data, question, corrected)) == expected
    validate_scope(data, corrected)
    return corrected


def validate_chain_figure(data, question, cid, scope, extra):
    exact = WORLD.kilogram_total(data, WORLD.question_scope(data, question))
    figure = WORLD.figure_for(data, scope)
    if extra == "parts":
        assert scope["parts"] == [data["partial_parts"][cid]]
        assert 0 < figure < exact
    elif extra == "value":
        a, b = data["range_offsets"][question["site"]][question["month"] - 1]
        low, high = figure["low"], figure["high"]
        assert 10 <= a <= 90 and 10 <= b <= 90 and a != b
        assert low == exact - a and high == exact + b
        assert 0 < low < exact < high and low + high != 2 * exact
        assert (low + high) // 2 != exact
    else:
        assert extra == "unit" and scope["unit"] == "crates"
        assert figure * data["kg_per_crate"] == exact and figure != exact


@cache
def parser_check(facet, index):
    data = facet_data(facet, index)
    for site, row in data["sites"].items():
        for month in WORLD.MONTHS:
            question = {"to": row["holder"], "site": site, "month": month}
            first = WORLD.first_scope(data, question)
            exact = WORLD.question_scope(data, question)
            validate_scope(data, exact)
            if first is not None:
                validate_scope(data, first)
                for fields in correction_sets():
                    corrected = validate_correction(data, question, first, fields, chain_pending=True)
                    if corrected != first or WORLD.case_kind(data, question)[1] == "chain":
                        for next_fields in correction_sets():
                            validate_correction(data, question, corrected, next_fields,
                                                chain_pending="month" not in fields)
    assert parse_reply(None) is None
    return True


@cache
def correction_sets():
    return tuple(fields for size in range(1, len(WORLD.FIELDS) + 1)
                 for fields in combinations(WORLD.FIELDS, size))


@cache
def chain_order(index):
    first = ["parts"] * 3 + ["value"] * 3 + ["unit"] * 4
    rng("chain-first").shuffle(first)
    field = first[index - 1]
    others = [name for name in WORLD.FIELDS[1:] if name != field]
    return [field, *rng("chain-rest", index).sample(others, len(others))]


def validate_first_figures(data):
    amounts = {}
    for case in data["cases"]:
        question = WORLD.case_question(data, case)
        scope = WORLD.first_scope(data, question)
        if scope is None:
            continue
        figure = WORLD.figure_for(data, scope)
        amount = figure if scope["exact"] else figure["low"]
        true = total(data, case["site"], case["month"])
        assert (amount == true) == (not WORLD.differences(data, question, scope))
        amounts[case["id"]] = amount
        assert amount % data["kg_per_crate"] == 0
        if not scope["exact"]:
            assert figure["high"] % data["kg_per_crate"] == 0
    if data["facet"] == "mixed":
        assert len(set(amounts.values())) == CASES
        assert all(500 <= amount <= 20_000 for amount in amounts.values())
        index = int(data["id"][1:])
        for kind in ("crate", "chain"):
            eligible = sorted(amounts[cid] for cid, label in data["faults"].items()
                              if label in (kind, "plain"))
            cid = next(cid for cid, label in data["faults"].items() if label == kind)
            assert eligible.index(amounts[cid]) == rank_strata(kind)[index - 1]
        assert sorted(data["chain_order"]) == sorted(WORLD.FIELDS[1:])


@cache
def facet_data(facet, index):
    data = base(index)
    mask = fault_masks(facet)[index - 1]
    faults = {case["id"]: kind for case, kind in zip(data["cases"], mask, strict=True)}
    assert facet == "plain" or has_both_holders(index, [p for p, kind in enumerate(mask) if kind != "plain"])
    assert sum(kind != "plain" for kind in mask) == (0 if facet == "plain" else 4 if facet == "mixed" else 3)
    result = {**data, "faults": faults, "facet": facet, "chain_order": chain_order(index),
              "form_seed": rng("forms", facet, index).getrandbits(64)}
    validate_first_figures(result)
    return result


@cache
def form_balance():
    counts = {"off": [0] * WORLD.FORMS, "exact": [0] * WORLD.FORMS}
    for facet in FACETS:
        for index in range(1, COUNT + 1):
            data = facet_data(facet, index)
            for case in data["cases"]:
                question = WORLD.case_question(data, case)
                scope = WORLD.first_scope(data, question)
                if scope is None:
                    scope = WORLD.question_scope(data, question)
                for step in range(3):
                    differs = WORLD.differences(data, question, scope)
                    form = WORLD.form_for(data, question, scope)
                    counts["off" if differs else "exact"][form] += 1
                    fields = [field for field in differs if field != "unit"]
                    if not fields:
                        break
                    scope = WORLD.corrected_scope(data, question, scope, fields, chain_pending=step == 0)
                else:
                    raise AssertionError("correction chain exceeds 2 repairs")
    totals = {key: sum(values) for key, values in counts.items()}
    for off, exact in zip(counts["off"], counts["exact"], strict=True):
        assert off > 0 and exact > 0
        assert abs(off / totals["off"] - exact / totals["exact"]) <= 0.1, counts
    return counts


def instance(facet, arm, index):
    assert facet in FACETS and arm in ARMS and 1 <= index <= COUNT
    assert parser_check(facet, index)
    return {**deepcopy(facet_data(facet, index)), "condition": f"{facet}-{arm}", "facet": facet,
            "arm": arm, "holders": [] if arm == "solo" else list(HOLDERS)}


def instances(condition):
    assert condition in conditions()
    assert form_balance()
    facet, arm = condition.split("-")
    result = []
    for index in range(1, COUNT + 1):
        peers = [instance(other, variant, index) for other in FACETS for variant in ARMS]
        shared = ("sites", "books", "cases", "range_offsets", "kg_per_crate", "question_budget", "file_budget", "tokens")
        assert all(all(row[key] == peers[0][key] for key in shared) for row in peers)
        arms = [row for row in peers if row["facet"] == facet]
        assert all(all(row[key] == arms[0][key] for key in ("faults", "partial_parts", "form_seed")) for row in arms)
        result.append(arms[ARMS.index(arm)])
    return result
