from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    result_store: Path
    page_threshold: str = "P0"
    security_page_recipients: tuple[str, ...] = ("secops-oncall", "security-pager")
    ping_recipients: tuple[str, ...] = ("eval-owner",)
    slack_channel: str = "#eval-notifications"
    no_notify: bool = False


def load_settings(path: str | Path = "config/settings.toml") -> Settings:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    return Settings(
        result_store=Path(raw["result_store"]),
        page_threshold=raw.get("page_threshold", "P0"),
        security_page_recipients=tuple(raw.get("security_page_recipients", ("secops-oncall", "security-pager"))),
        ping_recipients=tuple(raw.get("ping_recipients", ("pipeline-owner",))),
        slack_channel=raw.get("slack_channel", ""),
        no_notify=bool(raw.get("no_notify", False)),
    )
