"""Materialize labeled documents, sealed keys, manifests, and trajectory labels."""

from __future__ import annotations

import json
from pathlib import Path

from . import _extra_docs
from .keys_data import FAMILY_KEYS
from .labels_data import LABELS
from .schema import make_answer_key, make_trajectory_label

SEED_META = {
    "checkout-pool-exhaustion": {
        "01-empty": (
            "empty",
            "harbor-practice/experiments/judged-output/negative-controls/checkout-pool-exhaustion/01-empty.md",
        ),
        "02-style-only-fluent-generic": (
            "style-only-fluent",
            "harbor-practice/experiments/judged-output/negative-controls/checkout-pool-exhaustion/02-generic.md",
        ),
        "03-copied-evidence-logs": (
            "copied-evidence",
            "harbor-practice/experiments/judged-output/negative-controls/checkout-pool-exhaustion/03-copied-logs.md",
        ),
        "04-subtly-wrong-cause-vendor": (
            "subtly-wrong-cause",
            "harbor-practice/experiments/judged-output/negative-controls/checkout-pool-exhaustion/04-invented-cause.md",
        ),
        "05-right-cause-useless-actions-tbd": (
            "right-cause-useless-actions",
            "harbor-practice/experiments/judged-output/negative-controls/checkout-pool-exhaustion/05-no-actions.md",
        ),
        "06-correct-oracle": (
            "correct",
            "harbor-practice/datasets/judged-output/checkout-pool-exhaustion/solution/solve.sh",
        ),
    },
    "retry-storm-backlog": {
        "01-empty": (
            "empty",
            "harbor-practice/experiments/judged-output/negative-controls/retry-storm-backlog/01-empty.md",
        ),
        "02-style-only-fluent-generic": (
            "style-only-fluent",
            "harbor-practice/experiments/judged-output/negative-controls/retry-storm-backlog/02-generic.md",
        ),
        "03-copied-evidence-logs": (
            "copied-evidence",
            "harbor-practice/experiments/judged-output/negative-controls/retry-storm-backlog/03-copied-logs.md",
        ),
        "04-subtly-wrong-cause-deploy": (
            "subtly-wrong-cause",
            "harbor-practice/experiments/judged-output/negative-controls/retry-storm-backlog/04-invented-cause.md",
        ),
        "05-right-cause-useless-actions-tbd": (
            "right-cause-useless-actions",
            "harbor-practice/experiments/judged-output/negative-controls/retry-storm-backlog/05-no-actions.md",
        ),
        "06-correct-oracle": (
            "correct",
            "harbor-practice/datasets/judged-output/retry-storm-backlog/solution/solve.sh",
        ),
    },
}

EXTRA_BY_FAMILY = {
    "checkout-pool-exhaustion": _extra_docs.CHECKOUT,
    "retry-storm-backlog": _extra_docs.RETRY,
}


def calibration_dir() -> Path:
    return Path(__file__).resolve().parent


def write_extra_documents() -> None:
    root = calibration_dir()
    for family, docs in EXTRA_BY_FAMILY.items():
        family_dir = root / family
        family_dir.mkdir(parents=True, exist_ok=True)
        for doc_id, (variant, body) in docs.items():
            path = family_dir / f"{doc_id}.md"
            comment = f"<!-- calibration-variant: {variant} -->\n"
            text = body if body.startswith("<!-- calibration-variant:") else comment + body
            path.write_text(text, encoding="utf-8")


def write_manifests() -> None:
    root = calibration_dir()
    for family in SEED_META:
        documents = []
        seen: set[str] = set()
        for doc_id, (variant, source) in SEED_META[family].items():
            documents.append(
                {
                    "id": doc_id,
                    "path": f"{doc_id}.md",
                    "variant": variant,
                    "source": source,
                }
            )
            seen.add(doc_id)
        for doc_id, (variant, _body) in EXTRA_BY_FAMILY[family].items():
            if doc_id in seen:
                raise ValueError(f"duplicate doc id {family}/{doc_id}")
            documents.append(
                {
                    "id": doc_id,
                    "path": f"{doc_id}.md",
                    "variant": variant,
                    "source": "authored-for-calibration",
                }
            )
        payload = {"family": family, "documents": documents}
        (root / family / "corpus.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )


def write_answer_keys() -> None:
    root = calibration_dir()
    for family, builders in FAMILY_KEYS.items():
        key_dir = root / family / "answer-keys"
        key_dir.mkdir(parents=True, exist_ok=True)
        manifest = json.loads((root / family / "corpus.json").read_text(encoding="utf-8"))
        by_id = {entry["id"]: entry for entry in manifest["documents"]}
        for doc_id, builder in builders.items():
            entry = by_id[doc_id]
            key = make_answer_key(
                family=family,
                doc_id=doc_id,
                variant=entry["variant"],
                source=entry.get("source"),
                verdicts=builder(),
            )
            (key_dir / f"{doc_id}.json").write_text(
                json.dumps(key, indent=2) + "\n", encoding="utf-8"
            )


def write_trajectory_labels() -> None:
    label_dir = calibration_dir() / "trajectory-labels"
    label_dir.mkdir(parents=True, exist_ok=True)
    for trial_name, fields in LABELS.items():
        payload = make_trajectory_label(trial_name=trial_name, **fields)
        (label_dir / f"{trial_name}.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )


def main() -> int:
    write_extra_documents()
    write_manifests()
    write_answer_keys()
    write_trajectory_labels()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
