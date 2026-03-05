from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

Side = Literal["LONG", "SHORT"]


@dataclass(frozen=True)
class OrderIntent:
    action: Literal["OPEN", "CLOSE"]
    side: Optional[Side]          # None for CLOSE when flattening
    size: float
    limit_price: Optional[float]  # None => market


class Executor:
    """
    v1: prints intents instead of placing real orders.
    Next step: swap this with hyperliquid-python-sdk Exchange calls.
    """
    def __init__(self) -> None:
        pass

    def execute(self, intent: OrderIntent) -> None:
        print(f"[EXECUTE] {intent}")