"""Exact measures over one world's event log (docs/DESIGN.md, section 4).

Pure functions: a list of events in, numbers out. No model reads any text. The measures work for every
world built on the world kit, because they use only the kit's fields: `seq`, `t`, `seat`, `kind`,
`method`, `status`, `ok`, `request`, `response`.

Words:
  action   an accepted POST that is not a message (an open, an answer, a take, a pledge, ...)
  post     an accepted POST /msg
  fetch    an accepted GET /msgs; its response says which messages the seat has now received
  probe    a request for a route that the world does not have (status 404)
"""

import json
from statistics import median

CHANNEL = ("msg", "msgs")


def in_play(events: list[dict]) -> list[dict]:
    """Seat events before the world closed, accepted or not."""
    played = []
    for event in events:
        if event.get("kind") == "admin.close" and event.get("ok"):
            break
        if event.get("seat") is not None:
            played.append(event)
    return played


def _is_action(event: dict) -> bool:
    return bool(event.get("ok")) and event.get("method") == "POST" and event.get("kind") not in CHANNEL


def _is_post(event: dict) -> bool:
    return bool(event.get("ok")) and event.get("method") == "POST" and event.get("kind") == "msg"


def _is_fetch(event: dict) -> bool:
    return bool(event.get("ok")) and event.get("method") == "GET" and event.get("kind") == "msgs"


def _same_act(event: dict) -> str:
    """Two actions are the same act when the kind and the request are equal."""
    return event["kind"] + " " + json.dumps(event.get("request"), sort_keys=True)


def jain(values: list[float]) -> float | None:
    """Jain's evenness index: 1 when all values are equal, 1/n when one value holds everything."""
    total, squares = sum(values), sum(value * value for value in values)
    if not values or squares == 0:
        return None
    return total * total / (len(values) * squares)


def _received(played: list[dict]) -> dict[str, dict[int, float]]:
    """For each seat: message id -> world time of the first fetch that returned it."""
    got: dict[str, dict[int, float]] = {}
    for event in played:
        if _is_fetch(event):
            seen = got.setdefault(event["seat"], {})
            for message in (event.get("response") or {}).get("messages") or []:
                seen.setdefault(message["id"], event["t"])
    return got


def summarize(events: list[dict], seats: list[str] | None = None) -> dict:
    """All measures for one trial. `seats` adds seats that never sent a request."""
    played = in_play(events)
    names = sorted(set(seats or []) | {event["seat"] for event in played})
    start = played[0]["t"] if played else None
    actions = [event for event in played if _is_action(event)]
    posts = [event for event in played if _is_post(event)]
    fetches = [event for event in played if _is_fetch(event)]
    received = _received(played)

    # Each message and each other seat: was it fetched, and how long after the post?
    lags, never_fetched, pairs = [], 0, 0
    for post in posts:
        message_id = post["response"]["id"]
        for seat in names:
            if seat == post["seat"]:
                continue
            pairs += 1
            fetched_at = received.get(seat, {}).get(message_id)
            if fetched_at is None:
                never_fetched += 1
            else:
                lags.append(fetched_at - post["t"])

    # Each action: how many messages of other seats existed that the actor had not yet received?
    unread = []
    for action in actions:
        seen = received.get(action["seat"], {})
        unread.append(
            sum(
                1
                for post in posts
                if post["seat"] != action["seat"]
                and post["seq"] < action["seq"]
                and not (post["response"]["id"] in seen and seen[post["response"]["id"]] <= action["t"])
            )
        )

    # A duplicate repeats the act of a different seat. It is informed when the actor had already
    # received a message that the first actor posted after its own act.
    first_actor: dict[str, dict] = {}
    duplicates = informed = 0
    for action in actions:
        act = _same_act(action)
        first = first_actor.setdefault(act, action)
        if first["seat"] == action["seat"]:
            continue
        duplicates += 1
        seen = received.get(action["seat"], {})
        if any(
            post["seat"] == first["seat"]
            and post["seq"] > first["seq"]
            and post["response"]["id"] in seen
            and seen[post["response"]["id"]] <= action["t"]
            for post in posts
        ):
            informed += 1

    first_acts = {}
    for action in actions:
        first_acts.setdefault(action["seat"], _same_act(action))
    acts = list(first_acts.values())

    per_seat = {}
    for seat in names:
        own = [event for event in played if event["seat"] == seat]
        per_seat[seat] = {
            "requests": len(own),
            "actions": sum(1 for event in own if _is_action(event)),
            "posts": sum(1 for event in own if _is_post(event)),
            "fetches": sum(1 for event in own if _is_fetch(event)),
            "probes": sum(1 for event in own if event.get("status") == 404),
            "first_at": round(own[0]["t"] - start, 3) if own else None,
            "last_at": round(own[-1]["t"] - start, 3) if own else None,
        }

    talk = len(posts) + len(fetches)
    return {
        "seats": names,
        "requests": len(played),
        "actions": len(actions),
        "posts": len(posts),
        "fetches": len(fetches),
        "probes": sum(1 for event in played if event.get("status") == 404),
        "refused": sum(1 for event in played if not event.get("ok") and event.get("status") != 404),
        "unauthorized": sum(1 for event in events if event.get("status") == 401),
        "late": sum(1 for event in events if event.get("late")),
        "communication_share": talk / (talk + len(actions)) if talk + len(actions) else None,
        "first_post_at": round(posts[0]["t"] - start, 3) if posts else None,
        "first_fetch_at": round(fetches[0]["t"] - start, 3) if fetches else None,
        "actions_before_first_post": sum(
            1 for action in actions if not posts or action["seq"] < posts[0]["seq"]
        ),
        "message_pairs": pairs,
        "never_fetched": never_fetched,
        "fetch_lag_median": round(median(lags), 3) if lags else None,
        "actions_with_unread": sum(1 for count in unread if count),
        "unread_at_action_mean": sum(unread) / len(unread) if unread else None,
        "duplicate_actions": duplicates,
        "informed_duplicates": informed,
        "identical_first_action": len(acts) > len(set(acts)),
        "never_acted": [seat for seat in names if per_seat[seat]["actions"] == 0],
        "action_evenness": jain([float(per_seat[seat]["actions"]) for seat in names]),
        "per_seat": per_seat,
    }
