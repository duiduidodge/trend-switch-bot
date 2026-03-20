from __future__ import annotations

import math

import numpy as np
import pandas as pd


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length).mean()


def wma(series: pd.Series, length: int) -> pd.Series:
    weights = np.arange(1, length + 1)
    return series.rolling(length).apply(lambda values: np.dot(values, weights) / weights.sum(), raw=True)


def hma(series: pd.Series, length: int) -> pd.Series:
    half = max(1, length // 2)
    sqrt_len = max(1, int(math.sqrt(length)))
    raw = (2 * wma(series, half)) - wma(series, length)
    return wma(raw, sqrt_len)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / length, adjust=False).mean()


def adx(df: pd.DataFrame, length: int = 14) -> pd.Series:
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    atr_series = atr(df, length)
    plus_di = 100 * plus_dm.ewm(alpha=1 / length, adjust=False).mean() / atr_series.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / length, adjust=False).mean() / atr_series.replace(0, np.nan)
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)) * 100
    return dx.ewm(alpha=1 / length, adjust=False).mean()


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def zscore(series: pd.Series, length: int) -> pd.Series:
    mean = series.rolling(length).mean()
    std = series.rolling(length).std(ddof=0)
    return (series - mean) / std.replace(0, np.nan)


def macz(close: pd.Series, zscore_length: int = 50, signal_length: int = 9) -> pd.DataFrame:
    macd_line = ema(close, 12) - ema(close, 26)
    macz_line = zscore(macd_line, zscore_length)
    signal = ema(macz_line.fillna(0), signal_length)
    hist = macz_line - signal
    return pd.DataFrame(
        {
            "macd": macd_line,
            "macz": macz_line,
            "signal": signal,
            "hist": hist,
        }
    )

