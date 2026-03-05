from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests


DEFAULT_API_MAINNET = "https://api.hyperliquid.xyz"
DEFAULT_API_TESTNET = "https://api.hyperliquid-testnet.xyz"


def resolve_api_base(env: str, override: Optional[str] = None) -> str:
    if override:
        return override.rstrip("/")
    return (DEFAULT_API_TESTNET if env == "testnet" else DEFAULT_API_MAINNET).rstrip("/")


class HLInfoClient:
    def __init__(self, api_base: str, timeout_sec: int = 10) -> None:
        self.api_base = api_base
        self.timeout_sec = timeout_sec

    def candle_snapshot(self, coin: str, interval: str, start_ms: int, end_ms: int) -> List[Dict[str, Any]]:
        url = f"{self.api_base}/info"
        body = {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": interval,
                "startTime": int(start_ms),
                "endTime": int(end_ms),
            },
        }
        r = requests.post(url, json=body, timeout=self.timeout_sec)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise ValueError(f"Unexpected candleSnapshot response: {data}")
        return data

    def candle_snapshot_last_n(self, coin: str, interval: str, n: int) -> List[Dict[str, Any]]:
        # For 1m, n candles => n minutes lookback
        now_ms = int(time.time() * 1000)
        # crude mapping: for v1 we only support "1m" cleanly
        if interval != "1m":
            raise ValueError("v1 only supports interval=1m in candle_snapshot_last_n")
        start_ms = now_ms - n * 60_000
        return self.candle_snapshot(coin, interval, start_ms, now_ms)