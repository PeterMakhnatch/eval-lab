"""Which Run Atlas facet fields to trust: measured agreement, read from a retained file.

``<data_root>/run-atlas/field-reliability-v1.json`` (``evallab.field_reliability/v1``) holds,
per facet field, how often an independent blind re-rating agreed with the stored facet, with
the sample size. It carries agreement counts only: no run id, run text or label.

This module validates and serves that file (re-read only when it changes) and can build it
from a VF-11 style receipt. Nothing here opens a database or model. A missing file is "not
measured", which is different from "unreliable" or "reliable"; an unreadable one says why.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from evallab.production.atlas_view import json_reason, read_regular, strict_loads

SCHEMA = "evallab.field_reliability/v1"
FILE = "field-reliability-v1.json"
MAX_BYTES = 256 * 1024
Basis = Literal["exact", "within_1"]
_Count = Annotated[int, Field(ge=0)]
_Text = Annotated[str, Field(min_length=1, max_length=400)]
_AGREEMENT = re.compile(r"^(\d+)/(\d+) \((\d+(?:\.\d+)?)%\)$")
_SAMPLE_KEY = re.compile(r"^blind_rerate_(\d+)$")
_WITHIN_1 = "_within_1"


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


class FieldAgreement(_Strict):
    field: Literal[
        "goal",
        "approach",
        "tests",
        "grader_or_harness_touched",
        "answer_material_accessed",
        "success_claim_without_evidence",
        "eval_awareness",
        "shortcut",
        "suspicion",
        "evidence_steps",
    ]
    agree_n: _Count
    n: Annotated[int, Field(ge=1)]
    # How agreement was judged, when the receipt says: suspicion is scored within one level.
    basis: Basis | None = None

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.agree_n > self.n:
            raise ValueError("agreement cannot exceed the number of rated runs")
        return self


class FieldReliability(_Strict):
    schema_: Literal["evallab.field_reliability/v1"] = Field(alias="schema")
    measured_at: str
    source: _Text
    design: _Text
    sample_runs: Annotated[int, Field(ge=1)]
    fields: Annotated[list[FieldAgreement], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_document(self) -> Self:
        moment = datetime.fromisoformat(self.measured_at)
        if moment.tzinfo is None:
            raise ValueError("measured_at must name a time zone")
        names = [item.field for item in self.fields]
        if len(set(names)) != len(names):
            raise ValueError("a field may appear once")
        if any(item.n > self.sample_runs for item in self.fields):
            raise ValueError("a field cannot be rated on more runs than were sampled")
        return self


def wilson(agree: int, n: int, z: float = 1.96) -> list[float]:
    """The 95% Wilson interval for an agreement rate (percent, one decimal)."""
    p = agree / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return [round(100 * max(0.0, centre - margin), 1), round(100 * min(1.0, centre + margin), 1)]


def _served(document: FieldReliability) -> dict[str, Any]:
    rows: list[dict[str, Any]] = [
        {
            "field": item.field,
            "agree_n": item.agree_n,
            "n": item.n,
            "agreement": round(100 * item.agree_n / item.n, 1),
            "ci95": wilson(item.agree_n, item.n),
            "basis": item.basis,
        }
        for item in document.fields
    ]
    rows.sort(key=lambda row: (-row["agreement"], row["field"]))
    return {
        "measured_at": document.measured_at,
        "source": document.source,
        "design": document.design,
        "sample_runs": document.sample_runs,
        "fields": rows,
    }


def load_field_reliability(directory: Path) -> dict[str, Any]:
    """The served reliability document, ``not measured`` when absent, or why it is unreadable."""
    base: dict[str, Any] = {"schema": SCHEMA, "measured": False, "reliability": None, "error": None}
    try:
        raw = read_regular(directory, FILE, MAX_BYTES)
        if raw is None:
            return base | {"error": f"not measured: no {FILE} is retained"}
        document = FieldReliability.model_validate(strict_loads(raw.decode("utf-8")))
    except (OSError, ValueError, ValidationError, UnicodeDecodeError) as error:
        return base | {"error": f"unreadable: {json_reason(error)}"}
    return base | {
        "measured": True,
        "reliability": _served(document),
        "notice": (
            "Agreement of a blind independent re-rating with the stored facet, on a small "
            "sample. It says how far a field can be leaned on as a lead, not that a facet is "
            "right; intervals are wide. A field not listed was not measured."
        ),
    }


class FieldReliabilityCache:
    """Re-reads the file only when it changes on disk."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._lock = threading.Lock()
        self._key: tuple[int, int, int] | None = None
        self._loaded = False
        self._value: dict[str, Any] = {}

    def get(self) -> dict[str, Any]:
        try:
            seen = (self._directory / FILE).lstat()
            key: tuple[int, int, int] | None = (seen.st_mtime_ns, seen.st_size, seen.st_ino)
        except OSError:
            key = None
        with self._lock:
            if not self._loaded or key != self._key:
                self._key, self._loaded = key, True
                self._value = load_field_reliability(self._directory)
            return self._value


def from_vf11_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Build the document from a VF-11 receipt, copying agreement and sample size only.

    Every other part of the receipt (run prefixes, worker notes, other audits) is ignored.
    A percentage that disagrees with its own counts is refused.
    """
    sample = [
        (key, int(match.group(1)))
        for key in receipt
        if (match := _SAMPLE_KEY.match(str(key)))
    ]
    if len(sample) != 1:
        raise ValueError("the receipt must hold exactly one blind_rerate_<N> block")
    key, sample_runs = sample[0]
    block = receipt[key]
    agreement = block["per_field_agreement"]
    fields: list[dict[str, Any]] = []
    for name, text in agreement.items():
        match = _AGREEMENT.match(str(text))
        if match is None:
            raise ValueError(f"cannot read agreement for {name!r}")
        agree, n, percent = int(match.group(1)), int(match.group(2)), float(match.group(3))
        if n == 0 or round(100 * agree / n, 1) != percent:
            raise ValueError(f"agreement for {name!r} disagrees with its own counts")
        within = name.endswith(_WITHIN_1)
        fields.append(
            {
                "field": name.removesuffix(_WITHIN_1),
                "agree_n": agree,
                "n": n,
                "basis": "within_1" if within else None,
            }
        )
    document = {
        "schema": SCHEMA,
        "measured_at": str(receipt["checked_at_utc"]),
        "source": str(receipt["item"]).split(" (")[0] + " blind re-rate",
        "design": str(block["design"]),
        "sample_runs": sample_runs,
        "fields": fields,
    }
    FieldReliability.model_validate(document)
    return document


def write_field_reliability(
    directory: Path, document: Mapping[str, Any], *, replace: bool = False
) -> dict[str, Any]:
    """Validate and atomically write the file; an existing different file needs ``replace``."""
    FieldReliability.model_validate(document)
    body = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / FILE
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file():
            raise ValueError(f"{FILE} must be a regular file")
        if target.read_bytes() != body and not replace:
            raise FileExistsError(f"{target} already differs; pass --replace to overwrite it")
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".field-reliability-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return {
        "path": str(target),
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "fields": len(document["fields"]),
    }
