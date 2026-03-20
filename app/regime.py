from __future__ import annotations

from dataclasses import asdict

import pandas as pd

from app.config import Settings
from app.indicators import adx, atr, ema, hma, macz, rsi, sma
from app.models import Asset, MarketSnapshot, Regime, RegimeReport


def _price_structure(df: pd.DataFrame) -> tuple[bool, bool]:
    highs = df["high"].tail(5).reset_index(drop=True)
    lows = df["low"].tail(5).reset_index(drop=True)
    trend_up = bool((highs.diff().dropna() > 0).sum() >= 3 and (lows.diff().dropna() > 0).sum() >= 3)
    trend_down = bool((highs.diff().dropna() < 0).sum() >= 3 and (lows.diff().dropna() < 0).sum() >= 3)
    return trend_up, trend_down


def _range_bound(df: pd.DataFrame, atr_value: float) -> bool:
    recent = df.tail(24)
    price_range = recent["high"].max() - recent["low"].min()
    mid = recent["close"].mean()
    return price_range <= max(atr_value * 4, mid * 0.03)


def _support_resistance_flags(df: pd.DataFrame, atr_value: float) -> tuple[bool, bool]:
    recent = df.tail(24)
    close = recent["close"].iloc[-1]
    support = recent["low"].min()
    resistance = recent["high"].max()
    band = max(atr_value * 0.5, close * 0.003)
    return close <= support + band, close >= resistance - band


def _double_bottom(df: pd.DataFrame, atr_value: float) -> bool:
    recent = df.tail(20)
    lows = recent["low"].nsmallest(2).sort_values()
    if len(lows) < 2:
        return False
    return abs(lows.iloc[1] - lows.iloc[0]) <= atr_value * 0.35


def _double_top(df: pd.DataFrame, atr_value: float) -> bool:
    recent = df.tail(20)
    highs = recent["high"].nlargest(2).sort_values()
    if len(highs) < 2:
        return False
    return abs(highs.iloc[1] - highs.iloc[0]) <= atr_value * 0.35


def _failed_breakout(df: pd.DataFrame) -> bool:
    recent = df.tail(5)
    return recent["high"].iloc[-2] > recent["high"].iloc[:-2].max() and recent["close"].iloc[-1] < recent["close"].iloc[-2]


def _failed_breakdown(df: pd.DataFrame) -> bool:
    recent = df.tail(5)
    return recent["low"].iloc[-2] < recent["low"].iloc[:-2].min() and recent["close"].iloc[-1] > recent["close"].iloc[-2]


def build_market_snapshot(asset: Asset, candles: pd.DataFrame, funding_rate: float, settings: Settings) -> MarketSnapshot:
    df = candles.copy()
    df["ema12"] = ema(df["close"], settings.ema_fast_length)
    df["ema26"] = ema(df["close"], settings.ema_slow_length)
    df["sma50"] = sma(df["close"], settings.sma_trend_length)
    df["adx"] = adx(df, settings.adx_length)
    df["atr"] = atr(df, settings.atr_length)
    df["rsi"] = rsi(df["close"])
    df["hma_fast"] = hma(df["close"], settings.hma_fast_length)
    df["hma_slow"] = hma(df["close"], settings.hma_slow_length)
    macz_frame = macz(df["close"], settings.macz_zscore_length, settings.macz_signal_length)
    df = pd.concat([df, macz_frame], axis=1)

    latest = df.iloc[-1]
    previous = df.iloc[-2]
    atr_window = df["atr"].dropna().tail(24 * 20)
    atr_percentile = float((atr_window <= latest["atr"]).mean() * 100) if not atr_window.empty else 50.0
    trend_up, trend_down = _price_structure(df)
    range_bound = _range_bound(df, float(latest["atr"]))
    at_support, at_resistance = _support_resistance_flags(df, float(latest["atr"]))
    wick_pct = float(
        max(
            abs(latest["high"] - latest["close"]) / latest["close"],
            abs(latest["close"] - latest["low"]) / latest["close"],
        )
        * 100
    )
    volume_ratio = float(latest["volume"] / max(df["volume"].tail(20).mean(), 1e-9))

    return MarketSnapshot(
        asset=asset,
        mark_price=float(latest["close"]),
        funding_rate=funding_rate,
        adx=float(latest["adx"]),
        atr=float(latest["atr"]),
        atr_percentile=atr_percentile,
        ema12=float(latest["ema12"]),
        ema26=float(latest["ema26"]),
        sma50=float(latest["sma50"]),
        volume_ratio=volume_ratio,
        latest_close=float(latest["close"]),
        latest_high=float(latest["high"]),
        latest_low=float(latest["low"]),
        rsi=float(latest["rsi"]),
        wick_pct=wick_pct,
        trend_up=trend_up,
        trend_down=trend_down,
        range_bound=range_bound,
        at_support=at_support,
        at_resistance=at_resistance,
        failed_breakdown=_failed_breakdown(df),
        failed_breakout=_failed_breakout(df),
        double_bottom=_double_bottom(df, float(latest["atr"])),
        double_top=_double_top(df, float(latest["atr"])),
        hma_fast=float(latest["hma_fast"]),
        hma_slow=float(latest["hma_slow"]),
        hma_cross_up=bool(previous["hma_fast"] <= previous["hma_slow"] and latest["hma_fast"] > latest["hma_slow"]),
        hma_cross_down=bool(previous["hma_fast"] >= previous["hma_slow"] and latest["hma_fast"] < latest["hma_slow"]),
        macz_value=float(latest["macz"]),
        macz_signal=float(latest["signal"]),
        macz_hist=float(latest["hist"]),
        macz_cross_up=bool(previous["macz"] <= 0 and latest["macz"] > 0),
        macz_cross_down=bool(previous["macz"] >= 0 and latest["macz"] < 0),
    )


def detect_regime(snapshot: MarketSnapshot, direction_bias: str) -> RegimeReport:
    reasons: list[str] = []
    ema_bullish = snapshot.ema12 > snapshot.ema26 > snapshot.sma50
    ema_bearish = snapshot.ema12 < snapshot.ema26 < snapshot.sma50
    aligned_with_direction = ema_bullish if direction_bias == "LONG" else ema_bearish
    structure_ok = snapshot.trend_up if direction_bias == "LONG" else snapshot.trend_down

    if snapshot.atr_percentile > 90 or snapshot.wick_pct > 5 or abs(snapshot.funding_rate) > 0.001:
        regime = Regime.VOLATILE
        reasons.append("ATR/funding/wicks indicate unstable conditions.")
    elif snapshot.adx > 25 and aligned_with_direction and structure_ok and snapshot.atr_percentile < 75:
        regime = Regime.TRENDING
        reasons.append("ADX, EMA alignment, and price structure confirm trend conditions.")
    elif snapshot.adx < 20 and snapshot.range_bound and not (ema_bullish or ema_bearish):
        regime = Regime.RANGING
        reasons.append("Low ADX with flat structure indicates range behavior.")
    else:
        regime = Regime.NORMAL
        reasons.append("No strong trend, range, or volatility edge. Using balanced defaults.")

    return RegimeReport(
        regime=regime,
        reasons=reasons,
        factors={
            "adx": round(snapshot.adx, 2),
            "atr": round(snapshot.atr, 4),
            "atr_percentile": round(snapshot.atr_percentile, 2),
            "ema12": round(snapshot.ema12, 2),
            "ema26": round(snapshot.ema26, 2),
            "sma50": round(snapshot.sma50, 2),
            "wick_pct": round(snapshot.wick_pct, 2),
            "funding_rate": snapshot.funding_rate,
            "trend_up": snapshot.trend_up,
            "trend_down": snapshot.trend_down,
            "range_bound": snapshot.range_bound,
            "aligned_with_direction": aligned_with_direction,
        },
    )
