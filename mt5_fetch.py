"""Fetch M5 history from MT5 for the main short-TF symbols + account state (read-only)."""
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]
DAYS = 60


def to_df(rates: np.ndarray, digits: int) -> pd.DataFrame:
    df = pd.DataFrame(rates)
    df.index = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df[["open", "high", "low", "close", "tick_volume", "spread"]]
    df["digits"] = digits
    return df.dropna(subset=["open", "high", "low", "close"])


if __name__ == "__main__":
    if not mt5.initialize():
        print("INIT FAILED:", mt5.last_error())
        raise SystemExit

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=DAYS)

    for sym in SYMBOLS:
        if not mt5.symbol_select(sym, True):
            print(f"{sym}: select failed", mt5.last_error())
            continue
        info = mt5.symbol_info(sym)
        rates = mt5.copy_rates_range(sym, mt5.TIMEFRAME_M5, start, now)
        if rates is None or len(rates) == 0:
            print(f"{sym}: no data {mt5.last_error()}")
            continue
        df = to_df(rates, info.digits)
        out = f"mt5_{sym}.csv"
        df.to_csv(out)
        print(f"{sym}: digits={info.digits}  {len(df)} M5 bars  avg spread={df['spread'].mean():.1f} pts -> {out}")

    print("\n--- open positions ---")
    for p in mt5.positions_get() or []:
        print(f"  {p.symbol} {'BUY' if p.type == 0 else 'SELL'} {p.volume} lots @ {p.price_open} "
              f"-> {p.price_current}  P/L ${p.profit:.2f}")
    if not mt5.positions_get():
        print("  (none)")

    print("\n--- closed deals with commission/swap split (last 30) ---")
    deals = mt5.history_deals_get(start, now)
    if deals:
        tot_comm = sum(d.commission for d in deals)
        tot_swap = sum(d.swap for d in deals)
        n = len(deals)
        print(f"  {n} deals, total commission=${tot_comm:.2f}, total swap=${tot_swap:.2f}")
        for d in deals[-30:]:
            print(f"  {datetime.fromtimestamp(d.time, tz=timezone.utc)}  {d.symbol} "
                  f"{'BUY' if d.type == 0 else 'SELL'} {d.volume} lots  "
                  f"profit=${d.profit:.2f} comm=${d.commission:.2f} swap=${d.swap:.2f}")
    else:
        print("  (no closed deals in period)")

    mt5.shutdown()
