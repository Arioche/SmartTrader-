"""Fetch 5-minute OHLCV data from Yahoo Finance (up to 60 days, the max for 5m bars)."""
import json
import urllib.request

import pandas as pd

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def fetch(symbol: str, interval: str = "5m", range_: str = "60d") -> pd.DataFrame:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval={interval}&range={range_}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)

    result = data["chart"]["result"][0]
    ts = result["timestamp"]
    q = result["indicators"]["quote"][0]
    df = pd.DataFrame(
        {
            "open": q["open"],
            "high": q["high"],
            "low": q["low"],
            "close": q["close"],
            "volume": q["volume"],
        },
        index=pd.to_datetime(ts, unit="s", utc=True),
    )
    df = df.dropna(subset=["open", "high", "low", "close"])
    return df


if __name__ == "__main__":
    for sym in ("BTC-USD", "ETH-USD"):
        df = fetch(sym)
        out = f"data_{sym.replace('-', '_')}.csv"
        df.to_csv(out)
        print(f"{sym}: {len(df)} bars  {df.index[0]} -> {df.index[-1]}  saved to {out}")
