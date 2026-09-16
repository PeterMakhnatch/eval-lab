"""Deterministic synthetic long-context tasks for RLM harness development.

Three families, each generated from ``random.Random(seed)`` only, each with an
exact answer computed from the structured records that also produce the text:

- ``ledger-agg``: a noisy transaction log; queries aggregate by account, month,
  merchant, or *merchant category*. Categories are never written into the
  context (only merchant names are), so category queries need semantic
  classification (a sub-LM) while the arithmetic needs code.
- ``chain-lookup``: shuffled directory / assignment / approval / relocation
  records with distractors sharing names; queries need 2-4 hops and honour
  supersession (later memos override earlier ones).
- ``state-tracking``: an event stream of SET/DEL/RENAME/INC operations inside
  BEGIN/COMMIT/ROLLBACK blocks over an inventory; queries ask for a final
  value or a count under a predicate.

``meta['ground_truth']`` carries the structured payload so tests can recompute
the answer a second way.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

FAMILIES = ("ledger-agg", "chain-lookup", "state-tracking")

MERCHANTS: dict[str, tuple[str, ...]] = {
    "groceries": ("Trader Joe's", "Whole Foods Market", "Kroger", "Aldi", "Safeway"),
    "fuel": ("Shell", "Chevron", "ExxonMobil", "BP", "Sunoco"),
    "travel": ("Delta Air Lines", "United Airlines", "Marriott", "Hilton Hotels", "Amtrak"),
    "dining": ("Chipotle", "Olive Garden", "Starbucks", "Panera Bread", "Domino's Pizza"),
    "utilities": ("Con Edison", "PG&E", "Comcast Xfinity", "Verizon Wireless", "Duke Energy"),
    "software": ("Adobe Creative Cloud", "GitHub", "Notion Labs", "Slack Technologies", "Atlassian"),
}
MERCHANT_CATEGORY = {name: category for category, names in MERCHANTS.items() for name in names}
MONTHS = ("2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06")
MONTH_NAMES = {
    "2026-01": "January 2026",
    "2026-02": "February 2026",
    "2026-03": "March 2026",
    "2026-04": "April 2026",
    "2026-05": "May 2026",
    "2026-06": "June 2026",
}
NOISE_LINES = (
    "[INFO] ledger-sync heartbeat ok",
    "[WARN] retrying upstream connection (attempt 2)",
    "[DEBUG] cache warm: 1024 entries",
    "# ---- ledger export continues ----",
    "txn_id,account,merchant,amount,currency,date,memo",
    "[ERROR] malformed row skipped",
)
FIRST_NAMES = ("Ava", "Liam", "Noah", "Mia", "Ethan", "Zoe", "Lucas", "Isla", "Omar", "Priya", "Kenji", "Sofia", "Mateo", "Nora", "Ivan", "Leila")
LAST_NAMES = ("Patel", "Nguyen", "Okafor", "Silva", "Kowalski", "Haddad", "Fischer", "Tanaka", "Moreau", "Ivanova", "Castillo", "Banerjee")
OFFICES = ("Lisbon", "Austin", "Toronto", "Singapore", "Berlin", "Nairobi", "Denver", "Osaka")
DEPARTMENTS = ("Finance", "Platform", "Design", "Legal", "Growth", "Security", "Data")
PROJECTS = ("Aurora", "Beacon", "Cinder", "Delta-9", "Ember", "Fathom", "Granite", "Helix")


@dataclass(frozen=True)
class BenchTask:
    task_id: str
    family: str
    context: str
    query: str
    answer: str
    meta: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ledger-agg
# ---------------------------------------------------------------------------


def _fmt_date(rng: random.Random, month: str, day: int) -> str:
    year, mm = month.split("-")
    if rng.random() < 0.15:
        return f"{int(mm)}/{day}/{year}"  # US style, documented in the header
    return f"{month}-{day:02d}"


def _ledger_records(rng: random.Random, n: int) -> list[dict[str, Any]]:
    accounts = [f"ACC-{rng.randint(1000, 9999)}" for _ in range(6)]
    records = []
    for i in range(n):
        merchant = rng.choice(list(MERCHANT_CATEGORY))
        month = rng.choice(MONTHS)
        day = rng.randint(1, 28)
        amount = round(rng.uniform(2, 480), 2)
        if rng.random() < 0.07:
            amount = -round(rng.uniform(2, 120), 2)  # refund
        records.append(
            {
                "txn_id": f"TXN-{100000 + i}",
                "account": rng.choice(accounts),
                "merchant": merchant,
                "amount": amount,
                "month": month,
                "date": _fmt_date(rng, month, day),
                "memo": rng.choice(("pos purchase", "online order", "recurring", "refund", "auth hold released", "card-present")),
            }
        )
    return records


def _ledger_line(record: dict[str, Any]) -> str:
    return (
        f"{record['txn_id']} | {record['account']} | {record['merchant']} | "
        f"{record['amount']:.2f} USD | {record['date']} | memo:{record['memo']}"
    )


def _ledger_query(rng: random.Random, records: list[dict[str, Any]], kind: int) -> tuple[str, str, dict[str, Any]]:
    accounts = sorted({r["account"] for r in records})
    if kind == 0:
        account = rng.choice(accounts)
        month = rng.choice(MONTHS)
        total = sum(r["amount"] for r in records if r["account"] == account and r["month"] == month and r["amount"] > 0)
        return (
            f"What is the total spend (sum of positive amounts, USD, 2 decimals) for account {account} in {MONTH_NAMES[month]}? "
            "Dates appear as YYYY-MM-DD or M/D/YYYY.",
            f"{total:.2f}",
            {"kind": "account-month-total", "difficulty": 1},
        )
    if kind == 1:
        merchant = rng.choice(list(MERCHANT_CATEGORY))
        count = sum(1 for r in records if r["merchant"] == merchant and r["amount"] < 0)
        return (
            f"How many refunds (transactions with a negative amount) were recorded for the merchant {merchant}? Answer with an integer.",
            str(count),
            {"kind": "merchant-refund-count", "difficulty": 1},
        )
    if kind == 2:
        account = rng.choice(accounts)
        category = rng.choice(list(MERCHANTS))
        total = sum(
            r["amount"]
            for r in records
            if r["account"] == account and r["amount"] > 0 and MERCHANT_CATEGORY[r["merchant"]] == category
        )
        return (
            f"What is the total spend (sum of positive amounts, USD, 2 decimals) for account {account} at merchants in the "
            f"'{category}' category? The category is not written in the log; classify merchants by what kind of business they are "
            f"(categories in use: {', '.join(MERCHANTS)}).",
            f"{total:.2f}",
            {"kind": "account-category-total", "difficulty": 3, "category": category},
        )
    month = rng.choice(MONTHS)
    net: dict[str, float] = {}
    for r in records:
        if r["month"] == month:
            net[r["account"]] = net.get(r["account"], 0.0) + r["amount"]
    best = max(net, key=lambda a: (net[a], a))
    return (
        f"Which account had the largest net outflow (sum of all amounts, positive and negative) in {MONTH_NAMES[month]}? Answer with the account id.",
        best,
        {"kind": "max-net-account", "difficulty": 2},
    )


def make_ledger_task(seed: int, index: int, context_chars: int) -> BenchTask:
    rng = random.Random(f"{seed}:" + "ledger-agg" + f":{index}")
    line_len = 84  # observed mean rendered line length incl. noise and newline
    n_records = max(50, int(context_chars / line_len))
    records = _ledger_records(rng, n_records)
    lines = ["# ledger export v3; columns: txn_id | account | merchant | amount | date | memo; dates are YYYY-MM-DD or M/D/YYYY"]
    for record in records:
        lines.append(_ledger_line(record))
        if rng.random() < 0.06:
            lines.append(rng.choice(NOISE_LINES))
        if rng.random() < 0.02:
            lines.append(f"{record['txn_id']} | {record['account']} | {record['merchant']} | ERR | {record['date']} | memo:corrupt")
    rng.shuffle(lines[1:])
    context = "\n".join(lines) + "\n"
    kind = index % 4
    query, answer, extra = _ledger_query(rng, records, kind)
    meta = {"ground_truth": {"records": records}, **extra, "n_records": n_records}
    return BenchTask(f"ledger-agg-s{seed}-{index:02d}", "ledger-agg", context, query, answer, meta)


# ---------------------------------------------------------------------------
# chain-lookup
# ---------------------------------------------------------------------------


def _people(rng: random.Random, n: int) -> list[dict[str, Any]]:
    people = []
    used = set()
    while len(people) < n:
        name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        emp = f"E{rng.randint(10000, 99999)}"
        if emp in used:
            continue
        used.add(emp)
        people.append({"emp_id": emp, "name": name, "dept": rng.choice(DEPARTMENTS), "office": rng.choice(OFFICES)})
    for person in people:
        person["manager"] = rng.choice([p["emp_id"] for p in people if p is not person])
    return people


def make_chain_task(seed: int, index: int, context_chars: int) -> BenchTask:
    rng = random.Random(f"{seed}:" + "chain-lookup" + f":{index}")
    n_people = max(40, int(context_chars / 1800))
    people = _people(rng, n_people)
    by_id = {p["emp_id"]: p for p in people}
    invoices = []
    for i in range(n_people * 3):
        approver = rng.choice(people)
        invoices.append({"invoice": f"INV-{4000 + i}", "approver": approver["emp_id"], "amount": round(rng.uniform(100, 9000), 2), "project": rng.choice(PROJECTS)})
    relocations: dict[str, list[tuple[str, str]]] = {}
    for person in people:
        if rng.random() < 0.35:
            n_moves = rng.randint(1, 3)
            moves = []
            for k in range(n_moves):
                moves.append((f"2026-0{k + 1}-{rng.randint(10, 28)}", rng.choice(OFFICES)))
            relocations[person["emp_id"]] = moves
    lines: list[str] = []
    for p in people:
        lines.append(f"DIRECTORY: {p['name']} ({p['emp_id']}), department {p['dept']}, reports to {by_id[p['manager']]['name']} ({p['manager']}), home office {p['office']}.")
    for inv in invoices:
        lines.append(f"APPROVAL LOG: invoice {inv['invoice']} for project {inv['project']} (USD {inv['amount']:.2f}) approved by {by_id[inv['approver']]['name']} ({inv['approver']}).")
    for emp, moves in relocations.items():
        for date, office in moves:
            lines.append(f"RELOCATION MEMO dated {date}: {by_id[emp]['name']} ({emp}) is relocated to the {office} office effective immediately; earlier memos for {emp} are superseded.")
    # distractors: same names, different ids
    for _ in range(max(5, n_people // 4)):
        p = rng.choice(people)
        fake_id = f"E{rng.randint(10000, 99999)}"
        lines.append(f"DIRECTORY (archived, inactive): {p['name']} ({fake_id}), department {rng.choice(DEPARTMENTS)}, home office {rng.choice(OFFICES)}.")
    filler = ("PROJECT NOTE: {proj} sprint review moved to Thursday.", "FACILITIES: {off} office badge readers updated.", "PROJECT NOTE: {proj} budget line pending sign-off.")
    while sum(len(line) + 1 for line in lines) < context_chars:
        lines.append(rng.choice(filler).format(proj=rng.choice(PROJECTS), off=rng.choice(OFFICES)))
    rng.shuffle(lines)
    context = "\n".join(lines) + "\n"

    def current_office(emp: str) -> str:
        moves = relocations.get(emp)
        if not moves:
            return by_id[emp]["office"]
        return max(moves, key=lambda m: m[0])[1]

    kind = index % 3
    inv = rng.choice(invoices)
    approver = by_id[inv["approver"]]
    manager = by_id[approver["manager"]]
    if kind == 0:
        query = f"Which office is the manager of the approver of invoice {inv['invoice']} currently based in? Apply any relocation memos (latest dated memo wins). Answer with the office name only."
        answer = current_office(manager["emp_id"])
        hops = 3
    elif kind == 1:
        query = f"What is the employee id of the manager of the person who approved invoice {inv['invoice']}? Answer with the id only (format Exxxxx)."
        answer = manager["emp_id"]
        hops = 2
    else:
        grand = by_id[manager["manager"]]
        query = f"Which department does the manager of the manager of the approver of invoice {inv['invoice']} belong to? Use active DIRECTORY entries only (ignore archived ones). Answer with the department name only."
        answer = grand["dept"]
        hops = 4
    meta = {
        "ground_truth": {"people": people, "invoices": invoices, "relocations": relocations},
        "hops": hops,
        "difficulty": hops,
        "kind": ("office-of-manager", "manager-id", "grand-manager-dept")[kind],
    }
    return BenchTask(f"chain-lookup-s{seed}-{index:02d}", "chain-lookup", context, query, answer, meta)


# ---------------------------------------------------------------------------
# state-tracking
# ---------------------------------------------------------------------------


def make_state_task(seed: int, index: int, context_chars: int) -> BenchTask:
    rng = random.Random(f"{seed}:" + "state-tracking" + f":{index}")
    keys = [f"sku-{rng.randint(100, 999)}-{rng.choice('ABCDEFGH')}" for _ in range(60)]
    keys = sorted(set(keys))
    state: dict[str, int] = {}
    lines: list[str] = ["# inventory event stream; ops: SET k v | INC k d | DEL k | RENAME a b | BEGIN | COMMIT | ROLLBACK"]
    ops = 0
    in_txn = False
    snapshot: dict[str, int] | None = None
    while sum(len(line) + 1 for line in lines) < context_chars:
        r = rng.random()
        if not in_txn and r < 0.08:
            lines.append("BEGIN")
            in_txn = True
            snapshot = dict(state)
            continue
        if in_txn and r < 0.10:
            if rng.random() < 0.5:
                lines.append("COMMIT")
            else:
                lines.append("ROLLBACK")
                assert snapshot is not None
                state = dict(snapshot)
            in_txn = False
            snapshot = None
            continue
        op = rng.choices(("SET", "INC", "DEL", "RENAME"), weights=(5, 6, 2, 1))[0]
        ops += 1
        if op == "SET":
            k = rng.choice(keys)
            v = rng.randint(0, 500)
            state[k] = v
            lines.append(f"SET {k} {v}")
        elif op == "INC":
            k = rng.choice(keys)
            d = rng.randint(-40, 60)
            state[k] = state.get(k, 0) + d
            lines.append(f"INC {k} {d:+d}")
        elif op == "DEL":
            k = rng.choice(keys)
            state.pop(k, None)
            lines.append(f"DEL {k}")
        else:
            a, b = rng.sample(keys, 2)
            if a in state:
                state[b] = state.pop(a)
            lines.append(f"RENAME {a} {b}")
        if rng.random() < 0.03:
            lines.append(rng.choice(("[INFO] compaction ok", "# checkpoint", "[WARN] replica lag 2s")))
    if in_txn:
        lines.append("ROLLBACK")
        assert snapshot is not None
        state = dict(snapshot)
    context = "\n".join(lines) + "\n"
    kind = index % 2
    if kind == 0 and state:
        k = rng.choice(sorted(state))
        query = f"After applying the entire event stream (ROLLBACK discards every op since the matching BEGIN; INC on a missing key starts from 0; RENAME moves the value if the source exists), what is the final value of key {k}? Answer with an integer."
        answer = str(state[k])
    else:
        threshold = rng.randint(100, 400)
        query = f"After applying the entire event stream (ROLLBACK discards every op since the matching BEGIN; INC on a missing key starts from 0; RENAME moves the value if the source exists), how many keys have a final value strictly greater than {threshold}? Answer with an integer."
        answer = str(sum(1 for v in state.values() if v > threshold))
    meta = {"ground_truth": {"final_state": state}, "ops": ops, "difficulty": 2 if kind == 0 else 3, "kind": ("final-value", "count-over-threshold")[kind]}
    return BenchTask(f"state-tracking-s{seed}-{index:02d}", "state-tracking", context, query, answer, meta)


def generate_suite(seed: int, n_per_family: int, context_chars: int = 200_000) -> list[BenchTask]:
    tasks: list[BenchTask] = []
    for family, maker in (("ledger-agg", make_ledger_task), ("chain-lookup", make_chain_task), ("state-tracking", make_state_task)):
        for index in range(n_per_family):
            tasks.append(maker(seed, index, context_chars))
    return tasks
