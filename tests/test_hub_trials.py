"""Consumer regressions over an excerpt of acquired Harbor 0.24 public schemas.

No network/SDK is needed. The fixture provenance describes every excerpt; injected
faults below are test cases, not claims about the public acquisition.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from evallab import hub_trials as hub

FOOD = "terminal-bench/foodstuff-beta-activity"
HEAT = "terminal-bench/heat-pump-warranty"


class NativeFixture:
    def __init__(self):
        self.data = json.loads((Path(__file__).parent / "fixtures/hub_trials_native.json").read_bytes())
        self.calls = []
        self.downloads = []
        self.members = {}
        self.details = {}
        for cell in self.data["cells"]:
            self.members.setdefault(cell["row_id"], []).append(cell["trial_id"])
            self.details[cell["trial_id"]] = cell["detail"]
        self.mutate_files = None
        self.interrupt = None
        self.mutate_page = None

    def page(self, items, args):
        limit = int(args[args.index("--limit") + 1])
        page = int(args[args.index("--page") + 1])
        items = sorted(items, key=lambda item: item.get("id", item.get("trial_id")))
        payload = {
            "items": items[(page - 1) * limit : page * limit], "total": len(items),
            "page": page, "page_size": limit, "total_pages": (len(items) + limit - 1) // limit,
        }
        if self.mutate_page:
            self.mutate_page(payload)
        return payload

    def json(self, args):
        args = tuple(args)
        self.calls.append(args)
        if args[:3] == ("hub", "leaderboard", "show"):
            payload = copy.deepcopy(self.data["board"])
            for row in payload["rows"]:
                row["n_trials"] = len(self.members[row["id"]])
        elif args[:5] == ("hub", "leaderboard", "row", "trial", "list"):
            payload = self.page([{"trial_id": identity, "created_at": "2026-10-06T17:56:36.322737+00:00"} for identity in self.members[args[5]]], args)
        elif args[:3] == ("hub", "trial", "show"):
            payload = self.details[args[3]]
        elif args[:3] == ("hub", "job", "show"):
            payload = self.data["jobs"][args[3]]["show"]
        elif args[:3] == ("hub", "job", "trials"):
            assert "--include-retries" in args
            payload = self.page(self.data["jobs"][args[3]]["trials"], args)
        elif args[:2] == ("version", "show"):
            payload = self.data["dataset"]
        else:
            raise AssertionError(f"Unexpected native protocol command: {args}")
        return json.dumps(payload).encode()

    def download(self, trial_id, output):
        self.downloads.append(trial_id)
        tree = output / self.details[trial_id]["trial_name"]
        tree.mkdir()
        if self.interrupt:
            (tree / "partial.bin").write_bytes(b"unreceipted partial native bytes")
            raise self.interrupt
        files = copy.deepcopy(self.data["downloads"][trial_id])
        if self.mutate_files:
            self.mutate_files(files)
        for relative, content in files.items():
            if content is not None:
                path = tree / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content.encode())


def manifest(root):
    return json.loads((root / "manifest.json").read_bytes())


def acquire(root, native, **kwargs):
    return hub.download(root, tasks=kwargs.pop("tasks", [FOOD]), models=kwargs.pop("models", ["Opus 5.5"]), native=native, **kwargs)


def test_filtered_census_preserves_native_identity_rewards_and_unassociated_attempts(tmp_path):
    native = NativeFixture()
    report = acquire(tmp_path, native, page_size=2)
    stored = manifest(tmp_path)
    selection = stored["selection"]
    assert report["evidence_complete"] and report["scope"] == "full_census"
    assert list(selection["tasks"]) == [FOOD]
    assert [row["model_label"] for row in selection["rows"]] == ["Opus 5.5"]
    job_id = selection["jobs"][0]["id"]
    expected = {trial["id"] for trial in native.data["jobs"][job_id]["trials"] if trial["task_name"] == FOOD}
    assert {trial["id"] for trial in selection["trials"]} == expected
    assert any(trial["eligibility"] == "not_board_associated" for trial in selection["trials"])
    assert len(native.downloads) == 1  # Board association is not the whole include-retries job.
    assert any("--page" in call and call[call.index("--page") + 1] == "2" for call in native.calls)
    assert selection["dataset"]["version"] == "3.0.1"
    assert selection["dataset"]["revision"] == 4
    assert "v4.0.0" in selection["dataset"]["tags"]
    state = stored["downloads"][native.downloads[0]]
    assert state["evidence"]["hub_trial_id"] != state["evidence"]["result_trial_id"]
    assert state["evidence"]["native_rewards"] == {"reward": 0.0}
    assert state["evidence"]["reward_files"] == ["verifier/reward.txt"]
    assert not (tmp_path / state["path"] / "verifier/reward.json").exists()
    for relative, receipt in state["files"].items():
        raw = (tmp_path / state["path"] / relative).read_bytes()
        assert receipt == {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    assert hub.verify(tmp_path)["evidence_complete"]


def test_resume_is_offline_frozen_and_rejects_filter_changes(tmp_path, monkeypatch):
    acquire(tmp_path, NativeFixture(), tasks=[FOOD, HEAT], models=["Opus 5.5", "Sonnet 5.5"])
    before = (tmp_path / "manifest.json").read_bytes()
    native = NativeFixture()
    native.data["board"]["leaderboard"]["id"] = "changed upstream board"
    report = acquire(tmp_path, native, tasks=[HEAT, FOOD], models=["Sonnet 5.5", "Opus 5.5"])
    assert report["evidence_complete"]
    assert not native.calls and not native.downloads
    assert (tmp_path / "manifest.json").read_bytes() == before
    with pytest.raises(hub.HubError, match="different frozen request"):
        acquire(tmp_path, native, tasks=[FOOD])
    monkeypatch.setattr(hub.subprocess, "run", lambda *a, **k: pytest.fail("offline verify invoked Harbor"))
    assert hub.main(["verify", "--output", str(tmp_path)]) == 0


@pytest.mark.parametrize("fault", ["corrupt", "missing", "extra", "symlink"])
def test_resume_never_overwrites_or_blesses_changed_evidence(tmp_path, fault):
    acquire(tmp_path, NativeFixture())
    stored = manifest(tmp_path)
    state = next(iter(stored["downloads"].values()))
    tree = tmp_path / state["path"]
    path = tree / "agent/trajectory.json"
    if fault == "corrupt":
        path.write_bytes(b"partial/corrupt trajectory")
    elif fault == "missing":
        path.unlink()
    elif fault == "extra":
        (tree / "untracked.bin").write_bytes(b"not in receipt")
    else:
        path.unlink()
        path.symlink_to(tree / "result.json")
    before_manifest = (tmp_path / "manifest.json").read_bytes()
    report = hub.verify(tmp_path)
    assert not report["integrity_ok"] and not report["evidence_complete"]
    native = NativeFixture()
    with pytest.raises(hub.IntegrityError):
        acquire(tmp_path, native)
    assert not native.calls and not native.downloads
    assert (tmp_path / "manifest.json").read_bytes() == before_manifest
    if fault == "corrupt":
        assert path.read_bytes() == b"partial/corrupt trajectory"


def test_metadata_hash_and_frozen_selection_tampering_are_not_adopted(tmp_path):
    acquire(tmp_path, NativeFixture())
    stored = manifest(tmp_path)
    metadata = tmp_path / next(iter(stored["metadata"]))
    metadata.write_bytes(b"{}")
    native = NativeFixture()
    with pytest.raises(hub.IntegrityError, match="Metadata hash mismatch"):
        acquire(tmp_path, native)
    assert not native.calls
    stored["selection"]["dataset"]["version"] = "4.0.0"
    (tmp_path / "manifest.json").write_text(json.dumps(stored))
    with pytest.raises(hub.IntegrityError, match="Frozen selection hash mismatch"):
        hub.verify(tmp_path)


def test_missing_outputs_are_preserved_as_missing_without_synthetic_reward(tmp_path):
    native = NativeFixture()

    def remove_evidence(files):
        files.pop("agent/trajectory.json")
        files.pop("verifier/test-stdout.txt")
        files.pop("verifier/reward.txt")
        result = json.loads(files["result.json"])
        result["verifier_result"] = None
        files["result.json"] = json.dumps(result)

    native.mutate_files = remove_evidence
    report = acquire(tmp_path, native)
    state = next(iter(manifest(tmp_path)["downloads"].values()))
    assert report["download_statuses"] == {"missing_evidence": 1}
    assert not report["evidence_complete"]
    assert set(state["evidence"]["missing"]) == {
        "agent/trajectory.json", "verifier/test-stdout.txt", "verifier/reward.{txt,json}",
        "result.json:verifier_result.rewards",
    }
    assert hub.verify(tmp_path)["integrity_ok"]  # Intact bytes do not imply complete evidence.
    resumed = NativeFixture()
    assert not acquire(tmp_path, resumed)["evidence_complete"]
    assert not resumed.calls and not resumed.downloads
    assert not list(tmp_path.glob("trials/**/reward.json"))


def test_valid_hashes_do_not_bless_invalid_original_json(tmp_path):
    native = NativeFixture()
    native.mutate_files = lambda files: files.update({"agent/trajectory.json": "{truncated"})
    report = acquire(tmp_path, native)
    assert not report["evidence_complete"]
    state = next(iter(manifest(tmp_path)["downloads"].values()))
    assert state["evidence"]["invalid"] == ["agent/trajectory.json"]
    assert hub.verify(tmp_path)["integrity_ok"]


def test_board_membership_is_not_proof_of_exact_tb4_task_bytes(tmp_path):
    native = NativeFixture()
    for detail in native.details.values():
        detail["lock"]["task"]["source"] = "ryanmarten/terminal-bench-4-no-prebuild"
    report = acquire(tmp_path, native)
    assert not native.downloads and not report["evidence_complete"]
    assert report["cells"][0]["excluded_trial_ids"]
    assert not report["cells"][0]["selected_trial_ids"]
    assert any(trial["eligibility"] == "task_lock_mismatch" for trial in manifest(tmp_path)["selection"]["trials"])


def test_downloaded_lock_drift_is_retained_but_never_counted(tmp_path):
    native = NativeFixture()

    def drift(files):
        lock = json.loads(files["lock.json"])
        lock["task"]["digest"] = "sha256:" + "0" * 64
        files["lock.json"] = json.dumps(lock)

    native.mutate_files = drift
    report = acquire(tmp_path, native)
    assert report["download_statuses"] == {"identity_mismatch": 1}
    assert not report["evidence_complete"]
    assert hub.verify(tmp_path)["integrity_ok"]


@pytest.mark.parametrize("interruption", [hub.HubError("native download failed"), KeyboardInterrupt()])
def test_partial_attempt_is_not_adopted_and_resume_preserves_attempt_history(tmp_path, interruption):
    native = NativeFixture()
    native.interrupt = interruption
    if isinstance(interruption, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            acquire(tmp_path, native)
    else:
        assert not acquire(tmp_path, native)["evidence_complete"]
    before = manifest(tmp_path)
    trial_id, state = next(iter(before["downloads"].items()))
    partial = tmp_path / state["attempts"][0]["path"]
    partial_bytes = next(partial.rglob("partial.bin")).read_bytes()
    assert not hub.verify(tmp_path)["evidence_complete"]
    resumed = NativeFixture()
    assert acquire(tmp_path, resumed)["evidence_complete"]
    assert not resumed.calls and resumed.downloads == [trial_id]
    after = manifest(tmp_path)["downloads"][trial_id]
    assert len(after["attempts"]) == 2
    assert after["attempts"][0]["status"] == ("interrupted" if isinstance(interruption, KeyboardInterrupt) else "failed")
    assert after["attempts"][0]["files"]
    assert next(partial.rglob("partial.bin")).read_bytes() == partial_bytes
    assert after["status"] == "complete" and hub.verify(tmp_path)["integrity_ok"]


def test_receipted_stage_resumes_atomic_publication_without_download(tmp_path, monkeypatch):
    publish = hub.Acquisition.publish
    monkeypatch.setattr(hub.Acquisition, "publish", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        acquire(tmp_path, NativeFixture())
    assert next(iter(manifest(tmp_path)["downloads"].values()))["status"] == "staged"
    monkeypatch.setattr(hub.Acquisition, "publish", publish)
    resumed = NativeFixture()
    assert acquire(tmp_path, resumed)["evidence_complete"]
    assert not resumed.calls and not resumed.downloads
    assert hub.verify(tmp_path)["integrity_ok"]


def associate_one_native_retry(native):
    """Inject an additional board association using an actual acquired retry summary.

    The trial detail/result clone uses the acquired schema; this association is a
    test modification, not an assertion that the public board associates retries.
    """
    cell = next(cell for cell in native.data["cells"] if cell["public_model"] == "Opus 5.5")
    summary = next(trial for trial in native.data["jobs"][cell["job_id"]]["trials"] if trial["retry_index"] > 0)
    cell = next(cell for cell in native.data["cells"] if cell["public_model"] == "Opus 5.5" and cell["task"] == summary["task_name"])
    detail = copy.deepcopy(cell["detail"])
    detail.update({"id": summary["id"], "trial_name": summary["name"]})
    native.details[summary["id"]] = detail
    native.members[cell["row_id"]].append(summary["id"])
    files = copy.deepcopy(native.data["downloads"][cell["trial_id"]])
    result = json.loads(files["result.json"])
    result["trial_name"] = summary["name"]
    files["result.json"] = json.dumps(result)
    native.data["downloads"][summary["id"]] = files
    return summary


def test_full_census_preserves_retry_identity_and_smoke_omission_is_explicit(tmp_path):
    full_native = NativeFixture()
    retry = associate_one_native_retry(full_native)
    full = acquire(tmp_path / "full", full_native, tasks=[retry["task_name"]])
    assert full["scope"] == "full_census" and len(full_native.downloads) == 2
    selected = [trial for trial in manifest(tmp_path / "full")["selection"]["trials"] if trial["selected"]]
    assert {trial["id"] for trial in selected} == set(full_native.downloads)
    assert any(trial["id"] == retry["id"] and trial["retry_index"] == retry["retry_index"] and trial["attempt"] == retry["attempt"] for trial in selected)
    sample_native = NativeFixture()
    associate_one_native_retry(sample_native)
    sample = acquire(tmp_path / "sample", sample_native, tasks=[retry["task_name"]], smoke_trials_per_cell=1)
    assert sample["scope"] == "explicit_smoke_sample" and len(sample_native.downloads) == 1
    assert len(sample["cells"][0]["eligible_trial_ids"]) == 2
    assert len(sample["cells"][0]["sample_omitted_trial_ids"]) == 1
    assert sample["cells"][0]["selected_trial_ids"] == sorted(trial["id"] for trial in selected)[:1]
    assert manifest(tmp_path / "sample")["request"]["smoke_trials_per_cell"] == 1
    with pytest.raises(hub.HubError, match="different frozen request"):
        acquire(tmp_path / "sample", NativeFixture(), tasks=[retry["task_name"]])


def test_ambiguous_model_label_requires_stable_row_id(tmp_path):
    native = NativeFixture()
    for row in native.data["board"]["rows"]:
        row["metadata"]["model_display"]["label"] = "Opus 5.5"
    with pytest.raises(hub.HubError, match="matches 2 rows"):
        acquire(tmp_path / "ambiguous", native)
    row_id = native.data["cells"][0]["row_id"]
    assert acquire(tmp_path / "exact", native, models=[], row_ids=[row_id])["evidence_complete"]


def test_discovery_resumes_original_cached_metadata_after_native_failure(tmp_path):
    native = NativeFixture()
    original = native.json

    def unavailable(args):
        if tuple(args[:3]) == ("hub", "job", "show"):
            raise hub.HubError("Native Harbor command failed (exit 1)")
        return original(args)

    native.json = unavailable
    with pytest.raises(hub.HubError, match="exit 1"):
        acquire(tmp_path, native)
    stored = manifest(tmp_path)
    assert stored["state"] == "discovery_failed" and stored["selection"] is None
    originals = {name: (tmp_path / name).read_bytes() for name in stored["metadata"]}
    resumed = NativeFixture()
    assert acquire(tmp_path, resumed)["evidence_complete"]
    assert not any(call[:3] == ("hub", "leaderboard", "show") for call in resumed.calls)
    assert all((tmp_path / name).read_bytes() == raw for name, raw in originals.items())
    assert hub.verify(tmp_path)["integrity_ok"]


def test_pagination_bound_fails_closed_without_freezing_partial_census(tmp_path):
    native = NativeFixture()
    with pytest.raises(hub.HubError, match="exceeds --max-pages"):
        acquire(tmp_path, native, page_size=1, max_pages=1)
    stored = manifest(tmp_path)
    assert stored["state"] == "discovery_failed" and stored["selection"] is None
    assert not native.downloads and not hub.verify(tmp_path)["evidence_complete"]


def test_pagination_count_drift_is_not_a_frozen_census(tmp_path):
    native = NativeFixture()
    native.mutate_page = lambda page: page.update({"total": page["total"] + 1}) if page["page"] == 2 else None
    with pytest.raises(hub.HubError, match="total changed"):
        acquire(tmp_path, native, page_size=1)
    assert manifest(tmp_path)["selection"] is None
    assert not native.downloads


def test_unsafe_native_name_or_symlink_target_cannot_escape(tmp_path):
    native = NativeFixture()
    for detail in native.details.values():
        detail["trial_name"] = "../escape"
    with pytest.raises(hub.IntegrityError, match="Unsafe native trial name"):
        acquire(tmp_path / "unsafe", native)
    assert not native.downloads and not (tmp_path / "escape").exists()
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    native = NativeFixture()
    with pytest.raises(hub.IntegrityError, match="Symlink target"):
        acquire(linked, native)
    assert not native.calls and not list(target.iterdir())
