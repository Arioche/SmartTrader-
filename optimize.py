"""
Major-symbol strategy optimization ("training") — READ-ONLY.

Connects to the same MT5 terminal, pulls real price history for the major
symbols, and sweeps each strategy family's parameters using the EXACT same
cost engine the live dashboard uses (screen.run_generic: real per-bar spread +
$0.03/side commission, $1 risk to stop, min-lot floor, margin-call at $0).

No orders are sent. Nothing is written back to MT5.

Honesty rules (to avoid curve-fitted "training" that dies live):
  - Indicators are causal (EMA / rolling / ATR use current + past bars only).
  - Train on the FIRST 70% of the bars, validate on the LAST 30% (out-of-sample).
  - Each config is reported as  IS (in-sample)  vs  OOS (out-of-sample),
    and only configs that survive BOTH (enough trades, not busted) are ranked.
    The "realization gap" (OOS vs IS) is shown so you can see what holds up.

Outputs: optimize_report.json (full grid), optimize_top.csv (ranked), and a
per-major-symbol "best settings" summary printed to stdout.
"""
import itertools
import json
import time
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

import backtest as bt          # reuse the pure indicator helpers (ema, atr)
import screen as sc           # reuse to_df + run_generic (the live cost engine)

# ---------------------------------------------------------------- config
MAJORS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD",
          "USDCHF", "XAUUSD"]
TIMEFRAMES = ("M15", "H1")
DAYS = {"M15": 120, "H1": 365}
TRAIN_FRAC = 0.70            # first 70% in-sample, last 30% out-of-sample

MIN_IS_TRADES = 15           # require a real sample in-sample
MIN_OOS_TRADES = 8           # require it to actually fire out-of-sample
INITIAL = sc.INITIAL_CAPITAL

# Parameter grids per family (kept small so IS+OOS stay meaningful, not noise).
def grids():
    g = {}
    # Trend Pullback: fast/slow EMA, reward:risk
    g["Trend Pullback"] = [
        dict(fast=f, slow=s, rr=r)
        for f in (5, 9, 12) for s in (50, 60) for r in (2.0, 3.0)]
    # Mean Reversion: lookback window, z-score entry, stop ATR multiple
    g["Mean Reversion"] = [
        dict(window=w, z_entry=z, stop_atr=s)
        for w in (12, 24, 48) for z in (2.0, 2.5) for s in (2.0, 3.0)]
    # Breakout: Donchian length, reward:risk
    g["Breakout"] = [
        dict(don=d, rr=r) for d in (20, 30, 55) for r in (2.0, 3.0)]
    return g


# ------------------------------------------------ parametric signal builders
# Mirror backtest.py's logic but with tunable parameters. Same output schema:
# columns enter (bool), exit (bool), stop (float), target (float).
def sig_trend_pullback(df, long=True, fast=9, mid=21, slow=50, stop_atr=1.5, rr=2.0):
    ef = bt.ema(df["close"], fast); em = bt.ema(df["close"], mid); es = bt.ema(df["close"], slow)
    a = bt.atr(df); risk = stop_atr * a
    if long:
        trend = (ef > em) & (em > es)
        enter = trend & (df["close"].shift(1) < ef.shift(1)) & (df["close"] > ef)
        stop = (df["close"] - risk).where(enter); target = (df["close"] + rr * risk).where(enter)
        exit_sig = df["close"] < em
    else:
        trend = (ef < em) & (em < es)
        enter = trend & (df["close"].shift(1) > ef.shift(1)) & (df["close"] < ef)
        stop = (df["close"] + risk).where(enter); target = (df["close"] - rr * risk).where(enter)
        exit_sig = df["close"] > em
    return pd.DataFrame({"enter": enter.fillna(False), "exit": exit_sig,
                         "stop": stop, "target": target})


def sig_mean_reversion(df, long=True, window=24, z_entry=2.0, stop_atr=3.0,
                       ranging=0.3, z_exit=0.5):
    mid = df["close"].rolling(window).mean()
    sd = df["close"].rolling(window).std()
    z = (df["close"] - mid) / sd
    e9, e21 = bt.ema(df["close"], 9), bt.ema(df["close"], 21)
    a = bt.atr(df); rng = (e9 - e21).abs() < ranging * a
    if long:
        enter = (z < -z_entry) & rng
        stop = (df["close"] - stop_atr * a).where(enter)
        target = mid.where(enter); exit_sig = z > -z_exit
    else:
        enter = (z > z_entry) & rng
        stop = (df["close"] + stop_atr * a).where(enter)
        target = mid.where(enter); exit_sig = z < z_exit
    return pd.DataFrame({"enter": enter.fillna(False), "exit": exit_sig,
                         "stop": stop, "target": target})


def sig_breakout(df, long=True, don=30, ema_t=50, stop_atr=1.5, rr=2.0):
    hi = df["high"].rolling(don).max().shift(1)
    lo = df["low"].rolling(don).min().shift(1)
    et = bt.ema(df["close"], ema_t)
    a = bt.atr(df); risk = stop_atr * a
    e21 = bt.ema(df["close"], 21)
    if long:
        enter = (df["close"] > hi) & (df["close"] > et)
        stop = (df["close"] - risk).where(enter); target = (df["close"] + rr * risk).where(enter)
        exit_sig = df["close"] < e21
    else:
        enter = (df["close"] < lo) & (df["close"] < et)
        stop = (df["close"] + risk).where(enter); target = (df["close"] - rr * risk).where(enter)
        exit_sig = df["close"] > e21
    return pd.DataFrame({"enter": enter.fillna(False), "exit": exit_sig,
                         "stop": stop, "target": target})


BUILDERS = {
    "Trend Pullback": sig_trend_pullback,
    "Mean Reversion": sig_mean_reversion,
    "Breakout": sig_breakout,
}


def pf(s):
    v = s["profit_factor"]
    return v if v != float("inf") else 1e9


def main():
    if not mt5.initialize():
        print("MT5 INIT FAILED:", mt5.last_error())
        raise SystemExit(1)
    acc = mt5.account_info()
    print(f"connected: {acc.login} @ {acc.server}  (balance ${acc.balance}) — read-only")
    sc.set_account()   # backtest against the live account's balance + leverage
    now = datetime.now(timezone.utc)
    g = grids()
    results = []
    used_syms = []

    for sym in MAJORS:
        if not mt5.symbol_select(sym, True):
            print(f"  {sym}: not available on this broker — skipped")
            continue
        info = mt5.symbol_info(sym)
        if info is None or info.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL \
                or info.trade_tick_value <= 0:
            print(f"  {sym}: not tradeable (mode/tick) — skipped")
            continue
        meta = {"tick_size": info.trade_tick_size, "tick_value": info.trade_tick_value,
                "min_vol": info.volume_min, "contract": info.trade_contract_size}
        used_syms.append(sym)

        for tf in TIMEFRAMES:
            rates = mt5.copy_rates_range(sym, sc.TF[tf],
                                         now - timedelta(days=DAYS[tf]), now)
            if rates is None or len(rates) < 400:
                print(f"  {sym} {tf}: only {0 if rates is None else len(rates)} bars — skipped")
                continue
            df = sc.to_df(rates, meta["tick_size"], meta["tick_value"], meta["min_vol"])
            df = df.dropna(subset=["open", "high", "low", "close", "spread"])
            n = len(df)
            split = int(n * TRAIN_FRAC)
            train_df = df.iloc[:split]
            oos_df = df.iloc[split:]

            for fam in BUILDERS:
                long = fam in ("Trend Pullback", "Mean Reversion", "Breakout")  # base is long
                for direction, is_long in (("BUY", True), ("SELL", False)):
                    for p in g[fam]:
                        try:
                            sig = BUILDERS[fam](df, long=is_long, **p)
                        except Exception:
                            continue
                        _, is_s = sc.run_generic(train_df, sig.loc[train_df.index],
                                                 meta, direction=direction)
                        _, oos_s = sc.run_generic(oos_df, sig.loc[oos_df.index],
                                                  meta, direction=direction)
                        results.append({
                            "symbol": sym, "timeframe": tf, "family": fam,
                            "direction": direction, "params": p,
                            "bars": n, "is_bars": len(train_df), "oos_bars": len(oos_df),
                            "is": is_s, "oos": oos_s,
                        })

    print(f"\nfetched data for: {', '.join(used_syms) or '(none)'}")
    print(f"total backtests evaluated (IS+OOS each): {len(results)}\n")

    # ---- ranking: must survive BOTH IS and OOS, then rank by OOS profit factor
    def eligible(r):
        return (r["is"]["trades"] >= MIN_IS_TRADES and not r["is"]["busted"]
                and r["oos"]["trades"] >= MIN_OOS_TRADES and not r["oos"]["busted"])

    el = [r for r in results if eligible(r)]
    el.sort(key=lambda r: (-pf(r["oos"]), -pf(r["is"])))

    cols = ["symbol", "timeframe", "family", "direction"]
    with open("optimize_top.csv", "w", encoding="utf-8") as f:
        f.write("# TOP configs by OUT-OF-SAMPLE profit factor "
                "(survived IS+OOS, >=%d IS trades, >=%d OOS trades, not busted)\n"
                % (MIN_IS_TRADES, MIN_OOS_TRADES))
        hdr = cols + ["params",
                      "is_final", "is_ret%", "is_trades", "is_win%", "is_PF", "is_maxDD%",
                      "oos_final", "oos_ret%", "oos_trades", "oos_win%", "oos_PF", "oos_maxDD%"]
        f.write(",".join(hdr) + "\n")
        for r in el[:60]:
            row = [r["symbol"], r["timeframe"], r["family"], r["direction"],
                   json.dumps(r["params"]),
                   r["is"]["final"], round(r["is"]["return_pct"]*100, 1), r["is"]["trades"],
                   round(r["is"]["win_rate"]*100), _pfstr(r["is"]), round(r["is"]["max_dd"]*100, 1),
                   r["oos"]["final"], round(r["oos"]["return_pct"]*100, 1), r["oos"]["trades"],
                   round(r["oos"]["win_rate"]*100), _pfstr(r["oos"]), round(r["oos"]["max_dd"]*100, 1)]
            f.write(",".join(str(x) for x in row) + "\n")

    with open("optimize_report.json", "w", encoding="utf-8") as f:
        json.dump({"generated": now.isoformat(), "train_frac": TRAIN_FRAC,
                   "symbols": used_syms, "results": results}, f, default=float, indent=1)

    # ---- per-major-symbol "best" summary
    print("=" * 96)
    print("BEST PER MAJOR SYMBOL — best (timeframe, strategy, direction) by OUT-OF-SAMPLE PF")
    print("  (IS = first 70% of history, OOS = last 30%; only settings that survived both shown)")
    print("=" * 96)
    for sym in used_syms:
        best = [r for r in el if r["symbol"] == sym]
        if not best:
            print(f"\n{sym}: no config survived both IS and OOS (needs >={MIN_IS_TRADES} "
                  f"IS / >={MIN_OOS_TRADES} OOS trades, not busted). Data may be short or "
                  f"the majors are too mean for these setups.")
            continue
        print(f"\n{sym}:")
        for r in best[:4]:
            i, o = r["is"], r["oos"]
            gap = (o["final"] - i["final"])
            print(f"  {r['timeframe']:<3} {r['family']:<16} {r['direction']:<4} "
                  f"{json.dumps(r['params']):<34} "
                  f"IS: PF {_pfnum(i)}  {i['trades']:>3}tr  {i['win_rate']*100:>3.0f}%  ${i['final']:>7.2f} | "
                  f"OOS: PF {_pfnum(o)}  {o['trades']:>3}tr  {o['win_rate']*100:>3.0f}%  ${o['final']:>7.2f}  "
                  f"(gap {gap:+.2f})")

    print("\nwrote: optimize_top.csv, optimize_report.json")
    mt5.shutdown()


def _pfnum(s):
    v = s["profit_factor"]
    return "inf" if v == float("inf") else f"{v:.2f}"


def _pfstr(s):
    v = s["profit_factor"]
    return "inf" if v == float("inf") else f"{v:.3f}"


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"done in {time.time()-t0:.0f}s")
