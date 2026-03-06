from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd


Side = Literal["LONG", "SHORT", "FLAT"]


@dataclass(frozen=True)
class Signal:
    side: Side
    reason: str
    ma: float
    vol: float
    upper: float
    lower: float
    last: float


def compute_signal(
    closes: pd.Series,
    ma_len: int,
    vol_len: int,
    band_mult: float,
    allow_short: bool,
    min_vol_abs: float = 20.0,   # <-- NEW: minimum volatility (in USD) to allow trading
) -> Signal:
    """
    Mean reversion band signal:
      - MA = rolling mean
      - vol = rolling std dev
      - upper/lower = MA ± band_mult * vol

    Safety improvement:
      - If vol < min_vol_abs, return FLAT to avoid fee-churn in low-vol regimes.
    """
    if len(closes) < max(ma_len, vol_len) + 2:
        last = float(closes.iloc[-1])
        return Signal("FLAT", "not_enough_data", np.nan, np.nan, np.nan, np.nan, last)

    ma = float(closes.rolling(ma_len).mean().iloc[-1])
    vol = float(closes.rolling(vol_len).std(ddof=0).iloc[-1])
    last = float(closes.iloc[-1])

    # Guard against degenerate vol
    if not np.isfinite(vol) or vol <= 0:
        return Signal("FLAT", "bad_vol", ma, vol, np.nan, np.nan, last)

    upper = ma + band_mult * vol
    lower = ma - band_mult * vol

    # NEW: prevent fee-churn when candle feed is "flat" / very low movement
    if vol < float(min_vol_abs):
        return Signal("FLAT", "vol_too_low", ma, vol, upper, lower, last)

    if last < lower:
        return Signal("LONG", "below_lower_band", ma, vol, upper, lower, last)
    if allow_short and last > upper:
        return Signal("SHORT", "above_upper_band", ma, vol, upper, lower, last)

    return Signal("FLAT", "inside_bands", ma, vol, upper, lower, last)
