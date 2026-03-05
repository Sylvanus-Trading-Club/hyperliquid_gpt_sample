from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

Side = Literal["LONG", "SHORT", "FLAT"]


@dataclass
class BotState:
    position_side: Side = "FLAT"
    position_size: float = 0.0
    entry_price: Optional[float] = None

    # v1: simple “daily loss” tracker; you can replace with real fills later
    realized_pnl_today: float = 0.0