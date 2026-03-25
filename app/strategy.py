from __future__ import annotations

from dataclasses import asdict

from app.config import Settings
from app.models import Asset, DecisionAction, Direction, MarketSnapshot, PositionSnapshot, Regime, StrategyName, TradePlan
from app.regime import RegimeReport, build_market_snapshot, detect_regime
from app.risk import calculate_trade_levels, pyramid_risk_percent, risk_profile_for_regime


def _count_open_positions(positions: list[PositionSnapshot]) -> int:
    return sum(1 for position in positions if abs(position.size_asset) > 0)


def _primary_trigger_status(snapshot: MarketSnapshot, direction: Direction, strategy: StrategyName) -> tuple[bool, str]:
    if strategy == StrategyName.BTC_HMA:
        if direction == Direction.LONG:
            trend_state = bool(snapshot.hma_fast and snapshot.hma_slow and snapshot.hma_fast > snapshot.hma_slow and snapshot.latest_close > snapshot.hma_slow)
            signal_ok = bool(snapshot.hma_cross_up or trend_state)
            details = (
                f"hma_cross_up={bool(snapshot.hma_cross_up)}, "
                f"trend_state_bullish={trend_state}"
            )
        else:
            trend_state = bool(snapshot.hma_fast and snapshot.hma_slow and snapshot.hma_fast < snapshot.hma_slow and snapshot.latest_close < snapshot.hma_slow)
            signal_ok = bool(snapshot.hma_cross_down or trend_state)
            details = (
                f"hma_cross_down={bool(snapshot.hma_cross_down)}, "
                f"trend_state_bearish={trend_state}"
            )
    else:
        if direction == Direction.LONG:
            momentum_state = bool(snapshot.macz_value is not None and snapshot.macz_signal is not None and snapshot.macz_value > snapshot.macz_signal and (snapshot.macz_hist or 0) > 0)
            signal_ok = bool(snapshot.macz_cross_up or momentum_state)
            details = (
                f"macz_cross_up={bool(snapshot.macz_cross_up)}, "
                f"momentum_state_bullish={momentum_state}"
            )
        else:
            momentum_state = bool(snapshot.macz_value is not None and snapshot.macz_signal is not None and snapshot.macz_value < snapshot.macz_signal and (snapshot.macz_hist or 0) < 0)
            signal_ok = bool(snapshot.macz_cross_down or momentum_state)
            details = (
                f"macz_cross_down={bool(snapshot.macz_cross_down)}, "
                f"momentum_state_bearish={momentum_state}"
            )
    return signal_ok, details


def _validation_for_regime(snapshot: MarketSnapshot, regime: Regime, direction: Direction, strategy: StrategyName) -> tuple[bool, list[str], str]:
    confluences: list[str] = []
    if snapshot.volume_ratio > 1.0:
        confluences.append("volume expansion")

    if direction == Direction.LONG:
        structure_ok = snapshot.trend_up
        if snapshot.at_support:
            confluences.append("support")
        if snapshot.rsi < 45:
            confluences.append("RSI oversold")
        if snapshot.failed_breakdown:
            confluences.append("failed breakdown")
        if snapshot.double_bottom:
            confluences.append("double bottom")
    else:
        structure_ok = snapshot.trend_down
        if snapshot.at_resistance:
            confluences.append("resistance")
        if snapshot.rsi > 55:
            confluences.append("RSI overbought")
        if snapshot.failed_breakout:
            confluences.append("failed breakout")
        if snapshot.double_top:
            confluences.append("double top")

    signal_ok, trigger_details = _primary_trigger_status(snapshot, direction, strategy)

    if not signal_ok:
        return False, confluences, f"Primary trigger is not confirmed ({trigger_details})."

    if regime == Regime.TRENDING:
        if structure_ok and snapshot.volume_ratio > 1.0:
            return True, confluences, "Trending validation passed."
        return False, confluences, "Trending regime requires structure plus volume expansion."

    if regime == Regime.RANGING:
        if structure_ok:
            return False, confluences, "Range setup rejects momentum structure."
        if ("support" in confluences or "resistance" in confluences) and len(confluences) >= 3:
            return True, confluences, "Ranging validation passed."
        return False, confluences, "Ranging regime requires location and 3+ confluences."

    if regime == Regime.VOLATILE:
        if len(confluences) >= 3 and snapshot.volume_ratio > 1.1:
            return True, confluences, "Volatile regime validation passed with strict confluence."
        return False, confluences, "Volatile regime requires 3+ strong confluences and volume confirmation."

    if len(confluences) >= 2:
        return True, confluences, "Normal regime validation passed."
    return False, confluences, "Normal regime requires at least 2 confluence factors."


def _position_gate(
    asset: Asset,
    direction: Direction,
    regime: Regime,
    position: PositionSnapshot | None,
) -> tuple[DecisionAction, str]:
    if position is None:
        return DecisionAction.OPEN, "No existing position."

    pnl = position.unrealized_pnl_pct
    if position.direction == direction:
        if pnl < 0:
            return DecisionAction.HOLD, "Same-direction position is losing. Do not add."
        if regime == Regime.VOLATILE:
            return DecisionAction.SKIP, "Volatile regime forbids pyramiding."
        if regime == Regime.TRENDING and pnl > 2 and position.size_usd > 0:
            return DecisionAction.PYRAMID, "Trending regime allows pyramiding above 2% profit."
        if regime == Regime.RANGING and pnl > 4 and position.size_usd > 0:
            return DecisionAction.PYRAMID, "Ranging regime allows pyramiding above 4% profit."
        return DecisionAction.SKIP, "Already positioned in same direction."

    if pnl > 1:
        return DecisionAction.SKIP, "Opposing position is profitable. Do not fight a winner."
    if pnl < -2:
        return DecisionAction.CLOSE, "Opposing position is deeply underwater. Close and reassess."
    if regime in {Regime.TRENDING, Regime.RANGING}:
        return DecisionAction.CLOSE, "Opposing position conflicts with current regime bias."
    if regime == Regime.VOLATILE:
        return DecisionAction.CLOSE, "Volatile conflict: flatten and skip new entry."
    return DecisionAction.HOLD, "Normal regime: hold existing opposing position."


def _build_trade_plan(
    asset: Asset,
    strategy: StrategyName,
    direction: Direction,
    regime_report: RegimeReport,
    market: MarketSnapshot,
    portfolio_value: float,
    settings: Settings,
    action: DecisionAction,
    reason: str,
) -> TradePlan:
    profile = risk_profile_for_regime(regime_report.regime)
    if action == DecisionAction.PYRAMID:
        risk_percent = pyramid_risk_percent(regime_report.regime)
        if risk_percent <= 0:
            return TradePlan(asset, strategy, direction, regime_report.regime, DecisionAction.SKIP, "Pyramiding disabled.", market.mark_price)
        profile = profile.__class__(
            risk_percent=risk_percent,
            stop_percent=profile.stop_percent,
            leverage=profile.leverage,
            target_multiple=profile.target_multiple,
            max_hold_hours=profile.max_hold_hours,
        )

    levels = calculate_trade_levels(
        entry_price=market.mark_price,
        portfolio_value=portfolio_value,
        direction=direction,
        risk_percent=profile.risk_percent,
        stop_percent=profile.stop_percent,
        target_multiple=profile.target_multiple,
    )

    if profile.stop_percent > settings.max_stop_fraction:
        return TradePlan(asset, strategy, direction, regime_report.regime, DecisionAction.SKIP, "Stop too wide.", market.mark_price)
    if levels["position_size_usd"] > portfolio_value * settings.max_notional_multiple:
        return TradePlan(asset, strategy, direction, regime_report.regime, DecisionAction.SKIP, "Position too large.", market.mark_price)

    return TradePlan(
        asset=asset,
        strategy=strategy,
        direction=direction,
        regime=regime_report.regime,
        action=action,
        reason=reason,
        entry_price=market.mark_price,
        stop_price=levels["stop_price"],
        take_profit_price=levels["take_profit_price"],
        stop_percent=profile.stop_percent,
        risk_percent=profile.risk_percent,
        leverage=profile.leverage,
        target_multiple=profile.target_multiple,
        position_size_asset=levels["position_size_asset"],
        position_size_usd=levels["position_size_usd"],
        max_hold_hours=profile.max_hold_hours,
        regime_report=regime_report,
    )


def evaluate_signal(
    asset: Asset,
    strategy: StrategyName,
    direction: Direction,
    market: MarketSnapshot,
    position: PositionSnapshot | None,
    portfolio_value: float,
    open_positions: list[PositionSnapshot],
    settings: Settings,
) -> TradePlan:
    regime_report = detect_regime(market, direction.value)
    action, position_reason = _position_gate(asset, direction, regime_report.regime, position)
    if action == DecisionAction.HOLD:
        return TradePlan(asset, strategy, direction, regime_report.regime, action, position_reason, market.mark_price, regime_report=regime_report)
    if action == DecisionAction.SKIP and position is not None:
        return TradePlan(asset, strategy, direction, regime_report.regime, action, position_reason, market.mark_price, regime_report=regime_report)
    if _count_open_positions(open_positions) >= settings.max_concurrent_positions and position is None:
        return TradePlan(asset, strategy, direction, regime_report.regime, DecisionAction.SKIP, "Max concurrent positions reached.", market.mark_price, regime_report=regime_report)

    valid, confluences, validation_reason = _validation_for_regime(market, regime_report.regime, direction, strategy)
    if not valid:
        return TradePlan(
            asset,
            strategy,
            direction,
            regime_report.regime,
            DecisionAction.SKIP,
            validation_reason,
            market.mark_price,
            regime_report=regime_report,
            confluences=confluences,
        )

    if action == DecisionAction.CLOSE and regime_report.regime == Regime.VOLATILE:
        return TradePlan(asset, strategy, direction, regime_report.regime, DecisionAction.SKIP, "Conflict closed, but volatile regime blocks re-entry.", market.mark_price, regime_report=regime_report, confluences=confluences)

    plan = _build_trade_plan(
        asset=asset,
        strategy=strategy,
        direction=direction,
        regime_report=regime_report,
        market=market,
        portfolio_value=portfolio_value,
        settings=settings,
        action=DecisionAction.OPEN if position is None or action == DecisionAction.CLOSE else action,
        reason=validation_reason if action != DecisionAction.CLOSE else f"{position_reason} {validation_reason}",
    )
    plan.close_before_open = bool(position is not None and action == DecisionAction.CLOSE)
    plan.confluences = confluences
    plan.meta = {"market": asdict(market)}
    return plan


def position_structure_shift_reason(position: PositionSnapshot, market: MarketSnapshot) -> str | None:
    strategy = StrategyName.BTC_HMA if position.asset == Asset.BTC else StrategyName.ETH_MACZ
    opposite_direction = position.direction.opposite
    opposite_trigger_ok, _ = _primary_trigger_status(market, opposite_direction, strategy)
    if not opposite_trigger_ok:
        return None

    bearish_structure = (
        market.trend_down
        or (market.ema12 < market.ema26 and market.latest_close < market.sma50)
    )
    bullish_structure = (
        market.trend_up
        or (market.ema12 > market.ema26 and market.latest_close > market.sma50)
    )

    if position.direction == Direction.LONG and bearish_structure:
        return "Long thesis invalidated by bearish market structure shift."
    if position.direction == Direction.SHORT and bullish_structure:
        return "Short thesis invalidated by bullish market structure shift."
    return None
