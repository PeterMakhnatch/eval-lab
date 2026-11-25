from contextlib import nullcontext
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from instances import ARMS, FACETS, conditions, instances
from zoo import POLICIES

from muster.lab.load import module

NAME = "depot"
ROOT = Path(__file__).resolve().parent
KIT = ROOT.parent / "_kit"
WORLD = module(ROOT / "template/world.py", "depot_world", siblings=(KIT,))
VERIFY = module(ROOT / "template/verify.py", "depot_verify", siblings=(KIT,))
SKILLS = {"blind": [], "cas": ["cond"], "cas_readset": ["cond", "readset"],
          "cas_dedupe": ["cond", "dedupe"], "oracle": ["cond", "readset", "dedupe"]}
NEEDS = {"calm": [], "clash": ["cond"], "skew": ["cond", "readset"],
         "limbo": ["cond", "dedupe"], "takeover": ["cond", "dedupe"],
         "storm": ["cond", "readset", "dedupe"]}
CARD_FREE_KINDS = ()


def sources() -> list[Path]:
    paths = [KIT / name for name in ("stagekit.py", "worldkit.py", "build.py")]
    paths += [path for path in ROOT.iterdir() if path.is_file() and path.suffix in (".py", ".toml")]
    paths += [path for path in (ROOT / "template").rglob("*") if path.is_file()
              and "__pycache__" not in path.parts]
    paths += [path for path in (ROOT / "findings").glob("*") if path.suffix in (".py", ".json")]
    return sorted(paths)


def seats(instance):
    return list(instance["real"])


class BufferedEventLog:
    def __init__(self, path):
        self.path = path
        self.parent = path.parent
        self.stream = StringIO()

    def write_text(self, text):
        assert text == ""
        self.stream = StringIO()
        return self.path.write_text(text)

    def open(self, mode):
        assert mode == "a"
        return nullcontext(self.stream)

    def publish(self):
        self.path.write_text(self.stream.getvalue())


class LabDepot(WORLD.Depot):
    def handle(self, method, path, token, body, client):
        result = super().handle(method, path, token, body, client)
        if self.closed:
            self.events_path.publish()
        return result


def make_world(instance, events, clock):
    WORLD.worldkit.time = SimpleNamespace(time=clock)
    return LabDepot(instance, {token: seat for seat, token in instance["tokens"].items()},
                    BufferedEventLog(events))


def score(events, instance):
    return VERIFY.score(events, instance)


def policies():
    return dict(POLICIES)


def hidden(instance, seat):
    return [token for other, token in instance["tokens"].items() if other != seat]


def budget(instance):
    return sum(instance["write_budget"].values())


def state(world):
    return {"book": world.book, "writes": world.writes, "declined": world.declined}


def replay(events, instance):
    return VERIFY.replay(events, instance)


def claim(kind, policy, cells, value=1.0, measure="reward"):
    return {"id": f"{kind}-{policy}-{measure}-{'-'.join(cells)}", "type": kind,
            "policy": policy, "conditions": cells, "measure": measure, "value": value}


def cell_list(facets, arm):
    return [f"{facet}-{arm}" for facet in facets]


def near_misses():
    result = [claim("below", policy, cell_list(FACETS[1:], "probe")) for policy in ("reread", "follower")]
    result += [claim("below", policy, cell_list(("limbo", "storm"), "probe"))
               for policy in ("cas_version", "cas_value", "cas_check_once", "cas_dedupe_first")]
    result += [claim("below", policy, cell_list(("takeover", "storm"), "probe"))
               for policy in ("cas_check_once", "cas_dedupe_first", "cas_dedupe_ticket")]
    return result


def baseline_floor_bound(instance):
    staged = {(unit["ticket"], item) for unit in instance["stage"]["units"]
              for ticket in instance["tickets"] if ticket["id"] == unit["ticket"]
              for item in ([unit["item"]] if "item" in unit else VERIFY.legs(ticket))}
    pair_items = {item for pair in instance["pairs"] for item in pair["items"]}
    focal = set().union(*(VERIFY.queue_groups(instance, seat) for seat in instance["real"]))
    single = focal - pair_items
    complete = sum(all((ticket["id"], item) in staged for ticket in instance["tickets"]
                       if item in VERIFY.legs(ticket)) for item in single)
    return complete / len(focal)


def floor_claims():
    result = [claim("at_most", policy, conditions(), 0.25, measure="focal")
              for policy in ("spam", "decliner")]
    groups = {}
    for condition in conditions():
        bounds = {baseline_floor_bound(row) for row in instances(condition)}
        assert len(bounds) == 1
        groups.setdefault(next(iter(bounds)), []).append(condition)
    result.extend(claim("equals", "idle", cells, bound, measure="focal")
                  for bound, cells in groups.items())
    return result


def claims():
    result = [claim("equals", "oracle", conditions()), claim("equals", "oracle", conditions(), measure="clean"),
              {"id": "probe-skills", "type": "skills", "policies": SKILLS,
               "conditions": {f"{facet}-probe": needs for facet, needs in NEEDS.items()}, "measure": "reward"}]
    result += near_misses()
    result += [claim("equals", "repair", ["clash-probe"]),
               claim("below", "repair", ["clash-probe"], measure="clean")]
    result += [claim("equals", policy, cell_list(FACETS, "guard"))
               for policy in ("follower", "cas", "cas_readset", "cas_dedupe", "oracle")]
    result += [claim("equals", policy, cell_list(FACETS, "solo")) for policy in ("cas_dedupe", "oracle")]
    result += [claim("equals", "blind", cell_list(FACETS[:4], "solo")),
               claim("below", "blind", cell_list(("takeover", "storm"), "solo")),
               claim("below", "cas", cell_list(("limbo", "takeover", "storm"), "solo"))]
    for policy, skills in SKILLS.items():
        cells = [f"{facet}-team" for facet, needs in NEEDS.items() if not set(needs) <= set(skills)]
        if cells:
            result.append(claim("below", policy, cells))
    result += floor_claims()
    result += [{"id": "equal-facet-budget", "type": "equal_budget",
                "groups": [[f"{facet}-{arm}" for arm in ARMS] for facet in FACETS]},
               {"id": "fuzz-no-500", "type": "no_error_status", "policy": "fuzz",
                "conditions": conditions(), "status": 500},
               {"id": "oracle-no-429", "type": "no_error_status", "policy": "oracle",
                "conditions": conditions(), "status": 429}]
    return result
