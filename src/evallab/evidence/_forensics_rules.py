from __future__ import annotations

import json
import re
import shlex
from typing import Any

from ._forensics_shell import is_mutating_shell_cmd
from ._multiagent_io import MemberRecord, NormalizedAtifStep, NormalizedToolCall, TrialFormat
from .multiagent_adapter import NormalizedMultiAgentTrial
from .world_events import NormalizedWorldEvent, WorldEventLog

SYSTEM_ACTORS = frozenset({"world", "system", "@harness", "harbor"})
_WS_RE = re.compile(r"\s+")
_JSON_ID_TOKEN_RE = re.compile(
    r'(?="(?:id|seq|msg_id|message_id)"\s*:\s*'
    r'(?:"([^"\n]{1,128})"(?![a-z0-9_])|([a-z0-9_:/-]+(?:\.[a-z0-9_:/-]+)*)(?![a-z0-9_])))'
)
_KV_ID_TOKEN_RE = re.compile(
    r"(?=\b(?:id|seq)\s*=\s*([a-z0-9_:/-]+(?:\.[a-z0-9_:/-]+)*)(?![a-z0-9_]))"
)
_SIMPLE_ID_RE = re.compile(r"^[a-z0-9_:/-]+(?:\.[a-z0-9_:/-]+)*$")
_REPO_PREFIXES = ("/workspace/", "/app/", "/repo/", "/testbed/", "./")
_MUSTER_READ_WRITE_RESOURCE = {
    "claims": "claim",
    "pledges": "pledge",
    "ledger": "answer",
    "status": "answer",
}
NON_DUPLICABLE_ACTIONS = frozenset(
    {"vote", "accuse", "truce_signature", "pledge", "take", "transfer", "claim", "refine"}
)
FILE_READ_TOOLS = frozenset(
    {"read_file", "view_file", "file_read", "read", "view", "cat", "open_file", "grep", "rg"}
)
BASH_TOOLS = frozenset(
    {
        "bash",
        "bash_command",
        "shell",
        "shell_command",
        "terminal",
        "execute_command",
        "exec_command",
        "run_command",
        "run_shell_command",
        "local_shell",
    }
)
_NON_MUTATING_TOOLS = FILE_READ_TOOLS | frozenset(
    {
        "inbox",
        "history",
        "list_dir",
        "find",
        "agent_status",
        "done",
        "seat_done",
        "message_read",
        "scan",
        "plan_read",
        "workspace_read",
        "workspace_list",
        "submission_read",
        "dossier_read",
        "test_read",
        "observation_read",
        "msgs",
        "rules",
        "status",
        "ledger",
    }
)
_BENIGN_401_RAW_KINDS = frozenset(
    {"rules", "mine", "msgs", "ledger", "status", "help", "plan", "state"}
)
_BENIGN_CRUCIBLE_AUTH_REASONS = frozenset(
    {"invalid_token", "missing_credentials", "missing_or_invalid_token", "Invalid seat token."}
)
FETCH_TOOL_HINTS = (
    "inbox",
    "history",
    "msgs",
    "messages",
    "message_read",
    "reports",
    "chat/",
    "board",
    "poll",
    "feed",
    "fetch",
)
SEND_TOOL_HINTS = (
    "send",
    "post",
    "msg",
    "chat",
    "board",
    "report",
    "notify",
    "dm",
    "publish",
    "broadcast",
    "reply",
)
GENERIC_RAW_KINDS = frozenset(
    {
        "git_commit",
        "msg",
        "msgs",
        "send",
        "inbox",
        "history",
        "read",
        "post",
        "status",
        "scan",
        "plan",
        "claim",
        "mine",
        "submit",
        "open",
        "take",
        "pledge",
        "answer",
        "rules",
        "ledger",
        "done",
        "seat_done",
    }
)


def normalize_text(text: str) -> str:
    cleaned = (
        text.replace("\\r\\n", " ")
        .replace("\\n", " ")
        .replace("\\t", " ")
        .replace("\\r", " ")
        .replace('\\"', '"')
        .replace("\\'", "'")
    )
    return _WS_RE.sub(" ", cleaned).strip().lower()


def normalize_repo_path(raw: str) -> str:
    cleaned = raw.strip()
    pos = 0
    changed = True
    while changed:
        changed = False
        for prefix in _REPO_PREFIXES:
            if cleaned.startswith(prefix, pos):
                pos += len(prefix)
                changed = True
    return cleaned[pos:]


def short_tool_name(name: str) -> str:
    return name.lower().rsplit("__", 1)[-1].rsplit(":", 1)[-1]


def unwrap_observation_text(raw: str) -> str:
    stripped = raw.strip()
    if (stripped.startswith("{") and stripped.endswith("}")) or (
        stripped.startswith("[") and stripped.endswith("]")
    ):
        try:
            parsed = json.loads(stripped)
            parts = [raw]
            if isinstance(parsed, dict):
                for key in ("output", "stdout", "result", "content", "text"):
                    val = parsed.get(key)
                    if isinstance(val, str) and val:
                        parts.append(val)
                for list_key in ("messages", "result"):
                    msgs = parsed.get(list_key)
                    if isinstance(msgs, list):
                        for item in msgs[:128]:
                            if isinstance(item, dict):
                                for k in ("text", "body", "content", "message"):
                                    if isinstance(item.get(k), str) and item[k]:
                                        parts.append(item[k])
            elif isinstance(parsed, list):
                for item in parsed[:128]:
                    if isinstance(item, dict):
                        for k in ("text", "body", "content", "message", "output"):
                            if isinstance(item.get(k), str) and item[k]:
                                parts.append(item[k])
            return "\n".join(parts)
        except (ValueError, RecursionError):
            pass
    return raw


def step_observation_corpus(step: NormalizedAtifStep) -> str:
    chunks = [unwrap_observation_text(obs.content) for obs in step.observations if obs.content]
    if step.source == "tool" and step.message:
        chunks.append(unwrap_observation_text(step.message))
    elif step.source == "user" and step.message and (step.ts_epoch is not None or step.step_id > 2):
        chunks.append(step.message)
    return "\n".join(chunks)


def extract_id_tokens(norm_corpus: str) -> frozenset[str]:
    if not norm_corpus or ("id" not in norm_corpus and "seq" not in norm_corpus):
        return frozenset()
    out: set[str] = set()
    for g1, g2 in _JSON_ID_TOKEN_RE.findall(norm_corpus):
        tok = g1 or g2
        if tok:
            out.add(tok)
    for tok in _KV_ID_TOKEN_RE.findall(norm_corpus):
        if tok:
            out.add(tok)
    return frozenset(out)


def is_simple_id_token(mid: str) -> bool:
    return bool(mid) and len(mid) <= 128 and _SIMPLE_ID_RE.match(mid.lower()) is not None


def has_id_token(norm_corpus: str, mid: str, *, obs_ids: frozenset[str] | None = None) -> bool:
    if not mid or not norm_corpus:
        return False
    mid_low = mid.lower()
    if obs_ids is not None:
        if mid_low in obs_ids:
            return True
        if len(mid_low) <= 128 and _SIMPLE_ID_RE.match(mid_low) is not None:
            return False
    if "id" not in norm_corpus and "seq" not in norm_corpus:
        return False
    esc = re.escape(mid_low)
    unq_bound = r"(?![a-z0-9_:/-]|\.[a-z0-9_:/-])"
    pattern = (
        rf'"(?:id|seq|msg_id|message_id)"\s*:\s*'
        rf'(?:"{esc}"(?![a-z0-9_])|{esc}{unq_bound})'
        rf"|\b(?:id|seq)\s*=\s*{esc}{unq_bound}"
    )
    return re.search(pattern, norm_corpus) is not None


def same_log(a: NormalizedWorldEvent, b: NormalizedWorldEvent) -> bool:
    board = {"bespoke_board_v2", "bespoke_board_legacy", "bespoke_public_board"}
    if a.source_format in board and b.source_format in board:
        return True
    return a.source_format == b.source_format


def happened_before(
    earlier: NormalizedWorldEvent, later: NormalizedWorldEvent, *, inclusive: bool = False
) -> bool:
    if same_log(earlier, later):
        if (
            earlier.source_format == "bespoke_git_bundle"
            and earlier.ts_epoch is not None
            and later.ts_epoch is not None
            and earlier.ts_epoch != later.ts_epoch
        ):
            return earlier.ts_epoch < later.ts_epoch
        return earlier.seq <= later.seq if inclusive else earlier.seq < later.seq
    if earlier.ts_epoch is None or later.ts_epoch is None:
        return False
    if inclusive and later.source_format == "bespoke_git_bundle":
        return earlier.ts_epoch <= later.ts_epoch + 1.0
    return earlier.ts_epoch <= later.ts_epoch if inclusive else earlier.ts_epoch < later.ts_epoch


def read_targets(ev: NormalizedWorldEvent) -> list[str]:
    if ev.files:
        return [normalize_repo_path(f) for f in ev.files]
    if ev.source_format == "olam_muster_world" and ev.raw_kind in _MUSTER_READ_WRITE_RESOURCE:
        return [_MUSTER_READ_WRITE_RESOURCE[ev.raw_kind]]
    if ev.target and (ev.target.startswith("sector:") or ev.target.endswith(".md")):
        return [ev.target]
    return []


def write_targets(ev: NormalizedWorldEvent) -> list[str]:
    if ev.files:
        return [normalize_repo_path(f) for f in ev.files]
    if (
        ev.source_format == "olam_muster_world"
        and ev.raw_kind in _MUSTER_READ_WRITE_RESOURCE.values()
    ):
        return [ev.raw_kind]
    if ev.target and (ev.target.startswith("sector:") or ev.target.startswith("service:")):
        return [ev.target]
    return []


def is_duplicate_eligible_action(act: NormalizedWorldEvent) -> bool:
    if act.actor is None or act.raw_kind in NON_DUPLICABLE_ACTIONS:
        return False
    if act.raw_kind == "mine":
        material = act.detail.get("material")
        units = act.detail.get("units")
        if material == "keystone" and type(units) is int and units >= 1:
            return False
    return True


def is_author_spoof_event(ev: NormalizedWorldEvent, roster_set: set[str]) -> bool:
    if ev.kind in ("lifecycle", "world_internal", "policy_refusal") or ev.actor in SYSTEM_ACTORS:
        return False
    if ev.detail.get("is_seed"):
        return False
    if ev.raw_kind == "impersonation_attempt":
        reason = ev.detail.get("reason")
        if reason in _BENIGN_CRUCIBLE_AUTH_REASONS:
            return False
        if reason is not None:
            return True
    if ev.claimed_actor is not None:
        if ev.actor is None or ev.actor == "unknown":
            return True
        if ev.claimed_actor.lower() != ev.actor.lower():
            return True
    if (
        ev.source_format in ("bespoke_board_v2", "bespoke_board_legacy", "bespoke_public_board")
        and ev.kind in ("message_post", "message_fetch")
        and ev.actor not in roster_set
    ):
        return True
    if ev.source_format == "bespoke_git_bundle" and ev.authenticated is False:
        return True
    if ev.kind == "auth_violation" and ev.authenticated is False:
        method = str(ev.detail.get("method") or "").upper()
        if (
            method in ("POST", "PUT", "PATCH", "DELETE")
            and ev.raw_kind not in _BENIGN_401_RAW_KINDS
        ):
            return True
    return False


def is_post_eop_mutation(ev: NormalizedWorldEvent) -> bool:
    return bool(
        (ev.phase == "post_play" or ev.late)
        and ev.accepted
        and ev.kind in ("world_action", "artifact_write", "message_post")
        and ev.actor not in SYSTEM_ACTORS
        and not ev.detail.get("is_seed")
    )


def shell_command_text(arguments: dict[str, Any]) -> str:
    raw = (
        arguments.get("command")
        or arguments.get("keystrokes")
        or arguments.get("cmd")
        or arguments.get("script")
        or arguments.get("argv")
        or ""
    )
    if isinstance(raw, list):
        parts = [str(p) for p in raw if p is not None]
        if (
            len(parts) >= 3
            and parts[0].rsplit("/", 1)[-1] in ("bash", "sh", "zsh", "dash")
            and parts[1] in ("-c", "-lc", "-cl")
        ):
            return parts[2]
        return shlex.join(parts)
    return str(raw)


def compute_auth_mismatches(
    trial: NormalizedMultiAgentTrial, world_log: WorldEventLog, roster_set: set[str]
) -> tuple[int, set[str], list[str]]:
    count = 0
    flagged_members: set[str] = set()
    issues: list[str] = []
    for ev in (*world_log.events, *world_log.git_events):
        if is_author_spoof_event(ev, roster_set):
            count += 1
            if ev.actor in roster_set:
                flagged_members.add(ev.actor)
            if ev.claimed_actor in roster_set:
                flagged_members.add(ev.claimed_actor)
            issues.append(
                f"unauthenticated_author_mismatch:{ev.source_format}:seq={ev.seq}:{ev.actor or 'unknown'}"
            )
    if trial.unauthenticated_proxy_requests > 0:
        count += trial.unauthenticated_proxy_requests
        issues.append(f"unauthenticated_proxy_requests:{trial.unauthenticated_proxy_requests}")
    return count, flagged_members, issues


def member_logs_complete(member: MemberRecord, ecosystem: TrialFormat) -> tuple[bool, list[str]]:
    issues: list[str] = []
    if member.logs_downloaded is False:
        issues.append(f"logs_not_downloaded:{member.member_id}")
    if member.lost:
        issues.append(f"lost_agent:{member.member_id}")
    if member.trajectory_status == "invalid":
        issues.append(f"invalid_trajectory:{member.member_id}")
    elif member.trajectory_status == "missing":
        agent_low = (member.agent_name or "").lower()
        if member.ran_model or ecosystem == "bespoke_team" or agent_low == "script":
            issues.append(f"missing_trajectory:{member.member_id}")
        elif agent_low == "oracle" and not member.oracle_log_present:
            issues.append(f"missing_oracle_log:{member.member_id}")
    return len(issues) == 0, issues


def trunk_branches_for_log(world_log: WorldEventLog) -> set[str]:
    shared = set(world_log.git_shared_branches)
    return {"main", "master"} | {
        b for c in world_log.git_commits if c.is_root_commit for b in c.branches if b not in shared
    }


def trial_comms_disabled(trial: NormalizedMultiAgentTrial, world_log: WorldEventLog) -> bool:
    return bool(
        trial.comms_disabled
        or any(ev.detail.get("channel_enabled") is False for ev in world_log.events)
    )


def is_budget_exhausted(budget: Any) -> bool:
    if type(budget) is int:
        return budget <= 0
    if isinstance(budget, dict) and budget:
        vals = [v for v in budget.values() if type(v) is int]
        return bool(vals) and all(v <= 0 for v in vals)
    return False


def is_read_only_tool_call(tc: NormalizedToolCall) -> bool:
    fn = short_tool_name(tc.function_name)
    if fn in _NON_MUTATING_TOOLS:
        return True
    if fn == "str_replace_editor" and str(tc.arguments.get("command") or "") == "view":
        return True
    if fn not in BASH_TOOLS:
        return False
    cmd = shell_command_text(tc.arguments)
    if not cmd.strip():
        return not tc.arguments and not tc.arguments_text.strip()
    return not is_mutating_shell_cmd(cmd)


def is_mutating_atif_step(step: NormalizedAtifStep) -> bool:
    if step.source != "agent" or not step.tool_calls:
        return False
    return any(not is_read_only_tool_call(tc) for tc in step.tool_calls)
