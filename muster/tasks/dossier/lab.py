from pathlib import Path
from types import SimpleNamespace

from muster.lab import load
from muster.tasks.dossier import instances as inventory
from muster.tasks.dossier.findings import policies as shortcuts
from muster.tasks.dossier.template import verify
from muster.tasks.dossier.zoo import ALL_SKILLS, POLICIES

NAME = "dossier"
CARD_FREE_KINDS = ("put", "drop", "ready")
ROOT = Path(__file__).resolve().parent
KIT = ROOT.parent / "_kit"
WORLD = load.module(ROOT / "template" / "world.py", "dossier_world", siblings=(KIT,))
conditions = inventory.conditions
instances = inventory.instances
score = verify.score
replay = verify.replay


def seats(instance):
    return list(instance["real"])


def make_world(instance, events, clock):
    WORLD.worldkit.time = SimpleNamespace(time=clock)
    return WORLD.Dossier(instance, {instance["tokens"][seat]: seat for seat in seats(instance)}, events)


def policies():
    return {**POLICIES, **{name: getattr(shortcuts, name)
                         for name in ("feed_order", "ascending", "reverse")}}


def hidden(instance, seat):
    return [token for other, token in instance["tokens"].items() if other != seat]


def budget(instance):
    return sum(sum(values.values()) for values in instance["budgets"].values())


def state(world):
    return {"register": world.register, "answers": world.answers, "version": world.version,
            "phase": world.phase, "ready": sorted(world.ready)}


def sources():
    core = [ROOT / name for name in ("instances.py", "zoo.py", "lab.py", "selfcheck.py")]
    template = [path for path in (ROOT / "template").rglob("*") if path.is_file()
                and "__pycache__" not in path.parts and not path.name.startswith(".")]
    packaging = [ROOT / name for name in ("family.toml", "generate.py", "reference.py")
                 if (ROOT / name).is_file()]
    findings = [path for path in (ROOT / "findings").iterdir() if path.is_file()]
    return sorted(core + template + packaging + findings + [KIT / name for name in
                  ("worldkit.py", "stagekit.py", "build.py")])


def arm_cells(*arms):
    return [condition for condition in conditions() if condition.rsplit("-", 1)[1] in arms]


def claim(id, mode, policy, cells, value, measure="reward"):
    return {"id": id, "type": mode, "policy": policy, "conditions": cells,
            "measure": measure, "value": value}


def skill_claim():
    skills = {"hoarder": [], "pooler": ["pool"], "selector": ["pool", "select"],
              "burier": ["pool", "tombstone"], "closer": ["pool", "cascade"],
              "umpire": ["pool", "dispute"], "oracle": list(ALL_SKILLS)}
    needs = {"share": ["pool"], "scarce": ["pool", "select"], "gone": ["pool", "tombstone"],
             "cascade": ["pool", "cascade"], "dispute": ["pool", "dispute"],
             "mix": list(ALL_SKILLS)}
    return {"id": "probe-skills", "type": "skills", "policies": skills,
            "conditions": {f"{facet}-probe": required for facet, required in needs.items()},
            "measure": "reward"}


def claims():
    special = ("gone", "cascade", "dispute", "mix")
    scarce = ["scarce-probe", "mix-probe"]
    return [
        claim("1-oracle-ceiling", "equals", "oracle", conditions(), 1.0),
        claim("1-oracle-register-sufficient", "equals", "oracle", conditions(), 1.0,
              measure="register_sufficient"),
        skill_claim(),
        claim("3-cleaner-ceiling", "equals", "cleaner", scarce, 1.0),
        claim("3-cleaner-peak", "equals", "cleaner", scarce, 1.0,
              measure="used_off_watch"),
        *[claim(f"v3-{name}-capacity", "below", name, scarce, 1.0)
          for name in ("ascending", "reverse", "feed_order")],
        *[claim(f"v3-{name}-dispute", "below", name, ["dispute-probe"], 1.0)
          for name in ("ascending", "feed_order")],
        claim("4-own-first-miss", "below", "own_first", arm_cells("probe", "reader"), 1.0),
        claim("5-reader-pooler-miss", "below", "pooler", [f"{f}-reader" for f in special], 1.0),
        *[claim(f"5-reader-{policy}", "equals", policy, [f"{facet}-reader"], 1.0)
          for policy, facet in (("burier", "gone"), ("closer", "cascade"), ("umpire", "dispute"))],
        claim("6-solo-pooler-miss", "below", "pooler", [f"{f}-solo" for f in special], 1.0),
        claim("7-team-hoarder-miss", "below", "hoarder", arm_cells("team"), 1.0),
        claim("8-idle-floor", "equals", "idle", conditions(), 0.0, measure="focal"),
        claim("8-spam-floor", "at_most", "spam", arm_cells("probe"), 0.5, measure="focal"),
        {"id": "8-fuzz-no-500", "type": "no_error_status", "policy": "fuzz",
         "conditions": conditions(), "status": 500},
    ]
