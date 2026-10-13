"""
Backtest engine for short-timeframe strategies on 5-minute crypto data.

Reality constraints modeled (these are what kill most scalping strategies):
  - Long-only (spot trading; no shorting, no leverage on a small account)
  - Taker fee + slippage on BOTH entry and exit
  - Signals computed on bar close, executed at the NEXT bar's open (no lookahead)
  - Stop losses checked intra-bar; if a bar hits both stop and target, stop wins
  - Fixed $1 risk per trade, position size derived from stop distance
"""
import numpy as np
import pandas as pd

# --- cost model (typical spot taker fees) ---
FEE = 0.0010        # 0.10% one-way taker fee
SLIP = 0.0005       # 0.05% one-way slippage
COST_RT = (FEE + SLIP) * 2  # ~0.30% per round trip

RISK_PER_TRADE = 1.0    # $1 max loss per trade
INITIAL_CAPITAL = 100.0  # generic starting capital (override to test other sizes)
MIN_NOTIONAL = 10.0     # typical exchange minimum order size


# ---------- indicators ----------
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()


# ---------- strategy signal builders ----------
# Each returns a DataFrame indexed like df with columns:
#   enter (bool), exit (bool), stop (float), target (float)
# Signals are evaluated at bar close; the engine acts at the next bar's open.

def trend_pullback(df: pd.DataFrame) -> pd.DataFrame:
    """Long on a pullback that recovers the fast EMA, only inside a defined uptrend."""
    e9, e21, e50 = ema(df["close"], 9), ema(df["close"], 21), ema(df["close"], 50)
    a = atr(df)
    uptrend = (e9 > e21) & (e21 > e50)
    pullback_prev = df["close"].shift(1) < e9.shift(1)
    recover = df["close"] > e9
    enter = uptrend & pullback_prev & recover
    stop = (df["close"] - 1.5 * a).where(enter)
    risk = 1.5 * a
    target = (df["close"] + 2 * risk).where(enter)  # 2:1 reward:risk
    exit_sig = df["close"] < e21
    return pd.DataFrame(
        {"enter": enter.fillna(False), "exit": exit_sig, "stop": stop, "target": target}
    )


def mean_reversion(df: pd.DataFrame) -> pd.DataFrame:
    """Long when price stretches 2 sigma below its mean in a NON-trending regime."""
    mid = df["close"].rolling(24).mean()
    sd = df["close"].rolling(24).std()
    z = (df["close"] - mid) / sd
    e9, e21 = ema(df["close"], 9), ema(df["close"], 21)
    a = atr(df)
    # regime: flat EMAs relative to volatility => ranging
    ranging = (e9 - e21).abs() < 0.3 * a
    enter = (z < -2.0) & ranging
    stop = (df["close"] - 3.0 * a).where(enter)
    target = mid.where(enter)  # exit at the mean
    exit_sig = z > -0.5
    return pd.DataFrame(
        {"enter": enter.fillna(False), "exit": exit_sig, "stop": stop, "target": target}
    )


def breakout(df: pd.DataFrame) -> pd.DataFrame:
    """Long on a 30-bar Donchian high breakout, only above the 50 EMA."""
    hi = df["high"].rolling(30).max().shift(1)
    lo = df["low"].rolling(30).min().shift(1)
    e50 = ema(df["close"], 50)
    a = atr(df)
    enter = (df["close"] > hi) & (df["close"] > e50)
    stop = (df["close"] - 1.5 * a).where(enter)
    risk = 1.5 * a
    target = (df["close"] + 2 * risk).where(enter)
    exit_sig = df["close"] < ema(df["close"], 21)
    return pd.DataFrame(
        {"enter": enter.fillna(False), "exit": exit_sig, "stop": stop, "target": target}
    )


# ---------- short-side mirrors (exact inverse of the long setups) ----------

def trend_pullback_short(df: pd.DataFrame) -> pd.DataFrame:
    """Short a bounce that loses the fast EMA, only inside a defined downtrend."""
    e9, e21, e50 = ema(df["close"], 9), ema(df["close"], 21), ema(df["close"], 50)
    a = atr(df)
    downtrend = (e9 < e21) & (e21 < e50)
    bounce_prev = df["close"].shift(1) > e9.shift(1)
    lose = df["close"] < e9
    enter = downtrend & bounce_prev & lose
    stop = (df["close"] + 1.5 * a).where(enter)
    risk = 1.5 * a
    target = (df["close"] - 2 * risk).where(enter)  # 2:1 reward:risk
    exit_sig = df["close"] > e21
    return pd.DataFrame(
        {"enter": enter.fillna(False), "exit": exit_sig, "stop": stop, "target": target}
    )


def mean_reversion_short(df: pd.DataFrame) -> pd.DataFrame:
    """Short when price stretches 2 sigma ABOVE its mean in a NON-trending regime."""
    mid = df["close"].rolling(24).mean()
    sd = df["close"].rolling(24).std()
    z = (df["close"] - mid) / sd
    e9, e21 = ema(df["close"], 9), ema(df["close"], 21)
    a = atr(df)
    ranging = (e9 - e21).abs() < 0.3 * a
    enter = (z > 2.0) & ranging
    stop = (df["close"] + 3.0 * a).where(enter)
    target = mid.where(enter)  # fade back to the mean
    exit_sig = z < 0.5
    return pd.DataFrame(
        {"enter": enter.fillna(False), "exit": exit_sig, "stop": stop, "target": target}
    )


def breakout_short(df: pd.DataFrame) -> pd.DataFrame:
    """Short on a 30-bar Donchian low breakdown, only below the 50 EMA."""
    hi = df["high"].rolling(30).max().shift(1)
    lo = df["low"].rolling(30).min().shift(1)
    e50 = ema(df["close"], 50)
    a = atr(df)
    enter = (df["close"] < lo) & (df["close"] < e50)
    stop = (df["close"] + 1.5 * a).where(enter)
    risk = 1.5 * a
    target = (df["close"] - 2 * risk).where(enter)
    exit_sig = df["close"] > ema(df["close"], 21)
    return pd.DataFrame(
        {"enter": enter.fillna(False), "exit": exit_sig, "stop": stop, "target": target}
    )


# ---------- engine ----------
def run_backtest(df: pd.DataFrame, sig: pd.DataFrame) -> dict:
    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    n = len(df)

    ent = sig["enter"].fillna(False).values
    exi = sig["exit"].fillna(False).values
    stp = sig["stop"].values
    tgt = sig["target"].values

    cash = INITIAL_CAPITAL
    units = 0.0
    pos_stop = pos_target = entry_px = 0.0
    entry_bar = 0
    pending = None  # ("enter"|None)
    equity = np.empty(n)
    trades = []

    for i in range(n):
        px_open, px_close = o[i], c[i]

        # 1) act on signal from previous bar, at this bar's open
        if units == 0 and pending == "enter" and i > 0:
            eff_stop = stp[i - 1]
            dist = px_open - eff_stop
            if dist > 0:
                size_by_risk = RISK_PER_TRADE / dist          # units risking $1 to stop
                size_by_cash = cash / (px_open * (1 + FEE + SLIP))
                units = min(size_by_risk, size_by_cash)
                notional = units * px_open
                if notional >= MIN_NOTIONAL:
                    px_open = px_open * (1 + SLIP)  # pay slippage going long
                    cash -= units * px_open * (1 + FEE)
                    pos_stop, pos_target, entry_px = eff_stop, tgt[i - 1], px_open
                    entry_bar = i
                else:
                    units = 0.0  # position too small to be tradeable
        elif i > 0 and units > 0 and exi[i - 1]:
            # signal exit at open
            px_exit = px_open * (1 - SLIP)
            cash += units * px_exit * (1 - FEE)
            trades.append(_trade(entry_px, px_exit, i, units, "signal", entry_bar))
            units = 0.0

        pending = "enter" if ent[i] else None

        # 2) manage open position intra-bar
        if units > 0:
            if l[i] <= pos_stop:  # conservative: stop assumed hit before target
                px_exit = min(pos_stop, px_open) * (1 - SLIP)
                cash += units * px_exit * (1 - FEE)
                trades.append(_trade(entry_px, px_exit, i, units, "stop", entry_bar))
                units = 0.0
            elif pos_target > 0 and h[i] >= pos_target:
                px_exit = pos_target * (1 - SLIP)
                cash += units * px_exit * (1 - FEE)
                trades.append(_trade(entry_px, px_exit, i, units, "target", entry_bar))
                units = 0.0

        equity[i] = cash + units * px_close

    eq = pd.Series(equity, index=df.index)
    if units > 0:  # close at end for stats
        px_exit = c[-1] * (1 - SLIP)
        cash += units * px_exit * (1 - FEE)
        trades.append(_trade(entry_px, px_exit, n - 1, units, "eod", entry_bar))

    res = {"equity": eq, "trades": trades, "stats": _stats(eq, trades)}
    return res


def _trade(entry: float, exit_: float, i: int, units: float, why: str,
           entry_bar: int = 0) -> dict:
    pnl = (exit_ - entry) * units
    cost = (entry * units) * FEE + (exit_ * units) * FEE
    return {"entry_bar": entry_bar, "exit_bar": i, "units": units,
            "entry": entry, "exit": exit_, "pnl": pnl - cost, "why": why,
            "hold_bars": i - entry_bar}


def _stats(eq: pd.Series, trades: list) -> dict:
    final = eq.iloc[-1]
    ret = final / INITIAL_CAPITAL - 1
    peak = eq.cummax()
    dd = (eq / peak - 1).min()
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    pf = gross_win / gross_loss if gross_loss > 0 else np.inf
    bars = len(eq)
    days = max(bars / 288, 1)  # 5m bars per day
    return {
        "final": final, "return_pct": ret * 100, "max_dd_pct": dd * 100,
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
        "profit_factor": pf,
        "avg_trade": np.mean([t["pnl"] for t in trades]) if trades else 0,
        "avg_hold_bars": np.mean([t["hold_bars"] for t in trades]) if trades else 0,
        "avg_daily_pct": ((final / INITIAL_CAPITAL) ** (1 / days) - 1) * 100
        if final > 0 else -100.0,
    }
