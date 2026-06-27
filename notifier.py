from __future__ import annotations

import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass

from config import TradingConfig

log = logging.getLogger("notifier")


def format_daily(summary: dict, status: dict) -> tuple[str, str]:
    title = f"Daily trading summary for {summary.get('date', 'today')}"
    live = status.get("live", {})
    paper = status.get("paper", {})
    lines = [
        f"Trades opened: {summary.get('trades_opened', 0)}",
        f"Trades closed: {summary.get('trades_closed', 0)}",
        f"Realized P&L today: {summary.get('realized_pl_today', 0)}",
        f"Avg slippage bps: {summary.get('avg_slippage_bps', 'n/a')}",
        f"Paper equity: {paper.get('equity', 'n/a')}",
        f"Live equity: {live.get('equity', 'n/a')}",
    ]
    if live.get("floor_tripped"):
        lines.append("WARNING: live capital floor tripped")
    if live.get("can_trade") is False:
        lines.append("Live leg halted")
    return title, "\n".join(lines)


class Notifier:
    def __init__(self, cfg: TradingConfig):
        self.cfg = cfg
        self.log = logging.getLogger("notifier.client")

    def send(self, title: str, body: str) -> None:
        if not self.cfg.notifier.enabled:
            self.log.info("%s\n%s", title, body)
            return
        if self.cfg.notifier.provider.lower() != "telegram":
            self.log.info("unsupported notifier provider %s; logging only", self.cfg.notifier.provider)
            self.log.info("%s\n%s", title, body)
            return
        try:
            url = f"https://api.telegram.org/bot{self.cfg.notifier.token}/sendMessage"
            payload = urllib.parse.urlencode(
                {"chat_id": self.cfg.notifier.chat_id, "text": f"{title}\n\n{body}"}
            ).encode()
            urllib.request.urlopen(url, data=payload, timeout=5)
        except Exception as exc:
            self.log.warning("telegram notify failed: %s", exc)

