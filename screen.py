"""
Generic MT5 screener: 3 strategies x {M15, H1} over ALL tradeable symbols.

Cost model = the account's real behavior:
  - entry/exit at bid/ask using the REAL per-bar spread from the broker feed
  - commission $0.03 per side per 0.01 lot (measured from account deal history)
  - position floored at the symbol's min volume, sized by ~$1 risk to stop
  - broker margin-calls at $0 equity (account bust, not negative balance)
PnL in $ via the broker's own tick value:  ticks x tick_value x lots.
"""
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

import backtest as bt

# The MT5 Python API is not thread-safe; the dashboard runs a second thread
# (live account snapshots), so every mt5 call goes through this lock.
# RLock (not Lock): the EXEC send path holds this lock while calling
# clamp_stops_to_broker(), which takes it again — a plain Lock would
# self-deadlock the request forever.
mt5_lock = threading.RLock()

RISK_PER_TRADE = 1.0
# Starting capital + leverage are read from the connected MT5 account at
# runtime by set_account(); the defaults below apply only if it's unreachable.
INITIAL_CAPITAL = 100.0
LEVERAGE = 100
COMM_PER_001 = 0.06          # $0.03/side/0.01 lot, round trip
MIN_LOTS = 0.01
DAYS = {"M15": 120, "H1": 365}
TF = {"M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1}
STRATS = {"Trend Pullback": bt.trend_pullback,
          "Mean Reversion": bt.mean_reversion,
          "Breakout": bt.breakout,
          "Trend Pullback (short)": bt.trend_pullback_short,
          "Mean Reversion (short)": bt.mean_reversion_short,
          "Breakout (short)": bt.breakout_short}


def set_account():
    """Point the cost engine at the live account's balance and leverage."""
    global INITIAL_CAPITAL, LEVERAGE
    try:
        with mt5_lock:
            acc = mt5.account_info()
    except Exception:
        return
    if acc is not None:
        INITIAL_CAPITAL = float(acc.balance)
        LEVERAGE = int(acc.leverage or LEVERAGE)


def direction_of(strat_name):
    return "SELL" if strat_name.endswith("(short)") else "BUY"


def to_df(rates, tick_size, tick_value, min_vol):
    df = pd.DataFrame(rates)
    df.index = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df[["open", "high", "low", "close", "spread"]]
    df.attrs["meta"] = {"tick_size": tick_size, "tick_value": tick_value,
                        "min_vol": min_vol}
    return df.dropna(subset=["open", "high", "low", "close"])


def run_generic(df, sig, meta, direction="BUY"):
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    sp = df["spread"].values
    tick_size, tick_value, min_vol = meta["tick_size"], meta["tick_value"], meta["min_vol"]
    n = len(df)
    long = direction == "BUY"

    def pnl_ticks(px_from, px_to):
        d = (px_to - px_from) if long else (px_from - px_to)
        return d / tick_size * tick_value

    ent = sig["enter"].fillna(False).values
    exi = sig["exit"].fillna(False).values
    stp, tgt = sig["stop"].values, sig["target"].values

    cash, lots = INITIAL_CAPITAL, 0.0
    pos_stop = pos_target = entry_px = 0.0
    entry_bar = 0
    pending = None
    busted = False
    equity = np.empty(n)
    trades = []

    def exit_trade(px_bid, i, why):
        nonlocal cash, lots
        # longs fill at bid, shorts at ask
        exit_px = px_bid if long else px_bid + sp[i] * tick_size
        pnl = pnl_ticks(entry_px, exit_px) * lots - COMM_PER_001 * (lots / MIN_LOTS)
        cash += pnl
        trades.append({"entry_bar": entry_bar, "exit_bar": i, "lots": round(lots, 4),
                       "pnl": pnl, "why": why, "hold_bars": i - entry_bar})
        lots = 0.0

    contract = meta.get("contract") or 1000.0

    for i in range(n):
        bid_open = o[i]
        if lots == 0 and pending == "enter" and i > 0 and not busted:
            # cross the spread: buy at ask, sell at bid
            entry_fill = bid_open + sp[i - 1] * tick_size if long else bid_open
            eff_stop = stp[i - 1]
            stop_ticks = ((entry_fill - eff_stop) if long else
                          (eff_stop - entry_fill)) / tick_size
            if stop_ticks > 0:
                # lots_want in standard lots: $1 risk / ($ risk per std lot to stop)
                lots_want = RISK_PER_TRADE / (stop_ticks * tick_value)
                # margin cap in standard lots: available margin / notional per std lot
                lots_cap = cash * LEVERAGE / (contract * bid_open)
                if min_vol > lots_cap:
                    pending = None  # even the min order exceeds available margin; skip
                else:
                    lots = max(min_vol, min(lots_want, lots_cap))
                    lots = round(lots / min_vol) * min_vol  # round to volume step
                    entry_px = entry_fill
                    pos_stop, pos_target = eff_stop, tgt[i - 1]
                    entry_bar = i
        elif i > 0 and lots > 0 and exi[i - 1] and not busted:
            exit_trade(bid_open, i, "signal")

        pending = "enter" if ent[i] else None

        if lots > 0 and not busted:
            if long:
                if l[i] <= pos_stop:
                    exit_trade(min(pos_stop, bid_open), i, "stop")
                elif pos_target > 0 and h[i] >= pos_target:
                    exit_trade(pos_target, i, "target")
            else:
                if h[i] >= pos_stop:
                    exit_trade(max(pos_stop, bid_open), i, "stop")
                elif pos_target > 0 and l[i] <= pos_target:
                    exit_trade(pos_target, i, "target")

        if lots > 0:
            mtm = cash + pnl_ticks(entry_px, c[i]) * lots
            if mtm <= 0:
                exit_trade(c[i], i, "margin_call")
                busted = True
                cash = 0.0

        equity[i] = cash if not busted else 0.0

    if lots > 0 and not busted:
        exit_trade(c[-1], n - 1, "eod")
        equity[n - 1] = cash  # the equity array was filled pre-exit
    eq = pd.Series(equity, index=df.index)

    final = eq.iloc[-1]
    peak = eq.cummax()
    wins = [t for t in trades if t["pnl"] > 0]
    gl = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    gw = sum(t["pnl"] for t in wins)
    stats = {
        "final": final, "return_pct": final / INITIAL_CAPITAL - 1,
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) if trades else 0,
        "profit_factor": gw / gl if gl > 0 else (float("inf") if gw > 0 else 0),
        "max_dd": (eq / peak - 1).min(),
        "avg_hold": float(np.mean([t["hold_bars"] for t in trades])) if trades else 0,
        "busted": busted,
    }
    return eq, stats


def last_bar_signal(df, strat_name):
    """The strategy's signal row on the last CLOSED bar of df.

    Entry is 'ready' when enter is True and a stop is defined.
    """
    last = STRATS[strat_name](df).iloc[-1]
    ready = bool(last["enter"]) and not pd.isna(last["stop"])
    return {"ready": ready, "row": last}


def latest_signal(symbol, tf_name, strat_name):
    """Entry signal on the most recent CLOSED bar, sized like the backtest.

    Returns a preview dict (what a live order WOULD be) or None if the
    strategy is not currently signaling. Mirrors run_generic exactly:
    entry at the next bar's open ~ now, $1 risk to stop, floored at
    the broker's min volume, capped by available margin. Direction comes
    from the strategy name (BUY or SELL).
    """
    direction = direction_of(strat_name)
    long = direction == "BUY"
    with mt5_lock:
        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"{symbol}: no symbol info")
        tick = mt5.symbol_info_tick(symbol)
        rates = mt5.copy_rates_from_pos(symbol, TF[tf_name], 0, 400)
    if rates is None or len(rates) < 300:
        return None
    df = to_df(rates[:-1], info.trade_tick_size, info.trade_tick_value,
               info.volume_min)  # drop the still-forming bar
    sig = last_bar_signal(df, strat_name)
    if not sig["ready"]:
        return None
    last = sig["row"]

    ask, bid = tick.ask, tick.bid
    entry_fill = ask if long else bid
    stop = float(last["stop"])
    target = float(last["target"]) if not pd.isna(last["target"]) else 0.0
    tick_size = info.trade_tick_size
    stop_ticks = ((entry_fill - stop) if long else (stop - entry_fill)) / tick_size
    if stop_ticks <= 0:
        return None

    acc = mt5.account_info()
    cash = acc.balance
    lev = acc.leverage or LEVERAGE
    lots_want = RISK_PER_TRADE / (stop_ticks * info.trade_tick_value)
    notional_per_lot = info.trade_contract_size * entry_fill
    lots_cap = cash * lev / notional_per_lot
    if info.volume_min > lots_cap:
        return {"blocked": "even the minimum order exceeds available margin"}
    lots = max(info.volume_min, min(lots_want, lots_cap))
    lots = round(lots / info.volume_min) * info.volume_min
    lots = round(lots, 2)
    risk_usd = stop_ticks * info.trade_tick_value * lots

    return {
        "symbol": symbol, "timeframe": tf_name, "strategy": strat_name,
        "direction": direction,
        "signal_bar": str(df.index[-1]),
        "ask": ask, "bid": bid,
        "stop": round(stop, info.digits),
        "target": round(target, info.digits),
        "stop_ticks": round(stop_ticks, 1),
        "lots": lots,
        "risk_usd": round(risk_usd, 2),
        "balance": cash,
    }


def screen(progress_cb=None):
    """Run the full screen. Returns rows list + curves dict (equity series per row)."""
    set_account()
    now = datetime.now(timezone.utc)
    with mt5_lock:
        syms = mt5.symbols_get()
        metas = {}
        for s in syms:
            info = mt5.symbol_info(s.name)
            if info is None or not info.visible:
                continue
            if info.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
                continue
            if info.trade_tick_value <= 0:
                continue
            metas[s.name] = {"tick_size": info.trade_tick_size,
                             "tick_value": info.trade_tick_value,
                             "min_vol": info.volume_min,
                             "contract": info.trade_contract_size}
    rows, curves = [], {}
    jobs = [(name, tf) for name in metas for tf in ("M15", "H1")]
    total = len(jobs) * len(STRATS)
    done = 0
    for name, tf in jobs:
        meta = metas[name]
        with mt5_lock:
            rates = mt5.copy_rates_range(name, TF[tf], now - timedelta(days=DAYS[tf]), now)
        if rates is None or len(rates) < 300:
            continue
        df = to_df(rates, meta["tick_size"], meta["tick_value"], meta["min_vol"])
        for sname, fn in STRATS.items():
            done += 1
            try:
                eq, stats = run_generic(df, fn(df), meta,
                                        direction=direction_of(sname))
            except Exception as e:
                print(f"{name} {tf} {sname}: {type(e).__name__} {e}")
                continue
            row = {"symbol": name, "timeframe": tf, "strategy": sname,
                   "direction": direction_of(sname),
                   "bars": len(df),
                   **{k: (None if k == "profit_factor" and v == float("inf") else
                          round(v, 4) if isinstance(v, float) else v)
                      for k, v in stats.items()}}
            rows.append(row)
            curves[f"{name}|{tf}|{sname}"] = {
                "t": [int(x.timestamp()) for x in eq.index[::4]],
                "v": [round(y, 2) for y in eq.values[::4]],
            }
            if progress_cb:
                progress_cb(done, total, name, tf, sname)
    return rows, curves


if __name__ == "__main__":
    if not mt5.initialize():
        print("INIT FAILED:", mt5.last_error())
        raise SystemExit
    t0 = time.time()
    rows, curves = screen(progress_cb=lambda d, t, n, tf, s:
                          print(f"\r{d}/{t}  last: {n} {tf} {s}", end="") if d % 10 == 0 else None)
    print(f"\ndone in {time.time()-t0:.0f}s, {len(rows)} results")
    with open("screen_curves.json", "w") as f:
        json.dump(curves, f)
    df = pd.DataFrame(rows)
    df.to_csv("screen_results.csv", index=False)
    # quick top view
    surv = df[~df["busted"]]
    print("\nSurvivors ranked by final $:")
    print(surv.sort_values("final", ascending=False)[
        ["symbol", "timeframe", "strategy", "final", "return_pct", "trades",
         "win_rate", "profit_factor", "max_dd"]].head(15).to_string(index=False))
    print(f"\nBusted: {df['busted'].sum()} / {len(df)}")
    mt5.shutdown()
