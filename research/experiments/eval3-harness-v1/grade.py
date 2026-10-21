"""Grade retained QA reports with the public Eval3 policy; never execute evidence."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import statistics
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

from evallab.execution_contracts import openrouter_route
from evallab.interpretation.monitor_agent import (
    InvestigationBudget,
    InvestigatorError,
    OpenAIInvestigator,
    _payload,
)
from evallab.qa.ask import TOOL_HELP, WorkspaceTools
from evallab.qa.trace import _schema_problems

UPSTREAM_COMMIT = "e15a35a1da4237bf5dd2e664725aae66c207d65c"
STUDY_SCHEMA = "evallab.eval3.study/v1"
ADAPTER_VERSION = "eval3-policy-adapter/1"
STATES = {"completed", "limit_reached", "failed", "pending", "ungraded"}
TERMINAL = {"completed", "limit_reached"}
LEVELS = ("low", "medium", "high")
DEFAULT_MODEL = "openrouter-metered/openai/gpt-oss-120b"
MAX_OUTPUT = 6000


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def upstream() -> tuple[Any, Any]:
    distribution = importlib.metadata.distribution("eval3-grader")
    origin = json.loads(distribution.read_text("direct_url.json") or "{}")
    if origin.get("vcs_info", {}).get("commit_id") != UPSTREAM_COMMIT:
        raise ValueError("install the exact grader commit from this experiment's requirements.txt")
    from eval3_grader.policy import judge
    from eval3_grader.rankings import rank_catalog

    return judge, rank_catalog


def regular_file(path: Path) -> bytes:
    if any(part.is_symlink() for part in (path, *path.parents)) or not path.is_file():
        raise ValueError(f"evidence must be a regular file without symlinks: {path}")
    return path.read_bytes()


def checked_name(name: Any) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", name):
        raise ValueError("study, auditor, case and trial labels must be bounded plain identifiers")
    return name


def prepare(config: dict[str, Any], out: Path) -> dict[str, Any]:
    """Copy only a sealed, label-free source view and explicitly selected old reports."""
    if out.exists():
        raise ValueError(
            "output already exists; original reports and prepared studies are immutable"
        )
    view_root = Path(config["source_view"]).expanduser().absolute()
    view = json.loads(regular_file(view_root / "bundle.json"))
    if view.get("schema") != "tb4.audit.agent-view/v1" or view.get("split") != "dev":
        raise ValueError("this adapter requires a sealed DEV source view; TEST grading is refused")
    input_bytes = regular_file(view_root / "inputs.jsonl")
    if digest(input_bytes) != view["files"].get("inputs.jsonl"):
        raise ValueError("sealed task identity inventory changed")
    inputs = [json.loads(line) for line in input_bytes.splitlines()]
    identities = {row["package"]["name"]: row for row in inputs}
    cases = [checked_name(value) for value in config["cases"]]
    auditors = [checked_name(value) for value in config["auditors"]]
    if (
        not cases
        or not auditors
        or len(set(cases)) != len(cases)
        or len(set(auditors)) != len(auditors)
    ):
        raise ValueError("declare unique nonempty task and auditor cohorts before grading")
    if not set(cases) <= set(identities):
        raise ValueError("a requested task is absent from the sealed DEV view")
    study_id = checked_name(config["study_id"])
    out.mkdir(parents=True)
    tasks = []
    for case in cases:
        row = identities[case]
        task_digest = row["subject"]["task_digest"].removeprefix("sha256:")
        if row["split"] != "dev" or not re.fullmatch(r"[a-f0-9]{64}", task_digest):
            raise ValueError("invalid DEV task identity")
        files, text_files, binary_files = {}, [], []
        prefix = f"tasks/{case}/"
        for relative, expected in sorted(view["files"].items()):
            if not relative.startswith(prefix):
                continue
            if ".." in PurePosixPath(relative).parts or PurePosixPath(relative).is_absolute():
                raise ValueError("source view has an unsafe path")
            data = regular_file(view_root / relative)
            if digest(data) != expected:
                raise ValueError(f"sealed source digest mismatch: {relative}")
            local = "source/" + relative.removeprefix(prefix)
            target = out / "evidence" / case / local
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            files[local] = expected
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                binary_files.append(local)
            else:
                if b"\0" in data:
                    binary_files.append(local)
                else:
                    text_files.append(local)
        if not files:
            raise ValueError(f"no retained source for {case}")
        tasks.append(
            {
                "case": case,
                "task_tree_sha256": task_digest,
                "source_files": files,
                "text_files": text_files,
                "binary_files": binary_files,
                "source_inventory_sha256": digest(canonical(files).encode()),
            }
        )
    task_map = {task["case"]: task for task in tasks}
    trials, seen, observed = [], set(), set()
    for original in config["trials"]:
        row = dict(original)
        case, auditor = checked_name(row["case"]), checked_name(row["auditor"])
        run_id, trial_id = checked_name(row["run_id"]), checked_name(row["trial_id"])
        identity = (run_id, trial_id)
        if case not in task_map or auditor not in auditors or identity in seen:
            raise ValueError("unknown cohort member or duplicate trial identity")
        if row["state"] not in STATES:
            raise ValueError("unrecognized execution state; do not infer completion from a report")
        if not isinstance(row.get("task_digest"), str):
            raise ValueError("each retained trial requires its original task digest")
        if row["task_digest"].removeprefix("sha256:") != task_map[case]["task_tree_sha256"]:
            raise ValueError("auditor output belongs to a different task digest")
        seen.add(identity)
        observed.add((auditor, case))
        files, origins = {}, {}
        workspace = out / "evidence" / case / "trials" / f"{run_id}--{trial_id}"
        workspace.mkdir(parents=True)
        for scope, field in (("authored", "authored_files"), ("trace", "trace_files")):
            for index, value in enumerate(row.get(field, [])):
                source = Path(value).expanduser().absolute()
                data = regular_file(source)
                data.decode("utf-8")
                relative = f"{scope}/{index:03d}-{source.name}"
                target = workspace / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                files[relative] = digest(data)
                origins[relative] = str(source)
        if row["state"] in TERMINAL and not any(path.startswith("authored/") for path in files):
            raise ValueError(
                "a completed trial needs retained authored output, even an empty report"
            )
        trials.append(
            {
                "run_id": run_id,
                "trial_id": trial_id,
                "auditor": auditor,
                "case": case,
                "task_tree_sha256": task_map[case]["task_tree_sha256"],
                "state": row["state"],
                "workspace": str(workspace.relative_to(out)),
                "files": files,
                "origins": origins,
                "source_only": bool(row.get("source_only", False)),
                "reported_model": row.get("reported_model"),
            }
        )
    for auditor in auditors:
        for case in cases:
            if (auditor, case) not in observed:
                trials.append(
                    {
                        "run_id": study_id,
                        "trial_id": f"pending-{auditor}-{case}",
                        "auditor": auditor,
                        "case": case,
                        "task_tree_sha256": task_map[case]["task_tree_sha256"],
                        "state": "pending",
                        "files": {},
                        "origins": {},
                        "workspace": None,
                        "source_only": True,
                    }
                )
    study = {
        "schema": STUDY_SCHEMA,
        "study_id": study_id,
        "split": "dev",
        "auditors": auditors,
        "source_view": str(view_root),
        "source_view_sha256": digest(regular_file(view_root / "bundle.json")),
        "tasks": tasks,
        "trials": trials,
        "scope": "Retained local DEV reports; not Tokenless's unreleased 65-environment study.",
    }
    dump(out / "study.json", study)
    return study


class Evidence:
    def __init__(self, root: Path, task: dict[str, Any], trial: dict[str, Any] | None = None):
        self.sources = root / "evidence" / task["case"]
        self.source_files = task["source_files"]
        self.files = dict(self.source_files)
        self.text_files = set(task["text_files"])
        self.binary_files = task["binary_files"]
        self.workspace = root / trial["workspace"] if trial and trial["workspace"] else None
        if trial:
            self.files.update(trial["files"])
            self.text_files.update(trial["files"])
        self.observed_lines: dict[str, set[int]] = defaultdict(set)
        self.verify()

    def path(self, name: str) -> Path:
        if (
            name not in self.files
            or PurePosixPath(name).is_absolute()
            or ".." in PurePosixPath(name).parts
        ):
            raise ValueError("evidence path is outside the sealed inventory")
        if name.startswith("source/"):
            return self.sources / name
        if self.workspace is None:
            raise ValueError("no authored/trace capability in this phase")
        return self.workspace / name

    def verify(self) -> None:
        for name, expected in self.files.items():
            if digest(regular_file(self.path(name))) != expected:
                raise ValueError(f"prepared evidence changed: {name}")

    def run(self, tool: str, args: dict[str, Any], *, authored_only: bool) -> str:
        path = args.get("path", "")
        if not isinstance(path, str):
            raise ValueError("a scoped path is required")
        scope = path.split("/", 1)[0]
        if scope not in ({"authored"} if authored_only else {"source", "trace", "authored"}):
            raise ValueError("this phase cannot access that evidence scope")
        self.verify()
        root = self.sources if scope == "source" else self.workspace
        if root is None:
            raise ValueError("no trace was retained")
        files = {name for name in self.text_files if name.startswith(scope + "/")}
        result = WorkspaceTools(
            root, files, max_read_lines=400, max_chars=16000, clamp_reads=True
        ).run(tool, args)
        originals: dict[str, list[str]] = {}
        for emitted in result.splitlines():
            match = re.fullmatch(r"([^:]+):(\d+): (.*)", emitted)
            if not match or match[1] not in files:
                continue
            name, number = match[1], int(match[2])
            if name not in originals:
                originals[name] = regular_file(self.path(name)).decode("utf-8").splitlines()
            if 1 <= number <= len(originals[name]) and originals[name][number - 1] == match[3]:
                self.observed_lines[name].add(number)
        return result

    def citation(self, cite: dict[str, Any], *, authored: bool) -> None:
        scope, path = cite["scope"], cite["path"]
        if scope not in ({"authored"} if authored else {"source", "trace"}) or not path.startswith(
            scope + "/"
        ):
            raise ValueError("citation scope/path does not match its phase")
        if path not in self.text_files:
            raise ValueError("citation references unavailable text")
        lines = regular_file(self.path(path)).decode("utf-8").splitlines()
        start, end = cite["start_line"], cite["end_line"]
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
            raise ValueError("citation line range is outside the retained file")
        if not authored and not set(range(start, end + 1)) <= self.observed_lines[path]:
            raise ValueError("support citation was not independently read in this judgment")

    def index(self, *, authored_only: bool = False) -> dict[str, Any]:
        names = sorted(
            name for name in self.text_files if not authored_only or name.startswith("authored/")
        )
        return {
            "text_files": {
                name: len(regular_file(self.path(name)).decode("utf-8").splitlines())
                for name in names
            },
            "binary_files_not_text_readable": [] if authored_only else self.binary_files,
            "trace_present": any(name.startswith("trace/") for name in names),
        }


def validate_schema(schema: dict[str, Any], value: Any) -> None:
    problems = _schema_problems(schema, value)
    if problems:
        raise ValueError("; ".join(problems[:4]))
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise ValueError("unexpected structured-result properties")
        for key, child in properties.items():
            if key in value:
                validate_schema(child, value[key])
    if isinstance(value, list) and "items" in schema:
        for item in value:
            validate_schema(schema["items"], item)
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        raise ValueError("empty required text")
    if type(value) in (int, float) and value < schema.get("minimum", float("-inf")):
        raise ValueError("numeric result below its minimum")


class Judge:
    def __init__(self, *, model: str, approval: str, budget_dir: Path, max_usd: float, out: Path):
        if not approval.strip() or not 0 < max_usd <= 1:
            raise ValueError(
                "an explicit approval and a positive cumulative cap at most $1 are required"
            )
        factory, _ = upstream()
        self.route = openrouter_route(model)
        self.policy = factory(
            self.route.native_model,
            reasoning_effort=self.route.reasoning_pin["effort"],
            max_tokens=MAX_OUTPUT,
            request_deadline_sec=180,
        )
        self.config = {
            "upstream_commit": UPSTREAM_COMMIT,
            "adapter": ADAPTER_VERSION,
            "adapter_sha256": digest(Path(__file__).read_bytes()),
            "policy": self.policy,
            "provider": dict(self.route.provider_pin),
            "reasoning": {**self.route.reasoning_pin, "exclude": True},
            "response_transport": "json_object: disjoint calls[] or submit_result; no native tools",
        }
        self.config = json.loads(canonical(self.config))
        self.fingerprint = digest(canonical(self.config).encode())
        self.out = out
        self.sequence = 0
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            raise ValueError(
                "missing OPENROUTER_API_KEY; use keys run -- with the approved command"
            )
        self.transport = OpenAIInvestigator(
            endpoint="https://openrouter.ai/api/v1",
            model=self.route.native_model,
            api_key=key,
            timeout_seconds=180,
            response_format="json_object",
            provider=dict(self.route.provider_pin),
            reasoning=self.config["reasoning"],
        )
        self.budget = InvestigationBudget(
            budget_dir / "ledger.jsonl",
            budget_usd=max_usd,
            max_calls=512,
            input_usd_per_million=self.route.input_cost_micros_per_million / 1_000_000,
            output_usd_per_million=self.route.output_cost_micros_per_million / 1_000_000,
        )
        approval_path = budget_dir / "approval.json"
        expected = {"approval": approval, "config": self.config}
        if approval_path.exists():
            if json.loads(regular_file(approval_path)) != expected:
                raise ValueError("judge configuration and approval must not change within a budget")
        else:
            dump(approval_path, expected)
        dump(out / "judge.json", self.config)

    def phase(
        self, phase: str, identifier: str, payload: Any, evidence: Evidence | None = None
    ) -> dict[str, Any]:
        schema = self.policy["schemas"][phase]
        envelope = {
            "type": "object",
            "properties": {
                "submit_result": schema,
                "calls": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 4,
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string", "enum": ["read", "grep", "diff"]},
                            "args": {"type": "object"},
                        },
                        "required": ["tool", "args"],
                        "additionalProperties": False,
                    },
                },
            },
            "additionalProperties": False,
            "oneOf": [
                {"required": ["submit_result"], "not": {"required": ["calls"]}},
                {"required": ["calls"], "not": {"required": ["submit_result"]}},
            ],
        }
        system = (
            self.policy["base"]
            + "\n"
            + self.policy["prompts"][phase]
            + "\n"
            + self.policy["submit_description"]
        )
        system += '\nTransport: return exactly {"submit_result": RESULT} to invoke submit_result, or {"calls": [{"tool": NAME, "args": ARGS}]}. Never both. At most four read-only tools per turn.'
        system += "\nThere is no native function-calling API. Tool names in the policy are virtual JSON actions only. Respond in message content, never native tool_calls. Omit the unused action key entirely, including empty calls arrays."
        if evidence:
            system += (
                "\n"
                + TOOL_HELP
                + "\nCitation paths MUST include their scope prefix: authored/, source/, or trace/."
            )
            if phase == "validate":
                system += "\nIndependently read every support citation's full line range using read/grep before submitting a verdict. Authored allegations are not source proof."
        else:
            system += "\nAll frozen issue information is supplied; no tools exist in this phase."
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": canonical(payload)},
        ]
        for turn in range(self.policy["calls"][phase]):
            request = _payload(
                self.route.native_model,
                messages,
                MAX_OUTPUT,
                envelope,
                response_format="json_object",
                provider=dict(self.route.provider_pin),
                reasoning=self.config["reasoning"],
            )
            if (
                len(canonical(request).encode()) + 1024 + MAX_OUTPUT
                > self.route.context_input_tokens
            ):
                raise ValueError(
                    "judge context ceiling reached; no evidence was silently truncated"
                )
            reservation = self.budget.reserve(
                pass_id=f"{identifier}/{phase}/{turn}",
                payload=request,
                max_output_tokens=MAX_OUTPUT,
            )
            self.sequence += 1
            call_path = self.out / "calls" / f"{self.sequence:04d}.json"
            dump(
                call_path,
                {
                    "identifier": identifier,
                    "phase": phase,
                    "request": request,
                    "invocation_id": reservation.invocation_id,
                    "reserved_usd": reservation.reserved_usd,
                },
            )
            try:
                completion = self.transport.complete(
                    messages, max_output_tokens=MAX_OUTPUT, schema=envelope
                )
            except Exception as exc:
                observed = exc.observed_usage if isinstance(exc, InvestigatorError) else None
                self.budget.finish(reservation, None, failed=True, observed_usage=observed)
                dump(
                    call_path.with_suffix(".failure.json"),
                    {"error": str(exc), "observed_usage": observed},
                )
                raise
            self.budget.finish(reservation, completion, failed=False)
            dump(call_path.with_suffix(".response.json"), vars(completion))
            if completion.returned_model not in self.route.returned_models:
                raise ValueError(
                    "provider returned a different or unidentified model; no fallback is accepted"
                )
            answer = json.loads(completion.content)
            validate_schema(envelope, answer)
            if set(answer) == {"submit_result"}:
                return answer["submit_result"]
            if set(answer) != {"calls"} or evidence is None or not 1 <= len(answer["calls"]) <= 4:
                raise ValueError(
                    "invalid judge action; a complete result or 1..4 permitted reads is required"
                )
            results = []
            for call in answer["calls"]:
                try:
                    result = evidence.run(
                        call["tool"], call["args"], authored_only=phase == "authored"
                    )
                    results.append({"call": call, "ok": True, "content": result})
                except (OSError, ValueError, UnicodeError) as exc:
                    results.append({"call": call, "ok": False, "error": str(exc)})
            messages.extend(
                [
                    {"role": "assistant", "content": completion.content},
                    {"role": "user", "content": canonical({"untrusted_tool_results": results})},
                ]
            )
        raise ValueError(f"{phase} call ceiling reached without a complete result")


def partition(groups: list[list[str]], expected: set[str]) -> None:
    flattened = [item for group in groups for item in group]
    if (
        any(not group for group in groups)
        or len(flattened) != len(set(flattened))
        or set(flattened) != expected
    ):
        raise ValueError("link result must partition every supplied finding exactly once")


def importance(votes: list[dict[str, Any]]) -> str:
    if len(votes) != 5 or any(vote.get("importance") not in LEVELS for vote in votes):
        raise ValueError("importance requires all five valid blinded votes")
    return LEVELS[int(statistics.median(LEVELS.index(vote["importance"]) for vote in votes))]


def frozen(claim: dict[str, Any]) -> dict[str, Any]:
    return {key: claim["allegation"][key] for key in ("title", "scope", "qualification")}


def grade(
    study_dir: Path, out: Path, *, model: str, approval: str, budget_dir: Path, max_usd: float
) -> dict[str, Any]:
    factory, rank_catalog = upstream()
    del factory
    study = json.loads(regular_file(study_dir / "study.json"))
    if study.get("schema") != STUDY_SCHEMA or study.get("split") != "dev":
        raise ValueError("only a prepared DEV study is supported")
    if out.exists():
        raise ValueError("grading output already exists; do not overwrite judgments")
    for task in study["tasks"]:
        Evidence(study_dir, task)
        for trial in study["trials"]:
            if trial["case"] == task["case"]:
                Evidence(study_dir, task, trial)
    out.mkdir(parents=True)
    judge = Judge(model=model, approval=approval, budget_dir=budget_dir, max_usd=max_usd, out=out)
    catalog: dict[str, Any] = {
        "schema_version": 1,
        "assertion_contract": "assessment-defect-attribution-v2",
        "study_id": study["study_id"],
        "grading_policy_id": judge.policy["policy"],
        "grading_policy_sha256": judge.fingerprint,
        "scope": study["scope"],
        "adapter": ADAPTER_VERSION,
        "upstream_commit": UPSTREAM_COMMIT,
        "prepared_study_sha256": digest(regular_file(study_dir / "study.json")),
        "runs": [{"auditor": auditor} for auditor in study["auditors"]],
        "tasks": [],
        "trials": [],
        "claims": [],
    }
    errors = []
    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for original in study["trials"]:
        trial = {
            key: original[key]
            for key in ("run_id", "trial_id", "auditor", "case", "task_tree_sha256", "state")
        }
        trial.update(
            extraction_status="pending", matching_status="pending", importance_status="pending"
        )
        catalog["trials"].append(trial)
        by_case[trial["case"]].append(trial)
    for task in study["tasks"]:
        case = task["case"]
        target = {"case": case, "task_tree_sha256": task["task_tree_sha256"], "issues": []}
        catalog["tasks"].append(target)
        case_claims = []
        for trial in by_case[case]:
            if trial["state"] not in TERMINAL:
                continue
            original = next(
                row
                for row in study["trials"]
                if (row["run_id"], row["trial_id"]) == (trial["run_id"], trial["trial_id"])
            )
            evidence = Evidence(study_dir, task, original)
            identifier = f"{case}/{trial['run_id']}/{trial['trial_id']}"
            try:
                authored = {
                    name: "\n".join(
                        f"{i}: {line}"
                        for i, line in enumerate(
                            regular_file(evidence.path(name)).decode().splitlines(), 1
                        )
                    )
                    for name in sorted(evidence.text_files)
                    if name.startswith("authored/")
                }
                extracted = judge.phase(
                    "authored",
                    identifier,
                    {"index": evidence.index(authored_only=True), "authored": authored},
                    evidence,
                )
                dump(out / "phases" / f"{digest(identifier.encode())}-authored.json", extracted)
                if not extracted["review_complete"]:
                    raise ValueError("authored extraction is incomplete")
                for item in [*extracted["findings"], *extracted["nonfindings"]]:
                    evidence.citation(item["assertion"]["citation"], authored=True)
                trial["extraction_status"] = "complete"
                claims = []
                for index, allegation in enumerate(extracted["findings"]):
                    claim = {
                        "id": "f-" + digest(f"{identifier}/{index}".encode())[:20],
                        "run_id": trial["run_id"],
                        "trial_id": trial["trial_id"],
                        "auditor": trial["auditor"],
                        "allegation": allegation,
                        "status": "unresolved",
                        "grading_policy_id": judge.policy["policy"],
                        "grading_policy_sha256": judge.fingerprint,
                    }
                    catalog["claims"].append(claim)
                    claims.append(claim)
                    case_claims.append(claim)
                if claims:
                    assessed = judge.phase(
                        "validate",
                        identifier,
                        {
                            "allegations": [{"id": c["id"], **c["allegation"]} for c in claims],
                            "index": evidence.index(),
                            "trace_limit": "Source-only audit; no own replay was run."
                            if original["source_only"]
                            else "Use retained trace evidence only.",
                        },
                        evidence,
                    )
                    if len(assessed["verdicts"]) != len(claims) or {
                        v["id"] for v in assessed["verdicts"]
                    } != {c["id"] for c in claims}:
                        raise ValueError(
                            "validation must return exactly one verdict per frozen allegation"
                        )
                    verdicts = {v["id"]: v for v in assessed["verdicts"]}
                    for claim in claims:
                        verdict = verdicts[claim["id"]]
                        for cite in verdict["citations"]:
                            evidence.citation(cite, authored=False)
                        if verdict["status"] == "supported" and not any(
                            c["scope"] == "source" for c in verdict["citations"]
                        ):
                            raise ValueError(
                                "supported claims require independent pinned source citations"
                            )
                        if verdict["observed_execution"] and (
                            original["source_only"]
                            or not any(c["scope"] == "trace" for c in verdict["citations"])
                        ):
                            raise ValueError(
                                "a static source proof cannot be labelled an observed replay"
                            )
                    for claim in claims:
                        claim.update(verdicts[claim["id"]])
                    dump(out / "phases" / f"{digest(identifier.encode())}-validate.json", assessed)
            except (OSError, ValueError, RuntimeError) as exc:
                trial["grading_error"] = str(exc)
                if trial["extraction_status"] != "complete":
                    trial["extraction_status"] = "failed"
                errors.append(
                    {"phase": "extraction_or_validation", "trial": identifier, "error": str(exc)}
                )
        supported = {claim["id"]: claim for claim in case_claims if claim["status"] == "supported"}
        try:
            linked = (
                judge.phase(
                    "link",
                    case,
                    {
                        "findings": [
                            {"id": key, **frozen(value)} for key, value in sorted(supported.items())
                        ]
                    },
                )
                if supported
                else {"groups": [], "uncertain_pairs": []}
            )
            partition(linked["groups"], set(supported))
            uncertain = set()
            for pair in linked["uncertain_pairs"]:
                if (
                    pair["left"] not in supported
                    or pair["right"] not in supported
                    or pair["left"] == pair["right"]
                ):
                    raise ValueError("uncertain equivalence references unknown finding IDs")
                uncertain.update((pair["left"], pair["right"]))
            for group in linked["groups"]:
                if len(group) > 1 and any(
                    p["left"] in group and p["right"] in group for p in linked["uncertain_pairs"]
                ):
                    raise ValueError("uncertain findings must remain in separate groups")
            dump(out / "phases" / f"{case}-link.json", linked)
            for trial in by_case[case]:
                if trial["extraction_status"] != "complete":
                    continue
                owned = {
                    c["id"]
                    for c in case_claims
                    if (c["run_id"], c["trial_id"]) == (trial["run_id"], trial["trial_id"])
                }
                trial["matching_status"] = "unresolved" if owned & uncertain else "complete"
                trial["importance_status"] = "complete"
            for group in linked["groups"]:
                issue_id = "i-" + digest(canonical(sorted(group)).encode())[:20]
                issue = {
                    "id": issue_id,
                    "status": "supported",
                    "importance": None,
                    "importance_status": "pending",
                    "claim_ids": group,
                    "discoveries": [
                        {key: supported[c][key] for key in ("run_id", "trial_id", "auditor")}
                        for c in group
                    ],
                }
                target["issues"].append(issue)
                try:
                    descriptions = sorted({canonical(frozen(supported[c])) for c in group})
                    payload = {"frozen_issue": [json.loads(value) for value in descriptions]}
                    votes = [
                        judge.phase("impact", f"{case}/{issue_id}/vote-{vote}", payload)
                        for vote in range(5)
                    ]
                    issue.update(
                        importance=importance(votes), importance_status="complete", votes=votes
                    )
                except (OSError, ValueError, RuntimeError) as exc:
                    issue["importance_status"] = "failed"
                    issue["grading_error"] = str(exc)
                    owners = {(d["run_id"], d["trial_id"]) for d in issue["discoveries"]}
                    for trial in by_case[case]:
                        if (trial["run_id"], trial["trial_id"]) in owners:
                            trial["importance_status"] = "failed"
                    errors.append({"phase": "importance", "issue": issue_id, "error": str(exc)})
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append({"phase": "link", "case": case, "error": str(exc)})
            for trial in by_case[case]:
                if trial["extraction_status"] == "complete":
                    trial["matching_status"] = "failed"
    dump(out / "catalog.json", catalog)
    rankings = rank_catalog(
        catalog, bootstrap_samples=400, seed=20261021, include_unregularized=True
    )
    rankings["catalog_sha256"] = digest(regular_file(out / "catalog.json"))
    dump(out / "rankings.json", rankings)
    result = {
        "study_id": study["study_id"],
        "scope": study["scope"],
        "provisional": rankings["provisional"],
        "judge_fingerprint": judge.fingerprint,
        "upstream_commit": UPSTREAM_COMMIT,
        "calls": judge.sequence,
        "catalog": str(out / "catalog.json"),
        "rankings": str(out / "rankings.json"),
        "errors": errors,
        "budget_ledger": str(budget_dir / "ledger.jsonl"),
        "max_usd": max_usd,
        "limits": "Different judge/configuration and local DEV pool: not comparable with Tokenless leaderboard Elo. Static support is not an executed exploit or semantic certification.",
    }
    dump(out / "receipt.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser(
        "prepare", help="freeze retained reports and a sealed DEV task view; no model call"
    )
    prep.add_argument("config", type=Path)
    prep.add_argument("--out", type=Path, required=True)
    score = commands.add_parser(
        "grade", help="run the published judge policy and pinned official offline scorer"
    )
    score.add_argument("study", type=Path)
    score.add_argument("--out", type=Path, required=True)
    score.add_argument("--model", default=DEFAULT_MODEL)
    score.add_argument("--budget-dir", type=Path, required=True)
    score.add_argument("--max-usd", type=float, required=True)
    score.add_argument("--approve", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(json.loads(regular_file(args.config.absolute())), args.out.absolute())
        print(
            json.dumps(
                {
                    "study": str(args.out / "study.json"),
                    "tasks": len(result["tasks"]),
                    "trials": len(result["trials"]),
                }
            )
        )
        return 0
    result = grade(
        args.study.absolute(),
        args.out.absolute(),
        model=args.model,
        approval=args.approve,
        budget_dir=args.budget_dir.absolute(),
        max_usd=args.max_usd,
    )
    print(json.dumps(result, indent=2))
    return 1 if result["provisional"] or result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
