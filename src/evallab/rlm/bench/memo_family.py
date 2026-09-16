"""``memo-classify``: aggregation that needs semantic classification per record.

The first three families were solved by GLM-5.3-Flash with pure Python and zero
``llm_query`` calls: merchant *names* carry their category, chains are regexable,
state streams are replayable. This family removes the shortcut. Each expense
line carries only an amount and a free-text memo rendered from category-specific
templates with random slot fills (no merchant names, no category words, thousands
of distinct phrasings), so the only way to total a category is to classify every
memo, which at this scale means fanning memos out to sub-LM calls and aggregating
in code: the OOLONG-style workload the RLM harness exists for.

Ground truth is exact because the generator chose the category before rendering
the memo; the scorer keeps the suite's 0.5 % tolerance, and analysis can grade
partial credit from ``meta['ground_truth']``.
"""

from __future__ import annotations

import random

from evallab.rlm.bench.generators import BenchTask

SLOTS: dict[str, tuple[str, ...]] = {
    "grocery": (
        "eggs",
        "bread",
        "rice",
        "lentils",
        "olive oil",
        "yoghurt",
        "apples",
        "coffee beans",
        "flour",
        "chicken thighs",
        "cereal",
        "bananas",
        "salad greens",
        "frozen peas",
        "pasta",
        "tomato sauce",
        "oat milk",
        "cheddar",
    ),
    "litres": tuple(str(n) for n in range(18, 62)),
    "pump": tuple(str(n) for n in range(1, 13)),
    "road": (
        "the interstate",
        "the motorway services",
        "the bypass",
        "the coast road",
        "the ring road",
    ),
    "city": (
        "the client site",
        "the conference venue",
        "the regional office",
        "the offsite",
        "the trade fair",
        "the wedding",
    ),
    "nights": ("one night", "two nights", "three nights", "four nights"),
    "meal": ("lunch", "dinner", "brunch", "breakfast", "a late supper", "takeout"),
    "party": ("two", "three", "four", "six", "the whole team", "the in-laws", "the kids"),
    "dish": ("burritos", "ramen", "pizza", "tapas", "sushi", "burgers", "curry", "dumplings"),
    "service": (
        "electricity",
        "water and sewer",
        "gas heating",
        "home internet",
        "mobile plan",
        "trash pickup",
        "broadband",
        "district heating",
    ),
    "month": ("january", "february", "march", "april", "may", "june"),
    "tool": (
        "the design suite",
        "the code hosting plan",
        "the team workspace",
        "the password manager",
        "cloud storage",
        "the issue tracker",
        "the note-taking app",
        "video conferencing",
    ),
    "seats": (
        "one seat",
        "two seats",
        "five seats",
        "ten seats",
        "the family plan",
        "the pro tier",
    ),
}

CATEGORY_TEMPLATES: dict[str, tuple[str, ...]] = {
    "groceries": (
        "weekly run: {grocery}, {grocery} and {grocery}",
        "restocked the pantry with {grocery} and {grocery}",
        "{grocery}, {grocery} plus a bag of {grocery}",
        "big shop before the weekend: {grocery}, {grocery}, {grocery}",
        "picked up {grocery} and {grocery} on the way home",
    ),
    "fuel": (
        "topped up {litres} litres on {road}",
        "full tank at pump {pump} before the road trip",
        "{litres} litres of diesel for the van, pump {pump}",
        "refuelled on {road} after the airport run",
        "petrol, {litres} litres, {road}",
    ),
    "travel": (
        "economy ticket to {city}",
        "{nights} near {city}",
        "return train fare to {city}",
        "airport shuttle and checked bag on the way to {city}",
        "hotel deposit, {nights}, {city}",
    ),
    "dining": (
        "{meal} for {party}, {dish}",
        "{dish} delivered for movie night",
        "{meal} with {party} after the game",
        "quick {meal} between meetings, {dish}",
        "{dish} and drinks, split among {party}",
    ),
    "utilities": (
        "{month} {service} bill, autopay",
        "{service} statement for {month}",
        "{service}, higher than usual this {month}",
        "quarterly {service} bill",
        "{service} renewal, {month}",
    ),
    "software": (
        "{seats} for {tool}, yearly billing",
        "monthly renewal of {tool}",
        "{tool} upgrade, {seats}",
        "{seats} added to {tool} for the new hires",
        "annual license for {tool}",
    ),
}
CATEGORIES = tuple(CATEGORY_TEMPLATES)
NOISE = (
    "[INFO] expense-sync heartbeat ok",
    "# ---- expense export continues ----",
    "[DEBUG] receipts matched: 128",
    "[WARN] duplicate receipt id ignored",
)


def _render(rng: random.Random, template: str) -> str:
    out = template
    while "{" in out:
        start = out.index("{")
        end = out.index("}", start)
        slot = out[start + 1 : end]
        out = out[:start] + rng.choice(SLOTS[slot]) + out[end + 1 :]
    return out


def _records(rng: random.Random, n: int) -> list[dict[str, object]]:
    accounts = [f"ACC-{rng.randint(1000, 9999)}" for _ in range(4)]
    records = []
    for i in range(n):
        category = rng.choice(CATEGORIES)
        memo = _render(rng, rng.choice(CATEGORY_TEMPLATES[category]))
        if rng.random() < 0.3:
            memo = memo[0].upper() + memo[1:] + "."
        records.append(
            {
                "exp_id": f"EXP-{20000 + i}",
                "account": rng.choice(accounts),
                "amount": round(rng.uniform(4, 320), 2),
                "category": category,
                "memo": memo,
            }
        )
    return records


def make_memo_task(seed: int, index: int, context_chars: int) -> BenchTask:
    rng = random.Random(f"{seed}:memo-classify:{index}")
    n_records = max(40, int(context_chars / 82))
    records = _records(rng, n_records)
    lines = ["# expense export v2; columns: exp_id | account | amount | memo"]
    for record in records:
        lines.append(
            f"{record['exp_id']} | {record['account']} | {record['amount']:.2f} USD | {record['memo']}"
        )
        if rng.random() < 0.05:
            lines.append(rng.choice(NOISE))
    body = lines[1:]
    rng.shuffle(body)
    lines = [lines[0], *body]
    context = "\n".join(lines) + "\n"
    accounts = sorted({str(r["account"]) for r in records})
    category = rng.choice(CATEGORIES)
    kind = index % 2
    if kind == 0:
        account = rng.choice(accounts)
        total = sum(
            float(r["amount"])
            for r in records
            if r["account"] == account and r["category"] == category
        )
        query = (
            f"What is the total amount (USD, 2 decimals) spent by account {account} on '{category}'? Memos describe each "
            f"expense in plain language and never name the category; classify each memo into exactly one of "
            f"{', '.join(CATEGORIES)}."
        )
        answer = f"{total:.2f}"
        difficulty = 4
    else:
        count = sum(1 for r in records if r["category"] == category)
        query = (
            f"How many expense lines fall into the '{category}' category? Memos describe each expense in plain "
            f"language and never name the category; classify each memo into exactly one of {', '.join(CATEGORIES)}. "
            "Answer with an integer."
        )
        answer = str(count)
        difficulty = 3
    meta = {
        "ground_truth": {"records": records},
        "kind": ("account-category-total", "category-count")[kind],
        "category": category,
        "difficulty": difficulty,
        "n_records": n_records,
        "needs_semantic_classification": True,
    }
    return BenchTask(
        f"memo-classify-s{seed}-{index:02d}", "memo-classify", context, query, answer, meta
    )
