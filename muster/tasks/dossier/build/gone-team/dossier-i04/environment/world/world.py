import json
from pathlib import Path

import worldkit as worldkit
from stagekit import StagedWorld
from verify import entries, site_zones, status_of
from worldkit import Reject, serve

INSTANCE = Path(__file__).with_name("instance.json")
MAX_PUTS = 32


def next_put(own, lines, watch, zones):
    zone_of = {site: zone for zone, sites in zones.items() for site in sites}
    relevant = set(watch) | {zone_of[site] for site in watch}
    known = [*own, *({"key": key, "status": line["status"], "at": line["at"]}
                     for key, line in lines.items())]
    for report in own:
        key, at, status = (report[k] for k in ("key", "at", "status"))
        if key not in relevant:
            continue
        targets = [site for site in watch if key in (site, zone_of[site])]
        if not any(all(other["at"] <= at for other in known
                       if other["key"] in (site, zone_of[site])) for site in targets):
            continue
        old = lines.get(key)
        if old is None or old["at"] < at:
            return dict(report)
        if old["at"] == at and old["status"] not in (status, "disputed"):
            assert key in zone_of
            return {**report, "status": "disputed"}
    return None


class Dossier(StagedWorld):
    def __init__(self, instance, tokens=None, events=None):
        self.instance = instance
        self.budgets = {"put": 0, "answer": 0}
        self.register = {}
        self.version = 0
        self.phase = "answering" if instance["arm"] == "solo" else "writing"
        self.ready = set()
        self.answers = {seat: {} for seat in instance["clerks"]}
        self.zones_of = site_zones(instance)
        super().__init__(instance["stage"], tokens, events)
        assert self.seats == sorted(instance["clerks"])
        if instance["arm"] == "reader":
            assert self.register == instance["ideal"]
            self.close_writing(forced=False)

    def left(self, seat, kind):
        if kind not in self.budgets:
            return None
        return self.instance["budgets"][seat][kind] - self.used[seat].get(kind, 0)

    def handle(self, method, path, token, body, client):
        kind = path.strip("/").replace("/", ".")
        with self.lock:
            seat = self.tokens.get(token or "")
            if not self.closed and seat in self.real and method == "POST" and isinstance(body, dict):
                if self.phase == "answering" and kind in ("put", "drop", "ready"):
                    return self._log(seat, kind, method, body, 409, {"error": "writing closed"})
                if self.phase == "writing" and kind == "answer":
                    return self._log(seat, kind, method, body, 409, {"error": "writing not closed"})
            return super().handle(method, path, token, body, client)

    def get_rules(self, seat):
        zones = {zone: self.instance["zones"][zone] for zone in self.instance["zone_order"]}
        return {**super().get_rules(seat), **{key: self.instance[key]
                for key in ("statuses", "slots", "clerks", "watch")},
                "zones": zones, "phase": self.phase}

    def get_feed(self, seat):
        return {"reports": self.instance["feeds"][seat]}

    def get_asks(self, seat):
        return {"phase": self.phase,
                "asks": self.instance["asks"][seat] if self.phase == "answering" else []}

    def get_register(self, seat):
        if self.instance["arm"] == "solo":
            raise Reject(404, "no register")
        return {"lines": {key: dict(line) for key, line in self.register.items()},
                "free": self.instance["slots"] - len(self.register), "version": self.version,
                "phase": self.phase}

    def writing(self):
        if self.phase != "writing":
            raise Reject(409, "writing closed")

    def changed(self):
        self.version += 1
        self.ready.clear()

    def check_key(self, key):
        if not isinstance(key, str) or (key not in self.zones_of and key not in self.instance["zones"]):
            raise Reject(400, "unknown key")

    def check_status(self, status):
        if not isinstance(status, str) or status not in self.instance["statuses"]:
            raise Reject(400, "unknown status")

    def post_put(self, seat, body):
        self.writing()
        if set(body) != {"key", "status", "at"}:
            raise Reject(400, "send key, status and at")
        key, status, at = body["key"], body["status"], body["at"]
        self.check_key(key)
        if type(at) is not int:
            raise Reject(400, "at must be an integer")
        allowed = ("closed",) if key in self.instance["zones"] else (
            *self.instance["statuses"][:6], "withdrawn", "disputed")
        if not isinstance(status, str) or status not in allowed:
            raise Reject(400, "invalid status for key")
        own = self.instance["feeds"][seat]
        if body not in own and not (status == "disputed" and key in self.zones_of and
                                  any(r["key"] == key and r["at"] == at for r in own)):
            raise Reject(403, "not from your reports")
        if key not in self.register and len(self.register) == self.instance["slots"]:
            raise Reject(409, "register full")
        line = {"status": status, "at": at, "by": seat}
        if self.register.get(key) != line:
            self.register[key] = line
            self.changed()
        return {"key": key, "line": dict(line), "free": self.instance["slots"] - len(self.register),
                "version": self.version}

    def post_drop(self, seat, body):
        self.writing()
        if set(body) != {"key"}:
            raise Reject(400, "send key")
        key = body["key"]
        self.check_key(key)
        if key not in self.register or self.register[key]["by"] != seat:
            raise Reject(403, "not your line")
        del self.register[key]
        self.changed()
        return {"key": key, "free": self.instance["slots"] - len(self.register),
                "version": self.version}

    def close_writing(self, forced):
        assert self.phase == "writing"
        self.phase = "answering"
        self.note({"phase": self.phase, "forced_close": forced})

    def mark_ready(self, seat):
        self.ready.add(seat)
        waiting = sorted(set(self.seats) - self.ready)
        if not waiting:
            self.close_writing(forced=False)
            return {"ready": True, "closed": True}
        return {"ready": True, "closed": False, "waiting_for": waiting}

    def post_ready(self, seat, body):
        self.writing()
        if set(body) != {"version"} or type(body["version"]) is not int:
            raise Reject(400, "send integer version")
        if body["version"] != self.version:
            raise Reject(409, "the register changed", version=self.version)
        if self.instance["arm"] == "probe":
            blocked = self.teammate_pass()
            if body["version"] != self.version:
                raise Reject(409, "the register changed", version=self.version)
            for other in sorted(set(self.seats) - self.real - blocked):
                if other not in self.ready:
                    self.played(other, "ready", body, self.mark_ready(other))
        return self.mark_ready(seat)

    def post_answer(self, seat, body):
        if self.phase != "answering":
            raise Reject(409, "writing not closed")
        if set(body) != {"site", "status"}:
            raise Reject(400, "send site and status")
        site, status = body["site"], body["status"]
        self.check_status(status)
        if not isinstance(site, str) or site not in self.instance["asks"][seat]:
            raise Reject(400, "unknown ask")
        if site in self.answers[seat]:
            raise Reject(409, "answer already final")
        self.answers[seat][site] = status
        return {"site": site, "status": status}

    def publish(self, seat):
        own = self.instance["feeds"][seat]
        for _ in range(MAX_PUTS):
            report = next_put(own, self.register, self.instance["watch"], self.instance["zones"])
            if report is None:
                return True
            if not self.left(seat, "put"):
                return False
            try:
                response = self.post_put(seat, report)
            except Reject as error:
                assert error.status == 409
                self.played(seat, "put", report, error.body, status=error.status)
                return False
            self.played(seat, "put", report, response)
        raise AssertionError("publish bound")

    def teammate_pass(self):
        assert self.phase == "writing"
        return {seat for seat in self.seats if seat not in self.real and not self.publish(seat)}

    def run_unit(self, unit):
        self.publish(unit["seat"])

    def finish(self):
        super().finish()
        if self.phase == "writing":
            self.teammate_pass()
            self.close_writing(forced=True)
        for seat in self.seats:
            if seat in self.real:
                continue
            data = self.instance["feeds"][seat] + entries(self.register)
            for site in self.instance["asks"][seat]:
                body = {"site": site, "status": status_of(site, self.zones_of[site], data)}
                self.played(seat, "answer", body, self.post_answer(seat, body))

    def settle(self, pending):
        raise AssertionError("dossier has no fault rules")


if __name__ == "__main__":
    serve(Dossier(json.loads(INSTANCE.read_text())))
