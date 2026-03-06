from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Tuple, Any

import pandas as pd

from bot.config import load_config
from bot.hl_info import HLInfoClient, resolve_api_base
from bot.logger import setup_logging
from bot.risk import check_daily_loss
from bot.strategy import compute_signal
from bot.paper import (
    PaperAccount,
    PaperCfg,
    open_position,
    close_position,
    check_stops,
)

from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants


def candles_to_df(candles: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df["c"] = df["c"].astype(float)
    df = df.sort_values("t")
    return df


PERP_MAX_DECIMALS = 6


def quantize_down(x: float, decimals: int) -> float:
    if decimals < 0:
        decimals = 0
    q = Decimal("1") if decimals == 0 else Decimal("1").scaleb(-decimals)
    return float(Decimal(str(x)).quantize(q, rounding=ROUND_DOWN))


def get_sz_decimals_from_meta(meta: dict, coin: str) -> int:
    universe = meta.get("universe", [])
    for u in universe:
        if u.get("name") == coin:
            return int(u.get("szDecimals"))
    raise ValueError(f"Could not find szDecimals for coin={coin} in meta.universe")


def round_sz_perp(sz: float, sz_decimals: int) -> float:
    return quantize_down(sz, sz_decimals)


def round_px_perp(px: float, sz_decimals: int) -> float:
    px_decimals = max(0, PERP_MAX_DECIMALS - sz_decimals)
    return quantize_down(px, px_decimals)


@dataclass(frozen=True)
class LiveFillResult:
    ok: bool
    raw: dict


class LiveHL:
    """
    Live Hyperliquid client for perps.

    Uses an empty spot_meta to avoid testnet/mainnet spot metadata issues
    for a perp-only bot.
    """

    def __init__(self, env: str, account_address: str, api_wallet_private_key: str, coin: str):
        api_url = constants.TESTNET_API_URL if env == "testnet" else constants.MAINNET_API_URL
        self.env = env
        self.api_url = api_url
        self.account_address = account_address
        self.coin = coin

        pk = api_wallet_private_key.strip()
        if pk.startswith("0x"):
            pk = pk[2:]
        wallet = Account.from_key(bytes.fromhex(pk))

        empty_spot = {"universe": [], "tokens": []}
        self.info = Info(api_url, skip_ws=True, spot_meta=empty_spot)
        self.exchange = Exchange(wallet, api_url, spot_meta=empty_spot)

        self.meta = self.info.meta()
        self.sz_decimals = get_sz_decimals_from_meta(self.meta, coin)

    def user_state(self) -> dict:
        return self.info.user_state(self.account_address)

    def open_orders(self) -> Any:
        try:
            fn = getattr(self.info, "open_orders", None)
            if callable(fn):
                return fn(self.account_address)
        except Exception:
            return None
        return None

    def get_equity_and_position(self) -> Tuple[float, float]:
        us = self.user_state()

        equity = 0.0
        ms = us.get("marginSummary") or {}
        if "accountValue" in ms:
            try:
                equity = float(ms["accountValue"])
            except Exception:
                equity = 0.0

        pos_szi = 0.0
        for ap in us.get("assetPositions", []):
            p = ap.get("position", {})
            if p.get("coin") == self.coin:
                pos_szi = float(p.get("szi", 0.0))
                break

        return equity, pos_szi

    def place_limit_ioc(self, is_buy: bool, sz: float, px: float, reduce_only: bool) -> LiveFillResult:
        sz_r = round_sz_perp(sz, self.sz_decimals)
        px_r = round_px_perp(px, self.sz_decimals)
        if sz_r <= 0:
            return LiveFillResult(False, {"error": "rounded_size<=0", "sz": sz, "sz_r": sz_r})

        try:
            resp = self.exchange.order(
                self.coin,
                is_buy=is_buy,
                sz=sz_r,
                limit_px=px_r,
                order_type={"limit": {"tif": "Ioc"}},
                reduce_only=reduce_only,
            )
            return LiveFillResult(True, resp)
        except Exception as e:
            return LiveFillResult(False, {"error": str(e)})

    def place_market(self, is_buy: bool, sz: float, reduce_only: bool) -> LiveFillResult:
        sz_r = round_sz_perp(sz, self.sz_decimals)
        if sz_r <= 0:
            return LiveFillResult(False, {"error": "rounded_size<=0", "sz": sz, "sz_r": sz_r})

        try:
            resp = self.exchange.order(
                self.coin,
                is_buy=is_buy,
                sz=sz_r,
                order_type={"market": {}},
                reduce_only=reduce_only,
            )
            return LiveFillResult(True, resp)
        except Exception as e:
            return LiveFillResult(False, {"error": str(e)})

    def schedule_cancel_all(self, trigger_time_ms: Optional[int]) -> LiveFillResult:
        """
        Dead man's switch.
        If not renewed before trigger_time_ms, Hyperliquid cancels resting orders.
        Pass None to clear the scheduled cancel.
        """
        try:
            resp = self.exchange.schedule_cancel(trigger_time_ms)
            return LiveFillResult(True, resp)
        except Exception as e:
            return LiveFillResult(False, {"error": str(e)})



def append_snapshot(path: str, snap: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(snap, separators=(",", ":"), default=str) + "\n")


def main() -> None:
    cfg, secrets = load_config()

    logger = setup_logging(cfg.logging.level, cfg.logging.to_file, cfg.logging.file_path)

    api_base = resolve_api_base(cfg.env, secrets.api_url_override)
    candle_info = HLInfoClient(api_base)

    logger.info(f"mode={cfg.mode} env={cfg.env} api={api_base} coin={cfg.coin} interval={cfg.interval}")

    paper_cfg = PaperCfg(
        starting_equity_usd=cfg.paper.starting_equity_usd,
        taker_fee_bps=cfg.paper.taker_fee_bps,
        maker_fee_bps=cfg.paper.maker_fee_bps,
        slippage_bps=cfg.paper.slippage_bps,
        assume_taker=cfg.paper.assume_taker,
        trades_csv_path="trades.csv",
    )
    acct = PaperAccount(equity_usd=paper_cfg.starting_equity_usd)

    live: Optional[LiveHL] = None
    if cfg.mode == "live":
        live = LiveHL(
            env=cfg.env,
            account_address=secrets.account_address,
            api_wallet_private_key=secrets.api_wallet_private_key,
            coin=cfg.coin,
        )
        logger.warning(f"LIVE MODE ENABLED on {live.api_url} (coin={cfg.coin}) szDecimals={live.sz_decimals}")

    HALTED = False
    HALT_REASON: Optional[str] = None
    SNAPSHOT_PATH = "account_snapshots.jsonl"
    DMS_REFRESH_SEC = 30

    last_candle_ts = None

    while True:
        try:
            candles = candle_info.candle_snapshot_last_n(cfg.coin, cfg.interval, cfg.runtime.lookback_candles)
            df = candles_to_df(candles)

            latest_ts = df["t"].iloc[-1]
            if last_candle_ts is not None and latest_ts == last_candle_ts:
                time.sleep(cfg.runtime.loop_sleep_sec)
                continue
            last_candle_ts = latest_ts

            closes = df["c"]
            sig = compute_signal(
                closes=closes,
                ma_len=cfg.strategy.ma_len,
                vol_len=cfg.strategy.vol_len,
                band_mult=cfg.strategy.band_mult,
                allow_short=cfg.risk.allow_short,
            )
            last = float(sig.last)

            if cfg.mode == "paper":
                stop_result = check_stops(acct, paper_cfg, last_price=last)
                if stop_result:
                    logger.warning(
                        f"EXIT(stop/tp): {stop_result} pos={acct.pos.side} eq={acct.equity_usd:.2f} pnl_today={acct.realized_pnl_today:.2f}"
                    )

                equity = acct.equity_usd

                class _Shim:
                    realized_pnl_today = acct.realized_pnl_today

                dloss = check_daily_loss(_Shim, equity, cfg.risk.daily_loss_limit_pct)
                if not dloss.ok:
                    logger.warning(f"risk_block: {dloss.reason} eq={equity:.2f} pnl_today={acct.realized_pnl_today:.2f}")
                    time.sleep(cfg.runtime.loop_sleep_sec)
                    continue

                logger.info(
                    f"t={latest_ts} last={last:.2f} ma={sig.ma:.2f} vol={sig.vol:.2f} "
                    f"bands=[{sig.lower:.2f},{sig.upper:.2f}] signal={sig.side}:{sig.reason} "
                    f"pos={acct.pos.side} eq={acct.equity_usd:.2f} pnl_today={acct.realized_pnl_today:.2f}"
                )

                if acct.pos.side == "FLAT" and sig.side in ("LONG", "SHORT"):
                    max_notional = cfg.risk.max_position_pct_equity * equity
                    size_btc = max_notional / max(last, 1e-9)

                    if sig.side == "LONG":
                        stop_price = last - sig.vol
                        r = open_position(acct, paper_cfg, "LONG", size_btc, last, stop_price, None, sig.reason)
                        logger.warning(f"OPEN_LONG: {r} stop={stop_price:.2f}")
                    else:
                        stop_price = last + sig.vol
                        r = open_position(acct, paper_cfg, "SHORT", size_btc, last, stop_price, None, sig.reason)
                        logger.warning(f"OPEN_SHORT: {r} stop={stop_price:.2f}")

                if acct.pos.side != "FLAT" and cfg.strategy.exit_on_ma_cross:
                    if acct.pos.side == "LONG" and last >= sig.ma:
                        r = close_position(acct, paper_cfg, last, reason="ma_cross_exit")
                        logger.warning(f"CLOSE_LONG: {r}")
                    elif acct.pos.side == "SHORT" and last <= sig.ma:
                        r = close_position(acct, paper_cfg, last, reason="ma_cross_exit")
                        logger.warning(f"CLOSE_SHORT: {r}")

            else:
                assert live is not None

                trigger_time_ms = int((time.time() + DMS_REFRESH_SEC) * 1000)
                dms = live.schedule_cancel_all(trigger_time_ms)
                if not dms.ok:
                    logger.error(f"[LIVE] scheduleCancel failed: {dms.raw}")
                else:
                    logger.info(f"[LIVE] dead_man_switch_armed_until={trigger_time_ms}")

                equity, pos_szi = live.get_equity_and_position()
                oo = live.open_orders()

                open_orders_count = None
                try:
                    if oo is None:
                        open_orders_count = None
                    elif isinstance(oo, list):
                        open_orders_count = len(oo)
                    elif isinstance(oo, dict) and "orders" in oo and isinstance(oo["orders"], list):
                        open_orders_count = len(oo["orders"])
                    else:
                        open_orders_count = None
                except Exception:
                    open_orders_count = None

                pos_side = "FLAT"
                if pos_szi > 0:
                    pos_side = "LONG"
                elif pos_szi < 0:
                    pos_side = "SHORT"

                append_snapshot(
                    SNAPSHOT_PATH,
                    {
                        "ts_ms": int(time.time() * 1000),
                        "candle_t": str(latest_ts),
                        "env": cfg.env,
                        "coin": cfg.coin,
                        "last": last,
                        "signal": {"side": sig.side, "reason": sig.reason},
                        "account": {
                            "equity": equity,
                            "pos_side": pos_side,
                            "pos_szi": pos_szi,
                            "open_orders_count": open_orders_count,
                            "open_orders_raw": oo if open_orders_count is None else None,
                        },
                        "halted": HALTED,
                        "halt_reason": HALT_REASON,
                    },
                )

                logger.info(
                    f"[LIVE] t={latest_ts} last={last:.2f} ma={sig.ma:.2f} vol={sig.vol:.2f} "
                    f"bands=[{sig.lower:.2f},{sig.upper:.2f}] signal={sig.side}:{sig.reason} "
                    f"pos={pos_side} szi={pos_szi:.6f} eq={equity:.2f} oo={open_orders_count}"
                )

                if not HALTED and open_orders_count is not None and open_orders_count > 0:
                    HALTED = True
                    HALT_REASON = f"drift_open_orders_present:{open_orders_count}"
                    logger.error(f"[HALT] {HALT_REASON} — cancel orders in UI, then restart bot.")

                if not HALTED and pos_side != "FLAT" and sig.side in ("LONG", "SHORT"):
                    HALTED = True
                    HALT_REASON = f"drift_position_present:{pos_side}"
                    logger.error(f"[HALT] {HALT_REASON} — resolve position in UI, then restart bot.")

                if HALTED:
                    time.sleep(cfg.runtime.loop_sleep_sec)
                    continue

                class _Shim:
                    realized_pnl_today = 0.0

                dloss = check_daily_loss(_Shim, max(equity, 1e-9), cfg.risk.daily_loss_limit_pct)
                if not dloss.ok:
                    logger.warning(f"risk_block: {dloss.reason} eq={equity:.2f}")
                    time.sleep(cfg.runtime.loop_sleep_sec)
                    continue

                max_notional = cfg.risk.max_position_pct_equity * equity
                target_sz = max_notional / max(last, 1e-9)

                if pos_side == "LONG" and cfg.strategy.exit_on_ma_cross and last >= sig.ma:
                    res = live.place_limit_ioc(is_buy=False, sz=abs(pos_szi), px=last, reduce_only=True)
                    logger.warning(f"[LIVE] CLOSE_LONG IOC: {res.raw}")
                    if not res.ok:
                        res2 = live.place_market(is_buy=False, sz=abs(pos_szi), reduce_only=True)
                        logger.warning(f"[LIVE] CLOSE_LONG MKT: {res2.raw}")

                elif pos_side == "SHORT" and cfg.strategy.exit_on_ma_cross and last <= sig.ma:
                    res = live.place_limit_ioc(is_buy=True, sz=abs(pos_szi), px=last, reduce_only=True)
                    logger.warning(f"[LIVE] CLOSE_SHORT IOC: {res.raw}")
                    if not res.ok:
                        res2 = live.place_market(is_buy=True, sz=abs(pos_szi), reduce_only=True)
                        logger.warning(f"[LIVE] CLOSE_SHORT MKT: {res2.raw}")

                if pos_side == "FLAT":
                    if sig.side == "LONG":
                        res = live.place_limit_ioc(is_buy=True, sz=target_sz, px=last, reduce_only=False)
                        logger.warning(f"[LIVE] OPEN_LONG IOC: {res.raw}")
                    elif sig.side == "SHORT":
                        res = live.place_limit_ioc(is_buy=False, sz=target_sz, px=last, reduce_only=False)
                        logger.warning(f"[LIVE] OPEN_SHORT IOC: {res.raw}")

        except Exception as e:
            logger.exception(f"tick_error: {e}")

        time.sleep(cfg.runtime.loop_sleep_sec)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped by user")