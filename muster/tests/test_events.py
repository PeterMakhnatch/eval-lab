"""Exact measures over a world event log."""

from muster.events import in_play, jain, summarize


class Log:
    """Builds kit-shaped events with a clock that the test controls."""

    def __init__(self):
        self.events = []
        self.messages = []

    def _add(self, t, seat, kind, method, status, request=None, response=None, **extra):
        self.events.append(
            {
                "seq": len(self.events) + 1,
                "t": float(t),
                "seat": seat,
                "kind": kind,
                "method": method,
                "status": status,
                "ok": status == 200,
                "request": request,
                "response": response or {},
                **extra,
            }
        )

    def open(self, t, seat, box, status=200):
        self._add(t, seat, "open", "POST", status, {"box": box}, {"box": box})

    def post(self, t, seat, text):
        message = {"id": len(self.messages) + 1, "from": seat, "text": text}
        self.messages.append(message)
        self._add(t, seat, "msg", "POST", 200, {"text": text}, message)

    def fetch(self, t, seat):
        self._add(t, seat, "msgs", "GET", 200, None, {"messages": list(self.messages)})

    def probe(self, t, seat, kind):
        self._add(t, seat, kind, "GET", 404, None, {"error": "no such route"})

    def close(self, t):
        self._add(t, None, "admin.close", "POST", 200, {}, {"closed": True})

    def late(self, t, seat, box):
        self._add(t, seat, "open", "POST", 409, {"box": box}, {"error": "play is over"}, late=True)


def test_counts_and_the_close():
    log = Log()
    log.open(10, "p1", 1)
    log.post(11, "p1", "box 1 is empty")
    log.fetch(12, "p2")
    log.open(13, "p2", 2)
    log.probe(14, "p2", "team")
    log.open(15, "p2", 3, status=429)
    log.events.append(
        {"seq": 7, "t": 15.5, "seat": None, "kind": "open", "method": "POST", "status": 401, "ok": False}
    )
    log.close(16)
    log.late(17, "p1", 9)
    report = summarize(log.events, seats=["p1", "p2", "p3"])
    assert [event["seq"] for event in in_play(log.events)] == [1, 2, 3, 4, 5, 6]
    assert (report["actions"], report["posts"], report["fetches"]) == (2, 1, 1)
    assert (report["probes"], report["refused"], report["unauthorized"], report["late"]) == (1, 1, 1, 1)
    assert report["communication_share"] == 0.5
    assert report["never_acted"] == ["p3"]
    assert report["per_seat"]["p2"] == {
        "requests": 4,
        "actions": 1,
        "posts": 0,
        "fetches": 1,
        "probes": 1,
        "first_at": 2.0,
        "last_at": 5.0,
    }
    assert report["first_post_at"] == 1.0 and report["first_fetch_at"] == 2.0
    assert report["actions_before_first_post"] == 1


def test_a_sent_message_is_not_a_received_message():
    log = Log()
    log.post(10, "p1", "room 1 is empty")
    log.open(11, "p2", 5)  # p2 acts before it fetches: 1 unread message
    log.fetch(12, "p2")
    log.open(13, "p2", 6)  # now p2 has received it
    log.open(14, "p3", 7)  # p3 never fetches
    report = summarize(log.events, seats=["p1", "p2", "p3"])
    assert report["message_pairs"] == 2  # 1 message, 2 other seats
    assert report["never_fetched"] == 1
    assert report["fetch_lag_median"] == 2.0
    assert report["actions_with_unread"] == 2
    assert report["unread_at_action_mean"] == 2 / 3


def test_duplicates_split_by_what_the_actor_had_received():
    log = Log()
    log.open(10, "p1", 4)
    log.open(11, "p2", 4)  # duplicate, p1 said nothing yet: not informed
    log.post(12, "p1", "I opened box 4")
    log.open(13, "p3", 4)  # duplicate, p3 did not fetch: not informed
    log.fetch(14, "p3")
    log.open(15, "p3", 4)  # p3 repeats its own act: its first act was the duplicate, this one too
    log.open(16, "p1", 4)  # p1 repeats its own act: never a duplicate of another seat
    report = summarize(log.events)
    assert report["duplicate_actions"] == 3
    assert report["informed_duplicates"] == 1
    assert report["identical_first_action"] is True


def test_a_message_posted_before_the_act_does_not_make_the_duplicate_informed():
    log = Log()
    log.post(9, "p1", "hello")
    log.open(10, "p1", 4)
    log.fetch(11, "p2")
    log.open(12, "p2", 4)
    assert summarize(log.events)["informed_duplicates"] == 0


def test_distinct_first_actions_and_evenness():
    log = Log()
    for t, seat, box in ((1, "p1", 1), (2, "p2", 2), (3, "p3", 3), (4, "p1", 4)):
        log.open(t, seat, box)
    report = summarize(log.events)
    assert report["identical_first_action"] is False
    assert report["duplicate_actions"] == 0
    assert abs(report["action_evenness"] - 16 / 18) < 1e-12
    assert report["communication_share"] == 0.0


def test_jain_limits():
    assert jain([2.0, 2.0, 2.0]) == 1.0
    assert abs(jain([3.0, 0.0, 0.0]) - 1 / 3) < 1e-12
    assert jain([]) is None and jain([0.0, 0.0]) is None


def test_an_empty_log_gives_empty_measures():
    report = summarize([], seats=["p1"])
    assert report["requests"] == 0 and report["communication_share"] is None
    assert report["never_acted"] == ["p1"] and report["per_seat"]["p1"]["first_at"] is None
