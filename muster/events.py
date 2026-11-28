import hashlib
import hmac
import json
from statistics import median

CHANNEL = ("msg", "msgs")
GENESIS_HASH = "0" * 64
UNHASHED = ("entry_hash", "hmac")


def canonical(event: dict) -> str:
    assert isinstance(event, dict)
    body = {key: value for key, value in event.items() if key not in UNHASHED}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def entry_hash(event: dict) -> str:
    digest = hashlib.sha256(canonical(event).encode("utf-8")).hexdigest()
    assert len(digest) == 64
    return digest


def entry_hmac(digest: str, key: str) -> str:
    assert len(digest) == 64 and key
    return hmac.new(key.encode("utf-8"), digest.encode("ascii"), "sha256").hexdigest()


def chain(events: list[dict], key: str | None = None) -> list[dict]:
    previous, sealed = GENESIS_HASH, []
    for event in events:
        row = {k: v for k, v in event.items() if k not in UNHASHED}
        row["prev_hash"] = previous
        row["entry_hash"] = previous = entry_hash(row)
        if key:
            row["hmac"] = entry_hmac(previous, key)
        sealed.append(row)
    assert len(sealed) == len(events)
    return sealed


def in_play(events: list[dict]) -> list[dict]:
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
    return event["kind"] + " " + json.dumps(event.get("request"), sort_keys=True)


def jain(values: list[float]) -> float | None:
    total, squares = sum(values), sum(value * value for value in values)
    if not values or squares == 0:
        return None
    return total * total / (len(values) * squares)


def _received(fetches: list[dict]) -> dict[str, dict[int, float]]:
    got: dict[str, dict[int, float]] = {}
    for event in fetches:
        seen = got.setdefault(event["seat"], {})
        for message in (event.get("response") or {}).get("messages") or []:
            seen.setdefault(message["id"], event["t"])
    return got


def _message_delivery(
    posts: list[dict], names: list[str], received: dict[str, dict[int, float]]
) -> tuple[int, int, float | None]:
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
    assert pairs == never_fetched + len(lags)
    return pairs, never_fetched, round(median(lags), 3) if lags else None


def _unread_at_actions(
    actions: list[dict], posts: list[dict], received: dict[str, dict[int, float]]
) -> list[int]:
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
    return unread


def _duplicate_actions(
    actions: list[dict], posts: list[dict], received: dict[str, dict[int, float]]
) -> tuple[int, int, bool]:
    first_acts: dict[str, str] = {}
    first_actor: dict[str, dict] = {}
    duplicates = informed = 0
    for action in actions:
        act = _same_act(action)
        first_acts.setdefault(action["seat"], act)
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

    assert 0 <= informed <= duplicates <= len(actions)
    acts = first_acts.values()
    return duplicates, informed, len(acts) > len(set(acts))


def _seat_measures(played: list[dict], names: list[str], start: float) -> dict:
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

    return per_seat


def summarize(events: list[dict], seats: list[str] | None = None) -> dict:
    played = in_play(events)
    names = sorted(set(seats or []) | {event["seat"] for event in played})
    start = played[0]["t"] if played else 0.0
    actions = [event for event in played if _is_action(event)]
    posts = [event for event in played if _is_post(event)]
    fetches = [event for event in played if _is_fetch(event)]
    received = _received(fetches)
    pairs, never_fetched, lag_median = _message_delivery(posts, names, received)
    unread = _unread_at_actions(actions, posts, received)
    duplicates, informed, identical_first = _duplicate_actions(actions, posts, received)
    per_seat = _seat_measures(played, names, start)
    talk = len(posts) + len(fetches)
    assert talk + len(actions) <= len(played) <= len(events)
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
        "fetch_lag_median": lag_median,
        "actions_with_unread": sum(1 for count in unread if count),
        "unread_at_action_mean": sum(unread) / len(unread) if unread else None,
        "duplicate_actions": duplicates,
        "informed_duplicates": informed,
        "identical_first_action": identical_first,
        "never_acted": [seat for seat in names if per_seat[seat]["actions"] == 0],
        "action_evenness": jain([float(per_seat[seat]["actions"]) for seat in names]),
        "per_seat": per_seat,
    }
