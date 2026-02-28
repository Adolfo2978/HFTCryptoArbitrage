"""Safe Bybit trading utility.

Improvements over the original script:
- Removes hard-coded credentials.
- Adds dry-run mode by default.
- Uses AI signal from ai_signal_system.py before trading.
- Includes basic latency measurement and balance retrieval.
"""

from __future__ import annotations

import argparse
import os
import time
from typing import Any, Dict



def current_milli_time() -> int:
    return round(time.time() * 1000)


def create_session(testnet: bool = False):
    from pybit.unified_trading import HTTP
    api_key = os.getenv("BYBIT_API_KEY", "")
    api_secret = os.getenv("BYBIT_API_SECRET", "")

    if not api_key or not api_secret:
        raise RuntimeError(
            "Missing API credentials. Export BYBIT_API_KEY and BYBIT_API_SECRET environment variables."
        )

    return HTTP(testnet=testnet, api_key=api_key, api_secret=api_secret)


def get_available_usdt(session) -> str:
    balance = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
    coins = balance.get("result", {}).get("list", [{}])[0].get("coin", [])
    for item in coins:
        if item.get("coin") == "USDT":
            return item.get("availableToWithdraw", "0")
    return "0"


def place_market_order(session, symbol: str, side: str, qty: str) -> Dict[str, Any]:
    return session.place_order(
        category="linear",
        symbol=symbol,
        side=side,
        orderType="Market",
        qty=qty,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="AI-assisted Bybit order execution")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--qty", default="0.001")
    parser.add_argument("--testnet", action="store_true")
    parser.add_argument("--execute", action="store_true", help="Actually place orders. Default is dry-run.")
    parser.add_argument("--interval", default="1")
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()

    from ai_signal_system import run as run_ai_signal

    ai_summary = run_ai_signal(symbol=args.symbol, interval=args.interval, limit=args.limit, exchange="bybit")
    print("AI summary:", ai_summary)

    if not args.execute:
        print("Dry-run mode enabled. Use --execute to place orders.")
        return

    if ai_summary["signal"] == "HOLD":
        print("Signal is HOLD. No order sent.")
        return

    side = "Buy" if ai_summary["signal"] == "BUY" else "Sell"

    session = create_session(testnet=args.testnet)
    before = get_available_usdt(session)

    t0 = current_milli_time()
    order_res = place_market_order(session, args.symbol, side, args.qty)
    t1 = current_milli_time()

    after = get_available_usdt(session)

    print("Order response:", order_res)
    print(f"Execution time: {t1 - t0}ms")
    print(f"USDT before: {before}")
    print(f"USDT after : {after}")


if __name__ == "__main__":
    main()
