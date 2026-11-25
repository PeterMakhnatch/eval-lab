from itertools import combinations

from features import amounts, features

CASE_COUNT = 8
REPAIR_COUNT = 5


def gather():
    status, rules = yield ("GET", "/rules", None)
    assert status == 200
    status, body = yield ("GET", "/cases", None)
    assert status == 200
    cases = body["cases"]
    assert len(cases) == CASE_COUNT
    questions = [{"to": rules["sites"][c["site"]]["holder"], "site": c["site"],
                  "month": c["month"]} for c in cases]
    replies = []
    for question in questions:
        status, body = yield ("POST", "/ask", question)
        assert status == 200
        replies.append(body["reply"])
    return rules, cases, questions, replies


def repair(field, question, reply):
    if field == "retry":
        status, body = yield ("POST", "/ask", question)
    elif reply is not None:
        status, body = yield ("POST", "/correct", {"reply": reply["id"], "fields": [field]})
    else:
        return reply
    assert status == 200
    return body["reply"]


def file_all(cases, replies):
    for case, reply in zip(cases, replies, strict=True):
        low, _ = amounts(reply)
        status, _ = yield ("POST", "/file", {"case": case["id"], "value": low or 0})
        assert status == 200


def prioritizer(field, key, reverse):
    def run(seat):
        rules, cases, questions, replies = yield from gather()
        rows = features(cases, rules, replies)
        eligible = list(range(CASE_COUNT))
        if field != "retry":
            eligible = [p for p in eligible if replies[p] is not None]
        selected = sorted(eligible, key=lambda p: (rows[p].get(key) is not None,
            rows[p].get(key) or 0), reverse=reverse)[:REPAIR_COUNT]
        for p in selected:
            replies[p] = yield from repair(field, questions[p], replies[p])
        yield from file_all(cases, replies)
    return run


def posterior(field, exclude_large=False):
    def run(seat):
        rules, cases, questions, replies = yield from gather()
        rows = features(cases, rules, replies)
        remaining = set(range(CASE_COUNT))
        candidates = [frozenset(ps) for ps in combinations(remaining, 3)
                      if len({rows[p]["holder"] for p in ps}) == 2]
        if exclude_large:
            candidates = [ps for ps in candidates if all(not rows[p].get("above_20k") for p in ps)]
        for _ in range(REPAIR_COUNT):
            if not candidates:
                break
            eligible = [p for p in sorted(remaining) if replies[p] is not None or field == "retry"]
            if not eligible:
                break
            p = max(eligible, key=lambda p: sum(p in ps for ps in candidates))
            old = amounts(replies[p])[0]
            replies[p] = yield from repair(field, questions[p], replies[p])
            changed = old != amounts(replies[p])[0]
            candidates = [ps for ps in candidates if (p in ps) == changed]
            remaining.remove(p)
        yield from file_all(cases, replies)
    return run


FIELD_BY_FACET = {"month": "month", "part": "parts", "range": "value", "silent": "retry"}
KEYS = ("digits", "first_digit", "last_digit", "digit_sum", "distinct_digits", "mod_factor",
        "mod_3", "mod_7", "peer_abslog", "peer_mod_gap", "peer_same_digits", "peer_both_small",
        "peer_gap", "peer_position", "holder_position")


def policies(facet):
    field = FIELD_BY_FACET[facet]
    rows = {f"{key}_{'desc' if reverse else 'asc'}": prioritizer(field, key, reverse)
            for key in KEYS for reverse in (False, True)}
    rows["posterior"] = posterior(field)
    if facet == "month":
        rows["posterior_exclude_large"] = posterior(field, exclude_large=True)
    return rows


month_factor_remainder = prioritizer("month", "mod_factor", True)
