from __future__ import annotations

from functools import lru_cache
import logging
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    dry_run: bool = True
    enable_scheduler: bool = True
    signal_scan_interval_minutes: int = 5
    monitor_interval_minutes: int = 240
    database_path: str = "/tmp/trend_switch_bot.sqlite3"
    paper_account_value: float = 1000.0
    bot_version: str = "0.1.0"

    hyperliquid_env: str = Field(default="testnet")
    hyperliquid_secret_key: str = ""
    hyperliquid_account_address: str = ""
    hyperliquid_vault_address: str | None = None

    noon_hub_url: str | None = None
    noon_hub_ingest_key: str | None = None
    noon_hub_bot_slug: str = "trend-switch-bot"
    noon_hub_bot_name: str = "Trend Switch Bot"
    noon_hub_bot_environment: str = "production"
    noon_hub_bot_category: str = "trading"
    noon_hub_bot_strategy_family: str = "trend-switch"
    noon_hub_bot_venue: str = "hyperliquid"
    noon_hub_repo_url: str | None = None
    noon_hub_dashboard_url: str | None = None

    default_interval: str = "1h"
    candle_lookback_hours: int = 24 * 25
    max_concurrent_positions: int = 2
    daily_loss_limit_fraction: float = 0.03
    max_notional_multiple: float = 3.0
    max_stop_fraction: float = 0.08

    hma_fast_length: int = 20
    hma_slow_length: int = 55
    atr_length: int = 14
    adx_length: int = 14
    ema_fast_length: int = 12
    ema_slow_length: int = 26
    sma_trend_length: int = 50
    macz_zscore_length: int = 50
    macz_signal_length: int = 9

    def db_path(self) -> Path:
        path = Path(self.database_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            return path
        except OSError:
            fallback = Path("/tmp/trend_switch_bot.sqlite3")
            fallback.parent.mkdir(parents=True, exist_ok=True)
            logger.warning("Database path %s is not writable. Falling back to %s", path, fallback)
            return fallback


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
