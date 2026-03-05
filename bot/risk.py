from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .state import BotState


@dataclass(frozen=True)
class RiskDecision:
    ok: bool
    reason: str


def check_daily_loss(state: BotState, equity: float, daily_loss_limit_pct: float) -> RiskDecision:
    if equity <= 0:
        return RiskDecision(False, "bad_equity")
    if state.realized_pnl_today <= -abs(daily_loss_limit_pct) * equity:
        return RiskDecision(False, "daily_loss_limit_hit")
    return RiskDecision(True, "ok")


def check_one_position_rule(state: BotState, one_position_at_a_time: bool, desired_side: str) -> RiskDecision:
    if not one_position_at_a_time:
        return RiskDecision(True, "ok")
    if state.position_side != "FLAT" and desired_side != "FLAT":
        return RiskDecision(False, "already_in_position")
    return RiskDecision(True, "ok")