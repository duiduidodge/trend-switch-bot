import pandas as pd

from app.indicators import atr, ema, hma, macz


def test_indicator_outputs_exist():
    close = pd.Series([100 + i for i in range(80)], dtype=float)
    high = close + 2
    low = close - 2
    df = pd.DataFrame({"high": high, "low": low, "close": close})

    assert not ema(close, 12).dropna().empty
    assert not hma(close, 20).dropna().empty
    assert not atr(df, 14).dropna().empty

    macz_frame = macz(close)
    assert {"macd", "macz", "signal", "hist"} == set(macz_frame.columns)
    assert not macz_frame["signal"].dropna().empty
