from __future__ import annotations

import time
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


def candles_to_df(candles: list[dict]) -> pd.DataFrame:
    # candles have keys like: t,T,o,h,l,c,v,n,i,s (strings for prices/vol)
    df = pd.DataFrame(candles)
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df["c"] = df["c"].astype(float)
    df = df.sort_values("t")
    return df


def main() -> None:
    cfg, secrets = load_config()

    logger = setup_logging(cfg.logging.level, cfg.logging.to_file, cfg.logging.file_path)
    api_base = resolve_api_base(cfg.env, secrets.api_url_override)
    info = HLInfoClient(api_base)

    # Paper trading config + account
    paper_cfg = PaperCfg(
        starting_equity_usd=cfg.paper.starting_equity_usd,
        taker_fee_bps=cfg.paper.taker_fee_bps,
        maker_fee_bps=cfg.paper.maker_fee_bps,
        slippage_bps=cfg.paper.slippage_bps,
        assume_taker=cfg.paper.assume_taker,
        trades_csv_path="trades.csv",
    )
    acct = PaperAccount(equity_usd=paper_cfg.starting_equity_usd)

    logger.info(f"mode=paper env={cfg.env} api={api_base} coin={cfg.coin} interval={cfg.interval}")

    last_candle_ts = None  # <-- must be inside main (or declare global)

    while True:
        try:
            candles = info.candle_snapshot_last_n(cfg.coin, cfg.interval, cfg.runtime.lookback_candles)
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

            # 1) Stop/TP check first (may close position)
            stop_result = check_stops(acct, paper_cfg, last_price=last)
            if stop_result:
                logger.warning(
                    f"EXIT(stop/tp): {stop_result} pos={acct.pos.side} eq={acct.equity_usd:.2f} pnl_today={acct.realized_pnl_today:.2f}"
                )

            # 2) Risk gates (daily loss)
            equity = acct.equity_usd
            # Reuse existing risk function by passing a tiny shim-like object
            # (check_daily_loss reads .realized_pnl_today)
            class _Shim:
                realized_pnl_today = acct.realized_pnl_today

            dloss = check_daily_loss(_Shim, equity, cfg.risk.daily_loss_limit_pct)
            if not dloss.ok:
                logger.warning(f"risk_block: {dloss.reason} eq={equity:.2f} pnl_today={acct.realized_pnl_today:.2f}")
                time.sleep(cfg.runtime.loop_sleep_sec)
                continue

            # Status log
            logger.info(
                f"t={latest_ts} last={last:.2f} ma={sig.ma:.2f} vol={sig.vol:.2f} "
                f"bands=[{sig.lower:.2f},{sig.upper:.2f}] signal={sig.side}:{sig.reason} "
                f"pos={acct.pos.side} eq={acct.equity_usd:.2f} pnl_today={acct.realized_pnl_today:.2f}"
            )

            # 3) Entry if FLAT
            if acct.pos.side == "FLAT" and sig.side in ("LONG", "SHORT"):
                max_notional = cfg.risk.max_position_pct_equity * equity
                size_btc = max_notional / max(last, 1e-9)

                if sig.side == "LONG":
                    stop_price = last - sig.vol
                    r = open_position(
                        acct,
                        paper_cfg,
                        side="LONG",
                        size=size_btc,
                        ref_price=last,
                        stop_price=stop_price,
                        take_profit_price=None,
                        reason=sig.reason,
                    )
                    logger.warning(f"OPEN_LONG: {r} stop={stop_price:.2f}")

                elif sig.side == "SHORT":
                    stop_price = last + sig.vol
                    r = open_position(
                        acct,
                        paper_cfg,
                        side="SHORT",
                        size=size_btc,
                        ref_price=last,
                        stop_price=stop_price,
                        take_profit_price=None,
                        reason=sig.reason,
                    )
                    logger.warning(f"OPEN_SHORT: {r} stop={stop_price:.2f}")

            # 4) Exit on MA-cross (if enabled)
            if acct.pos.side != "FLAT" and cfg.strategy.exit_on_ma_cross:
                if acct.pos.side == "LONG" and last >= sig.ma:
                    r = close_position(acct, paper_cfg, ref_price=last, reason="ma_cross_exit")
                    logger.warning(f"CLOSE_LONG: {r}")
                elif acct.pos.side == "SHORT" and last <= sig.ma:
                    r = close_position(acct, paper_cfg, ref_price=last, reason="ma_cross_exit")
                    logger.warning(f"CLOSE_SHORT: {r}")

        except Exception as e:
            logger.exception(f"tick_error: {e}")

        time.sleep(cfg.runtime.loop_sleep_sec)


if __name__ == "__main__":
    main()