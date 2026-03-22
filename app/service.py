from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.db import BotDatabase
from app.hyperliquid_client import HyperliquidClient
from app.models import Asset, DecisionAction, Direction, PositionSnapshot, Regime, StrategyName, TradePlan
from app.regime import build_market_snapshot, detect_regime
from app.strategy import evaluate_signal


class TrendSwitchService:
    def __init__(self, settings: Settings, db: BotDatabase, client: HyperliquidClient):
        self.settings = settings
        self.db = db
        self.client = client

    def _signal_key(self, asset: Asset, direction: Direction) -> str:
        return f"last_signal:{asset.value}:{direction.value}"

    def _is_paper_mode(self) -> bool:
        return self.settings.dry_run

    def _market_price(self, asset: Asset) -> float:
        candles = self.client.candles(asset, self.settings.default_interval, 4)
        return float(candles.iloc[-1]["close"])

    def _paper_position_snapshot(self, row: dict[str, Any], refresh_mark: bool = True) -> PositionSnapshot:
        current_price = self._market_price(Asset(row["asset"])) if refresh_mark else float(row["current_price"])
        direction = Direction(row["direction"])
        entry_price = float(row["entry_price"])
        size_asset = float(row["size_asset"])
        leverage = float(row["leverage"])
        entry_notional = entry_price * size_asset
        current_notional = current_price * size_asset
        direction_multiplier = 1 if direction == Direction.LONG else -1
        unrealized_pnl_usd = (current_price - entry_price) * size_asset * direction_multiplier
        unrealized_pnl_pct = (unrealized_pnl_usd / max(entry_notional, 1e-9)) * 100
        snapshot = PositionSnapshot(
            asset=Asset(row["asset"]),
            direction=direction,
            entry_price=entry_price,
            current_price=current_price,
            size_asset=size_asset,
            size_usd=current_notional,
            leverage=leverage,
            unrealized_pnl_usd=unrealized_pnl_usd,
            unrealized_pnl_pct=unrealized_pnl_pct,
            margin_used=entry_notional / max(leverage, 1e-9),
            liquidation_price=row.get("liquidation_price"),
            raw=row.get("raw", {}),
        )
        self.db.upsert_paper_position(
            {
                **row,
                "asset": snapshot.asset.value,
                "direction": snapshot.direction.value,
                "current_price": snapshot.current_price,
                "size_usd": snapshot.size_usd,
                "unrealized_pnl_usd": snapshot.unrealized_pnl_usd,
                "unrealized_pnl_pct": snapshot.unrealized_pnl_pct,
                "margin_used": snapshot.margin_used,
                "raw": snapshot.raw,
            }
        )
        return snapshot

    def _paper_positions(self, refresh_marks: bool = True) -> list[PositionSnapshot]:
        return [self._paper_position_snapshot(row, refresh_mark=refresh_marks) for row in self.db.paper_positions()]

    def _positions(self, refresh_marks: bool = True) -> list[PositionSnapshot]:
        if self._is_paper_mode():
            return self._paper_positions(refresh_marks=refresh_marks)
        return self.client.positions()

    def _account_value(self) -> float:
        if not self._is_paper_mode():
            return self.client.account_value()
        unrealized = sum(position.unrealized_pnl_usd for position in self._paper_positions(refresh_marks=True))
        return self.settings.paper_account_value + self.db.paper_realized_pnl() + unrealized

    def _daily_closed_pnl(self) -> float:
        if not self._is_paper_mode():
            return self.client.daily_closed_pnl()
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        return self.db.paper_closed_pnl_since(start)

    def _open_orders(self) -> list[dict[str, Any]]:
        if not self._is_paper_mode():
            return self.client.open_orders() if self.client.account_address else []
        orders: list[dict[str, Any]] = []
        for row in self.db.paper_positions():
            side = "SELL" if row["direction"] == Direction.LONG.value else "BUY"
            if row.get("take_profit_price") is not None:
                orders.append(
                    {
                        "coin": row["asset"],
                        "side": side,
                        "sz": row["size_asset"],
                        "triggerPx": row["take_profit_price"],
                        "limitPx": row["take_profit_price"],
                        "paperType": "tp",
                    }
                )
            if row.get("stop_price") is not None:
                orders.append(
                    {
                        "coin": row["asset"],
                        "side": side,
                        "sz": row["size_asset"],
                        "triggerPx": row["stop_price"],
                        "limitPx": row["stop_price"],
                        "paperType": "sl",
                    }
                )
        return orders

    def _execute_trade(self, plan: TradePlan) -> dict[str, Any]:
        if not self._is_paper_mode():
            return self.client.execute_trade(plan)
        existing = self.db.paper_position(plan.asset.value)
        if existing is not None and existing["direction"] != plan.direction.value:
            raise RuntimeError(f"Cannot paper-open {plan.asset.value} {plan.direction.value} against existing opposing position.")

        opened_at = existing["opened_at"] if existing is not None else datetime.now(timezone.utc).isoformat()
        existing_size_asset = float(existing["size_asset"]) if existing is not None else 0.0
        existing_entry_notional = float(existing["entry_price"]) * existing_size_asset if existing is not None else 0.0
        new_size_asset = float(plan.position_size_asset or 0.0)
        total_size_asset = existing_size_asset + new_size_asset
        if total_size_asset <= 0:
            raise RuntimeError("Paper trade size must be positive.")

        total_entry_notional = existing_entry_notional + (plan.entry_price * new_size_asset)
        average_entry = total_entry_notional / total_size_asset
        current_notional = total_size_asset * plan.entry_price
        payload = {
            "asset": plan.asset.value,
            "direction": plan.direction.value,
            "entry_price": average_entry,
            "current_price": plan.entry_price,
            "size_asset": total_size_asset,
            "size_usd": current_notional,
            "leverage": float(plan.leverage or 1),
            "unrealized_pnl_usd": 0.0,
            "unrealized_pnl_pct": 0.0,
            "margin_used": total_entry_notional / max(float(plan.leverage or 1), 1e-9),
            "liquidation_price": None,
            "strategy": plan.strategy.value,
            "stop_price": plan.stop_price,
            "take_profit_price": plan.take_profit_price,
            "max_hold_hours": plan.max_hold_hours,
            "opened_at": opened_at,
            "raw": {
                "mode": "paper",
                "last_plan": asdict(plan),
            },
        }
        self.db.upsert_paper_position(payload)
        return {
            "status": "paper",
            "action": "PYRAMID" if existing is not None else "OPEN",
            "asset": plan.asset.value,
            "direction": plan.direction.value,
            "entry_price": average_entry,
            "mark_price": plan.entry_price,
            "size_asset": total_size_asset,
            "size_usd": current_notional,
            "stop_price": plan.stop_price,
            "take_profit_price": plan.take_profit_price,
        }

    def _close_position_execution(self, asset: Asset, reason: str | None = None, exit_price: float | None = None) -> dict[str, Any]:
        if not self._is_paper_mode():
            return self.client.close_position(asset)
        row = self.db.paper_position(asset.value)
        if row is None:
            return {"status": "paper", "action": "close", "asset": asset.value, "message": "No open paper position."}

        snapshot = self._paper_position_snapshot(row, refresh_mark=False)
        close_price = exit_price if exit_price is not None else self._market_price(asset)
        direction_multiplier = 1 if snapshot.direction == Direction.LONG else -1
        pnl_usd = (close_price - snapshot.entry_price) * snapshot.size_asset * direction_multiplier
        entry_notional = snapshot.entry_price * snapshot.size_asset
        exit_notional = close_price * snapshot.size_asset
        pnl_pct = (pnl_usd / max(entry_notional, 1e-9)) * 100
        closed_at = datetime.now(timezone.utc).isoformat()
        self.db.insert_paper_closed_trade(
            {
                "asset": asset.value,
                "direction": snapshot.direction.value,
                "entry_price": snapshot.entry_price,
                "exit_price": close_price,
                "size_asset": snapshot.size_asset,
                "entry_notional_usd": entry_notional,
                "exit_notional_usd": exit_notional,
                "leverage": snapshot.leverage,
                "pnl_usd": pnl_usd,
                "pnl_pct": pnl_pct,
                "strategy": row.get("strategy"),
                "reason": reason,
                "opened_at": row.get("opened_at"),
                "closed_at": closed_at,
                "raw": row.get("raw", {}),
            }
        )
        self.db.delete_paper_position(asset.value)
        return {
            "status": "paper",
            "action": "close",
            "asset": asset.value,
            "exit_price": close_price,
            "closed_pnl": pnl_usd,
            "pnl_pct": pnl_pct,
            "reason": reason,
            "closed_at": closed_at,
        }

    def _partial_close_paper_position(self, position: PositionSnapshot, fraction: float, reason: str, exit_price: float) -> dict[str, Any]:
        row = self.db.paper_position(position.asset.value)
        if row is None:
            return {"status": "paper", "action": "partial", "asset": position.asset.value, "message": "No open paper position."}

        fraction = max(0.0, min(fraction, 1.0))
        close_size = position.size_asset * fraction
        remain_size = position.size_asset - close_size
        direction_multiplier = 1 if position.direction == Direction.LONG else -1
        entry_notional = position.entry_price * close_size
        exit_notional = exit_price * close_size
        pnl_usd = (exit_price - position.entry_price) * close_size * direction_multiplier
        pnl_pct = (pnl_usd / max(entry_notional, 1e-9)) * 100
        self.db.insert_paper_closed_trade(
            {
                "asset": position.asset.value,
                "direction": position.direction.value,
                "entry_price": position.entry_price,
                "exit_price": exit_price,
                "size_asset": close_size,
                "entry_notional_usd": entry_notional,
                "exit_notional_usd": exit_notional,
                "leverage": position.leverage,
                "pnl_usd": pnl_usd,
                "pnl_pct": pnl_pct,
                "strategy": row.get("strategy"),
                "reason": reason,
                "opened_at": row.get("opened_at"),
                "closed_at": datetime.now(timezone.utc).isoformat(),
                "raw": row.get("raw", {}),
            }
        )
        if remain_size <= 1e-12:
            self.db.delete_paper_position(position.asset.value)
        else:
            remain_entry_notional = position.entry_price * remain_size
            self.db.upsert_paper_position(
                {
                    **row,
                    "size_asset": remain_size,
                    "current_price": exit_price,
                    "size_usd": exit_price * remain_size,
                    "unrealized_pnl_usd": (exit_price - position.entry_price) * remain_size * direction_multiplier,
                    "unrealized_pnl_pct": ((exit_price - position.entry_price) * direction_multiplier / max(position.entry_price, 1e-9)) * 100,
                    "margin_used": remain_entry_notional / max(position.leverage, 1e-9),
                    "raw": row.get("raw", {}),
                }
            )
        return {
            "status": "paper",
            "action": "partial",
            "asset": position.asset.value,
            "fraction": fraction,
            "exit_price": exit_price,
            "closed_pnl": pnl_usd,
            "pnl_pct": pnl_pct,
        }

    def _update_paper_position_orders(self, asset: Asset, stop_price: float | None = None, take_profit_price: float | None = None) -> dict[str, Any]:
        row = self.db.paper_position(asset.value)
        if row is None:
            return {"status": "paper", "action": "adjust", "asset": asset.value, "message": "No open paper position."}
        payload = {
            **row,
            "stop_price": stop_price if stop_price is not None else row.get("stop_price"),
            "take_profit_price": take_profit_price if take_profit_price is not None else row.get("take_profit_price"),
            "raw": row.get("raw", {}),
        }
        self.db.upsert_paper_position(payload)
        return {
            "status": "paper",
            "action": "adjust",
            "asset": asset.value,
            "stop_price": payload["stop_price"],
            "take_profit_price": payload["take_profit_price"],
        }

    def _should_halt_for_daily_loss(self, account_value: float) -> bool:
        pnl = self._daily_closed_pnl()
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
        if self._is_paper_mode():
            return self.db.paper_realized_pnl()
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
        account_value = self._account_value()
        if self._should_halt_for_daily_loss(account_value):
            payload = {"reason": "Daily loss limit reached.", "account_value": account_value}
            self.db.log("signals", DecisionAction.SKIP.value, payload)
            return [payload]

        positions = self._positions(refresh_marks=True)
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
            if plan.close_before_open and position is not None:
                close_result = self._close_position_execution(asset, reason=plan.reason, exit_price=market.mark_price)
                payload["close_result"] = close_result
            if plan.action in {DecisionAction.OPEN, DecisionAction.PYRAMID}:
                execution = self._execute_trade(plan)
                payload["execution"] = execution
            self.db.log("signals", plan.action.value, payload, asset.value, direction.value)
            self.db.set_state(self._signal_key(asset, direction), candle_time)
            outputs.append(payload)

        reporter = getattr(self, "noon_hub_reporter", None)
        if reporter is not None and outputs:
            reporter.publish_actions(outputs, "signal")

        return outputs

    def run_monitor(self) -> list[dict[str, Any]]:
        positions = self._positions(refresh_marks=True)
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
                payload["execution"] = self._close_position_execution(position.asset, reason=reason, exit_price=market.mark_price)
            elif action == DecisionAction.PARTIAL and partial_fraction is not None and self._is_paper_mode():
                payload["execution"] = self._partial_close_paper_position(position, partial_fraction, reason, market.mark_price)
            elif action == DecisionAction.ADJUST and self._is_paper_mode():
                payload["execution"] = self._update_paper_position_orders(position.asset, stop_price=new_stop)
            self.db.log("monitor", action.value, payload, position.asset.value, position.direction.value)
            outputs.append(payload)

        reporter = getattr(self, "noon_hub_reporter", None)
        if reporter is not None and outputs:
            reporter.publish_actions(outputs, "monitor")

        return outputs

    def close_position(self, asset: Asset) -> dict[str, Any]:
        result = self._close_position_execution(asset, reason="Manual close requested.")
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
        account_value = self._account_value()
        daily_pnl = self._daily_closed_pnl()
        positions = [asdict(position) for position in self._positions(refresh_marks=True)]
        open_orders = self._open_orders()
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
            "paper_positions": [asdict(position) for position in self._positions(refresh_marks=True)] if self._is_paper_mode() else [],
            "recent_logs": self.db.recent_logs(),
        }
