"""Runtime settings, loaded from ``config/settings.toml``."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    root: Path
    result_store: Path
    slack_channel: str = "#eval-results"
    # Maintenance switch: when true the dispatcher sends nothing at all.
    no_notify: bool = False
    security_page_recipients: tuple[str, ...] = ()
    ping_recipients: tuple[str, ...] = ()
    page_threshold: str = "P0"


def load_settings(path: str | Path) -> Settings:
    path = Path(path).resolve()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    root = path.parent.parent
    store = Path(data["store"]["result_store"])
    if not store.is_absolute():
        store = root / store
    alerts = data.get("alerts", {})
    return Settings(
        root=root,
        result_store=store,
        slack_channel=alerts.get("slack_channel", "#eval-results"),
        no_notify=bool(alerts.get("no_notify", False)),
        security_page_recipients=tuple(alerts.get("security_page_recipients", ())),
        ping_recipients=tuple(alerts.get("ping_recipients", ())),
        page_threshold=alerts.get("page_threshold", "P0"),
    )
