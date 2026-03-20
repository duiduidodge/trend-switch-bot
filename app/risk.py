from __future__ import annotations

from dataclasses import dataclass

from app.models import Direction, Regime


@dataclass(frozen=True)
class RiskProfile:
    risk_percent: float
    stop_percent: float
    leverage: int
    target_multiple: float
    max_hold_hours: int


def risk_profile_for_regime(regime: Regime) -> RiskProfile:
    if regime == Regime.TRENDING:
        return RiskProfile(0.0175, 0.06, 6, 2.0, 84)
    if regime == Regime.RANGING:
        return RiskProfile(0.0125, 0.04, 4, 1.5, 36)
    if regime == Regime.VOLATILE:
        return RiskProfile(0.0075, 0.05, 3, 2.0, 36)
    return RiskProfile(0.0125, 0.05, 5, 2.0, 60)


def pyramid_risk_percent(regime: Regime) -> float:
    if regime == Regime.TRENDING:
        return 0.01
    if regime == Regime.RANGING:
        return 0.0075
    return 0.0


def calculate_trade_levels(
    entry_price: float,
    portfolio_value: float,
    direction: Direction,
    risk_percent: float,
    stop_percent: float,
    target_multiple: float,
) -> dict[str, float]:
    if direction == Direction.LONG:
        stop_price = entry_price * (1 - stop_percent)
        stop_distance = entry_price - stop_price
        take_profit_price = entry_price + (stop_distance * target_multiple)
    else:
        stop_price = entry_price * (1 + stop_percent)
        stop_distance = stop_price - entry_price
        take_profit_price = entry_price - (stop_distance * target_multiple)

    risk_amount = portfolio_value * risk_percent
    position_size_asset = risk_amount / stop_distance
    position_size_usd = position_size_asset * entry_price

    return {
        "stop_price": stop_price,
        "stop_distance": stop_distance,
        "take_profit_price": take_profit_price,
        "risk_amount": risk_amount,
        "position_size_asset": position_size_asset,
        "position_size_usd": position_size_usd,
    }
