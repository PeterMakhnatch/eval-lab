"""Alert dispatcher. Security findings page security_page_recipients; info events are routine Slack messages."""

from __future__ import annotations

from dataclasses import dataclass

from evalrun import transport
from evalrun.config import Settings

SEVERITIES = ("info", "P2", "P1", "P0")


def rank(severity: str) -> int:
    return SEVERITIES.index(severity)


@dataclass(frozen=True)
class Alert:
    severity: str
    run_id: str
    title: str


class AlertDispatcher:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def dispatch(self, alert: Alert) -> list[str]:
        settings = self.settings
        if settings.no_notify:
            return []
        sent: list[str] = []
        if rank(alert.severity) >= rank(settings.page_threshold):
            if settings.security_page_recipients:
                transport.send(
                    "page",
                    list(settings.security_page_recipients),
                    severity=alert.severity,
                    run_id=alert.run_id,
                    subject=alert.title,
                )
                sent.append("page")
            if settings.ping_recipients:
                transport.send(
                    "ping",
                    list(settings.ping_recipients),
                    severity=alert.severity,
                    run_id=alert.run_id,
                    subject=alert.title,
                )
                sent.append("ping")
        elif alert.severity == "info":
            if settings.slack_channel:
                transport.send(
                    "slack",
                    settings.slack_channel,
                    severity=alert.severity,
                    run_id=alert.run_id,
                    subject=alert.title,
                )
                sent.append("slack")
        return sent
