import math
import re


def amounts(reply):
    if reply is None:
        return None, None
    if "text" in reply:
        match = re.search(r"(?:(\d+) and )?(\d+) (?:kg|crates)\b", reply["text"])
        assert match
        first, last = match.groups()
        return (int(first), int(last)) if first else (int(last), int(last))
    if reply["value"] is None:
        return reply["low"], reply["high"]
    return reply["value"], reply["value"]


def form(reply):
    if reply is None:
        return -1
    text = reply.get("text", "")
    return next((i for i, prefix in enumerate(("At ", "The ", "End "))
                 if text.startswith(prefix)), 3)


def features(cases, rules, replies):
    rows = []
    lows = [amounts(r)[0] for r in replies]
    for p, (case, reply, low) in enumerate(zip(cases, replies, lows, strict=True)):
        site = rules["sites"][case["site"]]
        peer = next(i for i, c in enumerate(cases) if c["site"] == case["site"] and i != p)
        high = amounts(reply)[1]
        digits = str(low) if low is not None else ""
        other = lows[peer]
        row = {"position": p, "site": case["site"], "holder": site["holder"],
               "ask_month": case["month"], "site_parts": len(site["parts"]),
               "factor": rules["kg_per_crate"], "delivery": int(reply["id"][1:]) if reply else None,
               "form": form(reply), "amount": low, "peer_amount": other,
               "width": high-low if low is not None else None,
               "length": len(reply.get("text", "")) if reply else 0,
               "present": reply is not None,
               "budget_after_ask": rules["budget_left"]["questions"]-p-1,
               "peer_gap": abs(case["month"]-cases[peer]["month"]), "peer_position": peer,
               "holder_position": sum(r["holder"] == site["holder"] for r in rows)}
        if low is not None:
            row.update(digits=len(digits), first_digit=int(digits[0]), last_digit=low%10,
                       digit_sum=sum(map(int, digits)), distinct_digits=len(set(digits)),
                       mod_factor=low%rules["kg_per_crate"], mod_3=low%3, mod_7=low%7,
                       above_20k=low>20000,
                       rank=sum(v is not None and v<low for v in lows),
                       per_part=low/len(site["parts"]))
        if low is not None and other is not None:
            row.update(peer_ratio=low/other, peer_abslog=abs(math.log(low/other)),
                       peer_signed_log=math.log(low/other),
                       peer_mod_gap=(low-other)%rules["kg_per_crate"],
                       peer_same_digits=len(str(low))==len(str(other)),
                       peer_both_small=max(low,other)<=20000,
                       peer_both_large=min(low,other)>20000)
        rows.append(row)
    return rows
