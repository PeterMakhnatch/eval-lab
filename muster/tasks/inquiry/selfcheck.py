import json
import subprocess
import sys
from copy import deepcopy
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from muster.lab import episode  # noqa: E402
from muster.lab.schedule import parse  # noqa: E402
from muster.tasks.inquiry import lab  # noqa: E402
from muster.tasks.inquiry.instances import (  # noqa: E402
    COUNT,
    FACETS,
    QUESTIONS,
    form_balance,
    instance,
    parser_check,
)
from muster.tasks.inquiry.zoo import (  # noqa: E402
    FIELDS,
    MAX_CORRECTIONS,
    ask,
    correct,
    filed_value,
    missing_fields,
    parse_reply,
    question,
    setup,
)


def magnitude_only(seat):
    rules, cases = yield from setup()
    figures = []
    for case in cases:
        response = yield from ask(question(rules, case))
        scope = parse_reply(response["reply"])
        value = 0 if scope is None else scope["low"] if scope["value"] is None else scope["value"]
        figures.append(value)
    for position in sorted(range(len(cases)), key=figures.__getitem__)[:rules["budget_left"]["questions"] - len(cases)]:
        response = yield from ask(question(rules, cases[position]))
        scope = parse_reply(response["reply"])
        figures[position] = 0 if scope is None else scope["low"] if scope["value"] is None else scope["value"]
    for case, value in zip(cases, figures, strict=True):
        yield "POST", "/file", {"case": case["id"], "value": value}


def magnitude_check():
    for facet in ("month", "part", "mixed"):
        results = []
        for index in range(1, COUNT + 1):
            data = instance(facet, "probe", index)
            result = episode.run(lab, data, {"p1": "magnitude"}, parse("round"), zoo={"magnitude": magnitude_only})
            assert result.rewards["reward"] < 1.0 and result.rewards["questions"] == QUESTIONS
            results.append(result.rewards["reward"])
        print(f"magnitude-only {facet}: {results}")


def client(world, token):
    def request(method, path, body=None, expected=200):
        before = deepcopy(lab.state(world))
        status, response = world.handle(method, path, token, body, "10.0.0.2")
        assert status == expected, (path, body, status, response, expected)
        if expected != 200:
            assert before == lab.state(world)
            if path in ("/ask", "/correct"):
                assert_check(world.instance["arm"], response, ["reply"])
        return response
    return request


def assert_check(arm, response, fields):
    if arm == "flagged":
        assert response["check"] == {"answers_your_question": not fields, "differs_in": fields}
    else:
        assert "check" not in response


def check_replies(data, request):
    arm = data["arm"]
    chain = next(case for case in data["cases"] if data["faults"][case["id"]] == "chain")
    body = question({"sites": data["sites"]}, chain)
    part = data["sites"][chain["site"]]["parts"][0]
    request("POST", "/ask", {**body, "to": "wrong"}, 404)
    request("POST", "/ask", {**body, "part": part}, 400)
    original = request("POST", "/ask", body)
    assert_check(arm, original, ["month"])
    repeat = request("POST", "/ask", body)
    assert repeat["reply"] == original["reply"] and repeat["budget_left"] == 11
    rid = original["reply"]["id"]
    request("POST", "/correct", {"reply": rid, "fields": ["month", "month"]}, 400)
    request("POST", "/correct", {"reply": rid, "fields": ["unknown"]}, 400)
    request("POST", "/correct", {"reply": "absent", "fields": ["month"]}, 404)
    month = request("POST", "/correct", {"reply": rid, "fields": ["month"]})
    assert month["reply"]["id"] != rid
    extra_field = data["chain_order"][0]
    assert_check(arm, month, [extra_field])
    fixed = request("POST", "/correct", {"reply": month["reply"]["id"], "fields": [extra_field]})
    assert_check(arm, fixed, [])
    both = request("POST", "/correct", {"reply": rid, "fields": ["month", "parts"]})
    assert_check(arm, both, [])
    both_scope = parse_reply(both["reply"])
    assert both_scope["parts"] == data["sites"][chain["site"]]["parts"]
    assert both_scope["month"] == chain["month"] and both_scope["value"] is not None
    assert both["reply"]["id"] != month["reply"]["id"]
    assert request("POST", "/ask", body)["reply"] == original["reply"]
    final = request("POST", "/correct", {"reply": both["reply"]["id"], "fields": ["value"]})
    assert parse_reply(final["reply"]) == parse_reply(fixed["reply"])
    assert_check(arm, final, [])
    assert final["budget_left"] == 5
    original_value = parse_reply(original["reply"])["value"]
    request("POST", "/file", {"case": chain["id"], "value": original_value})
    late_correction = request("POST", "/correct", {"reply": rid, "fields": ["parts"]})
    assert parse_reply(late_correction["reply"]) == parse_reply(original["reply"])
    assert_check(arm, late_correction, ["month"])
    other = data["off_months"][chain["id"]]
    extra = request("POST", "/ask", {**body, "month": other})
    assert_check(arm, extra, [])
    parsed = parse_reply(extra["reply"])
    assert parsed["month"] == other and parsed["parts"] == data["sites"][chain["site"]]["parts"]
    assert parsed["value"] == sum(book[other - 1] for book in data["books"][chain["site"]].values())
    charged = request("POST", "/correct", {"reply": final["reply"]["id"], "fields": ["value", "unit"]})
    assert charged["budget_left"] == 1
    request("POST", "/correct", {"reply": rid, "fields": ["parts", "month"]}, 429)
    assert request("POST", "/correct", {"reply": final["reply"]["id"], "fields": ["parts"]})["budget_left"] == 0
    request("POST", "/ask", body, 429)
    assert request("GET", "/rules")["budget_left"]["questions"] == 0
    return chain["id"]


def check_chain_branches(data, root):
    world = lab.make_world(data, root / "chain.jsonl", lambda: 0.0)
    request = client(world, data["tokens"]["p1"])
    chain = next(case for case in data["cases"] if data["faults"][case["id"]] == "chain")
    body = question({"sites": data["sites"]}, chain)
    original = request("POST", "/ask", body)["reply"]
    rid, fields = original["id"], ["month", "parts", "value"]
    crate = request("POST", "/correct", {"reply": rid, "fields": fields})
    assert_check(data["arm"], crate, ["unit"])
    repeat = request("POST", "/correct", {"reply": rid, "fields": fields[::-1]})
    assert_check(data["arm"], repeat, ["unit"])
    first, second = crate["reply"], repeat["reply"]
    assert first["id"] != second["id"]
    assert {k: v for k, v in first.items() if k != "id"} == {k: v for k, v in second.items() if k != "id"}
    exact = request("POST", "/correct", {"reply": rid, "fields": list(FIELDS)})
    assert_check(data["arm"], exact, [])
    assert filed_value(parse_reply(first), "oracle", data) == parse_reply(exact["reply"])["value"]
    later = request("POST", "/correct", {"reply": rid, "fields": ["parts"]})
    assert_check(data["arm"], later, ["month"])
    assert parse_reply(later["reply"]) == parse_reply(original)
    fixed = request("POST", "/correct", {"reply": later["reply"]["id"], "fields": ["month"]})
    assert_check(data["arm"], fixed, [])
    assert parse_reply(fixed["reply"]) == parse_reply(exact["reply"])
    assert fixed["budget_left"] == 0
    assert world.handle("POST", "/admin/close", None, {}, "127.0.0.1")[0] == 200
    events = [json.loads(line) for line in (root / "chain.jsonl").read_text().splitlines()]
    assert lab.state(world) == lab.replay(events, data)


def check_filing(data, request, *, already_filed):
    for case in data["cases"]:
        if case["id"] == already_filed:
            continue
        request("POST", "/file", {"case": case["id"], "value": 0})
    assert request("GET", "/rules")["budget_left"]["file"] == 0
    request("POST", "/file", {"case": data["cases"][0]["id"], "value": 40}, 409)


def scenario(data, root):
    events = root / "events.jsonl"
    world = lab.make_world(data, events, lambda: 0.0)
    request = client(world, data["tokens"]["p1"])
    assert request("GET", "/rules")["budget_left"] == {"questions": QUESTIONS, "file": 8}
    already_filed = None
    if data["arm"] == "solo":
        body = {"site": "north", "month": 4, "to": data["sites"]["north"]["holder"]}
        request("POST", "/ask", body, 404)
        request("POST", "/correct", {"reply": "r1", "fields": ["month"]}, 404)
        records = request("GET", "/records")["records"]
        assert len(records) == len(data["cases"])
        for record, case in zip(records, data["cases"], strict=True):
            parsed = parse_reply(record)
            assert parsed["site"] == case["site"] and parsed["month"] == case["month"]
            assert parsed["parts"] == data["sites"][case["site"]]["parts"]
            assert parsed["unit"] == ("crates" if data["faults"][case["id"]] == "crate" else "kg")
    else:
        request("GET", "/records", expected=404)
        already_filed = check_replies(data, request)
    check_filing(data, request, already_filed=already_filed)
    assert world.handle("POST", "/admin/close", None, {}, "127.0.0.1")[0] == 200
    log = [json.loads(line) for line in events.read_text().splitlines()]
    assert lab.state(world) == lab.replay(log, data)
    measures = lab.score(log, data)
    assert measures["reward"] == 0 and measures["unfiled"] == 0
    if data["arm"] != "solo":
        assert measures["questions"] == QUESTIONS and measures["refused_for_budget"] == 2
        assert measures["off_replies_seen"] == 5 and measures["filed_off_reply"] == 0
        assert "right.silent" not in measures and measures["n.silent"] == 0
    assert measures["exact"] == 0.0


def check_silence(data, root):
    world = lab.make_world(data, root / "silent.jsonl", lambda: 0.0)
    request = client(world, data["tokens"]["p1"])
    case = next(case for case in data["cases"] if data["faults"][case["id"]] == "silent")
    body = question({"sites": data["sites"]}, case)
    first = request("POST", "/ask", body)
    assert first["reply"] is None
    assert_check(data["arm"], first, ["reply"])
    second = request("POST", "/ask", body)
    assert_check(data["arm"], second, [])
    assert request("POST", "/ask", body)["reply"] == second["reply"]
    assert world.desk.measures["questions"] == 3
    assert world.desk.measures["repeats"] == 1
    assert world.desk.measures["off_replies_seen"] == 0


def chance_baselines():
    for facet, policy in (("month", "fix_month"), ("part", "fix_parts"), ("range", "fix_value")):
        results = []
        for index in range(1, COUNT + 1):
            data = instance(facet, "probe", index)
            result = episode.run(lab, data, {"p1": policy}, parse("round"))
            results.append(result.rewards["reward"])
        print(f"{policy} in {facet}-probe: {results.count(1.0)}/{COUNT} exact; {results}")


def route_clerk(seat, *, month_fields, request_kg, pre_parts=False, skip_recheck=False):
    rules, cases = yield from setup()
    for case in cases:
        response = yield from ask(question(rules, case))
        if response is None:
            continue
        parts = rules["sites"][case["site"]]["parts"]
        scope = parse_reply(response["reply"])
        if pre_parts and scope["month"] != case["month"]:
            response = yield from correct(response["reply"], ["parts"])
            assert response is not None
            scope = parse_reply(response["reply"])
            assert missing_fields(scope, case, parts) == ["month"]
        for _ in range(MAX_CORRECTIONS):
            fields = missing_fields(scope, case, parts)
            if not request_kg:
                fields = [field for field in fields if field != "unit"]
            if not fields:
                yield "POST", "/file", {"case": case["id"], "value": filed_value(scope, "oracle", rules)}
                break
            if "month" in fields:
                fields = list(month_fields)
            updated = yield from correct(response["reply"], fields)
            if updated is None:
                break
            response = updated
            scope = parse_reply(response["reply"])
            if skip_recheck and "month" in fields:
                yield "POST", "/file", {"case": case["id"], "value": filed_value(scope, "truster", rules)}
                break


def budget_routes():
    routes = {
        "month_then_read": (("month",), False, 12),
        "month_then_read_and_kg": (("month",), True, 13),
        "month_parts_then_read": (("month", "parts"), False, 13),
        "month_parts_value_and_convert": (("month", "parts", "value"), False, 13),
        "all_four": (FIELDS, False, None),
    }
    for index in range(1, COUNT + 1):
        data = instance("mixed", "probe", index)
        chain = next(case["id"] for case in data["cases"] if data["faults"][case["id"]] == "chain")
        last = [case["id"] for case in data["cases"] if data["faults"][case["id"]] in ("chain", "part", "range")][-1]
        for name, (fields, request_kg, cost) in routes.items():
            if cost is not None and not request_kg and len(fields) < 3:
                extra = next(field for field in data["chain_order"] if field not in fields)
                if extra == "unit":
                    cost -= 1
            policy = partial(route_clerk, month_fields=fields, request_kg=request_kg)
            result = episode.run(lab, data, {"p1": name}, parse("round"), zoo={name: policy})
            reward, questions = result.rewards["reward"], result.rewards["questions"]
            if cost is not None:
                assert reward == 1.0 and questions == cost
            else:
                assert reward < 1.0 and result.rewards["refused_for_budget"] >= 1
            refused = [event["request"] for event in result.events if event["status"] == 429]
            print(f"{data['id']} {name} chain={chain} last_correction={last}: "
                  f"reward={reward} questions={questions:g} refused={refused}")
        for policy, fields, cost in (("oracle", ("month",), 12), ("wide", ("month", "parts"), 13)):
            if next(field for field in data["chain_order"] if field not in fields) == "unit":
                cost -= 1
            result = episode.run(lab, data, {"p1": policy}, parse("round"))
            assert result.rewards["reward"] == 1.0 and result.rewards["questions"] == cost
            print(f"{data['id']} {policy}: reward=1.0 questions={cost}")
        check_pre_parts_route(data, chain)


def check_pre_parts_route(data, chain):
    extra_field = data["chain_order"][0]
    read_cost = 12 if extra_field == "unit" else 13
    for skip_recheck, expected_cost, expected_reward in ((False, read_cost, 1.0), (True, 12, 0.875)):
        name = "parts_then_month_unchecked" if skip_recheck else "parts_then_month_then_read"
        policy = partial(route_clerk, month_fields=("month",), request_kg=False,
                         pre_parts=True, skip_recheck=skip_recheck)
        result = episode.run(lab, data, {"p1": name}, parse("round"), zoo={name: policy})
        reward, questions = result.rewards["reward"], result.rewards["questions"]
        assert reward == expected_reward and questions == expected_cost
        assert result.rewards["right.chain"] == float(not skip_recheck)
        assert result.rewards["refused_for_budget"] == 0
        fields = chain_corrections(result.events, next(case for case in data["cases"] if case["id"] == chain))
        expected = [["parts"], ["month"]]
        if not skip_recheck and extra_field != "unit":
            expected.append([extra_field])
        assert fields == expected
        print(f"{data['id']} {name} chain={chain}: reward={reward} questions={questions:g} "
              f"chain corrections={len(fields)}")


def chain_corrections(events, case):
    replies, fields = set(), []
    for event in events:
        if not event["ok"]:
            continue
        body = event["request"]
        if event["kind"] == "ask" and body["site"] == case["site"] and body["month"] == case["month"]:
            replies.add(event["response"]["reply"]["id"])
        elif event["kind"] == "correct" and body["reply"] in replies:
            replies.add(event["response"]["reply"]["id"])
            fields.append(body["fields"])
    return fields


def main():
    for condition in lab.conditions():
        rows = lab.instances(condition)
        assert len(rows) == COUNT
    assert all(parser_check(facet, index) for facet in FACETS for index in range(1, COUNT + 1))
    print("generator assertions and parser round trips PASS")
    print(f"sentence form counts (oracle path, one probe arm per facet): {form_balance()}")
    with TemporaryDirectory(prefix="inquiry-check-") as directory:
        for arm in ("probe", "fields", "flagged", "solo"):
            for index in range(1, COUNT + 1):
                scenario(instance("mixed", arm, index), Path(directory))
                if arm != "solo":
                    check_chain_branches(instance("mixed", arm, index), Path(directory))
                    check_silence(instance("silent", arm, index), Path(directory))
    print("reply, budget, filing and replay boundaries PASS")
    magnitude_check()
    chance_baselines()
    budget_routes()
    return subprocess.run([sys.executable, "-m", "muster", "card", "inquiry"],
                          cwd=Path(__file__).resolve().parents[3], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
