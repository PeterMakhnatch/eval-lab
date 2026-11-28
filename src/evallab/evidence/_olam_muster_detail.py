from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path
from typing import Any

from ._multiagent_io import as_str, sanitize_utf8_text

_FORBIDDEN_DETAIL_KEYS = frozenset({"reward", "rewards", "verifier_result", "score"})
_MAX_DETAIL_ITEMS = 10_000
_MAX_CHAIN_ROWS = 2_000_000
_MAX_CHAIN_FILE_BYTES = 128 * 1024 * 1024
_GENESIS_HASH = "0" * 64
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_UNSIGNED_CHAIN_KEYS = ("entry_hash", "hmac")
_TARGET_KEYS = (
    "room",
    "box",
    "sector",
    "item",
    "slot",
    "target",
    "path",
    "file",
    "round",
    "zone",
    "setting",
    "choice",
)


def _exact_int(value: object) -> int | None:
    return value if type(value) is int else None


def _clean_id(value: object) -> int | str | None:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.strip():
        return sanitize_utf8_text(value.strip())
    return None


def _claim_lock_target(request: dict[str, Any], response: dict[str, Any]) -> str | None:
    for raw in (response.get("target"), request.get("target"), response.get("released")):
        if (txt := as_str(raw)) is not None:
            return txt
    for raw_b in (response.get("box"), request.get("box")):
        if (b := _exact_int(raw_b)) is not None:
            return str(b)
    return None


def verify_muster_hash_chain(rows: list[dict[str, Any]]) -> list[bool] | None:
    if len(rows) > _MAX_CHAIN_ROWS:
        return [False] * _MAX_CHAIN_ROWS
    if not any("prev_hash" in r or "entry_hash" in r for r in rows):
        return None
    has_hmac = any("hmac" in r for r in rows)
    flags: list[bool] = []
    prev = _GENESIS_HASH
    intact = True
    for idx, rec in enumerate(rows, start=1):
        eh = rec.get("entry_hash")
        hm = rec.get("hmac")
        if has_hmac:
            hm_ok = isinstance(hm, str) and _HEX64_RE.fullmatch(hm) is not None
        else:
            hm_ok = hm is None
        ok_row = (
            intact
            and type(rec.get("seq")) is int
            and rec.get("seq") == idx
            and rec.get("prev_hash") == prev
            and isinstance(eh, str)
            and _HEX64_RE.fullmatch(eh) is not None
            and hm_ok
        )
        if ok_row:
            body = {k: v for k, v in rec.items() if k not in _UNSIGNED_CHAIN_KEYS}
            try:
                canon = json.dumps(
                    body,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()
            except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
                ok_row = False
            else:
                ok_row = digest == eh
                if ok_row:
                    prev = digest
        if not ok_row:
            intact = False
        flags.append(intact)
    return flags


def verify_chain_jsonl_file(path: Path, max_lines: int = 500_000) -> bool | None:
    try:
        st = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_CHAIN_FILE_BYTES:
        return False
    rows: list[dict[str, Any]] = []
    raw_count = 0
    total_bytes = 0
    try:
        with path.open("rb") as fh:
            for raw_bytes in fh:
                total_bytes += len(raw_bytes)
                if total_bytes > _MAX_CHAIN_FILE_BYTES:
                    return False
                raw_count += 1
                if raw_count > max_lines:
                    return False
                ln = raw_bytes.decode("utf-8").rstrip("\r\n")
                if not ln.strip():
                    continue
                obj = json.loads(ln)
                if not isinstance(obj, dict):
                    return False
                rows.append(obj)
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return False
    if not rows:
        return None
    flags = verify_muster_hash_chain(rows)
    if flags is None:
        return None
    return bool(flags) and all(flags)


def resolve_muster_fetched_ids(
    response: dict[str, Any], posted_ids: list[tuple[int, str]]
) -> list[str]:
    raw_msgs = response.get("messages")
    if isinstance(raw_msgs, list):
        return [
            sanitize_utf8_text(str(m["id"]))
            for m in raw_msgs[:_MAX_DETAIL_ITEMS]
            if isinstance(m, dict) and m.get("id") is not None
        ]
    rup = _exact_int(response.get("read_up_to"))
    if rup is not None and rup >= 1:
        return [mid for mnum, mid in posted_ids if mnum <= rup]
    return []


def muster_target(raw_kind: str, request: dict[str, Any]) -> str:
    if not request:
        return raw_kind
    key_parts = [
        f"{k}={sanitize_utf8_text(str(request[k]))}"
        for k in _TARGET_KEYS
        if request.get(k) is not None
    ]
    return f"{raw_kind}:{','.join(key_parts)}" if key_parts else raw_kind


def _enrich_open_detail(
    raw_kind: str,
    request: dict[str, Any],
    response: dict[str, Any],
    detail: dict[str, Any],
) -> None:
    box = _exact_int(response.get("box"))
    if box is None:
        box = _exact_int(request.get("box"))
    if box is not None:
        detail["box"] = box
    room = _exact_int(response.get("room"))
    if room is None:
        room = _exact_int(request.get("room"))
    if room is not None:
        detail["room"] = room
    if isinstance(response.get("flag"), bool):
        detail["has_flag"] = response["flag"]
    raw_team = response.get("team_opens")
    if isinstance(raw_team, list):
        detail["team_open_boxes"] = [
            b
            for item in raw_team[:_MAX_DETAIL_ITEMS]
            if isinstance(item, dict) and (b := _exact_int(item.get("box"))) is not None
        ]
    raw_own = response.get("opens")
    if raw_kind == "mine" and isinstance(raw_own, list):
        detail["own_open_boxes"] = [
            b
            for item in raw_own[:_MAX_DETAIL_ITEMS]
            if isinstance(item, dict) and (b := _exact_int(item.get("box"))) is not None
        ]


def _enrich_bargain_detail(
    raw_kind: str,
    request: dict[str, Any],
    response: dict[str, Any],
    detail: dict[str, Any],
) -> None:
    if raw_kind == "game":
        if (pool := _exact_int(response.get("pool"))) is not None:
            detail["pool"] = pool
        if (cap := _exact_int(response.get("cap"))) is not None:
            detail["cap"] = cap
        if (p_need := _exact_int(response.get("your_need"))) is not None:
            detail["private_need"] = p_need
    elif raw_kind in ("claim", "claims"):
        if (cid := _clean_id(response.get("id"))) is not None:
            detail["claim_id"] = cid
        c_need = _exact_int(response.get("need"))
        if c_need is None:
            c_need = _exact_int(request.get("need"))
        if c_need is not None:
            detail["claimed_need"] = c_need
        raw_claims = response.get("claims")
        if isinstance(raw_claims, list):
            has_lock = any(
                isinstance(x, dict) and ("target" in x or "owner" in x)
                for x in raw_claims[:_MAX_DETAIL_ITEMS]
            )
            if not has_lock:
                seen_claims: list[dict[str, Any]] = []
                for item in raw_claims[:_MAX_DETAIL_ITEMS]:
                    if not isinstance(item, dict):
                        continue
                    from_s = as_str(item.get("from"))
                    need_i = _exact_int(item.get("need"))
                    if from_s is not None and need_i is not None:
                        seen_claims.append(
                            {"id": _clean_id(item.get("id")), "from": from_s, "need": need_i}
                        )
                detail["seen_claims"] = seen_claims
    elif raw_kind in ("pledge", "pledges"):
        if (pid := _clean_id(response.get("id"))) is not None:
            detail["pledge_id"] = pid
        p_amt = _exact_int(response.get("amount"))
        if p_amt is None:
            p_amt = _exact_int(request.get("amount"))
        if p_amt is not None:
            detail["pledged_amount"] = p_amt
        raw_pledges = response.get("pledges")
        if isinstance(raw_pledges, list):
            seen_pledges: list[dict[str, Any]] = []
            for item in raw_pledges[:_MAX_DETAIL_ITEMS]:
                if not isinstance(item, dict):
                    continue
                from_s = as_str(item.get("from"))
                amt_i = _exact_int(item.get("amount"))
                if from_s is not None and amt_i is not None:
                    seen_pledges.append(
                        {"id": _clean_id(item.get("id")), "from": from_s, "amount": amt_i}
                    )
            detail["seen_pledges"] = seen_pledges
    elif raw_kind == "take":
        t_amt = _exact_int(response.get("amount"))
        if t_amt is None:
            t_amt = _exact_int(request.get("amount"))
        if t_amt is not None:
            detail["taken_amount"] = t_amt


def _enrich_claim_lock_detail(
    raw_kind: str,
    ok: bool,
    request: dict[str, Any],
    response: dict[str, Any],
    detail: dict[str, Any],
) -> None:
    ctgt = _claim_lock_target(request, response)
    box = _exact_int(response.get("box"))
    if box is None:
        box = _exact_int(request.get("box"))
    if raw_kind == "claim":
        is_lock = ctgt is not None or "owner" in response or response.get("error") == "claim_limit"
        if not is_lock:
            return
        if ctgt is not None:
            detail["claim_target"] = ctgt
        if box is not None:
            detail["box"] = box
        if (owner := as_str(response.get("owner"))) is not None:
            detail["claim_owner"] = owner
        if isinstance(response.get("used"), bool):
            detail["claim_used"] = response["used"]
        if ok:
            detail["claim_granted"] = True
    elif raw_kind == "release":
        if ctgt is not None:
            detail["claim_target"] = ctgt
        if (rel := as_str(response.get("released"))) is not None:
            detail["released_target"] = rel
        if box is not None:
            detail["box"] = box
        if ok:
            detail["claim_released"] = True
        elif (owner := as_str(response.get("owner"))) is not None:
            detail["claim_owner"] = owner
    elif raw_kind == "claims":
        raw_c = response.get("claims")
        if not isinstance(raw_c, list):
            return
        active: list[dict[str, Any]] = []
        for item in raw_c[:_MAX_DETAIL_ITEMS]:
            if not isinstance(item, dict):
                continue
            tgt, own = as_str(item.get("target")), as_str(item.get("owner"))
            if tgt is not None and own is not None:
                active.append({"target": tgt, "owner": own, "used": bool(item.get("used"))})
        if active:
            detail["active_claims"] = active


def _enrich_commit_reveal_detail(
    request: dict[str, Any], response: dict[str, Any], detail: dict[str, Any]
) -> None:
    rnd = _exact_int(response.get("round"))
    if rnd is None:
        rnd = _exact_int(request.get("round"))
    if rnd is not None:
        detail["round"] = rnd
    if (comm := as_str(response.get("commitment"))) is not None:
        detail["commitment"] = comm
    if (wf := _exact_int(response.get("waiting_for"))) is not None:
        detail["waiting_for"] = wf
    if isinstance(response.get("revealed"), bool):
        detail["revealed"] = response["revealed"]
    elif isinstance(response.get("complete"), bool):
        detail["revealed"] = response["complete"]
    raw_c = response.get("committed")
    if isinstance(raw_c, list):
        detail["committed_seats"] = [
            sanitize_utf8_text(s) for s in raw_c[:_MAX_DETAIL_ITEMS] if isinstance(s, str) and s
        ]
    elif type(raw_c) is int:
        detail["committed_count"] = raw_c
    raw_rounds = response.get("rounds")
    if isinstance(raw_rounds, list):
        detail["completed_rounds"] = len(raw_rounds)
        if raw_rounds and isinstance(raw_rounds[-1], dict):
            revs = raw_rounds[-1].get("reveals")
            if isinstance(revs, dict):
                detail["revealed_seats"] = sorted(
                    sanitize_utf8_text(k) for k in revs if isinstance(k, str) and k
                )
    for rkey in ("reveals", "clues"):
        revs = response.get(rkey)
        if isinstance(revs, dict) and "revealed_seats" not in detail:
            detail["revealed_seats"] = sorted(
                sanitize_utf8_text(k) for k in revs if isinstance(k, str) and k
            )


def _enrich_guard_detail(
    ok: bool,
    request: dict[str, Any],
    response: dict[str, Any],
    detail: dict[str, Any],
) -> None:
    if ok:
        return
    err_low = (as_str(response.get("error")) or "").strip().lower()
    if err_low == "unread_messages" or "unread_messages" in err_low or "unread message" in err_low:
        detail["guard_blocked"] = "unread_messages"
        if (uc := _exact_int(response.get("unread_count"))) is not None:
            detail["unread_count"] = uc
        if (lmid := _clean_id(response.get("latest_msg_id"))) is not None:
            detail["latest_msg_id"] = lmid
    elif (
        err_low == "already_claimed" or "already_claimed" in err_low or "already claimed" in err_low
    ):
        detail["guard_blocked"] = "already_claimed"
        owner = (
            as_str(response.get("owner"))
            or as_str(response.get("claim_owner"))
            or as_str(response.get("by"))
        )
        if owner is not None:
            detail["claim_owner"] = owner
        if (ctgt := _claim_lock_target(request, response)) is not None:
            detail["claim_target"] = ctgt
    elif err_low == "claim_limit":
        detail["guard_blocked"] = "claim_limit"
        if (lim := _exact_int(response.get("limit"))) is not None:
            detail["claim_limit"] = lim
    elif err_low == "awaiting_reveals":
        detail["guard_blocked"] = "awaiting_reveals"


def _enrich_answer_detail(
    request: dict[str, Any],
    response: dict[str, Any],
    status_code: int | None,
    detail: dict[str, Any],
) -> None:
    raw_totals = (
        request.get("totals") if isinstance(request.get("totals"), dict) else response.get("totals")
    )
    if isinstance(raw_totals, dict) and 0 < len(raw_totals) <= _MAX_DETAIL_ITEMS:
        clean_totals = {
            sanitize_utf8_text(k): v
            for k, v in sorted(raw_totals.items())
            if isinstance(k, str) and type(v) is int
        }
        if len(clean_totals) == len(raw_totals):
            detail["answer_totals"] = clean_totals
    if accepted_by := as_str(response.get("by")):
        detail["accepted_by"] = accepted_by
    err_low = (as_str(response.get("error")) or "").lower()
    if status_code == 409 or "already answered" in err_low:
        detail["already_answered"] = True


def _enrich_rules_and_msgs(
    raw_kind: str, ok: bool, response: dict[str, Any], detail: dict[str, Any]
) -> None:
    if raw_kind == "rules" and ok:
        if isinstance(response.get("channel"), bool):
            detail["channel_enabled"] = response["channel"]
        if isinstance(response.get("claim_locks"), bool):
            detail["claim_locks_enabled"] = response["claim_locks"]
        if isinstance(response.get("commit_reveal"), bool):
            detail["commit_reveal_enabled"] = response["commit_reveal"]
        if isinstance(response.get("unread_guard"), list):
            detail["unread_guard_kinds"] = [
                sanitize_utf8_text(x)
                for x in response["unread_guard"][:64]
                if isinstance(x, str) and x
            ]
        if (uc := _exact_int(response.get("unread_count"))) is not None:
            detail["unread_count"] = uc
    elif raw_kind == "msgs" and ok:
        if (rup := _exact_int(response.get("read_up_to"))) is not None:
            detail["read_up_to"] = rup
        if (lsi := _exact_int(response.get("last_seen_id"))) is not None:
            detail["last_seen_id"] = lsi
        if (uc := _exact_int(response.get("unread_count"))) is not None:
            detail["unread_count"] = uc


def build_muster_event_detail(
    *,
    raw_kind: str,
    method: str,
    ok: bool,
    status_code: int | None,
    request: dict[str, Any],
    response: dict[str, Any],
) -> dict[str, Any]:
    detail: dict[str, Any] = {}
    if method:
        detail["method"] = method
    if err_msg := as_str(response.get("error")):
        detail["error"] = err_msg
    if "budget_left" in response:
        raw_bl = response["budget_left"]
        if type(raw_bl) is int:
            detail["budget_left"] = raw_bl
        elif isinstance(raw_bl, dict) and len(raw_bl) <= _MAX_DETAIL_ITEMS:
            detail["budget_left"] = {
                sanitize_utf8_text(k): v
                for k, v in sorted(raw_bl.items())
                if isinstance(k, str) and k.lower() not in _FORBIDDEN_DETAIL_KEYS and type(v) is int
            }
    _enrich_rules_and_msgs(raw_kind, ok, response, detail)
    _enrich_guard_detail(ok, request, response, detail)
    if raw_kind in ("open", "opened", "mine"):
        _enrich_open_detail(raw_kind, request, response, detail)
    elif raw_kind in ("game", "claim", "claims", "pledge", "pledges", "take"):
        _enrich_bargain_detail(raw_kind, request, response, detail)
        if raw_kind in ("claim", "claims"):
            _enrich_claim_lock_detail(raw_kind, ok, request, response, detail)
    elif raw_kind == "release":
        _enrich_claim_lock_detail(raw_kind, ok, request, response, detail)
    elif raw_kind in ("commit", "submit_clue", "reveal", "reveals"):
        _enrich_commit_reveal_detail(request, response, detail)
    elif raw_kind == "answer":
        _enrich_answer_detail(request, response, status_code, detail)
    assert not (_FORBIDDEN_DETAIL_KEYS & detail.keys())
    return detail
