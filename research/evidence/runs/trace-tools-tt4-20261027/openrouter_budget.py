"""Durable, process-safe spend-reservation guard for the OpenRouter TT4/TT7 paid arms.

Pure standard library: no HTTP, no keys, no canonical-ledger writes, no default state
location. The transport owns the state directory, supplies a fresh ``fetch_cash`` callback per
reservation, and MUST NOT send an HTTP request until ``reserve`` has returned.

Guarantees
* One ``flock`` on ``state.lock`` spans the fresh cash callback, every check and the durable
  state replacement, so concurrent threads and processes cannot jointly exceed a cap.
* State is replaced atomically (temp file + fsync + ``os.replace`` + directory fsync). After
  the first committed write, an initialized sentinel is written/fsynced in the stable lock
  file before returning. Missing state after initialization makes every operation fail closed.
  A failed write raises before any reservation is returned.
* Attempt ids are unique forever. A duplicate is always refused; it never permits a second send.
* ``pending`` and ``failed`` attempts keep their whole bound; nothing is ever released on error.
  Only ``settle`` with provider-billed evidence turns a hold into the (smaller) actual cost.
* Caps and dispatch approval are constants of this module. A state file that disagrees with them,
  or whose integrity digest does not match, makes every operation fail closed. No method changes
  approval; ``tt7`` is reserved but not approved, so it cannot reserve.
* Cash guard on every reserve: fresh available cash must cover the new bound, every pending/
  failed hold of every arm, every settled actual of every arm (no settled debit is ever assumed
  to be reflected in the balance), and the remaining unspent cap of every other OPEN arm.

Over-bound settlement is fail-closed: the observed cost is recorded, the attempt stays
``pending`` (only ``fail`` may follow) and its exposure becomes ``max(bound, observed)``.
The lock sentinel is never replaced or cleared; a blank lock with no state is uninitialized.
"""

import contextlib
import decimal
import fcntl
import functools
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

SCHEMA = "openrouter-budget-v1"
CAPS = MappingProxyType({"tt4": "14.85", "tt7": "35.00"})
DISPATCH_APPROVED = MappingProxyType({"tt4": True, "tt7": False})
DEFAULT_STATE_DIR = Path.home() / ".local/state/evallab-failure-library/openrouter-budget-20261028"
CASH_MAX_AGE = timedelta(seconds=15)
CASH_MAX_FUTURE = timedelta(seconds=2)
STATE_FILE = "state.json"
LOCK_FILE = "state.lock"
STATUSES = ("pending", "settled", "failed")

_ZERO = Decimal(0)
_CTX = decimal.Context(prec=60)
_INITIALIZED_SENTINEL = b"openrouter-budget-v1:initialized\n"
_PLAIN_DECIMAL = re.compile(r"[0-9]{1,10}(?:\.[0-9]{1,12})?")
_ATTEMPT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}")
_MAX_JSON_BYTES = 4096
_MAX_TEXT = 500
_ARM_KEYS = {
    "cap_usd",
    "dispatch_approved",
    "open",
    "closed_reason",
    "closed_at",
    "released_earmark_usd",
}
_ATTEMPT_KEYS = {
    "arm",
    "status",
    "bound_usd",
    "actual_usd",
    "proof",
    "error",
    "overbound_observed_usd",
    "cash",
    "metadata",
    "created_at",
    "updated_at",
    "seq",
    "history",
}
_STATE_KEYS = {"schema", "seq", "caps", "arms", "attempts", "integrity"}


class BudgetError(RuntimeError):
    """A refusal or integrity failure; ``code`` is the stable machine-readable reason."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class Reservation:
    """A durable hold. The caller may send exactly one HTTP request for ``attempt_id``."""

    attempt_id: str
    arm: str
    bound_usd: Decimal
    reserved_at: str
    cash_available_usd: Decimal
    cash_fetched_at: str
    cash_source: str
    required_cash_usd: Decimal
    arm_used_usd: Decimal
    arm_remaining_usd: Decimal

    def to_dict(self):
        return {
            "attempt_id": self.attempt_id,
            "arm": self.arm,
            "bound_usd": _fmt(self.bound_usd),
            "reserved_at": self.reserved_at,
            "cash_available_usd": _fmt(self.cash_available_usd),
            "cash_fetched_at": self.cash_fetched_at,
            "cash_source": self.cash_source,
            "required_cash_usd": _fmt(self.required_cash_usd),
            "arm_used_usd": _fmt(self.arm_used_usd),
            "arm_remaining_usd": _fmt(self.arm_remaining_usd),
        }


def _utc_now():
    return datetime.now(UTC)


def _fmt(value):
    return format(value, "f")


def _iso(moment):
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _decimal(value, name, *, positive=False):
    """Finite, nonnegative, plain-notation decimal; never a bool, NaN, infinity or negative."""
    if isinstance(value, bool):
        raise BudgetError("invalid_decimal", f"{name} must not be a bool")
    if isinstance(value, str):
        if not _PLAIN_DECIMAL.fullmatch(value):
            raise BudgetError(
                "invalid_decimal", f"{name} must be a plain nonnegative decimal string"
            )
        number = Decimal(value)
    elif isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, float):
        number = Decimal(repr(value))
    else:
        raise BudgetError("invalid_decimal", f"{name} has unsupported type {type(value).__name__}")
    if not number.is_finite() or number.is_signed():
        raise BudgetError("invalid_decimal", f"{name} must be finite and nonnegative")
    if number.as_tuple().exponent < -12 or number.adjusted() > 9:
        raise BudgetError("invalid_decimal", f"{name} is outside the supported range")
    if positive and number == 0:
        raise BudgetError("invalid_decimal", f"{name} must be positive")
    return number


def _aware_utc(value, name):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise BudgetError("invalid_time", f"{name} is not an ISO timestamp") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise BudgetError("invalid_time", f"{name} must be an ISO timestamp in UTC")
    return value.astimezone(UTC)


def _text(value, name, code, limit=_MAX_TEXT):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise BudgetError(code, f"{name} must be a nonempty string of at most {limit} characters")
    return value


def _is_json(value):
    if value is None or isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf"))
    if isinstance(value, list):
        return all(_is_json(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json(item) for key, item in value.items())
    return False


def _json_safe(value, name, code):
    if not _is_json(value):
        raise BudgetError(code, f"{name} must be finite JSON with string keys")
    try:
        encoded = json.dumps(value, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        raise BudgetError(code, f"{name} must be finite JSON with string keys") from None
    if len(encoded.encode()) > _MAX_JSON_BYTES:
        raise BudgetError(code, f"{name} exceeds {_MAX_JSON_BYTES} bytes")
    return json.loads(encoded)


def _attempt_id(value):
    if not isinstance(value, str) or not _ATTEMPT_ID.fullmatch(value):
        raise BudgetError(
            "invalid_attempt_id", "attempt_id must match [A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}"
        )
    return value


def _metadata(value):
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise BudgetError("invalid_metadata", "metadata must be a mapping")
    return _json_safe(dict(value), "metadata", "invalid_metadata")


def _proof(value):
    if isinstance(value, str):
        return _text(value, "proof", "invalid_proof", _MAX_JSON_BYTES)
    if isinstance(value, Mapping) and value:
        return _json_safe(dict(value), "proof", "invalid_proof")
    raise BudgetError(
        "invalid_proof", "proof must be a nonempty string or mapping of provider evidence"
    )


def _error_text(value):
    if isinstance(value, BaseException):
        value = f"{type(value).__name__}: {value}"
    if not isinstance(value, str) or not value.strip():
        raise BudgetError("invalid_error", "error must be a nonempty string or exception")
    return value[:_MAX_TEXT]


def _cash(raw, now):
    """Validate the fresh cash reading; returns (record, available Decimal)."""
    if not isinstance(raw, Mapping):
        raise BudgetError("cash_invalid", "fetch_cash must return a mapping")
    available = raw.get("available_usd")
    if not isinstance(available, str):
        raise BudgetError("cash_invalid", "available_usd must be a decimal string")
    try:
        amount = _decimal(available, "available_usd")
        fetched = _aware_utc(raw.get("fetched_at"), "fetched_at")
        source = _text(raw.get("source"), "source", "cash_invalid", 200)
    except BudgetError as exc:
        raise BudgetError("cash_invalid", str(exc)) from None
    age = now - fetched
    if age > CASH_MAX_AGE:
        raise BudgetError("cash_stale", f"cash reading is {age.total_seconds():.3f}s old (max 15s)")
    if -age > CASH_MAX_FUTURE:
        raise BudgetError(
            "cash_future", f"cash reading is {(-age).total_seconds():.3f}s in the future (max 2s)"
        )
    return {"available_usd": _fmt(amount), "fetched_at": _iso(fetched), "source": source}, amount


def state_digest(state):
    """SHA-256 of the canonical state without its integrity field."""
    body = {key: value for key, value in state.items() if key != "integrity"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _initial_state():
    state = {
        "schema": SCHEMA,
        "seq": 0,
        "caps": dict(CAPS),
        "arms": {
            arm: {
                "cap_usd": cap,
                "dispatch_approved": DISPATCH_APPROVED[arm],
                "open": True,
                "closed_reason": None,
                "closed_at": None,
                "released_earmark_usd": None,
            }
            for arm, cap in CAPS.items()
        },
        "attempts": {},
    }
    state["integrity"] = {"sha256": state_digest(state)}
    return state


def _require(condition, message, code="tampered_state"):
    if not condition:
        raise BudgetError(code, message)


def _stored_decimal(value, name, *, positive=False):
    _require(isinstance(value, str), f"{name} is not stored as a string")
    try:
        return _decimal(value, name, positive=positive)
    except BudgetError as exc:
        raise BudgetError("tampered_state", str(exc)) from None


def _check_attempt(attempt_id, record):
    _require(_ATTEMPT_ID.fullmatch(attempt_id) is not None, f"invalid attempt id {attempt_id!r}")
    _require(
        isinstance(record, dict) and set(record) == _ATTEMPT_KEYS,
        f"attempt {attempt_id!r} has an unexpected shape",
    )
    _require(record["arm"] in CAPS, f"attempt {attempt_id!r} has an unknown arm")
    status = record["status"]
    _require(status in STATUSES, f"attempt {attempt_id!r} has an unknown status")
    bound = _stored_decimal(record["bound_usd"], "bound_usd", positive=True)
    if status == "settled":
        actual = _stored_decimal(record["actual_usd"], "actual_usd")
        _require(actual <= bound, f"attempt {attempt_id!r} settled above its bound")
        _require(
            record["proof"] not in (None, "", {}), f"attempt {attempt_id!r} settled without proof"
        )
        _require(
            record["overbound_observed_usd"] is None,
            f"attempt {attempt_id!r} settled after an over-bound cost",
        )
    else:
        _require(
            record["actual_usd"] is None and record["proof"] is None,
            f"attempt {attempt_id!r} has settlement data without being settled",
        )
        if record["overbound_observed_usd"] is not None:
            observed = _stored_decimal(record["overbound_observed_usd"], "overbound_observed_usd")
            _require(
                observed > bound, f"attempt {attempt_id!r} records a non-excess over-bound cost"
            )
    if status == "failed":
        _require(
            isinstance(record["error"], str) and record["error"],
            f"attempt {attempt_id!r} failed without an error",
        )
    else:
        _require(record["error"] is None, f"attempt {attempt_id!r} has an error without failing")
    cash = record["cash"]
    _require(
        isinstance(cash, dict)
        and set(cash) == {"available_usd", "fetched_at", "source", "required_cash_usd"},
        f"attempt {attempt_id!r} lost its cash evidence",
    )
    _require(
        isinstance(record["metadata"], dict), f"attempt {attempt_id!r} metadata is not a mapping"
    )
    _require(
        isinstance(record["seq"], int) and not isinstance(record["seq"], bool),
        f"attempt {attempt_id!r} seq is invalid",
    )
    for stamp in ("created_at", "updated_at"):
        try:
            _aware_utc(record[stamp], stamp)
        except BudgetError as exc:
            raise BudgetError("tampered_state", f"attempt {attempt_id!r}: {exc}") from None
    history = record["history"]
    _require(
        isinstance(history, list)
        and history
        and all(isinstance(event, dict) for event in history)
        and history[0].get("event") == "reserved",
        f"attempt {attempt_id!r} history is not append-only from 'reserved'",
    )


def _check_state(state):
    _require(isinstance(state, dict) and set(state) == _STATE_KEYS, "state has an unexpected shape")
    _require(state["schema"] == SCHEMA, "state schema is not recognised")
    _require(
        isinstance(state["seq"], int) and not isinstance(state["seq"], bool) and state["seq"] >= 0,
        "state seq is invalid",
    )
    _require(state["caps"] == dict(CAPS), "immutable caps were changed", "tampered_caps")
    arms = state["arms"]
    _require(isinstance(arms, dict) and set(arms) == set(CAPS), "arm set was changed")
    for name, arm in arms.items():
        _require(
            isinstance(arm, dict) and set(arm) == _ARM_KEYS, f"arm {name!r} has an unexpected shape"
        )
        _require(arm["cap_usd"] == CAPS[name], f"arm {name!r} cap was changed", "tampered_caps")
        _require(
            arm["dispatch_approved"] is DISPATCH_APPROVED[name],
            f"arm {name!r} dispatch approval was changed",
            "tampered_approval",
        )
        _require(isinstance(arm["open"], bool), f"arm {name!r} open flag is invalid")
        if arm["open"]:
            _require(
                arm["closed_reason"] is None
                and arm["closed_at"] is None
                and arm["released_earmark_usd"] is None,
                f"open arm {name!r} carries close data",
            )
        else:
            _require(
                isinstance(arm["closed_reason"], str)
                and arm["closed_reason"]
                and isinstance(arm["closed_at"], str),
                f"closed arm {name!r} lost its close data",
            )
            _stored_decimal(arm["released_earmark_usd"], "released_earmark_usd")
    integrity = state["integrity"]
    _require(
        isinstance(integrity, dict) and integrity.get("sha256") == state_digest(state),
        "state digest does not match",
    )
    _require(isinstance(state["attempts"], dict), "attempts are not a mapping")
    for attempt_id, record in state["attempts"].items():
        _check_attempt(attempt_id, record)


def _reject_constant(token):
    raise ValueError(f"non-finite JSON constant {token}")


def _hold(record):
    """Exposure of a non-settled attempt: its bound, or a larger over-bound cost seen at settle."""
    bound = Decimal(record["bound_usd"])
    observed = record["overbound_observed_usd"]
    return max(bound, Decimal(observed)) if observed is not None else bound


def _charge(record):
    return Decimal(record["actual_usd"]) if record["status"] == "settled" else _hold(record)


def _figures(state):
    """Per-arm settled/held/used/remaining/earmark Decimals plus status counts."""
    figures = {}
    for name in CAPS:
        figures[name] = {"settled": _ZERO, "held": _ZERO, "counts": dict.fromkeys(STATUSES, 0)}
    for record in state["attempts"].values():
        figure = figures[record["arm"]]
        figure["counts"][record["status"]] += 1
        if record["status"] == "settled":
            figure["settled"] += Decimal(record["actual_usd"])
        else:
            figure["held"] += _hold(record)
    for name, cap_text in CAPS.items():
        figure = figures[name]
        arm = state["arms"][name]
        figure["cap"] = Decimal(cap_text)
        figure["used"] = figure["settled"] + figure["held"]
        figure["remaining"] = figure["cap"] - figure["used"]
        figure["earmark"] = max(figure["remaining"], _ZERO) if arm["open"] else _ZERO
    return figures


def _snapshot(state, arm_name):
    figures = _figures(state)
    arms = {}
    for name, figure in figures.items():
        arm = state["arms"][name]
        arms[name] = {
            "cap_usd": arm["cap_usd"],
            "dispatch_approved": arm["dispatch_approved"],
            "open": arm["open"],
            "closed_reason": arm["closed_reason"],
            "closed_at": arm["closed_at"],
            "released_earmark_usd": arm["released_earmark_usd"],
            "settled_usd": _fmt(figure["settled"]),
            "held_usd": _fmt(figure["held"]),
            "used_usd": _fmt(figure["used"]),
            "remaining_usd": _fmt(figure["remaining"]),
            "earmark_usd": _fmt(figure["earmark"]),
            "attempts": dict(figure["counts"]),
        }
    return {
        "schema": SCHEMA,
        "seq": state["seq"],
        "arm": arm_name,
        "caps": dict(CAPS),
        "arms": arms,
        "global": {
            "held_usd": _fmt(sum((f["held"] for f in figures.values()), _ZERO)),
            "settled_usd": _fmt(sum((f["settled"] for f in figures.values()), _ZERO)),
            "open_earmark_usd": _fmt(sum((f["earmark"] for f in figures.values()), _ZERO)),
        },
        "attempts": [
            {
                "attempt_id": attempt_id,
                "arm": record["arm"],
                "status": record["status"],
                "bound_usd": record["bound_usd"],
                "actual_usd": record["actual_usd"],
                "charge_usd": _fmt(_charge(record)),
                "created_at": record["created_at"],
                "updated_at": record["updated_at"],
            }
            for attempt_id, record in sorted(
                state["attempts"].items(), key=lambda item: item[1]["seq"]
            )
        ],
    }


def _atomic_write(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _exact(method):
    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        with decimal.localcontext(_CTX):
            return method(*args, **kwargs)

    return wrapper


class Budget:
    """One arm's view of the shared reservation state under ``state_dir``."""

    def __init__(self, state_dir, arm, clock=_utc_now):
        if not isinstance(arm, str) or arm not in CAPS:
            raise BudgetError("unknown_arm", f"arm must be one of {sorted(CAPS)}")
        if not callable(clock):
            raise BudgetError(
                "invalid_clock", "clock must be callable and return an aware UTC datetime"
            )
        self.state_dir = Path(state_dir).expanduser()
        self.arm = arm
        self._clock = clock

    # -- infrastructure -------------------------------------------------------------------

    def _now(self):
        return _aware_utc(self._clock(), "clock()")

    @contextlib.contextmanager
    def _locked(self):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.state_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield descriptor
        finally:
            os.close(descriptor)

    def _load(self, descriptor):
        os.lseek(descriptor, 0, os.SEEK_SET)
        marker = os.read(descriptor, len(_INITIALIZED_SENTINEL) + 1)
        try:
            raw = (self.state_dir / STATE_FILE).read_bytes()
        except FileNotFoundError:
            if marker:
                raise BudgetError(
                    "missing_state",
                    "initialized budget state.json is missing; refusing to reset the budget",
                ) from None
            return None
        if marker not in (b"", _INITIALIZED_SENTINEL):
            raise BudgetError("tampered_state", "state.lock has an invalid initialized sentinel")
        try:
            state = json.loads(raw, parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError):
            raise BudgetError("tampered_state", "state file is not valid finite JSON") from None
        _check_state(state)
        return state

    def _load_or_init(self, descriptor):
        return self._load(descriptor) or _initial_state()

    def _write(self, state, descriptor):
        state["integrity"] = {"sha256": state_digest(state)}
        _check_state(state)  # never persist a state this module would refuse to read
        data = (json.dumps(state, sort_keys=True, indent=1, allow_nan=False) + "\n").encode()
        try:
            _atomic_write(self.state_dir / STATE_FILE, data)
            if os.fstat(descriptor).st_size == 0:
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.write(descriptor, _INITIALIZED_SENTINEL) != len(_INITIALIZED_SENTINEL):
                    raise OSError("incomplete initialized sentinel write")
            os.fsync(descriptor)
        except OSError as exc:
            raise BudgetError("state_write_failed", f"{type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _event(state, record, name, moment, **detail):
        state["seq"] += 1
        record["history"].append({"seq": state["seq"], "at": _iso(moment), "event": name, **detail})
        record["updated_at"] = _iso(moment)

    def _own_attempt(self, state, attempt_id):
        record = state["attempts"].get(attempt_id)
        if record is None:
            raise BudgetError("unknown_attempt", f"attempt {attempt_id!r} was never reserved")
        if record["arm"] != self.arm:
            raise BudgetError(
                "wrong_arm", f"attempt {attempt_id!r} belongs to arm {record['arm']!r}"
            )
        return record

    # -- public API -----------------------------------------------------------------------

    @_exact
    def reserve(self, attempt_id, upper_bound_usd, fetch_cash, metadata=None):
        """Durably hold ``upper_bound_usd`` for one HTTP attempt; raises BudgetError to refuse."""
        attempt_id = _attempt_id(attempt_id)
        bound = _decimal(upper_bound_usd, "upper_bound_usd", positive=True)
        metadata = _metadata(metadata)
        if not callable(fetch_cash):
            raise BudgetError("invalid_cash_callback", "fetch_cash must be callable")
        with self._locked() as descriptor:
            state = self._load_or_init(descriptor)
            if not DISPATCH_APPROVED[self.arm]:
                raise BudgetError(
                    "arm_not_approved",
                    f"arm {self.arm!r} is reserved but not approved for dispatch",
                )
            if not state["arms"][self.arm]["open"]:
                raise BudgetError("arm_closed", f"arm {self.arm!r} is closed")
            if attempt_id in state["attempts"]:
                raise BudgetError(
                    "duplicate_attempt",
                    f"attempt {attempt_id!r} already exists; ids are never reused",
                )
            figures = _figures(state)
            figure = figures[self.arm]
            if figure["used"] + bound > figure["cap"]:
                raise BudgetError(
                    "arm_cap",
                    f"bound {_fmt(bound)} exceeds remaining {_fmt(figure['remaining'])} of cap {_fmt(figure['cap'])}",
                )
            try:
                raw_cash = fetch_cash()
            except Exception as exc:
                raise BudgetError(
                    "cash_unavailable", f"fetch_cash failed: {type(exc).__name__}: {exc}"
                ) from exc
            now = self._now()
            cash, available = _cash(raw_cash, now)
            required = (
                bound
                + sum((f["held"] for f in figures.values()), _ZERO)
                + sum((f["settled"] for f in figures.values()), _ZERO)
                + sum((f["earmark"] for name, f in figures.items() if name != self.arm), _ZERO)
            )
            if available < required:
                raise BudgetError(
                    "cash_low", f"available {_fmt(available)} < required {_fmt(required)}"
                )
            state["seq"] += 1
            stamp = _iso(now)
            state["attempts"][attempt_id] = {
                "arm": self.arm,
                "status": "pending",
                "bound_usd": _fmt(bound),
                "actual_usd": None,
                "proof": None,
                "error": None,
                "overbound_observed_usd": None,
                "cash": {**cash, "required_cash_usd": _fmt(required)},
                "metadata": metadata,
                "created_at": stamp,
                "updated_at": stamp,
                "seq": state["seq"],
                "history": [
                    {
                        "seq": state["seq"],
                        "at": stamp,
                        "event": "reserved",
                        "bound_usd": _fmt(bound),
                        "required_cash_usd": _fmt(required),
                    }
                ],
            }
            self._write(state, descriptor)
            used = figure["used"] + bound
            return Reservation(
                attempt_id=attempt_id,
                arm=self.arm,
                bound_usd=bound,
                reserved_at=stamp,
                cash_available_usd=available,
                cash_fetched_at=cash["fetched_at"],
                cash_source=cash["source"],
                required_cash_usd=required,
                arm_used_usd=used,
                arm_remaining_usd=figure["cap"] - used,
            )

    @_exact
    def settle(self, attempt_id, actual_usd, proof):
        """Record provider-billed cost for a pending attempt and release the unused remainder."""
        attempt_id = _attempt_id(attempt_id)
        actual = _decimal(actual_usd, "actual_usd")
        proof = _proof(proof)
        with self._locked() as descriptor:
            state = self._load_or_init(descriptor)
            record = self._own_attempt(state, attempt_id)
            if record["status"] != "pending":
                raise BudgetError(
                    "bad_transition", f"attempt {attempt_id!r} is already {record['status']}"
                )
            if record["overbound_observed_usd"] is not None:
                raise BudgetError(
                    "bad_transition",
                    f"attempt {attempt_id!r} saw an over-bound cost; only fail() may follow",
                )
            now = self._now()
            bound = Decimal(record["bound_usd"])
            if actual > bound:
                record["overbound_observed_usd"] = _fmt(actual)
                self._event(
                    state, record, "settle_rejected_over_bound", now, observed_usd=_fmt(actual)
                )
                self._write(state, descriptor)
                raise BudgetError(
                    "over_bound",
                    f"billed {_fmt(actual)} exceeds reserved bound {_fmt(bound)}; hold retained at the larger value",
                )
            record.update(status="settled", actual_usd=_fmt(actual), proof=proof)
            self._event(state, record, "settled", now, actual_usd=_fmt(actual))
            self._write(state, descriptor)
            figure = _figures(state)[self.arm]
            return {
                "attempt_id": attempt_id,
                "arm": self.arm,
                "status": "settled",
                "bound_usd": record["bound_usd"],
                "actual_usd": record["actual_usd"],
                "released_usd": _fmt(bound - actual),
                "arm_used_usd": _fmt(figure["used"]),
                "arm_remaining_usd": _fmt(figure["remaining"]),
                "settled_at": record["updated_at"],
            }

    @_exact
    def fail(self, attempt_id, error):
        """Mark a pending attempt failed/unknown. Its whole hold is retained, never released."""
        attempt_id = _attempt_id(attempt_id)
        message = _error_text(error)
        with self._locked() as descriptor:
            state = self._load_or_init(descriptor)
            record = self._own_attempt(state, attempt_id)
            if record["status"] != "pending":
                raise BudgetError(
                    "bad_transition", f"attempt {attempt_id!r} is already {record['status']}"
                )
            record.update(status="failed", error=message)
            self._event(state, record, "failed", self._now(), error=message)
            self._write(state, descriptor)
            figure = _figures(state)[self.arm]
            return {
                "attempt_id": attempt_id,
                "arm": self.arm,
                "status": "failed",
                "retained_usd": _fmt(_hold(record)),
                "arm_used_usd": _fmt(figure["used"]),
                "arm_remaining_usd": _fmt(figure["remaining"]),
                "failed_at": record["updated_at"],
            }

    @_exact
    def snapshot(self):
        """JSON-safe totals and per-attempt statuses; never prompts, metadata, proofs or errors."""
        with self._locked() as descriptor:
            return _snapshot(self._load_or_init(descriptor), self.arm)

    @_exact
    def close_arm(self, reason):
        """Stop future dispatch and release only this arm's unused earmark (idempotent)."""
        reason = _text(reason, "reason", "invalid_reason")
        with self._locked() as descriptor:
            state = self._load_or_init(descriptor)
            arm = state["arms"][self.arm]
            if arm["open"]:
                released = max(_figures(state)[self.arm]["remaining"], _ZERO)
                now = self._now()
                arm.update(
                    open=False,
                    closed_reason=reason,
                    closed_at=_iso(now),
                    released_earmark_usd=_fmt(released),
                )
                state["seq"] += 1
                self._write(state, descriptor)
                already = False
            else:
                already = True
            figure = _figures(state)[self.arm]
            return {
                "arm": self.arm,
                "open": False,
                "already_closed": already,
                "closed_reason": arm["closed_reason"],
                "closed_at": arm["closed_at"],
                "released_earmark_usd": arm["released_earmark_usd"],
                "settled_usd": _fmt(figure["settled"]),
                "held_usd": _fmt(figure["held"]),
                "used_usd": _fmt(figure["used"]),
            }
