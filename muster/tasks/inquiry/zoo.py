import re
from functools import partial

FIELDS = ("month", "parts", "value", "unit")
MAX_CASES = 8
MAX_CORRECTIONS = 4
SITE = r"(?P<site>[A-Z][a-z]+)"
PARTS = r"(?P<parts>[A-Z](?: and [A-Z]){0,2})"
MONTH = r"(?P<month>[1-6])"
AMOUNT = r"(?P<amount>\d+|between \d+ and \d+)"
UNIT = r"(?P<unit>kg|crates)"
PATTERNS = tuple(re.compile(pattern) for pattern in (
    rf"At {SITE}, parts? {PARTS}(?: together)? held {AMOUNT} {UNIT} at the end of month {MONTH}\.",
    rf"The end-of-month {MONTH} stock of parts? {PARTS} at {SITE} was {AMOUNT} {UNIT}\.",
    rf"{SITE}, parts? {PARTS}, end of month {MONTH}: {AMOUNT} {UNIT}\.",
    rf"End of month {MONTH}, {SITE} \(parts? {PARTS}\): {AMOUNT} {UNIT}\.",
))
SKILLS = {
    "truster": [], "persistent": ["retry"], "matcher": ["retry", "match"],
    "coverer": ["retry", "cover"], "exacter": ["retry", "exact"],
    "converter": ["retry", "convert"], "oracle": ["retry", "match", "cover", "exact", "convert", "recheck"],
}
CONVERT = {"converter", "oracle", "wide", "no_recheck", "namer", "midpoint"}
FIXED_FIELDS = {"fix_month": "month", "fix_parts": "parts", "fix_value": "value"}
FIXED_CASES = 5


def parse_reply(reply):
    if reply is None:
        return None
    if "text" not in reply:
        keys = ("site", "parts", "month", "unit", "value")
        result = {key: reply[key] for key in keys}
        if result["value"] is None:
            result.update(low=reply["low"], high=reply["high"])
        return result
    matches = [match for pattern in PATTERNS if (match := pattern.fullmatch(reply["text"]))]
    assert len(matches) == 1, reply
    fields = matches[0].groupdict()
    numbers = [int(value) for value in fields["amount"].removeprefix("between ").split(" and ")]
    scope = {"site": fields["site"].lower(), "parts": fields["parts"].split(" and "),
             "month": int(fields["month"]), "unit": fields["unit"], "value": numbers[0]}
    if len(numbers) == 2:
        scope.update(value=None, low=numbers[0], high=numbers[1])
    return scope


def get(path):
    status, response = yield "GET", path, None
    assert status == 200, (status, response)
    return response


def setup():
    rules = yield from get("/rules")
    cases = (yield from get("/cases"))["cases"]
    assert len(cases) == MAX_CASES
    return rules, cases


def question(rules, case):
    return {"site": case["site"], "month": case["month"], "to": rules["sites"][case["site"]]["holder"]}


def missing_fields(scope, case, parts):
    if scope is None:
        return ["reply"]
    assert scope["site"] == case["site"]
    return [field for field, wrong in zip(FIELDS, (scope["month"] != case["month"],
            set(scope["parts"]) != set(parts), scope["value"] is None, scope["unit"] != "kg"), strict=True)
            if wrong]


def filed_value(scope, mode, rules):
    if scope is None:
        return 0
    value = scope["value"]
    if value is None:
        value = (scope["low"] + scope["high"]) // 2 if mode == "midpoint" else scope["low"]
    return value * rules["kg_per_crate"] if mode in CONVERT and scope["unit"] == "crates" else value


def ask(body):
    status, result = yield "POST", "/ask", body
    return result if status == 200 else None


def correct(reply, fields):
    status, result = yield "POST", "/correct", {"reply": reply["id"], "fields": fields}
    return result if status == 200 else None


def wanted(mode, response, case, parts):
    if mode == "reactive":
        return response.get("check", {}).get("differs_in", [])
    if mode == "truster":
        return []
    differs = missing_fields(parse_reply(response["reply"]), case, parts)
    if differs == ["reply"] or mode == "repeater":
        return differs
    if mode == "fix_all":
        return list(FIELDS) if differs else []
    if mode in ("oracle", "wide", "no_recheck", "namer", "midpoint"):
        if mode in ("wide", "namer") and "month" in differs and "parts" not in differs:
            differs.append("parts")
        return [field for field in differs if field != "unit" and not (mode == "midpoint" and field == "value")]
    field = {"matcher": "month", "coverer": "parts", "exacter": "value"}.get(mode)
    return [field] if field in differs else []


def resolve(mode, response, rules, case):
    if response is None:
        return 0
    body = question(rules, case)
    parts = rules["sites"][case["site"]]["parts"]
    scope = parse_reply(response["reply"])
    for _ in range(MAX_CORRECTIONS):
        fields = wanted(mode, response, case, parts)
        if not fields:
            break
        if mode == "repeater" or fields == ["reply"]:
            updated = yield from ask(body)
        else:
            updated = yield from correct(response["reply"], fields)
        if updated is None:
            break
        response = updated
        scope = parse_reply(response["reply"])
        if mode == "repeater":
            break
        if mode in ("no_recheck", "namer") and fields != ["reply"]:
            return filed_value(scope, "truster", rules)
    return filed_value(scope, mode, rules)


def clerk(seat, *, mode):
    rules, cases = yield from setup()
    if rules["records"]:
        records = (yield from get("/records"))["records"]
        assert len(records) == len(cases)
        for record in records:
            value = filed_value(parse_reply(record), mode, rules)
            yield "POST", "/file", {"case": record["case"], "value": value}
        return
    if mode in FIXED_FIELDS:
        yield from fixed_clerk(mode, rules, cases)
        return
    for case in cases:
        response = yield from ask(question(rules, case))
        value = yield from resolve(mode, response, rules, case)
        yield "POST", "/file", {"case": case["id"], "value": value}


def fixed_clerk(mode, rules, cases):
    responses = [None] * len(cases)
    for position, case in enumerate(cases):
        responses[position] = yield from ask(question(rules, case))
    for position, (case, response) in enumerate(zip(cases, responses, strict=True)):
        reply = response["reply"] if response is not None else None
        if position < FIXED_CASES and reply is not None:
            updated = yield from correct(reply, [FIXED_FIELDS[mode]])
            if updated is not None:
                reply = updated["reply"]
        value = filed_value(parse_reply(reply), mode, rules)
        yield "POST", "/file", {"case": case["id"], "value": value}


def idle(seat):
    yield "GET", "/rules", None


def spam(seat):
    _, cases = yield from setup()
    for case in cases:
        yield "POST", "/file", {"case": case["id"], "value": 0}


def fuzz(seat):
    rules, cases = yield from setup()
    case = cases[0]
    body = {"site": case["site"], "month": case["month"], "to": "invalid"}
    if not rules["records"]:
        body = question(rules, case)
    for malformed in ({}, {**body, "site": []}, {**body, "to": {}}, {**body, "month": True},
                      {**body, "month": []}, {**body, "part": []}, {**body, "part": "missing"}):
        yield "POST", "/ask", malformed
    response = yield from ask(body)
    if response and response["reply"] is None:
        response = yield from ask(body)
    rid = response["reply"]["id"] if response and response["reply"] else "missing"
    for fields in ([], ["month", "month"], ["other"], [{}], "month", None):
        yield "POST", "/correct", {"reply": rid, "fields": fields}
    yield "POST", "/correct", {"reply": [], "fields": ["month"]}
    for malformed in ({}, {"case": [], "value": 0}, {"case": case["id"], "value": False},
                      {"case": case["id"], "value": "8"}, {"case": case["id"], "value": 8.0}):
        yield "POST", "/file", malformed


POLICIES = {**{mode: partial(clerk, mode=mode) for mode in (*SKILLS, "wide", "no_recheck", "namer", "midpoint",
            "repeater", "fix_all", *FIXED_FIELDS, "reactive")},
            "idle": idle, "spam": spam, "fuzz": fuzz}
