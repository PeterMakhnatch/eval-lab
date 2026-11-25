import json
import os
import sys
import time
import urllib.error
import urllib.request

MAX_REQUESTS = 2000
TRANSPORT_RETRIES = 3
REQUEST_TIMEOUT_SEC = 30
RETRY_PAUSE_SEC = 1


def exchange(opener, world, token, action):
    method, path, body = action
    assert method in ("GET", "POST") and path.startswith("/") and not path.startswith("//")
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        world + path,
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    for attempt in range(TRANSPORT_RETRIES + 1):
        try:
            try:
                response = opener.open(request, timeout=REQUEST_TIMEOUT_SEC)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status = response.code
                reply = json.load(response)
            print(method, path, status, flush=True)
            assert isinstance(reply, dict)
            return status, reply
        except (OSError, urllib.error.URLError) as error:
            print(f"transport error: {method} {path}: {error}", file=sys.stderr)
            if attempt == TRANSPORT_RETRIES:
                raise SystemExit(3) from error
            time.sleep(RETRY_PAUSE_SEC)
    raise AssertionError("unreachable")


def main(policies):
    world = os.environ["WORLD"].rstrip("/")
    seat = os.environ["SEAT"]
    token = os.environ["SEAT_TOKEN"]
    assert len(sys.argv) == 2 and sys.argv[1] in policies
    player = policies[sys.argv[1]](seat)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    reply = None
    for index in range(MAX_REQUESTS + 1):
        try:
            action = next(player) if index == 0 else player.send(reply)
        except StopIteration:
            return
        if index == MAX_REQUESTS:
            raise SystemExit("request limit exceeded")
        reply = exchange(opener, world, token, action)
    raise AssertionError("unreachable")
