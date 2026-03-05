from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

import os
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

class PaperSimCfg(BaseModel):
    starting_equity_usd: float = 10_000.0
    taker_fee_bps: float = 4.0
    maker_fee_bps: float = 2.0
    slippage_bps: float = 1.0
    assume_taker: bool = True

class RuntimeCfg(BaseModel):
    loop_sleep_sec: int = 5
    lookback_candles: int = 120


class StrategyCfg(BaseModel):
    ma_len: int = 20
    vol_len: int = 20
    band_mult: float = 1.5
    exit_on_ma_cross: bool = True
    tp_bps: float = 2.0


class RiskCfg(BaseModel):
    max_leverage: float = 1.0
    max_position_pct_equity: float = 0.15
    max_loss_per_trade_pct: float = 0.0075
    daily_loss_limit_pct: float = 0.02
    one_position_at_a_time: bool = True
    allow_short: bool = True


class ExecutionCfg(BaseModel):
    order_type: Literal["limit", "market"] = "limit"
    limit_price_slippage_bps: float = 1.0
    reduce_only_for_exits: bool = True


class LoggingCfg(BaseModel):
    level: str = "INFO"
    to_file: bool = True
    file_path: str = "bot.log"


class AppCfg(BaseModel):
    env: Literal["testnet", "mainnet"] = "testnet"
    mode: Literal["paper", "live"] = "paper"
    paper: PaperSimCfg = Field(default_factory=PaperSimCfg)
    coin: str = "BTC"
    interval: str = "1m"
    runtime: RuntimeCfg = Field(default_factory=RuntimeCfg)
    strategy: StrategyCfg = Field(default_factory=StrategyCfg)
    risk: RiskCfg = Field(default_factory=RiskCfg)
    execution: ExecutionCfg = Field(default_factory=ExecutionCfg)
    logging: LoggingCfg = Field(default_factory=LoggingCfg)


@dataclass(frozen=True)
class Secrets:
    account_address: str
    api_wallet_private_key: str
    api_url_override: Optional[str]


def load_config(config_path: str = "config.yaml") -> tuple[AppCfg, Secrets]:
    load_dotenv()

    cfg_text = Path(config_path).read_text(encoding="utf-8")
    raw = yaml.safe_load(cfg_text) or {}
    cfg = AppCfg.model_validate(raw)

    account_address = os.getenv("HL_ACCOUNT_ADDRESS", "").strip()
    pk = os.getenv("HL_API_WALLET_PRIVATE_KEY", "").strip()
    api_url_override = os.getenv("HL_API_URL", "").strip() or None

    if not account_address:
        raise ValueError("Missing HL_ACCOUNT_ADDRESS in .env")
    if not pk:
        raise ValueError("Missing HL_API_WALLET_PRIVATE_KEY in .env")

    return cfg, Secrets(
        account_address=account_address,
        api_wallet_private_key=pk,
        api_url_override=api_url_override,
    )