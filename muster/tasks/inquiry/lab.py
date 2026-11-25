from contextlib import nullcontext
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from muster.lab.load import module
from muster.tasks.inquiry.instances import ARMS, FACETS, conditions
from muster.tasks.inquiry.instances import instances as instances
from muster.tasks.inquiry.zoo import POLICIES, SKILLS

NAME = "inquiry"
ROOT = Path(__file__).resolve().parent
KIT = ROOT.parent / "_kit"
WORLD = module(ROOT / "template/world.py", "inquiry_world", siblings=(KIT,))
VERIFY = module(ROOT / "template/verify.py", "inquiry_verify", siblings=(KIT,))
CARD_FREE_KINDS = ("ask", "correct")
NEEDS = {"plain": [], "month": ["match"], "part": ["cover"], "range": ["exact"], "silent": ["retry"],
         "mixed": ["match", "cover", "exact", "convert", "recheck"]}


class BufferedEventLog:
    def __init__(self, path):
        self.path = path
        self.parent = path.parent
        self.stream = StringIO()

    def write_text(self, text):
        assert text == ""
        self.stream = StringIO()
        return 0

    def open(self, mode):
        assert mode == "a"
        return nullcontext(self.stream)

    def publish(self):
        self.path.write_text(self.stream.getvalue())


class LabInquiry(WORLD.Inquiry):
    def handle(self, method, path, token, body, client):
        result = super().handle(method, path, token, body, client)
        if self.closed:
            self.events_path.publish()
        return result


def sources():
    return [KIT / "worldkit.py", KIT / "build.py", ROOT / "lab.py", ROOT / "instances.py",
            ROOT / "zoo.py", ROOT / "selfcheck.py", ROOT / "generate.py", ROOT / "family.toml",
            *sorted(path for path in (ROOT / "template").rglob("*")
                    if path.is_file() and "__pycache__" not in path.parts),
            *sorted(path for path in (ROOT / "findings").iterdir()
                    if path.is_file() and path.suffix in (".py", ".json"))]


def seats(instance):
    return list(instance["real"])


def make_world(instance, events, clock):
    WORLD.worldkit.time = SimpleNamespace(time=clock)
    return LabInquiry(instance, {token: seat for seat, token in instance["tokens"].items()},
                      BufferedEventLog(events))


def score(events, instance):
    return VERIFY.score(events, instance)


def policies():
    return dict(POLICIES)


def hidden(instance, seat):
    return [token for other, token in instance["tokens"].items() if other != seat]


def budget(instance):
    assert instance["question_budget"] == 13 and instance["file_budget"] == 8
    return instance["question_budget"] + instance["file_budget"]


def state(world):
    return world.desk.state()


def replay(events, instance):
    return VERIFY.replay(events, instance)


def cells(facets, arms):
    return [f"{facet}-{arm}" for facet in facets for arm in arms]


def claim(identifier, kind, policy, selected, value=1.0):
    return {"id": identifier, "type": kind, "policy": policy, "conditions": selected,
            "measure": "reward", "value": value}


def near_misses():
    selections = {"midpoint": ("range", "mixed"),
                  "repeater": ("month", "part", "range", "mixed"),
                  "fix_all": ("month", "part", "range", "mixed")}
    return [claim(f"04-{policy}", "below", policy, cells(facets, ("probe", "fields")))
            for policy, facets in selections.items()]


def fixed_misses():
    selections = {"fix_month": ("part", "range", "mixed"),
                  "fix_parts": ("month", "range", "mixed"),
                  "fix_value": ("month", "part", "mixed")}
    return [claim(f"05-{policy}", "below", policy, cells(facets, ("probe", "fields")))
            for policy, facets in selections.items()]

def claims():
    return [claim("01-oracle", "equals", "oracle", conditions()),
            {"id": "02-skills", "type": "skills", "policies": SKILLS,
             "conditions": {condition: NEEDS[condition.split("-")[0]]
                            for condition in cells(FACETS, ("probe", "fields"))}, "measure": "reward"},
            claim("03-wide-pass", "equals", "wide",
                  cells(tuple(facet for facet in FACETS if facet != "month"), ("probe", "fields"))),
            claim("03-wide-month-budget", "below", "wide", cells(("month",), ("probe", "fields"))),
            claim("04-no_recheck-pass", "equals", "no_recheck", cells(FACETS[:-1], ("probe", "fields"))),
            claim("04-no_recheck-fail", "below", "no_recheck", cells(("mixed",), ("probe", "fields"))),
            claim("04-namer-pass", "equals", "namer", cells(("plain", "part", "range", "silent"), ("probe", "fields"))),
            claim("04-namer-fail", "below", "namer", cells(("month", "mixed"), ("probe", "fields"))),
            *near_misses(),
            *fixed_misses(),
            *(claim(f"06-{policy}", "equals", policy, cells(FACETS, ("flagged",)))
              for policy in ("reactive", "oracle")),
            *(claim(f"07-{policy}", "equals", policy, cells(FACETS, ("solo",)))
              for policy in ("oracle", "converter")),
            claim("07-truster", "below", "truster", ["mixed-solo"]),
            *(claim(f"08-{policy}", "equals", policy, conditions(), 0.0) for policy in ("idle", "spam")),
            {"id": "08-fuzz", "type": "no_error_status", "policy": "fuzz", "conditions": conditions(), "status": 500},
            {"id": "09-equal-budget", "type": "equal_budget",
             "groups": [cells((facet,), ARMS[:3]) for facet in FACETS]}]
