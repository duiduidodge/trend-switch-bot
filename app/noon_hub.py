from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import requests

from app.models import DecisionAction

logger = logging.getLogger(__name__)


class NoonHubReporter:
    def __init__(self, settings, service):
        self.settings = settings
        self.service = service
        self.started_at = time.time()
        self.session = requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.noon_hub_url and self.settings.noon_hub_ingest_key)

    def _iso_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _headers(self) -> dict[str, str]:
        return {
            "content-type": "application/json",
            "x-noon-hub-key": self.settings.noon_hub_ingest_key or "",
        }

    def _post(self, path: str, payload: dict[str, Any]) -> None:
        if not self.enabled:
            return

        base_url = (self.settings.noon_hub_url or "").rstrip("/")
        response = self.session.post(
            f"{base_url}{path}",
            json=payload,
            headers=self._headers(),
            timeout=20,
        )
        response.raise_for_status()

    def _service_positions(self):
        return self.service._positions(refresh_marks=True)

    def _bot_identity(self) -> dict[str, Any]:
        return {
            "slug": self.settings.noon_hub_bot_slug,
            "name": self.settings.noon_hub_bot_name,
            "environment": self.settings.noon_hub_bot_environment,
            "category": self.settings.noon_hub_bot_category,
            "strategyFamily": self.settings.noon_hub_bot_strategy_family,
            "venue": self.settings.noon_hub_bot_venue,
            "repoUrl": self.settings.noon_hub_repo_url,
            "dashboardUrl": self.settings.noon_hub_dashboard_url,
            "status": "RUNNING",
        }

    def register_bot(self) -> None:
        try:
            self._post("/hub/bots/register", self._bot_identity())
        except Exception:
            logger.exception("Failed to register with Noon Hub")

    def publish_heartbeat(self, status: str = "RUNNING", message: str = "Scheduler active") -> None:
        try:
            self._post(
                "/hub/heartbeat",
                {
                    "botSlug": self.settings.noon_hub_bot_slug,
                    "name": self.settings.noon_hub_bot_name,
                    "status": status,
                    "message": message,
                    "version": self.settings.bot_version,
                    "uptimeSec": int(time.time() - self.started_at),
                    "observedAt": self._iso_now(),
                },
            )
        except Exception:
            logger.exception("Failed to publish Noon Hub heartbeat")

    def publish_snapshot(self) -> None:
        if not self.enabled:
            return

        try:
            account_value = self.service._account_value()
            daily_pnl = self.service._daily_closed_pnl()
            positions = self._service_positions()
            win_count, loss_count = self.service.stats()
            closed_count = win_count + loss_count
            realized_pnl = self.service.realized_pnl()
            unrealized_pnl = sum(position.unrealized_pnl_usd for position in positions)
            drawdown_pct = self.service.drawdown_pct(account_value)
            observed_at = self._iso_now()

            self._post(
                "/hub/metrics",
                {
                    "botSlug": self.settings.noon_hub_bot_slug,
                    "name": self.settings.noon_hub_bot_name,
                    "equityUsd": account_value,
                    "cashUsd": max(account_value - sum(position.margin_used for position in positions), 0.0),
                    "dailyPnlUsd": daily_pnl,
                    "realizedPnlUsd": realized_pnl,
                    "unrealizedPnlUsd": unrealized_pnl,
                    "drawdownPct": drawdown_pct,
                    "winRatePct": (win_count / closed_count * 100) if closed_count else None,
                    "openPositions": len(positions),
                    "observedAt": observed_at,
                },
            )

            if positions:
                self._post(
                    "/hub/positions",
                    {
                        "botSlug": self.settings.noon_hub_bot_slug,
                        "name": self.settings.noon_hub_bot_name,
                        "snapshotTime": observed_at,
                        "positions": [
                            {
                                "symbol": position.asset.value,
                                "side": position.direction.value,
                                "status": "OPEN",
                                "quantity": position.size_asset,
                                "entryPrice": position.entry_price,
                                "markPrice": position.current_price,
                                "pnlUsd": position.unrealized_pnl_usd,
                                "pnlPct": position.unrealized_pnl_pct,
                                "openedAt": observed_at,
                            }
                            for position in positions
                        ],
                    },
                )
        except Exception:
            logger.exception("Failed to publish Noon Hub snapshot")

    def publish_event(
        self,
        *,
        event_type: str,
        severity: str,
        title: str,
        body: str,
        symbol: str | None = None,
    ) -> None:
        try:
            self._post(
                "/hub/events",
                {
                    "botSlug": self.settings.noon_hub_bot_slug,
                    "name": self.settings.noon_hub_bot_name,
                    "eventType": event_type,
                    "severity": severity,
                    "title": title,
                    "body": body,
                    "symbol": symbol,
                    "eventAt": self._iso_now(),
                },
            )
        except Exception:
            logger.exception("Failed to publish Noon Hub event")

    def publish_actions(self, outputs: list[dict[str, Any]], event_type: str) -> None:
        for output in outputs:
            action = output.get("action")
            if action in {DecisionAction.NONE.value, DecisionAction.SKIP.value, DecisionAction.HOLD.value}:
                continue
            symbol = output.get("asset")
            reason = output.get("reason") or output.get("message") or "Bot action emitted"
            self.publish_event(
                event_type=event_type,
                severity="INFO",
                title=f"{self.settings.noon_hub_bot_name}: {action}",
                body=str(reason),
                symbol=str(symbol) if symbol else None,
            )
