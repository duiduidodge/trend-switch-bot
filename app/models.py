from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Regime(str, Enum):
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    VOLATILE = "VOLATILE"
    NORMAL = "NORMAL"


class Asset(str, Enum):
    BTC = "BTC"
    ETH = "ETH"


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def is_buy(self) -> bool:
        return self == Direction.LONG

    @property
    def opposite(self) -> "Direction":
        return Direction.SHORT if self == Direction.LONG else Direction.LONG


class StrategyName(str, Enum):
    BTC_HMA = "BTC_HMA"
    ETH_MACZ = "ETH_MACZ"


class DecisionAction(str, Enum):
    OPEN = "OPEN"
    PYRAMID = "PYRAMID"
    HOLD = "HOLD"
    SKIP = "SKIP"
    CLOSE = "CLOSE"
    PARTIAL = "PARTIAL"
    ADJUST = "ADJUST"
    NONE = "NONE"


@dataclass
class MarketSnapshot:
    asset: Asset
    mark_price: float
    funding_rate: float
    adx: float
    atr: float
    atr_percentile: float
    ema12: float
    ema26: float
    sma50: float
    volume_ratio: float
    latest_close: float
    latest_high: float
    latest_low: float
    rsi: float
    wick_pct: float
    trend_up: bool
    trend_down: bool
    range_bound: bool
    at_support: bool
    at_resistance: bool
    failed_breakdown: bool
    failed_breakout: bool
    double_bottom: bool
    double_top: bool
    hma_fast: float | None = None
    hma_slow: float | None = None
    hma_cross_up: bool | None = None
    hma_cross_down: bool | None = None
    macz_value: float | None = None
    macz_signal: float | None = None
    macz_hist: float | None = None
    macz_cross_up: bool | None = None
    macz_cross_down: bool | None = None


@dataclass
class RegimeReport:
    regime: Regime
    reasons: list[str]
    factors: dict[str, Any]


@dataclass
class PositionSnapshot:
    asset: Asset
    direction: Direction
    entry_price: float
    current_price: float
    size_asset: float
    size_usd: float
    leverage: float
    unrealized_pnl_usd: float
    unrealized_pnl_pct: float
    margin_used: float
    liquidation_price: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class TradePlan:
    asset: Asset
    strategy: StrategyName
    direction: Direction
    regime: Regime
    action: DecisionAction
    reason: str
    entry_price: float
    stop_price: float | None = None
    take_profit_price: float | None = None
    stop_percent: float | None = None
    risk_percent: float | None = None
    leverage: int | None = None
    target_multiple: float | None = None
    position_size_asset: float | None = None
    position_size_usd: float | None = None
    max_hold_hours: int | None = None
    regime_report: RegimeReport | None = None
    confluences: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
