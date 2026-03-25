from app.models import Direction, Regime
from app.risk import calculate_trade_levels, risk_profile_for_regime


def test_normal_long_sizing_matches_example_shape():
    profile = risk_profile_for_regime(Regime.NORMAL)
    result = calculate_trade_levels(
        entry_price=95_000,
        portfolio_value=1_000,
        direction=Direction.LONG,
        risk_percent=profile.risk_percent,
        stop_percent=profile.stop_percent,
        target_multiple=profile.target_multiple,
    )
    assert round(result["stop_price"], 2) == 90_250.00
    assert round(result["take_profit_price"], 2) == 104_500.00
    assert round(result["position_size_usd"], 2) == 1000.00


def test_normal_short_sizing_is_symmetric():
    profile = risk_profile_for_regime(Regime.NORMAL)
    result = calculate_trade_levels(
        entry_price=3_000,
        portfolio_value=1_000,
        direction=Direction.SHORT,
        risk_percent=profile.risk_percent,
        stop_percent=profile.stop_percent,
        target_multiple=profile.target_multiple,
    )
    assert round(result["stop_price"], 2) == 3_150.00
    assert round(result["take_profit_price"], 2) == 2_700.00
    assert round(result["position_size_usd"], 2) == 1000.00
