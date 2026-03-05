from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional, Dict, Any
import csv
import os
import time


Side = Literal["LONG", "SHORT", "FLAT"]


@dataclass
class PaperPosition:
    side: Side = "FLAT"
    size: float = 0.0           # base units (BTC)
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    take_profit_price: Optional[float] = None


@dataclass
class PaperAccount:
    equity_usd: float
    realized_pnl_today: float = 0.0
    # Avoid shared mutable defaults
    pos: PaperPosition = field(default_factory=PaperPosition)


@dataclass(frozen=True)
class PaperCfg:
    starting_equity_usd: float = 10_000.0
    taker_fee_bps: float = 4.0
    maker_fee_bps: float = 2.0
    slippage_bps: float = 1.0
    assume_taker: bool = True
    trades_csv_path: str = "trades.csv"


def _bps(x_bps: float) -> float:
    return x_bps / 10_000.0


def _fee_bps(cfg: PaperCfg) -> float:
    return cfg.taker_fee_bps if cfg.assume_taker else cfg.maker_fee_bps


def _apply_slippage(side: Literal["BUY", "SELL"], price: float, slippage_bps: float) -> float:
    s = _bps(slippage_bps)
    if side == "BUY":
        return price * (1.0 + s)
    return price * (1.0 - s)


def _ensure_csv(path: str) -> None:
    if os.path.exists(path):
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "ts_ms", "event", "side", "size", "fill_price",
            "notional", "fee_usd", "pnl_usd", "equity_usd", "reason"
        ])


def open_position(
    acct: PaperAccount,
    cfg: PaperCfg,
    side: Literal["LONG", "SHORT"],
    size: float,
    ref_price: float,
    stop_price: float,
    take_profit_price: Optional[float],
    reason: str,
) -> Dict[str, Any]:
    if acct.pos.side != "FLAT":
        return {"ok": False, "reason": "already_in_position"}

    _ensure_csv(cfg.trades_csv_path)

    if size <= 0:
        return {"ok": False, "reason": "bad_size"}

    if side == "LONG":
        fill = _apply_slippage("BUY", ref_price, cfg.slippage_bps)
    else:
        fill = _apply_slippage("SELL", ref_price, cfg.slippage_bps)

    notional = abs(size * fill)
    fee = notional * _bps(_fee_bps(cfg))

    # Deduct fee immediately
    acct.equity_usd -= fee
    # Track fee in daily realized PnL too (fee-inclusive daily loss)
    acct.realized_pnl_today -= fee

    acct.pos = PaperPosition(
        side=side,
        size=size,
        entry_price=fill,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
    )

    _log_trade(cfg.trades_csv_path, "OPEN", side, size, fill, notional, fee, 0.0, acct.equity_usd, reason)
    return {"ok": True, "fill": fill, "fee": fee}


def close_position(
    acct: PaperAccount,
    cfg: PaperCfg,
    ref_price: float,
    reason: str,
) -> Dict[str, Any]:
    if acct.pos.side == "FLAT" or acct.pos.entry_price is None:
        return {"ok": False, "reason": "no_position"}

    _ensure_csv(cfg.trades_csv_path)

    side = acct.pos.side
    size = acct.pos.size
    entry = acct.pos.entry_price

    # Closing trade direction:
    if side == "LONG":
        fill = _apply_slippage("SELL", ref_price, cfg.slippage_bps)
        pnl = (fill - entry) * size
    else:  # SHORT
        fill = _apply_slippage("BUY", ref_price, cfg.slippage_bps)
        pnl = (entry - fill) * size

    notional = abs(size * fill)
    fee = notional * _bps(_fee_bps(cfg))

    acct.equity_usd += pnl
    acct.equity_usd -= fee

    # Fee-inclusive realized PnL
    acct.realized_pnl_today += pnl - fee

    _log_trade(cfg.trades_csv_path, "CLOSE", side, size, fill, notional, fee, pnl, acct.equity_usd, reason)

    # Reset position
    acct.pos = PaperPosition()
    return {"ok": True, "fill": fill, "fee": fee, "pnl": pnl}


def check_stops(acct: PaperAccount, cfg: PaperCfg, last_price: float) -> Optional[Dict[str, Any]]:
    """
    If stop or take-profit is hit, close.
    """
    pos = acct.pos
    if pos.side == "FLAT" or pos.entry_price is None:
        return None

    # Stop
    if pos.stop_price is not None:
        if pos.side == "LONG" and last_price <= pos.stop_price:
            return close_position(acct, cfg, last_price, reason="stop_hit")
        if pos.side == "SHORT" and last_price >= pos.stop_price:
            return close_position(acct, cfg, last_price, reason="stop_hit")

    # Take profit
    if pos.take_profit_price is not None:
        if pos.side == "LONG" and last_price >= pos.take_profit_price:
            return close_position(acct, cfg, last_price, reason="take_profit_hit")
        if pos.side == "SHORT" and last_price <= pos.take_profit_price:
            return close_position(acct, cfg, last_price, reason="take_profit_hit")

    return None


def _log_trade(
    path: str,
    event: str,
    side: str,
    size: float,
    fill_price: float,
    notional: float,
    fee_usd: float,
    pnl_usd: float,
    equity_usd: float,
    reason: str,
) -> None:
    ts_ms = int(time.time() * 1000)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([ts_ms, event, side, size, fill_price, notional, fee_usd, pnl_usd, equity_usd, reason])
        