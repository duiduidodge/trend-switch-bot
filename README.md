# Trend Switch Bot

Regime-adaptive trading bot for Hyperliquid, built as a Railway-friendly Python service.

This implementation follows your framework:

- trades `BTC` with an HMA Cloud trigger on `1H`
- trades `ETH` with a MAC-Z trigger on `1H`
- trades `PAXG` (Gold) with an HMA Cloud trigger on `1H`
- detects regime before every decision
- enforces one position per asset
- adapts risk, leverage, stops, targets, filters, and pyramiding by regime
- manages open positions on a timed loop
- defaults to `DRY_RUN=true`

## What It Does

The service exposes HTTP endpoints and can also run a background scheduler:

- `GET /healthz`
- `POST /run/signals`
- `POST /run/monitor`
- `GET /state`

Noon Hub integration:

- registers the bot on startup when `NOON_HUB_URL` and `NOON_HUB_INGEST_KEY` are set
- publishes heartbeat, metrics, and open-position snapshots every minute
- emits Noon Hub events for signal, monitor, and manual-close actions

Signal cycle:

1. Pull latest `1H` candles for BTC, ETH, and PAXG from Hyperliquid
2. Compute regime features: ADX, ATR percentile, EMA alignment, price structure, wick behavior, funding
3. Run the relevant trigger:
   - BTC: HMA Cloud bullish / bearish
   - ETH: MAC-Z bullish / bearish
   - PAXG: HMA Cloud bullish / bearish
4. Validate regime-specific entry conditions
5. Check existing position and apply conflict / pyramid logic
6. Size the trade from account value and stop distance
7. Place market entry plus TP/SL trigger orders when `DRY_RUN=false`
8. Log every decision to SQLite

Monitor cycle:

1. Pull current open positions
2. Re-detect current regime per asset
3. Apply invalidation rules
4. Tighten stop / take partial / close if required
5. Log the management decision

## Indicator Notes

`MAC-Z` is not a standard exchange-native indicator, so this project implements a documented approximation:

- MACD line = EMA(12) - EMA(26)
- MAC-Z = rolling z-score of the MACD line
- signal = EMA(9) of MAC-Z
- histogram = MAC-Z - signal

If you want a different MAC-Z formula, swap it in [`app/indicators.py`](/Users/dodge/Desktop/Vibe Code Project/Trend Switch Bot/app/indicators.py).

## Railway Deployment

1. Create a Railway service from this repo.
2. Add the environment variables from `.env.example`.
3. Mount a persistent volume if you want logs/state to survive redeploys.
4. Keep `DRY_RUN=true` until you validate behavior on testnet.
5. Set `NOON_HUB_URL` and `NOON_HUB_INGEST_KEY` if you want this bot to appear in Noon Hub.

## Local Run

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
uvicorn app.main:app --reload
```

## Test

```bash
pytest
```
