import hashlib
import json
from collections import Counter
from pathlib import Path

from worldkit import Reject, World, serve

INSTANCE = Path(__file__).with_name("instance.json")
FIELDS = ("month", "parts", "value", "unit")
MONTHS = tuple(range(1, 7))
FORMS = 4
KINDS = ("plain", "month", "part", "chain", "range", "silent", "crate")
COUNTERS = ("questions", "asks", "corrections", "fields_named", "fields_named_unneeded",
            "repeats", "refused_for_budget", "off_replies_seen", "filed_off_reply", "filed_before_reply")


def question_scope(instance, question):
    site = question["site"]
    parts = instance["sites"][site]["parts"]
    return {"site": site, "parts": list(parts), "month": question["month"], "unit": "kg",
            "exact": True}


def differences(instance, question, scope):
    wanted = question_scope(instance, question)
    keys = {"value": "exact"}
    return [field for field in FIELDS if scope[keys.get(field, field)] != wanted[keys.get(field, field)]]


def kilogram_total(instance, scope):
    book = instance["books"][scope["site"]]
    return sum(book[part][scope["month"] - 1] for part in scope["parts"])


def figure_for(instance, scope):
    total = kilogram_total(instance, scope)
    if scope["unit"] == "crates":
        factor = instance["kg_per_crate"]
        assert total % factor == 0
        total //= factor
    if scope["exact"]:
        return total
    a, b = instance["range_offsets"][scope["site"]][scope["month"] - 1]
    assert scope["unit"] == "kg" and 10 <= a <= 90 and 10 <= b <= 90 and a != b
    low, high = total - a, total + b
    assert 0 < low < total < high and low + high != 2 * total
    return {"low": low, "high": high}


def case_kind(instance, question):
    case = next((c for c in instance["cases"] if c["site"] == question["site"]
                 and c["month"] == question["month"]), None)
    return (case["id"], instance["faults"][case["id"]]) if case else (None, "plain")


def first_scope(instance, question):
    scope = question_scope(instance, question)
    cid, kind = case_kind(instance, question)
    if kind in ("month", "chain"):
        scope["month"] = instance["off_months"][cid]
    if kind == "part":
        scope["parts"] = [instance["partial_parts"][cid]]
    if kind == "range":
        scope["exact"] = False
    if kind == "crate":
        scope["unit"] = "crates"
    return None if kind == "silent" else scope


def corrected_scope(instance, question, scope, fields, *, chain_pending):
    wanted = question_scope(instance, question)
    keys = ["exact" if field == "value" else field for field in fields]
    updated = {**scope, **{key: wanted[key] for key in keys}}
    cid, kind = case_kind(instance, question)
    if chain_pending and kind == "chain" and "month" in fields:
        assert differences(instance, question, scope) == ["month"]
        extra = next((field for field in instance["chain_order"] if field not in fields), None)
        if extra == "parts":
            updated["parts"] = [instance["partial_parts"][cid]]
        elif extra == "value":
            updated["exact"] = False
        elif extra == "unit":
            updated["unit"] = "crates"
    assert not set(fields).intersection(differences(instance, question, updated))
    return updated


def public_scope(scope, figure):
    return {"site": scope["site"], "parts": list(scope["parts"]),
            "month": scope["month"], "unit": scope["unit"],
            **({"value": figure} if scope["exact"] else {"value": None, **figure})}


def render(scope, figure, form):
    assert 0 <= form < FORMS
    site, month, unit = scope["site"].capitalize(), scope["month"], scope["unit"]
    parts = ("part " if len(scope["parts"]) == 1 else "parts ") + " and ".join(scope["parts"])
    together = " together" if len(scope["parts"]) > 1 else ""
    amount = str(figure) if scope["exact"] else f"between {figure['low']} and {figure['high']}"
    forms = (
        f"At {site}, {parts}{together} held {amount} {unit} at the end of month {month}.",
        f"The end-of-month {month} stock of {parts} at {site} was {amount} {unit}.",
        f"{site}, {parts}, end of month {month}: {amount} {unit}.",
        f"End of month {month}, {site} ({parts}): {amount} {unit}.",
    )
    return forms[form]


def form_for(instance, question, scope):
    key = json.dumps([instance["form_seed"], question, scope], sort_keys=True)
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], "big") % FORMS


def case_question(instance, case):
    return {"site": case["site"], "month": case["month"],
            "to": instance["sites"][case["site"]]["holder"]}


def question_key(question):
    return question["site"], question["month"]


class Desk:
    def __init__(self, instance):
        self.instance = instance
        self.cases = {case["id"]: case for case in instance["cases"]}
        self.replies = {}
        self.first = {}
        self.last = {}
        self.chain_traps = {}
        self.files = {}
        self.records_read = False
        self.measures = Counter(dict.fromkeys(COUNTERS, 0))

    def left(self):
        return {"questions": self.instance["question_budget"] - self.measures["questions"],
                "file": self.instance["file_budget"] - len(self.files)}

    def require_questions(self, cost):
        if self.instance["arm"] == "solo":
            raise Reject(404, "no colleagues in this arm")
        if self.left()["questions"] < cost:
            raise Reject(429, "not enough questions left", budget_left=self.left()["questions"])

    def new_reply(self, question, scope):
        rid = f"r{len(self.replies) + 1}"
        record = {"id": rid, "question": dict(question), "scope": scope,
                  "figure": figure_for(self.instance, scope)}
        self.replies[rid] = record
        return record

    def delivered(self, record):
        if record is not None:
            question, scope = record["question"], record["scope"]
            self.last[question_key(question)] = record["id"]
            self.measures["off_replies_seen"] += bool(differences(self.instance, question, scope))
        return record

    def envelope(self, record):
        reply = None
        differs = ["reply"]
        if record is not None:
            scope, question, figure = record["scope"], record["question"], record["figure"]
            reply = {"id": record["id"], "from": question["to"]}
            differs = differences(self.instance, question, scope)
            if self.instance["arm"] == "fields":
                reply.update(public_scope(scope, figure))
            else:
                reply["text"] = render(scope, figure, form_for(self.instance, question, scope))
        result = {"reply": reply, "budget_left": self.left()["questions"]}
        if self.instance["arm"] == "flagged":
            result["check"] = {"answers_your_question": not differs, "differs_in": differs}
        return result

    def validate_question(self, body):
        if set(body) != {"to", "site", "month"}:
            raise Reject(400, "expected to, site and month")
        site, month = body["site"], body["month"]
        sites = self.instance["sites"]
        if not isinstance(site, str) or site not in sites or body["to"] != sites[site]["holder"]:
            raise Reject(404, "unknown site or wrong holder")
        if type(month) is not int or month not in MONTHS:
            raise Reject(404, "unknown month")
        return dict(body)

    def ask(self, body):
        question = self.validate_question(body)
        self.require_questions(1)
        key = question_key(question)
        old_id = self.first.get(key)
        if old_id is not None:
            record = self.replies[old_id]
            self.measures["repeats"] += 1
        else:
            scope = (question_scope(self.instance, question) if key in self.first
                     else first_scope(self.instance, question))
            record = self.new_reply(question, scope) if scope is not None else None
            self.first[key] = record["id"] if record else None
        self.measures["asks"] += 1
        self.measures["questions"] += 1
        return self.delivered(record)

    def correct(self, body):
        rid, fields = body.get("reply"), body.get("fields")
        if set(body) != {"reply", "fields"}:
            raise Reject(400, "expected reply and fields")
        if not isinstance(fields, list) or not 1 <= len(fields) <= len(FIELDS) or any(
            not isinstance(field, str) or field not in FIELDS for field in fields
        ) or len(set(fields)) != len(fields):
            raise Reject(400, "name 1 to 4 distinct known fields")
        if not isinstance(rid, str) or rid not in self.replies:
            raise Reject(404, "unknown reply")
        self.require_questions(len(fields))
        old = self.replies[rid]
        question, scope = old["question"], old["scope"]
        needed = differences(self.instance, question, scope)
        key = question_key(question)
        correction = (rid, tuple(sorted(fields)))
        _, kind = case_kind(self.instance, question)
        if kind == "chain" and "month" in fields and key not in self.chain_traps:
            self.chain_traps[key] = correction
        record = self.new_reply(question, corrected_scope(
            self.instance, question, scope, fields,
            chain_pending=self.chain_traps.get(key) == correction))
        self.measures["corrections"] += 1
        self.measures["questions"] += len(fields)
        self.measures["fields_named"] += len(fields)
        self.measures["fields_named_unneeded"] += sum(field not in needed for field in fields)
        return self.delivered(record)

    def records(self):
        if self.instance["arm"] != "solo":
            raise Reject(404, "records are held by colleagues")
        result = []
        for cid, case in self.cases.items():
            question = case_question(self.instance, case)
            scope = question_scope(self.instance, question)
            if self.instance["faults"][cid] == "crate":
                scope["unit"] = "crates"
            figure = figure_for(self.instance, scope)
            result.append({"case": cid, "text": render(scope, figure, form_for(self.instance, question, scope))})
        self.records_read = True
        return {"records": result}

    def file(self, body):
        cid, value = body.get("case"), body.get("value")
        if set(body) != {"case", "value"} or type(value) is not int:
            raise Reject(400, "expected case and whole number value")
        if not isinstance(cid, str) or cid not in self.cases:
            raise Reject(404, "unknown case")
        if cid in self.files:
            raise Reject(409, "case already filed")
        assert self.left()["file"] > 0
        case = self.cases[cid]
        rid = self.last.get((case["site"], case["month"]))
        self.measures["filed_before_reply"] += rid is None and not self.records_read
        if rid is not None:
            record = self.replies[rid]
            if differences(self.instance, record["question"], record["scope"]):
                figure = record["figure"]
                values = figure.values() if isinstance(figure, dict) else (figure,)
                self.measures["filed_off_reply"] += value in values
        self.files[cid] = value
        return {"case": cid, "value": value, "budget_left": self.left()["file"]}

    def state(self):
        return {"replies": self.replies, "first": [[list(key), rid] for key, rid in self.first.items()],
                "last": [[list(key), rid] for key, rid in self.last.items()],
                "chain_traps": [[list(key), rid, list(fields)]
                                for key, (rid, fields) in self.chain_traps.items()],
                "files": self.files, "records_read": self.records_read, "budget_left": self.left(),
                "accepted_measures": {key: value for key, value in self.measures.items()
                                      if key != "refused_for_budget"}}


class Inquiry(World):
    def __init__(self, instance, tokens=None, events=None):
        self.instance = instance
        self.desk = Desk(instance)
        super().__init__(tokens, events)

    def _log(self, seat, kind, method, body, status, response, late=False):
        if (self.instance["arm"] == "flagged" and seat is not None and method == "POST"
                and kind in ("ask", "correct") and status != 200):
            response = {**response, **self.desk.envelope(None)}
        return super()._log(seat, kind, method, body, status, response, late=late)

    def get_rules(self, seat):
        sites = self.instance["sites"]
        solo = self.instance["arm"] == "solo"
        if solo:
            sites = {site: {"parts": row["parts"]} for site, row in sites.items()}
        return {**super().get_rules(seat), "sites": sites, "months": list(MONTHS),
                "kg_per_crate": self.instance["kg_per_crate"], "fields": list(FIELDS),
                "budget_left": self.desk.left(), "records": solo}

    def get_cases(self, seat):
        return {"cases": self.instance["cases"]}

    def get_records(self, seat):
        return self.desk.records()

    def post_ask(self, seat, body):
        return self.desk.envelope(self.desk.ask(body))

    def post_correct(self, seat, body):
        return self.desk.envelope(self.desk.correct(body))

    def post_file(self, seat, body):
        return self.desk.file(body)


if __name__ == "__main__":
    serve(Inquiry(json.loads(INSTANCE.read_text())))
