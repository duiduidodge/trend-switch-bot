from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any
import logging
import math

import pandas as pd
import requests
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.utils.constants import MAINNET_API_URL, TESTNET_API_URL

from app.config import Settings
from app.models import Asset, Direction, PositionSnapshot, TradePlan

logger = logging.getLogger(__name__)


class HyperliquidClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = MAINNET_API_URL if settings.hyperliquid_env.lower() == "mainnet" else TESTNET_API_URL
        self.session = requests.Session()
        self.meta = {"universe": []}
        self.spot_meta = {"universe": [], "tokens": []}
        self.exchange: Exchange | None = None
        try:
            self.meta = self._post_info({"type": "meta"})
            if settings.hyperliquid_secret_key:
                wallet = Account.from_key(settings.hyperliquid_secret_key)
                self.exchange = Exchange(
                    wallet=wallet,
                    base_url=self.base_url,
                    meta=self.meta,
                    spot_meta=self.spot_meta,
                    account_address=settings.hyperliquid_account_address or None,
                    vault_address=settings.hyperliquid_vault_address or None,
                )
        except Exception:
            logger.exception("Failed to initialize Hyperliquid client")

    def _post_info(self, payload: dict[str, Any]) -> Any:
        response = self.session.post(f"{self.base_url}/info", json=payload, timeout=20)
        response.raise_for_status()
        return response.json()

    def _market_data_enabled(self) -> bool:
        return bool(self.settings.market_data_url)

    def _market_data_get(self, path: str, params: dict[str, Any]) -> Any:
        if not self.settings.market_data_url:
            raise RuntimeError("MARKET_DATA_URL is not configured.")
        base_url = self.settings.market_data_url.rstrip("/")
        response = self.session.get(
            f"{base_url}{path}",
            params=params,
            timeout=self.settings.market_data_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def _interval_hours(self, interval: str) -> int:
        if interval.endswith("h"):
            return int(interval[:-1])
        raise ValueError(f"Unsupported interval for MCP candles: {interval}")

    @property
    def account_address(self) -> str:
        if self.settings.hyperliquid_account_address:
            return self.settings.hyperliquid_account_address
        if self.exchange is None:
            return ""
        return self.exchange.wallet.address

    def account_value(self) -> float:
        if self.settings.dry_run:
            return self.settings.paper_account_value
        if not self.account_address:
            return self.settings.paper_account_value
        state = self._post_info({"type": "clearinghouseState", "user": self.account_address, "dex": ""})
        return float(state["marginSummary"]["accountValue"])

    def daily_closed_pnl(self) -> float:
        if self.settings.dry_run:
            return 0.0
        if not self.account_address:
            return 0.0
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        fills = self._post_info({"type": "userFillsByTime", "user": self.account_address, "startTime": int(start.timestamp() * 1000)})
        return sum(float(fill.get("closedPnl", 0.0)) for fill in fills)

    def positions(self) -> list[PositionSnapshot]:
        if self.settings.dry_run:
            return []
        if not self.account_address:
            return []
        state = self._post_info({"type": "clearinghouseState", "user": self.account_address, "dex": ""})
        mids = self._post_info({"type": "allMids", "dex": ""})
        snapshots: list[PositionSnapshot] = []
        for item in state.get("assetPositions", []):
            position = item["position"]
            szi = float(position["szi"])
            if abs(szi) < 1e-12:
                continue
            asset = Asset(position["coin"])
            entry = float(position["entryPx"])
            current = float(mids[position["coin"]])
            size_asset = abs(szi)
            size_usd = float(position["positionValue"])
            pnl_usd = float(position["unrealizedPnl"])
            direction = Direction.LONG if szi > 0 else Direction.SHORT
            pnl_pct = (pnl_usd / max(size_usd, 1e-9)) * 100
            leverage_value = float(position["leverage"]["value"])
            liquidation = float(position["liquidationPx"]) if position["liquidationPx"] else None
            snapshots.append(
                PositionSnapshot(
                    asset=asset,
                    direction=direction,
                    entry_price=entry,
                    current_price=current,
                    size_asset=size_asset,
                    size_usd=size_usd,
                    leverage=leverage_value,
                    unrealized_pnl_usd=pnl_usd,
                    unrealized_pnl_pct=pnl_pct,
                    margin_used=float(position["marginUsed"]),
                    liquidation_price=liquidation,
                    raw=position,
                )
            )
        return snapshots

    def position_for_asset(self, asset: Asset) -> PositionSnapshot | None:
        for position in self.positions():
            if position.asset == asset:
                return position
        return None

    def candles(self, asset: Asset, interval: str, hours: int) -> pd.DataFrame:
        if self._market_data_enabled():
            bars = max(1, math.ceil(hours / self._interval_hours(interval)))
            payload = self._market_data_get(
                "/candles",
                {
                    "symbol": asset.value,
                    "timeframe": interval,
                    "limit": bars,
                },
            )
            candles = payload["candles"]
            df = pd.DataFrame(
                {
                    "open_time": candles["openTimes"],
                    "close_time": candles["closeTimes"],
                    "open": candles["opens"],
                    "high": candles["highs"],
                    "low": candles["lows"],
                    "close": candles["closes"],
                    "volume": candles["volumes"],
                }
            )
            if df.empty:
                raise RuntimeError(f"No candles returned for {asset.value}")
            for column in ["open", "high", "low", "close", "volume"]:
                df[column] = df[column].astype(float)
            return df

        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        rows = self._post_info(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": asset.value,
                    "interval": interval,
                    "startTime": int(start.timestamp() * 1000),
                    "endTime": int(end.timestamp() * 1000),
                },
            }
        )
        df = pd.DataFrame(rows)
        if df.empty:
            raise RuntimeError(f"No candles returned for {asset.value}")
        df = df.rename(
            columns={
                "t": "open_time",
                "T": "close_time",
                "o": "open",
                "h": "high",
                "l": "low",
                "c": "close",
                "v": "volume",
            }
        )
        for column in ["open", "high", "low", "close", "volume"]:
            df[column] = df[column].astype(float)
        return df

    def funding_rate(self, asset: Asset) -> float:
        if self._market_data_enabled():
            payload = self._market_data_get("/funding", {"symbol": asset.value})
            return float(payload["fundingRate"])
        meta, ctxs = self._post_info({"type": "metaAndAssetCtxs"})
        for asset_meta, ctx in zip(meta["universe"], ctxs):
            if asset_meta["name"] == asset.value:
                return float(ctx["funding"])
        return 0.0

    def open_orders(self) -> list[dict[str, Any]]:
        if self.settings.dry_run:
            return []
        if not self.account_address:
            return []
        return self._post_info({"type": "frontendOpenOrders", "user": self.account_address, "dex": ""})

    def cancel_orders_for_asset(self, asset: Asset) -> list[Any]:
        if self.exchange is None:
            return []
        results = []
        for order in self.open_orders():
            if order["coin"] == asset.value:
                results.append(self.exchange.cancel(asset.value, int(order["oid"])))
        return results

    def execute_trade(self, plan: TradePlan) -> dict[str, Any]:
        if self.settings.dry_run:
            return {"status": "dry_run", "plan": asdict(plan)}
        if self.exchange is None:
            raise RuntimeError("Hyperliquid credentials are not configured.")

        self.exchange.update_leverage(plan.leverage or 1, plan.asset.value)
        entry = self.exchange.market_open(
            plan.asset.value,
            is_buy=plan.direction.is_buy,
            sz=plan.position_size_asset or 0.0,
        )

        exit_side_is_buy = not plan.direction.is_buy
        tp_order = {
            "coin": plan.asset.value,
            "is_buy": exit_side_is_buy,
            "sz": plan.position_size_asset or 0.0,
            "limit_px": plan.take_profit_price,
            "order_type": {
                "trigger": {
                    "triggerPx": plan.take_profit_price,
                    "isMarket": True,
                    "tpsl": "tp",
                }
            },
            "reduce_only": True,
        }
        sl_order = {
            "coin": plan.asset.value,
            "is_buy": exit_side_is_buy,
            "sz": plan.position_size_asset or 0.0,
            "limit_px": plan.stop_price,
            "order_type": {
                "trigger": {
                    "triggerPx": plan.stop_price,
                    "isMarket": True,
                    "tpsl": "sl",
                }
            },
            "reduce_only": True,
        }
        exits = self.exchange.bulk_orders([tp_order, sl_order], grouping="positionTpsl")
        return {"status": "live", "entry": entry, "exits": exits}

    def close_position(self, asset: Asset) -> dict[str, Any]:
        if self.settings.dry_run:
            return {"status": "dry_run", "action": "close", "asset": asset.value}
        if self.exchange is None:
            raise RuntimeError("Hyperliquid credentials are not configured.")
        self.cancel_orders_for_asset(asset)
        result = self.exchange.market_close(asset.value)
        return {"status": "live", "result": result}
