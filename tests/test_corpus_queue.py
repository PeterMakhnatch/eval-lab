"""Exercise the production journal against SQLite and an isolated local PostgreSQL.

PostgreSQL cases skip only when server binaries are not installed. The temporary
server listens solely on its private Unix socket; no shared lab database, corpus
bytes, queue, provider credentials, or paid execution are touched.
"""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

import pytest

from evallab.production.corpus_queue import (
    DATASETS,
    CorpusRegistry,
    SplitLockError,
    source_record_digest,
)

DATASET = "tb4-audit-v1"
SECONDARY_KEY = "envcheck-tb4-39"  # real secondary assignments key, not a corpus
FROZEN_TOKEN = "test-only-explicit-frozen-evaluation"
DEV = {"dataset_id": DATASET, "item_id": "finding-dev"}
TEST = {"dataset_id": DATASET, "item_id": "finding-test"}

def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _item(item_id: str, split: str, task: str, digest: str | None = None) -> dict:
    return {"item_id": item_id, "split": split, "subject": {"task_id": task, "task_digest": digest}}


@pytest.fixture
def corpus_root(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    keys = root / "research/answer-keys"
    _write_jsonl(
        keys / DATASET / "items.jsonl",
        [
            _item("finding-dev", "dev", "terminal-bench/dev-task", "sha256:dev"),
            _item("finding-test", "test", "terminal-bench/sealed-task", "sha256:sealed"),
        ],
    )
    _write_jsonl(
        keys / DATASET / "tasks.jsonl",
        [
            _item("ignored", "dev", "terminal-bench/dev-task", "sha256:dev"),
            _item("ignored", "test", "terminal-bench/sealed-task", "sha256:sealed"),
        ],
    )
    _write_jsonl(
        keys / "cheatbench-actions-v1/items.jsonl",
        [
            _item("cheat-dev", "dev", "cheatbench/independent"),
            _item("cheat-test", "test", "cheatbench/held"),
            _item("renamed-sealed", "dev", "cheatbench/renamed", "sha256:sealed"),
        ],
    )
    _write_jsonl(
        keys / "hacktrace-act-v1/items.jsonl",
        [
            _item("hack-dev", "dev", "hacktrace/independent"),
            _item("name-alias", "dev", "another-benchmark/sealed-task@v2"),
        ],
    )
    inventory_path = root / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory_path.parent.mkdir(parents=True)
    inventory_path.write_text(
        json.dumps(
            {
                "dataset_id": "mimo-defect-map-v1",
                "heldout_task_ids_excluded_from_training_all_versions": ["mimo-held"],
                "tasks": [
                    {
                        "record_id": "mimo-dev@sha256:version1",
                        "task_id": "mimo-dev",
                        "package_digest": "sha256:mimo-dev",
                        "materialization": {"locator": "/immutable/mimo-dev"},
                    },
                    {
                        "record_id": "mimo-held@sha256:version1",
                        "task_id": "mimo-held",
                        "package_digest": "sha256:mimo-held",
                    },
                    {
                        "record_id": "mimo-hidden@sha256:version1",
                        "task_id": "mimo-hidden",
                        "heldout_excluded_from_training_all_versions": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def registry(corpus_root: Path) -> CorpusRegistry:
    return CorpusRegistry(corpus_root, frozen_eval_token=FROZEN_TOKEN)




def test_registry_indexes_all_four_public_corpora_and_exact_mimo_versions(
    registry: CorpusRegistry,
) -> None:
    assert {item.dataset_id for item in registry.items()} == set(DATASETS)
    assert registry.get(DATASET, "terminal-bench/dev-task").task_id == "terminal-bench/dev-task"
    mimo = registry.get("mimo-defect-map-v1", "mimo-dev@sha256:version1")
    assert mimo.metadata["materialization"]["locator"] == "/immutable/mimo-dev"
    assert mimo.source_path.name == "inventory.json"
    with pytest.raises(KeyError):
        registry.get("mimo-defect-map-v1", "mimo-dev")


def test_bare_numeric_item_ids_seal_structurally_but_not_as_text(corpus_root: Path) -> None:
    _write_jsonl(
        corpus_root / "research/answer-keys/cheatbench-actions-v1/items.jsonl",
        [_item("44", "test", "cheatbench/numbered-held"), _item("45", "dev", "cheatbench/numbered")],
    )
    registry = CorpusRegistry(corpus_root)
    seals = registry.sealed_identifiers()
    assert "44" not in seals
    assert "numbered-held" in seals
    assert registry.sealed_identity(item_id=" 44 ") == "item_id"
    assert registry.sealed_identity(item_id="45") is None
    with pytest.raises(SplitLockError):
        registry.admit("cheatbench-actions-v1", "44")


def test_mimo_without_ledger_rows_resolves_through_inventory_names(
    registry: CorpusRegistry, corpus_root: Path,
) -> None:
    key = " MIMO-DEFECT-MAP-V1 "
    assert registry.assigned_split(key, task_id="mimo-code/MIMO-DEV@v3") == "dev"
    assert registry.assigned_split(key, item_id=" mimo-dev@sha256:version1 ") == "dev"
    assert registry.assigned_split(key, task_id="mimo-code/mimo-held") == "test"
    assert registry.assigned_split(key, task_id="mimo-hidden") == "test"
    assert registry.assigned_split(key, task_id="mimo-unknown") is None
    # Once the ledger carries the key, its explicit rows alone authorize DEV.
    ledger = corpus_root / "research/answer-keys/splits/assignments.csv"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("key_id,item_id,task,split\nmimo-defect-map-v1,other,,dev\n")
    assert CorpusRegistry(corpus_root).assigned_split(key, task_id="mimo-dev") is None



def test_registry_dev_admission_and_test_split_rejection(registry: CorpusRegistry) -> None:
    assert registry.admit(**DEV).split == "dev"
    for token in (None, "wrong-token"):
        with pytest.raises(SplitLockError, match="TEST split is sealed"):
            registry.admit(**TEST, frozen_eval_token=token)
    assert registry.admit(**TEST, frozen_eval_token=FROZEN_TOKEN).split == "test"
    assert all(item.split == "dev" for item in registry.items(split="DEV"))


def test_frozen_token_is_explicit_not_truthy_or_self_authorizing(corpus_root: Path) -> None:
    registry = CorpusRegistry(corpus_root)
    for token in (FROZEN_TOKEN, "true", "allow-test", ""):
        with pytest.raises(SplitLockError):
            registry.admit(**TEST, frozen_eval_token=token)


@pytest.mark.parametrize(
    "dataset,item_id",
    [
        ("mimo-defect-map-v1", "mimo-held@sha256:version1"),
        ("mimo-defect-map-v1", "mimo-hidden@sha256:version1"),
        ("cheatbench-actions-v1", "renamed-sealed"),
        ("hacktrace-act-v1", "name-alias"),
    ],
)
def test_test_lock_follows_heldout_task_name_and_digest(
    registry: CorpusRegistry, dataset: str, item_id: str
) -> None:
    assert registry.get(dataset, item_id).split == "test"
    with pytest.raises(SplitLockError):
        registry.admit(dataset, item_id)


def test_canonical_assignment_can_only_tighten_a_split(corpus_root: Path) -> None:
    path = corpus_root / "research/answer-keys/splits/assignments.csv"
    path.parent.mkdir()
    path.write_text(
        "key_id,item_id,task,split\n"
        "tb4-audit-v1,finding-dev,terminal-bench/dev-task,test\n"
        "tb4-audit-v1,finding-test,terminal-bench/sealed-task,dev\n",
        encoding="utf-8",
    )
    registry = CorpusRegistry(corpus_root)
    for ref in (DEV, TEST):
        with pytest.raises(SplitLockError):
            registry.admit(**ref)


def _assignments(corpus_root: Path, rows: str) -> Path:
    path = corpus_root / "research/answer-keys/splits/assignments.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("key_id,item_id,task,split\n" + rows, encoding="utf-8")
    return path


def test_registry_normalizes_seals_without_rewriting_source_identities(corpus_root: Path) -> None:
    source = _item(
        " Canonical-Sealed-Item ", " TEST ", " Namespace@release/Canonical-Sealed-Task@v2 ",
        " SHA256:CANONICAL-SEALED-DIGEST ",
    )
    source["key_id"] = " TB4-AUDIT-V1 "
    _write_jsonl(
        corpus_root / "research/answer-keys/tb4-audit-v1/items.jsonl",
        [_item("finding-dev", "dev", "terminal-bench/dev-task"), source],
    )
    _write_jsonl(
        corpus_root / "research/answer-keys/cheatbench-actions-v1/items.jsonl",
        [
            _item("name-alias", "dev", "Other/CANONICAL-SEALED-TASK@v9"),
            _item("digest-alias", "dev", "other/unrelated", " sha256:canonical-sealed-digest "),
        ],
    )
    registry = CorpusRegistry(corpus_root)
    stored = registry.get(" TB4-AUDIT-V1 ", "canonical-sealed-item")
    assert stored.item_id == source["item_id"]
    assert stored.task_id == source["subject"]["task_id"]
    assert stored.task_digest == source["subject"]["task_digest"]
    assert stored.metadata == source
    assert stored.source_record_sha256 == source_record_digest(source)
    assert registry.sealed_identity(item_id=" CANONICAL-SEALED-ITEM ") == "item_id"
    assert registry.sealed_identity(
        task_id=" other@release/canonical-sealed-task@v99 ",
    ) == "task name"
    assert registry.sealed_identity(
        task_digest=" sha256:canonical-sealed-digest ",
    ) == "task digest"
    for dataset, item_id in (
        (DATASET, " CANONICAL-SEALED-ITEM "),
        (" CHEATBENCH-ACTIONS-V1 ", " NAME-ALIAS "),
        ("cheatbench-actions-v1", "digest-alias"),
    ):
        with pytest.raises(SplitLockError):
            registry.admit(dataset, item_id)
    assert registry.admit(" TB4-AUDIT-V1 ", " FINDING-DEV ").item_id == "finding-dev"


def test_normalized_assignment_test_wins_without_global_secondary_queue_seals(
    corpus_root: Path,
) -> None:
    _assignments(
        corpus_root,
        " MIXED-KEY , ITEM-DEV , Namespace/Dev-Task@v1 , DEV \n"
        " mixed-key , ITEM-TEST , Namespace/Other-Task@v1 , TEST \n"
        f" {SECONDARY_KEY.upper()} , FINDING-DEV , TERMINAL-BENCH/DEV-TASK@v9 , TEST \n",
    )
    registry = CorpusRegistry(corpus_root)
    assert registry.has_assignment_authority()
    assert registry.has_split_assignments(" MIXED-KEY ")
    assert registry.assigned_split(
        " mixed-key ", item_id=" item-test ", task_id="Other/DEV-TASK@v7",
    ) == "test"
    assert registry.assigned_split(
        " MIXED-KEY ", task_id=" Namespace@old/ DEV-TASK @v99 ",
    ) == "dev"
    assert registry.assigned_test_anywhere(item_id=" Finding-Dev ")
    assert registry.assigned_test_anywhere(task_id=" Alias@old/DEV-TASK@v3 ")
    assert registry.sealed_identity(task_id=" Alias/DEV-TASK@v3 ") is None
    assert registry.admit(" TB4-AUDIT-V1 ", " FINDING-DEV ").split == "dev"


def test_package_seal_normalizes_heldout_versions_and_digest(corpus_root: Path) -> None:
    record = {
        "record_id": " Raw-Package-Record ",
        "task_id": " Namespace/MIXED-PACKAGE-TASK@v5 ",
        "package_digest": " SHA256:PACKAGE-SEALED ",
    }
    inventory = corpus_root / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory.write_text(json.dumps({
        "dataset_id": "mimo-defect-map-v1",
        "heldout_task_ids_excluded_from_training_all_versions": [" Mixed-Package-Task "],
        "tasks": [record],
    }), encoding="utf-8")
    registry = CorpusRegistry(corpus_root)
    stored = registry.get(" MIMO-DEFECT-MAP-V1 ", "raw-package-record")
    assert stored.split == "test"
    assert stored.item_id == record["record_id"]
    assert stored.task_id == record["task_id"]
    assert stored.task_digest == record["package_digest"]
    assert stored.source_record_sha256 == source_record_digest(record)
    assert registry.sealed_identity(task_digest=" sha256:package-sealed ") == "task digest"


@pytest.mark.parametrize("ledger", (None, "", "key_id,item_id,task,split\n"))
def test_source_only_registry_without_nonempty_ledger_is_not_assignment_authority(
    corpus_root: Path, ledger: str | None,
) -> None:
    if ledger is not None:
        path = corpus_root / "research/answer-keys/splits/assignments.csv"
        path.parent.mkdir()
        path.write_text(ledger, encoding="utf-8")
    registry = CorpusRegistry(corpus_root)
    if ledger is None:
        assert registry.split_assignments_digest is None
    else:
        assert registry.split_assignments_digest is not None
    assert not registry.has_assignment_authority()
    assert registry.get(DATASET, "finding-dev").task_id == "terminal-bench/dev-task"


@pytest.mark.parametrize("ledger", (
    "key_id,item_id,task\nk,i,t\n",
    "key_id,item_id,task,split\nk,i,t\n",
    'key_id,item_id,task,split\n"k,i,t,dev\n',
))
def test_malformed_assignment_ledger_cannot_be_authority(
    corpus_root: Path, ledger: str,
) -> None:
    path = corpus_root / "research/answer-keys/splits/assignments.csv"
    path.parent.mkdir()
    path.write_text(ledger, encoding="utf-8")
    with pytest.raises((ValueError, KeyError, csv.Error)):
        CorpusRegistry(corpus_root)


def test_secondary_key_heldout_never_seals_canonical_dev_or_cross_dataset_alias(
    corpus_root: Path,
) -> None:
    _assignments(
        corpus_root,
        f"{SECONDARY_KEY},terminal-bench/dev-task,terminal-bench/dev-task,heldout\n"
        f"{SECONDARY_KEY},another-benchmark/dev-task@v9,another-benchmark/dev-task@v9,heldout\n",
    )
    _write_jsonl(
        corpus_root / "research/answer-keys/hacktrace-act-v1/items.jsonl",
        [
            _item("hack-dev", "dev", "hacktrace/independent"),
            _item("name-alias", "dev", "another-benchmark/sealed-task@v2"),
            _item("secondary-name-alias", "dev", "another-benchmark/dev-task@v9"),
            _item("mimo-name-alias", "dev", "mirror/mimo-held@v9"),
        ],
    )
    registry = CorpusRegistry(corpus_root)
    assert registry.admit(**DEV).split == "dev"
    assert registry.admit("hacktrace-act-v1", "secondary-name-alias").split == "dev"
    # Canonical and MiMo locks are unchanged by secondary scoping.
    with pytest.raises(SplitLockError):
        registry.admit("hacktrace-act-v1", "name-alias")
    with pytest.raises(SplitLockError):
        registry.admit("hacktrace-act-v1", "mimo-name-alias")
    # Secondary keys are not registered corpora; their lookups fail closed.
    for item_id in ("terminal-bench/dev-task", "another-benchmark/dev-task@v9"):
        with pytest.raises(KeyError):
            registry.admit(SECONDARY_KEY, item_id)


def test_canonical_key_assignment_still_seals_task_names_globally(
    corpus_root: Path,
) -> None:
    _assignments(
        corpus_root,
        "tb4-audit-v1,finding-dev,terminal-bench/dev-task,test\n",
    )
    _write_jsonl(
        corpus_root / "research/answer-keys/hacktrace-act-v1/items.jsonl",
        [
            _item("hack-dev", "dev", "hacktrace/independent"),
            _item("canonical-name-alias", "dev", "another-benchmark/dev-task@v9"),
        ],
    )
    registry = CorpusRegistry(corpus_root)
    with pytest.raises(SplitLockError):
        registry.admit(**DEV)
    with pytest.raises(SplitLockError):
        registry.admit("hacktrace-act-v1", "canonical-name-alias")


def test_assignment_key_scoping_is_order_independent(corpus_root: Path) -> None:
    secondary = f"{SECONDARY_KEY},terminal-bench/dev-task,terminal-bench/dev-task,heldout\n"
    canonical = "tb4-audit-v1,finding-test,terminal-bench/sealed-task,test\n"
    observed = []
    for rows in ((secondary, canonical), (canonical, secondary)):
        _assignments(corpus_root, "".join(rows))
        registry = CorpusRegistry(corpus_root)
        observed.append((registry.admit(**DEV).split, registry.get(**TEST).split))
        with pytest.raises(KeyError):
            registry.admit(SECONDARY_KEY, "terminal-bench/dev-task")
    assert observed == [("dev", "test"), ("dev", "test")]


def test_frozen_token_neither_registers_secondary_keys_nor_unseals_canonical(
    corpus_root: Path,
) -> None:
    _assignments(
        corpus_root,
        f"{SECONDARY_KEY},terminal-bench/dev-task,terminal-bench/dev-task,heldout\n",
    )
    registry = CorpusRegistry(corpus_root, frozen_eval_token=FROZEN_TOKEN)
    assert registry.admit(**DEV).split == "dev"
    with pytest.raises(KeyError):
        registry.admit(SECONDARY_KEY, "terminal-bench/dev-task", frozen_eval_token=FROZEN_TOKEN)
    with pytest.raises(SplitLockError):
        registry.admit(**TEST)
    assert registry.admit(**TEST, frozen_eval_token=FROZEN_TOKEN).split == "test"


def _admits(
    registry: CorpusRegistry, dataset_id: str, item_id: str, token: str | None = None
) -> bool:
    try:
        registry.admit(dataset_id, item_id, frozen_eval_token=token)
    except (KeyError, SplitLockError):
        return False
    return True


def _sealed(registry: CorpusRegistry, dataset_id: str, item_id: str) -> bool:
    try:
        registry.admit(dataset_id, item_id)
    except SplitLockError:
        return True
    return False


def test_real_canonical_tb4_splits_ignore_secondary_key_heldouts(
    tmp_path: Path,
) -> None:
    # Iterate committed split metadata only; identifiers are never printed.
    root = Path(__file__).resolve().parents[1]
    task_index = root / "research/answer-keys/tb4-audit-v1/tasks.jsonl"
    task_ids: dict[str, list[str]] = {"dev": [], "test": []}
    digests = {}
    for line in task_index.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        subject = row.get("subject") or {}
        task_id = subject.get("task_id")
        if not task_id:
            continue
        task_ids["dev" if row.get("split") == "dev" else "test"].append(task_id)
        if subject.get("task_digest"):
            digests[task_id] = subject["task_digest"]
    registry = CorpusRegistry(root)
    admitted_dev = sum(_admits(registry, DATASET, task_id) for task_id in task_ids["dev"])
    sealed_test = sum(_sealed(registry, DATASET, task_id) for task_id in task_ids["test"])
    assert (len(task_ids["dev"]), admitted_dev) == (25, 25)
    assert (len(task_ids["test"]), sealed_test) == (41, 41)
    # cargo-flight-dispatch is held out only under the secondary envcheck key;
    # its canonical DEV row must still admit.
    assert (
        sum(
            _admits(registry, DATASET, task_id)
            for task_id in task_ids["dev"]
            if CorpusRegistry.task_name(task_id) == "cargo-flight-dispatch"
        )
        == 1
    )
    tokened = CorpusRegistry(root, frozen_eval_token=FROZEN_TOKEN)
    unlocked = sum(_admits(tokened, DATASET, task_id, FROZEN_TOKEN) for task_id in task_ids["test"])
    assert unlocked == 41
    with pytest.raises(KeyError):
        registry.admit(SECONDARY_KEY, "terminal-bench/dev-task")

    # Synthetic aliases under the other canonical corpora prove the TEST lock is
    # global by task name, and by digest wherever committed metadata carries one.
    alias_root = tmp_path / "alias-corpus"
    keys = alias_root / "research/answer-keys"
    name_aliases: list[tuple[str, str]] = []
    digest_aliases: list[tuple[str, str]] = []
    dev_aliases: list[tuple[str, str]] = []
    cheatbench_rows: list[dict] = []
    hacktrace_rows: list[dict] = []
    for index, task_id in enumerate(task_ids["test"]):
        name = CorpusRegistry.task_name(task_id)
        cheatbench_rows.append(_item(f"cb-name-{index}", "dev", f"synthetic-alias/{name}"))
        name_aliases.append(("cheatbench-actions-v1", f"cb-name-{index}"))
        hacktrace_rows.append(_item(f"ht-name-{index}", "dev", f"another-alias/{name}@v9"))
        name_aliases.append(("hacktrace-act-v1", f"ht-name-{index}"))
        if task_id in digests:
            hacktrace_rows.append(
                _item(
                    f"ht-digest-{index}",
                    "dev",
                    f"digest-only/alias-{index}",
                    digests[task_id],
                )
            )
            digest_aliases.append(("hacktrace-act-v1", f"ht-digest-{index}"))
    for index, task_id in enumerate(task_ids["dev"]):
        name = CorpusRegistry.task_name(task_id)
        cheatbench_rows.append(_item(f"cb-dev-{index}", "dev", f"synthetic-alias/{name}"))
        dev_aliases.append(("cheatbench-actions-v1", f"cb-dev-{index}"))
    _write_jsonl(keys / "cheatbench-actions-v1/items.jsonl", cheatbench_rows)
    _write_jsonl(keys / "hacktrace-act-v1/items.jsonl", hacktrace_rows)
    for name in ("items.jsonl", "tasks.jsonl"):
        target = keys / "tb4-audit-v1" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / "research/answer-keys/tb4-audit-v1" / name, target)
    (keys / "splits").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        root / "research/answer-keys/splits/assignments.csv",
        keys / "splits/assignments.csv",
    )
    inventory = alias_root / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(root / "research/experiments/mimo-defect-map-v1/inventory.json", inventory)
    aliased = CorpusRegistry(alias_root)
    sealed_by_name = sum(_sealed(aliased, dataset, item_id) for dataset, item_id in name_aliases)
    sealed_by_digest = sum(
        _sealed(aliased, dataset, item_id) for dataset, item_id in digest_aliases
    )
    admitted_aliases = sum(_admits(aliased, dataset, item_id) for dataset, item_id in dev_aliases)
    assert (len(name_aliases), sealed_by_name) == (82, 82)
    assert digest_aliases and (len(digest_aliases), sealed_by_digest) == (41, 41)
    assert (len(dev_aliases), admitted_aliases) == (25, 25)


def test_real_mimo_heldout_name_and_digest_seal_is_unchanged() -> None:
    root = Path(__file__).resolve().parents[1]
    inventory = json.loads(
        (root / "research/experiments/mimo-defect-map-v1/inventory.json").read_text(
            encoding="utf-8"
        )
    )
    heldout = set(inventory["heldout_task_ids_excluded_from_training_all_versions"])
    registry = CorpusRegistry(root)
    sealed = sum(
        _sealed(registry, item.dataset_id, item.item_id)
        for item in registry.items(dataset_id="mimo-defect-map-v1")
        if item.task_id in heldout
    )
    assert (len(heldout), sealed) == (33, 33)


def test_registry_metadata_cannot_be_mutated_to_relabel_later_admission(
    registry: CorpusRegistry,
) -> None:
    item = registry.get(**TEST)
    item.metadata["split"] = "dev"
    item.metadata["subject"]["task_id"] = "new-name"
    assert registry.get(**TEST).metadata["split"] == "test"
    with pytest.raises(SplitLockError):
        registry.admit(**TEST)


def test_missing_and_unknown_split_indices_fail_closed(corpus_root: Path) -> None:
    path = corpus_root / "research/answer-keys/cheatbench-actions-v1/items.jsonl"
    _write_jsonl(path, [_item("unknown", "train", "cheatbench/unknown")])
    with pytest.raises(ValueError, match="unsupported corpus split"):
        CorpusRegistry(corpus_root)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        CorpusRegistry(corpus_root)


def test_real_local_public_corpus_indices_are_readable() -> None:
    registry = CorpusRegistry(Path(__file__).resolve().parents[1])
    assert {item.dataset_id for item in registry.items()} == set(DATASETS)
    assert len(registry.items(dataset_id="mimo-defect-map-v1")) == 567
    assert registry.items(dataset_id=DATASET, split="dev")
    assert registry.items(dataset_id=DATASET, split="test")
    dev = registry.items(dataset_id=DATASET, split="dev")[0]
    sealed = registry.items(dataset_id=DATASET, split="test")[0]
    assert registry.admit(dev.dataset_id, dev.item_id).split == "dev"
    with pytest.raises(SplitLockError):
        registry.admit(sealed.dataset_id, sealed.item_id)


