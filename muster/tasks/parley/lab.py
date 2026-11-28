from contextlib import nullcontext
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from muster.lab.load import module
from muster.tasks.parley import instances as inventory
from muster.tasks.parley.zoo import POLICIES

NAME = "parley"
ROOT = Path(__file__).resolve().parent
KIT = ROOT.parent / "_kit"
WORLD = module(ROOT / "template/world.py", "parley_world", siblings=(KIT,))
VERIFY = module(ROOT / "template/verify.py", "parley_verify", siblings=(KIT,))
conditions = inventory.conditions
instances = inventory.instances
score = VERIFY.score
replay = VERIFY.replay
CARD_FREE_KINDS = ("batch", "ready")


class BufferedEventLog:
    def __init__(self, path):
        self.path = path
        self.parent = path.parent
        self.stream = StringIO()

    def write_text(self, text):
        assert text == ""
        return self.path.write_text(text)

    def open(self, mode):
        assert mode == "a"
        return nullcontext(self.stream)

    def publish(self):
        self.path.write_text(self.stream.getvalue())


class LabParley(WORLD.Parley):
    def handle(self, method, path, token, body, client):
        result = super().handle(method, path, token, body, client)
        if self.closed:
            self.events_path.publish()
        return result


def make_world(instance, events, clock):
    WORLD.worldkit.time = SimpleNamespace(time=clock)
    return LabParley(instance, {token: seat for seat, token in instance["tokens"].items()}, BufferedEventLog(events))


def seats(instance):
    return list(instance["real"])


def state(world):
    return world.state()


def policies():
    return dict(POLICIES)


def hidden(instance, seat):
    return [token for clerk, token in instance["tokens"].items() if clerk != seat]


def sources():
    paths = [ROOT / name for name in ("family.toml", "generate.py", "instances.py", "lab.py", "zoo.py", "selfcheck.py")]
    paths += [path for path in (ROOT / "template").rglob("*") if path.is_file() and "__pycache__" not in path.parts]
    paths += [KIT / name for name in ("worldkit.py", "build.py")]
    return sorted(paths)


def cells(facets, arms):
    return [f"{facet}-{arm}" for facet in facets for arm in arms]


def claim(id, mode, policy, conditions, value, measure="reward"):
    return {"id": id, "type": mode, "policy": policy, "conditions": conditions,
            "value": value, "measure": measure}


def claims():
    team_arms = ("team", "open")
    calm = cells(("calm",), inventory.ARMS)
    hypotheses = cells(inventory.FACETS[1:], team_arms)
    initial = cells(("calm", "shared", "tight"), inventory.ARMS)
    late = cells(("late", "storm"), inventory.ARMS)
    return [claim("oracle-all", "equals", "oracle", conditions(), 1.0),
            claim("oracle-ends", "equals", "oracle", conditions(), 1.0, "ended"),
            claim("own-calm", "equals", "own", calm, 1.0, "focal"),
            claim("own-shared-wrong", "equals", "own", hypotheses, 0.0, "right.shared"),
            claim("sharer-misses", "below", "sharer", cells(inventory.FACETS[1:], ("team",)), 1.0),
            claim("blindready-no-late", "equals", "blindready", initial, 1.0),
            claim("blindready-late-stale", "equals", "blindready", late, 1.0, "stale"),
            claim("hog-roomy", "equals", "hog", cells(("shared", "late"), team_arms), 1.0),
            claim("hog-tight", "at_most", "hog", cells(("tight", "storm"), team_arms), 11 / 12),
            claim("blindready-solo", "at_most", "blindready", cells(("late", "storm"), ("solo",)), 11 / 12),
            {"id": "oracle-no-budget-refusal", "type": "no_error_status", "policy": "oracle",
             "conditions": conditions(), "status": 429}]
