from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.db import BotDatabase
from app.hyperliquid_client import HyperliquidClient
from app.models import Asset, DecisionAction, Direction, Regime, StrategyName, TradePlan
from app.regime import build_market_snapshot, detect_regime
from app.strategy import evaluate_signal


class TrendSwitchService:
    def __init__(self, settings: Settings, db: BotDatabase, client: HyperliquidClient):
        self.settings = settings
        self.db = db
        self.client = client

    def _signal_key(self, asset: Asset, direction: Direction) -> str:
        return f"last_signal:{asset.value}:{direction.value}"

    def _should_halt_for_daily_loss(self, account_value: float) -> bool:
        pnl = self.client.daily_closed_pnl()
        return pnl <= -(account_value * self.settings.daily_loss_limit_fraction)

    def stats(self) -> tuple[int, int]:
        win_count = 0
        loss_count = 0
        for log in self.db.recent_logs(limit=500):
            action = log.get("action")
            payload = log.get("payload", {})
            pnl_usd = payload.get("pnl_usd") or payload.get("closed_pnl") or payload.get("closedPnl")
            if action != DecisionAction.CLOSE.value or pnl_usd is None:
                continue
            if float(pnl_usd) >= 0:
                win_count += 1
            else:
                loss_count += 1
        return win_count, loss_count

    def realized_pnl(self) -> float:
        realized = 0.0
        for log in self.db.recent_logs(limit=500):
            payload = log.get("payload", {})
            pnl_usd = payload.get("pnl_usd") or payload.get("closed_pnl") or payload.get("closedPnl")
            if pnl_usd is not None:
                realized += float(pnl_usd)
        return realized

    def drawdown_pct(self, account_value: float) -> float:
        peak = max(account_value, self.settings.paper_account_value)
        if peak <= 0:
            return 0.0
        return max((peak - account_value) / peak * 100, 0.0)

    def run_signals(self) -> list[dict[str, Any]]:
        account_value = self.client.account_value()
        if self._should_halt_for_daily_loss(account_value):
            payload = {"reason": "Daily loss limit reached.", "account_value": account_value}
            self.db.log("signals", DecisionAction.SKIP.value, payload)
            return [payload]

        positions = self.client.positions()
        outputs: list[dict[str, Any]] = []
        configs = [
            (Asset.BTC, StrategyName.BTC_HMA, Direction.LONG),
            (Asset.BTC, StrategyName.BTC_HMA, Direction.SHORT),
            (Asset.ETH, StrategyName.ETH_MACZ, Direction.LONG),
            (Asset.ETH, StrategyName.ETH_MACZ, Direction.SHORT),
        ]

        for asset, strategy, direction in configs:
            candles = self.client.candles(asset, self.settings.default_interval, self.settings.candle_lookback_hours)
            candle_time = int(candles.iloc[-1]["close_time"])
            if self.db.get_state(self._signal_key(asset, direction)) == candle_time:
                continue

            market = build_market_snapshot(asset, candles, self.client.funding_rate(asset), self.settings)
            position = next((p for p in positions if p.asset == asset), None)
            plan = evaluate_signal(asset, strategy, direction, market, position, account_value, positions, self.settings)

            payload = asdict(plan)
            payload["timestamp"] = datetime.now(timezone.utc).isoformat()
            if plan.action == DecisionAction.CLOSE and position is not None:
                close_result = self.client.close_position(asset)
                payload["close_result"] = close_result
            if plan.action in {DecisionAction.OPEN, DecisionAction.PYRAMID}:
                execution = self.client.execute_trade(plan)
                payload["execution"] = execution
            self.db.log("signals", plan.action.value, payload, asset.value, direction.value)
            self.db.set_state(self._signal_key(asset, direction), candle_time)
            outputs.append(payload)

        reporter = getattr(self, "noon_hub_reporter", None)
        if reporter is not None and outputs:
            reporter.publish_actions(outputs, "signal")

        return outputs

    def run_monitor(self) -> list[dict[str, Any]]:
        positions = self.client.positions()
        if not positions:
            payload = {"message": "No open positions to manage"}
            self.db.log("monitor", DecisionAction.NONE.value, payload)
            return [payload]

        outputs: list[dict[str, Any]] = []
        for position in positions:
            candles = self.client.candles(position.asset, self.settings.default_interval, self.settings.candle_lookback_hours)
            market = build_market_snapshot(position.asset, candles, self.client.funding_rate(position.asset), self.settings)
            regime_report = detect_regime(market, position.direction.value)
            action = DecisionAction.NONE
            reason = "No change."
            new_stop = None
            partial_fraction = None

            if position.direction == Direction.LONG:
                opposite_trend = market.ema12 < market.ema26 and market.adx > 30
                if market.latest_close < position.entry_price and regime_report.regime == Regime.VOLATILE:
                    action, reason = DecisionAction.CLOSE, "Long position is underwater in volatile regime."
                elif position.unrealized_pnl_pct < -5 and opposite_trend:
                    action, reason = DecisionAction.CLOSE, "Long thesis invalidated by bearish trend shift."
            else:
                opposite_trend = market.ema12 > market.ema26 and market.adx > 30
                if market.latest_close > position.entry_price and regime_report.regime == Regime.VOLATILE:
                    action, reason = DecisionAction.CLOSE, "Short position is underwater in volatile regime."
                elif position.unrealized_pnl_pct < -5 and opposite_trend:
                    action, reason = DecisionAction.CLOSE, "Short thesis invalidated by bullish trend shift."

            if action == DecisionAction.NONE:
                if regime_report.regime == Regime.VOLATILE and position.unrealized_pnl_pct <= 0:
                    action, reason = DecisionAction.CLOSE, "Protect capital during unfavorable volatility."
                elif position.unrealized_pnl_pct > 5:
                    action, reason = DecisionAction.ADJUST, "Lock gains after strong move."
                    if position.direction == Direction.LONG:
                        new_stop = position.entry_price * 1.02
                    else:
                        new_stop = position.entry_price * 0.98
                elif position.unrealized_pnl_pct > 3:
                    action, reason = DecisionAction.ADJUST, "Move stop to breakeven."
                    new_stop = position.entry_price
                elif position.unrealized_pnl_pct > 0 and regime_report.regime == Regime.RANGING:
                    action, reason = DecisionAction.PARTIAL, "Take faster profits in ranging regime."
                    partial_fraction = 0.5

            payload = {
                "asset": position.asset.value,
                "direction": position.direction.value,
                "entry_price": position.entry_price,
                "current_price": position.current_price,
                "pnl_pct": position.unrealized_pnl_pct,
                "pnl_usd": position.unrealized_pnl_usd,
                "regime": regime_report.regime.value,
                "regime_factors": regime_report.factors,
                "action": action.value,
                "reason": reason,
                "new_stop": new_stop,
                "partial_fraction": partial_fraction,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            if action == DecisionAction.CLOSE:
                payload["execution"] = self.client.close_position(position.asset)
            self.db.log("monitor", action.value, payload, position.asset.value, position.direction.value)
            outputs.append(payload)

        reporter = getattr(self, "noon_hub_reporter", None)
        if reporter is not None and outputs:
            reporter.publish_actions(outputs, "monitor")

        return outputs

    def close_position(self, asset: Asset) -> dict[str, Any]:
        result = self.client.close_position(asset)
        payload = {
            "asset": asset.value,
            "action": DecisionAction.CLOSE.value,
            "execution": result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.db.log("manual", DecisionAction.CLOSE.value, payload, asset.value)
        reporter = getattr(self, "noon_hub_reporter", None)
        if reporter is not None:
            reporter.publish_event(
                event_type="manual-close",
                severity="INFO",
                title=f"{self.settings.noon_hub_bot_name}: manual close",
                body=f"Manual close requested for {asset.value}",
                symbol=asset.value,
            )
        return payload

    def dashboard_data(self) -> dict[str, Any]:
        account_value = self.client.account_value()
        daily_pnl = self.client.daily_closed_pnl()
        positions = [asdict(position) for position in self.client.positions()]
        open_orders = self.client.open_orders() if self.client.account_address else []
        recent_logs = self.db.recent_logs(limit=40)
        latest_signal_state = {
            key: value
            for key, value in {
                "BTC_LONG": self.db.get_state(self._signal_key(Asset.BTC, Direction.LONG)),
                "BTC_SHORT": self.db.get_state(self._signal_key(Asset.BTC, Direction.SHORT)),
                "ETH_LONG": self.db.get_state(self._signal_key(Asset.ETH, Direction.LONG)),
                "ETH_SHORT": self.db.get_state(self._signal_key(Asset.ETH, Direction.SHORT)),
            }.items()
        }
        latest_actions: dict[str, Any] = {}
        for log in recent_logs:
            asset = log.get("asset")
            if asset and asset not in latest_actions:
                latest_actions[asset] = {
                    "category": log["category"],
                    "action": log["action"],
                    "direction": log.get("direction"),
                    "created_at": log["created_at"],
                    "reason": log["payload"].get("reason") or log["payload"].get("message"),
                }

        return {
            "overview": {
                "env": self.settings.app_env,
                "dry_run": self.settings.dry_run,
                "hyperliquid_env": self.settings.hyperliquid_env,
                "scheduler_enabled": self.settings.enable_scheduler,
                "account_value": account_value,
                "daily_closed_pnl": daily_pnl,
                "daily_loss_limit": -(account_value * self.settings.daily_loss_limit_fraction),
                "positions_count": len(positions),
                "open_orders_count": len(open_orders),
                "max_positions": self.settings.max_concurrent_positions,
                "halted_by_loss_limit": self._should_halt_for_daily_loss(account_value),
            },
            "positions": positions,
            "open_orders": open_orders,
            "recent_logs": recent_logs,
            "latest_signal_state": latest_signal_state,
            "latest_actions": latest_actions,
            "controls": {
                "signal_scan_interval_minutes": self.settings.signal_scan_interval_minutes,
                "monitor_interval_minutes": self.settings.monitor_interval_minutes,
                "default_interval": self.settings.default_interval,
            },
        }

    def state(self) -> dict[str, Any]:
        return {
            "env": self.settings.app_env,
            "dry_run": self.settings.dry_run,
            "hyperliquid_env": self.settings.hyperliquid_env,
            "recent_logs": self.db.recent_logs(),
        }
