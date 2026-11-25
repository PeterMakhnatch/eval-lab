import re
from math import log


def figure(reply):
    if reply is None:
        return None
    if "text" not in reply:
        return reply["value"] if reply["value"] is not None else reply["low"]
    match = re.search(r"(?:(\d+) and )?(\d+) (?:kg|crates)\b", reply["text"])
    assert match
    return int(match[1] or match[2])


def chosen(field, rank):
    def policy(seat):
        status, rules = yield ("GET", "/rules", None)
        assert status == 200
        status, body = yield ("GET", "/cases", None)
        assert status == 200
        cases = body["cases"]
        replies = []
        for c in cases:
            question = {"to": rules["sites"][c["site"]]["holder"], "site": c["site"],
                        "month": c["month"]}
            status, body = yield ("POST", "/ask", question)
            assert status == 200
            replies.append(body["reply"])
        lows = list(map(figure, replies))
        keys = []
        for i, c in enumerate(cases):
            peer = next(j for j, other in enumerate(cases) if j != i and other["site"] == c["site"])
            keys.append(rank(lows[i], lows[peer], c, cases[peer], rules))
        for i in sorted(range(len(cases)), key=lambda i: keys[i])[:5]:
            status, body = yield (
                "POST", "/correct", {"reply": replies[i]["id"], "fields": [field]})
            assert status == 200
            replies[i] = body["reply"]
        for c, reply in zip(cases, replies, strict=True):
            status, _ = yield ("POST", "/file", {"case": c["id"], "value": figure(reply)})
            assert status == 200
    return policy


month_peer_proximity = chosen("month", lambda a, b, c, d, r: abs(log(a/b)))
part_factor_remainder = chosen("parts", lambda a, b, c, d, r: -(a % r["kg_per_crate"]))
range_distinct_digits = chosen("value", lambda a, b, c, d, r: -len(set(str(a))))
range_peer_gap = chosen("value", lambda a, b, c, d, r: abs(c["month"]-d["month"]))
