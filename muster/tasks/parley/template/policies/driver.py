import os
import sys
import time
import urllib.error
import urllib.request


def exchange(opener, world, token, action):
    method, path, body = action
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(world + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        response = opener.open(request, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        status = response.code
        reply = json.load(response)
    assert isinstance(reply, dict)
    return status, reply


def main(policies):
    world = os.environ["WORLD"].rstrip("/")
    seat = os.environ["SEAT"]
    token = os.environ["SEAT_TOKEN"]
    assert len(sys.argv) == 2 and sys.argv[1] in policies
    player = policies[sys.argv[1]](seat)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    reply = None
    for index in range(3001):
        try:
            action = next(player) if index == 0 else player.send(reply)
        except StopIteration:
            return
        assert index < 3000
        if action[:2] == ("GET", "/rules") and index:
            time.sleep(0.1)
        reply = exchange(opener, world, token, action)
    raise AssertionError("request limit")
