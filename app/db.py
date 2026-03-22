from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class BotDatabase:
    def __init__(self, path: Path):
        self.path = path
        self._init_db()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS trade_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    category TEXT NOT NULL,
                    asset TEXT,
                    direction TEXT,
                    action TEXT NOT NULL,
                    payload TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS paper_positions (
                    asset TEXT PRIMARY KEY,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    current_price REAL NOT NULL,
                    size_asset REAL NOT NULL,
                    size_usd REAL NOT NULL,
                    leverage REAL NOT NULL,
                    unrealized_pnl_usd REAL NOT NULL,
                    unrealized_pnl_pct REAL NOT NULL,
                    margin_used REAL NOT NULL,
                    liquidation_price REAL,
                    strategy TEXT,
                    stop_price REAL,
                    take_profit_price REAL,
                    max_hold_hours INTEGER,
                    opened_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    raw TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS paper_closed_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    size_asset REAL NOT NULL,
                    entry_notional_usd REAL NOT NULL,
                    exit_notional_usd REAL NOT NULL,
                    leverage REAL NOT NULL,
                    pnl_usd REAL NOT NULL,
                    pnl_pct REAL NOT NULL,
                    strategy TEXT,
                    reason TEXT,
                    opened_at TEXT,
                    closed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    raw TEXT NOT NULL DEFAULT '{}'
                );
                """
            )

    def log(self, category: str, action: str, payload: dict[str, Any], asset: str | None = None, direction: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO trade_logs(category, asset, direction, action, payload) VALUES (?, ?, ?, ?, ?)",
                (category, asset, direction, action, json.dumps(payload, default=str)),
            )

    def set_state(self, key: str, value: Any) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO bot_state(key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (key, json.dumps(value, default=str)),
            )

    def get_state(self, key: str, default: Any = None) -> Any:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return json.loads(row["value"])

    def recent_logs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT created_at, category, asset, direction, action, payload FROM trade_logs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) | {"payload": json.loads(row["payload"])} for row in rows]

    def paper_positions(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT asset, direction, entry_price, current_price, size_asset, size_usd, leverage,
                       unrealized_pnl_usd, unrealized_pnl_pct, margin_used, liquidation_price,
                       strategy, stop_price, take_profit_price, max_hold_hours, opened_at, updated_at, raw
                FROM paper_positions
                ORDER BY asset
                """
            ).fetchall()
        return [dict(row) | {"raw": json.loads(row["raw"])} for row in rows]

    def paper_position(self, asset: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT asset, direction, entry_price, current_price, size_asset, size_usd, leverage,
                       unrealized_pnl_usd, unrealized_pnl_pct, margin_used, liquidation_price,
                       strategy, stop_price, take_profit_price, max_hold_hours, opened_at, updated_at, raw
                FROM paper_positions
                WHERE asset = ?
                """,
                (asset,),
            ).fetchone()
        if row is None:
            return None
        return dict(row) | {"raw": json.loads(row["raw"])}

    def upsert_paper_position(self, payload: dict[str, Any]) -> None:
        opened_at = payload.get("opened_at") or datetime.now(timezone.utc).isoformat()
        updated_at = datetime.now(timezone.utc).isoformat()
        raw = payload.get("raw", {})
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO paper_positions(
                    asset, direction, entry_price, current_price, size_asset, size_usd, leverage,
                    unrealized_pnl_usd, unrealized_pnl_pct, margin_used, liquidation_price,
                    strategy, stop_price, take_profit_price, max_hold_hours, opened_at, updated_at, raw
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asset) DO UPDATE SET
                    direction = excluded.direction,
                    entry_price = excluded.entry_price,
                    current_price = excluded.current_price,
                    size_asset = excluded.size_asset,
                    size_usd = excluded.size_usd,
                    leverage = excluded.leverage,
                    unrealized_pnl_usd = excluded.unrealized_pnl_usd,
                    unrealized_pnl_pct = excluded.unrealized_pnl_pct,
                    margin_used = excluded.margin_used,
                    liquidation_price = excluded.liquidation_price,
                    strategy = excluded.strategy,
                    stop_price = excluded.stop_price,
                    take_profit_price = excluded.take_profit_price,
                    max_hold_hours = excluded.max_hold_hours,
                    opened_at = excluded.opened_at,
                    updated_at = excluded.updated_at,
                    raw = excluded.raw
                """,
                (
                    payload["asset"],
                    payload["direction"],
                    payload["entry_price"],
                    payload["current_price"],
                    payload["size_asset"],
                    payload["size_usd"],
                    payload["leverage"],
                    payload["unrealized_pnl_usd"],
                    payload["unrealized_pnl_pct"],
                    payload["margin_used"],
                    payload.get("liquidation_price"),
                    payload.get("strategy"),
                    payload.get("stop_price"),
                    payload.get("take_profit_price"),
                    payload.get("max_hold_hours"),
                    opened_at,
                    updated_at,
                    json.dumps(raw, default=str),
                ),
            )

    def delete_paper_position(self, asset: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM paper_positions WHERE asset = ?", (asset,))

    def insert_paper_closed_trade(self, payload: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO paper_closed_trades(
                    asset, direction, entry_price, exit_price, size_asset, entry_notional_usd,
                    exit_notional_usd, leverage, pnl_usd, pnl_pct, strategy, reason, opened_at, closed_at, raw
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["asset"],
                    payload["direction"],
                    payload["entry_price"],
                    payload["exit_price"],
                    payload["size_asset"],
                    payload["entry_notional_usd"],
                    payload["exit_notional_usd"],
                    payload["leverage"],
                    payload["pnl_usd"],
                    payload["pnl_pct"],
                    payload.get("strategy"),
                    payload.get("reason"),
                    payload.get("opened_at"),
                    payload.get("closed_at") or datetime.now(timezone.utc).isoformat(),
                    json.dumps(payload.get("raw", {}), default=str),
                ),
            )

    def paper_realized_pnl(self) -> float:
        with self.connect() as conn:
            row = conn.execute("SELECT COALESCE(SUM(pnl_usd), 0.0) AS pnl FROM paper_closed_trades").fetchone()
        return float(row["pnl"] if row is not None else 0.0)

    def paper_closed_pnl_since(self, start_iso: str) -> float:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl_usd), 0.0) AS pnl FROM paper_closed_trades WHERE closed_at >= ?",
                (start_iso,),
            ).fetchone()
        return float(row["pnl"] if row is not None else 0.0)
