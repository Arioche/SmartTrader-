"""
Live MT5 trading dashboard.

- Background thread: runs the full M15/H1 screen over all tradeable symbols,
  refreshing the live account snapshot between jobs.
- Frontend (auto-refreshing, no build step):
    * account card: balance / equity / margin free
    * open positions with floating P&L
    * screen progress (live)
    * ranked results table (survivors vs margin-called)
    * click a row -> its equity curve

Run:  python dashboard.py   ->  http://127.0.0.1:8765
"""
import json
import os
import sys
import threading
import time
import traceback
import uuid

__version__ = "1.0"


def _log_thread_crash(args):
    """Background-thread exceptions would otherwise die silently (exit 1,
    no log). Print them loudly so a crash is diagnosable next time."""
    sys.__excepthook__(args)
    sys.stderr.write("\n*** UNCAUGHT EXCEPTION IN THREAD "
                     f"{args.thread.name if args.thread else '?'} ***\n")
    sys.stderr.flush()


sys.excepthook = _log_thread_crash
from datetime import datetime, timezone

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

import MetaTrader5 as mt5
import screen as sc

app = Flask(__name__)


@app.errorhandler(Exception)
def _json_error(e):
    """Any unhandled error becomes clean JSON so the UI never hangs on a 500."""
    if isinstance(e, HTTPException):
        return jsonify({"ok": False, "error": e.description}), e.code
    traceback.print_exc()
    return jsonify({"ok": False, "error": f"internal error: {type(e).__name__}"}), 500


# ---------------- shared state (written by worker thread only) ----------------
STATE = {
    "account": None,
    "positions": [],
    "screen": {"state": "starting", "done": 0, "total": 0, "last": "", "elapsed": 0,
               "started": time.time()},
    "rows": [],
    "curves": {},
    "backtest_start": None,   # account balance the last screen backtests started from
    # live[key] = {"ready": bool, "ts": epoch}  (key = "SYM|TF|Strat")
    "live": {},
    "live_meta": {"state": "waiting for screen", "last_scan": 0, "interval": 45},
}
ROWS_LOCK = threading.Lock()

# ---------------- live execution state ----------------
EXEC = {"pending": None, "log": []}   # pending: preview + one-time token
EXEC_LOCK = threading.Lock()
PREVIEW_TTL = 120                     # seconds a preview stays confirmable
RISK_CAP_PCT = 0.05                   # never risk more than 5% of balance
CONFIRM_TEXT = "EXECUTE"
EXEC_MAGIC = 771101
_HERE = os.path.dirname(os.path.abspath(__file__))
EXEC_STATE_FILE = os.path.join(_HERE, "exec_state.json")     # pending preview
EXEC_HIST_FILE = os.path.join(_HERE, "exec_history.jsonl")   # durable audit log


def exec_log(entry):
    entry["ts"] = datetime.now(timezone.utc).strftime("%H:%M:%S")
    entry["ts_full"] = datetime.now(timezone.utc).isoformat()
    EXEC["log"].append(entry)
    EXEC["log"] = EXEC["log"][-50:]
    try:
        with open(EXEC_HIST_FILE, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
            f.flush()
    except Exception:
        pass


def save_exec_state():
    """Persist the pending preview (token + ack) to disk. If the process is
    killed mid-flow (heartbeat self-termination on a frozen MT5 link), the
    user's open modal keeps working after the watchdog relaunches it — the
    one-time token survives the restart. Atomic write, no temp-file cruft."""
    try:
        with EXEC_LOCK:
            payload = {"pending": EXEC["pending"], "saved": time.time()}
        tmp = EXEC_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, default=str)
            f.flush()
            os.replace(tmp, EXEC_STATE_FILE)
    except Exception:
        pass


def load_exec_state():
    """Restore a pending preview + recent audit log from a previous run."""
    try:
        with open(EXEC_STATE_FILE) as f:
            payload = json.load(f)
        pending = payload.get("pending")
        if pending and time.time() - float(pending.get("created", 0)) <= PREVIEW_TTL:
            with EXEC_LOCK:
                EXEC["pending"] = pending
        else:
            with EXEC_LOCK:
                EXEC["pending"] = None
            try:
                os.remove(EXEC_STATE_FILE)
            except FileNotFoundError:
                pass
    except (FileNotFoundError, ValueError, OSError):
        pass
    try:
        with open(EXEC_HIST_FILE) as f:
            EXEC["log"] = [json.loads(l) for l in f.readlines()[-50:] if l.strip()]
    except (FileNotFoundError, ValueError, OSError):
        pass


def snap_account():
    with sc.mt5_lock:
        acc = mt5.account_info()
        raw = list(mt5.positions_get() or [])
    if acc:
        STATE["account"] = {
            "login": acc.login, "server": acc.server,
            "balance": round(acc.balance, 2), "equity": round(acc.equity, 2),
            "margin": round(acc.margin, 2), "margin_free": round(acc.margin_free, 2),
            "leverage": acc.leverage, "currency": acc.currency,
        }
    pos = []
    for p in raw:
        pos.append({
            "symbol": p.symbol, "side": "BUY" if p.type == 0 else "SELL",
            "volume": p.volume, "open": p.price_open, "current": p.price_current,
            "sl": p.sl, "tp": p.tp,
            "pl": round(p.profit, 2), "time": datetime.fromtimestamp(
                p.time, tz=timezone.utc).strftime("%m-%d %H:%M"),
        })
    STATE["positions"] = pos


def _mt5_initialize():
    """Initialize with a wall-clock timeout; MT5 allows only ONE external
    Python connection per account, so a zombie process holding the slot
    makes initialize() hang forever. Retry until it gets through."""
    while True:
        box = {}

        def go():
            box["ok"] = bool(mt5.initialize())
            box["err"] = str(mt5.last_error())

        t = threading.Thread(target=go, daemon=True)
        t.start()
        t.join(45)
        if t.is_alive():
            STATE["screen"]["state"] = "waiting for MT5 connection " \
                                       "(zombie connection?) — retrying…"
            print("[worker] initialize hung; retrying in 10s", flush=True)
            time.sleep(10)
            continue
        if box.get("ok"):
            return True
        STATE["screen"]["state"] = "MT5 init failed: " + box.get("err", "?")
        print(f"[worker] initialize failed: {box.get('err')}; retrying in 10s",
              flush=True)
        time.sleep(10)


def worker():
    t0 = time.time()
    try:
        with sc.mt5_lock:
            _mt5_initialize()
        STATE["screen"]["state"] = "running"
        snap_account()
        rows, curves = sc.screen(progress_cb=on_job)
        with ROWS_LOCK:
            STATE["rows"] = rows
            STATE["curves"] = curves
            STATE["backtest_start"] = round(sc.INITIAL_CAPITAL, 2)
        STATE["screen"]["state"] = "done"
    except Exception:
        STATE["screen"]["state"] = "error: " + traceback.format_exc(limit=3)
    finally:
        STATE["screen"]["elapsed"] = round(time.time() - t0, 1)
        # keep the MT5 connection open: account_loop keeps the account card live


def on_job(done, total, sym, tf, strat):
    with ROWS_LOCK:
        STATE["screen"]["done"] = done
        STATE["screen"]["total"] = total
        STATE["screen"]["last"] = f"{sym} {tf} {strat}"
        STATE["screen"]["elapsed"] = round(time.time() - STATE["screen"]["started"], 1)
    # keep the account card live while working (cheap calls between jobs)
    try:
        snap_account()
    except Exception:
        pass


def account_loop():
    """Keep account/positions fresh even after the screen finishes."""
    while True:
        time.sleep(5)
        try:
            with sc.mt5_lock:
                term = mt5.terminal_info()
                if term is not None:
                    MT5_STATE["last_ok"] = time.time()   # the MT5 link answered
            if term is None:
                continue
            snap_account()
        except Exception:
            pass


HERE = os.path.dirname(os.path.abspath(__file__))
MT5_STATE = {"last_ok": time.time(), "start": time.time()}


def mt5_heartbeat():
    """The watchdog only restarts a DEAD process. A stuck one (an MT5 DLL call
    blocking forever, holding mt5_lock, freezing every preview/exec/account
    update while the page shows stale cached data) stays alive and must be
    recovered another way: this thread self-terminates the process after no
    successful MT5 call for 60s, so the watchdog relaunches it with a fresh
    connection."""
    while True:
        time.sleep(20)
        now = time.time()
        if now - MT5_STATE["start"] < 60:      # startup grace
            continue
        if now - MT5_STATE["last_ok"] > 60:
            try:
                with open(os.path.join(HERE, "dashboard_crash.log"), "a") as f:
                    f.write(f"[{datetime.now(timezone.utc).isoformat()}] "
                            f"MT5 heartbeat: no successful MT5 call for "
                            f"{now - MT5_STATE['last_ok']:.0f}s — link frozen, "
                            f"self-terminating for watchdog restart\n")
            except Exception:
                pass
            os._exit(3)


LIVE_INTERVAL = 45
_bar_cache = {}   # key -> (last_closed_bar_start, ready)


def live_signal_loop():
    """Watch every survivor row; set STATE['live'][key]=ready when a strategy
    signals an entry on the newest CLOSED bar. Recomputes only when a new bar
    closes for that row (tracked via its last-closed-bar start)."""
    while True:
        time.sleep(LIVE_INTERVAL)
        with ROWS_LOCK:
            rows = [r for r in STATE["rows"] if not r["busted"]]
            if not rows:
                continue
        now = time.time()
        for r in rows:
            key = f"{r['symbol']}|{r['timeframe']}|{r['strategy']}"
            try:
                with sc.mt5_lock:
                    info = mt5.symbol_info(r["symbol"])
                    if info is None:
                        continue
                    rates = mt5.copy_rates_from_pos(
                        r["symbol"], sc.TF[r["timeframe"]], 0, 320)
                if rates is None or len(rates) < 300:
                    continue
                closed_start = int(rates[-2]["time"])   # last CLOSED bar
                cached = _bar_cache.get(key)
                if cached and cached[0] == closed_start:
                    STATE["live"][key] = {"ready": cached[1], "ts": now}
                    continue
                df = sc.to_df(rates[:-1], info.trade_tick_size,
                              info.trade_tick_value, info.volume_min)
                res = sc.last_bar_signal(df, r["strategy"])
                _bar_cache[key] = (closed_start, res["ready"])
                STATE["live"][key] = {"ready": res["ready"], "ts": now}
            except Exception:
                continue
        STATE["live_meta"] = {"state": "scanning", "last_scan": now,
                              "interval": LIVE_INTERVAL,
                              "watching": len(rows)}


# ---------------- API ----------------
@app.get("/api/status")
def status():
    return jsonify({"account": STATE["account"],
                    "positions": STATE["positions"],
                    "screen": STATE["screen"],
                    "start": STATE["backtest_start"],
                    "version": __version__})


@app.get("/api/results")
def results():
    with ROWS_LOCK:
        return jsonify(STATE["rows"])


@app.get("/api/live-signals")
def live_signals():
    with ROWS_LOCK:
        live = {k: dict(v) for k, v in STATE["live"].items()}
        row_by_key = {f"{r['symbol']}|{r['timeframe']}|{r['strategy']}": r
                      for r in STATE["rows"]}
    for k, v in live.items():
        r = row_by_key.get(k)
        if v.get("ready") and r is not None:
            v["bt"] = {"trades": r["trades"], "win": r["win_rate"],
                       "pf": r["profit_factor"], "final": r["final"]}
    return jsonify({"meta": STATE["live_meta"], "live": live})


@app.get("/api/curve")
def curve():
    key = request.args.get("key", "")
    with ROWS_LOCK:
        data = STATE["curves"].get(key)
    return jsonify(data) if data else (jsonify(None), 404)


# ---------------- live execution (REAL MONEY) ----------------
@app.get("/api/exec")
def exec_state():
    return jsonify({"pending": EXEC["pending"], "log": EXEC["log"][-20:],
                    "confirm_text": CONFIRM_TEXT})


@app.post("/api/execute/preview")
def execute_preview():
    """Compute what the strategy's signal WOULD order. No order is sent."""
    d = request.get_json(force=True) or {}
    symbol, tf, strat = d.get("symbol"), d.get("timeframe"), d.get("strategy")
    if not symbol or tf not in ("M15", "H1") or strat not in sc.STRATS:
        return jsonify({"ok": False, "error": "bad request"}), 400
    try:
        prev = sc.latest_signal(symbol, tf, strat)
    except Exception as e:
        return jsonify({"ok": False, "error": f"preview failed: {e}"}), 400
    if prev is None:
        return jsonify({"ok": False, "no_signal": True,
                        "error": f"{strat} on {symbol} {tf} is NOT currently "
                                 "signaling an entry on the latest closed bar. "
                                 "Nothing to execute."})
    if prev.get("blocked"):
        return jsonify({"ok": False, "error": prev["blocked"]}), 400
    # show the stops as the broker will actually accept them
    prev["stop_orig"] = prev["stop"]
    prev["target_orig"] = prev["target"]
    prev["stop"], prev["target"], prev["stop_note"] = clamp_stops_to_broker(
        prev["symbol"], prev["direction"], prev["stop"], prev["target"])
    # if the stop moved, the real risk to it changes — rescale the $ at risk
    if prev["stop"] and prev["stop"] != prev["stop_orig"]:
        entry_fill = prev["ask"] if prev["direction"] == "BUY" else prev["bid"]
        new_dist = abs(prev["stop"] - entry_fill)
        old_dist = abs(prev["stop_orig"] - entry_fill)
        if old_dist > 0 and new_dist > old_dist:
            prev["risk_usd"] = round(prev["risk_usd"] * new_dist / old_dist, 2)
    # safety flags (we do NOT silently shrink the strategy's own spec)
    prev["risk_pct_balance"] = round(100 * prev["risk_usd"] / prev["balance"], 1)
    prev["big_risk"] = prev["risk_usd"] > RISK_CAP_PCT * prev["balance"]
    prev["token"] = uuid.uuid4().hex
    prev["created"] = time.time()
    with EXEC_LOCK:
        EXEC["pending"] = prev
    save_exec_state()
    exec_log({"event": "preview", "symbol": symbol, "tf": tf, "strategy": strat,
              "direction": prev.get("direction", "BUY"),
              "lots": prev["lots"], "risk": prev["risk_usd"]})
    return jsonify({"ok": True, "preview": prev})


@app.post("/api/execute/ack")
def execute_ack():
    """User acknowledged the real-money warning for the pending preview."""
    d = request.get_json(force=True) or {}
    with EXEC_LOCK:
        prev = EXEC["pending"]
        ok = bool(prev and prev.get("token") == d.get("token"))
        if ok:
            prev["real_money_warning_ack"] = True
    if ok:
        save_exec_state()   # outside the lock — it takes EXEC_LOCK itself
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "no matching pending preview"}), 400


def mt5_link_busy():
    """True if the MT5 link has not answered in a while. A blocked EXEC is
    better as an instant 'link busy, retry' answer than a multi-minute hang
    while a competing app holds the single MT5 connection slot."""
    return time.time() - MT5_STATE["last_ok"] > 25


def existing_exec_position(symbol, direction):
    """Position already open from a previous EXEC of the same trade. The
    double-send guard: never let one strategy signal open the same position
    twice (e.g. after a confusing failure or a modal that survived a
    restart). Returns the position or None. Never raises."""
    try:
        with sc.mt5_lock:
            pos = next((p for p in (mt5.positions_get(symbol=symbol) or [])
                        if p.magic == EXEC_MAGIC
                        and ((direction == "BUY") == (p.type == mt5.POSITION_TYPE_BUY))),
                       None)
        return pos
    except Exception:
        return None


@app.post("/api/execute/confirm")
def execute_confirm():
    """Send the order. Requires the one-time token + typed confirmation."""
    try:
        return _execute_confirm()
    except Exception:
        tb = traceback.format_exc(limit=3)
        exec_log({"event": "error", "symbol": "n/a", "error": tb.splitlines()[-1]})
        return jsonify({"ok": False,
                        "error": "internal error sending order — no order was placed. "
                                 "See execution log."}), 400


def clamp_stops_to_broker(symbol, direction, stop, target):
    """Clamp SL/TP to the broker's minimum stop distance (info.trade_stops_level)
    measured from the CURRENT price. MT5 rejects orders whose stops sit closer
    to price than that (retcode 10016 "Invalid stops"). Returns (sl, tp, note)
    where sl/tp are floats (0.0 = none) and note is '' or a human-readable
    description of every adjustment made."""
    with sc.mt5_lock:
        info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)
    if info is None or tick is None:
        return stop, target, ""
    digits = info.digits
    min_dist = (getattr(info, "trade_stops_level", 0) or 0) * info.trade_tick_size
    is_buy = direction == "BUY"
    price = tick.ask if is_buy else tick.bid
    rd = lambda v: round(v, digits)
    sl, tp = stop, (target or 0.0)
    notes = []
    if min_dist > 0:
        if is_buy:
            if sl and price - sl < min_dist:
                notes.append(f"SL moved {rd(sl)} -> {rd(price - min_dist)} "
                             f"(broker min stop distance {info.trade_stops_level} pts)")
                sl = rd(price - min_dist)
            if tp and tp - price < min_dist:
                notes.append(f"TP moved {rd(tp)} -> {rd(price + min_dist)} "
                             f"(broker min stop distance {info.trade_stops_level} pts)")
                tp = rd(price + min_dist)
        else:
            if sl and sl - price < min_dist:
                notes.append(f"SL moved {rd(sl)} -> {rd(price + min_dist)} "
                             f"(broker min stop distance {info.trade_stops_level} pts)")
                sl = rd(price + min_dist)
            if tp and price - tp < min_dist:
                notes.append(f"TP moved {rd(tp)} -> {rd(price - min_dist)} "
                             f"(broker min stop distance {info.trade_stops_level} pts)")
                tp = rd(price - min_dist)
        # a stop that lands on the wrong side of price is invalid — drop it
        if is_buy:
            if sl and sl >= price:
                notes.append("SL dropped (would sit on the wrong side of price)")
                sl = 0.0
            if tp and tp <= price:
                notes.append("TP dropped (would sit on the wrong side of price)")
                tp = 0.0
        else:
            if sl and sl <= price:
                notes.append("SL dropped (would sit on the wrong side of price)")
                sl = 0.0
            if tp and tp >= price:
                notes.append("TP dropped (would sit on the wrong side of price)")
                tp = 0.0
    return sl, tp, "; ".join(notes)


def verify_position_stops(symbol, want_sl, want_tp, retries=6):
    """After a fill, read the position back from the broker and confirm its
    SL/TP match what we asked for. If the broker stripped them, attach them
    with an SLTP modify. Returns the values actually on the position."""
    import time as _t
    with sc.mt5_lock:
        pos = None
        for _ in range(retries):
            pos = next((p for p in (mt5.positions_get(symbol=symbol) or [])
                        if p.magic == EXEC_MAGIC), None)
            if pos is not None:
                break
            _t.sleep(0.5)  # the position may take a moment to appear
        if pos is None:
            return {"sl": want_sl, "tp": want_tp,
                    "note": "warning: position not found to verify — "
                            "check SL/TP in MT5 yourself"}
        sl0, tp0 = pos.sl, pos.tp
        missing = (want_sl and not sl0) or (want_tp and not tp0)
        if missing:
            req = {"action": mt5.TRADE_ACTION_SLTP, "symbol": symbol,
                   "position": pos.ticket,
                   "sl": want_sl or sl0, "tp": want_tp or tp0,
                   "magic": EXEC_MAGIC}
            res = mt5.order_send(req)
            if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
                why = (f"retcode {res.retcode} {getattr(res,'retcode_description','')}: "
                       f"{res.comment}" if res is not None else
                       f"{mt5.last_error()}")
                return {"sl": sl0, "tp": tp0,
                        "note": "broker stripped SL/TP and re-attach failed: " + why}
        # re-read after a possible modify so we report what is really there
        _t.sleep(0.3)
        p2 = next((p for p in (mt5.positions_get(symbol=symbol) or [])
                   if p.magic == EXEC_MAGIC), pos)
        note = ("verified on broker" if not missing
                else "broker stripped them — re-attached, verified")
        return {"sl": p2.sl, "tp": p2.tp, "note": note}


def _execute_confirm():
    d = request.get_json(force=True) or {}
    token, typed = d.get("token"), (d.get("confirm") or "").strip().upper()
    with EXEC_LOCK:
        prev = EXEC["pending"]
    if not prev or prev.get("token") != token:
        return jsonify({"ok": False, "error": "no matching pending preview "
                                               "(expired or already used)"}), 400
    if time.time() - prev["created"] > PREVIEW_TTL:
        with EXEC_LOCK:
            EXEC["pending"] = None
        return jsonify({"ok": False,
                        "error": "preview expired — market moved, request a new one"}), 400
    if typed != CONFIRM_TEXT:
        return jsonify({"ok": False, "error": f"typed confirmation must be '{CONFIRM_TEXT}'"}), 400
    if not prev.get("real_money_warning_ack"):
        return jsonify({"ok": False, "error": "real-money acknowledgment required"}), 400
    # never consume the token on a non-final refusal: the modal stays usable
    if mt5_link_busy():
        return jsonify({"ok": False, "busy": True,
                        "error": "MT5 link is busy (another app is using the "
                                 "connection). No order sent — click Send "
                                 "order again in a few seconds."}), 503
    ex = existing_exec_position(prev["symbol"], prev["direction"])
    if ex is not None:
        return jsonify({"ok": False, "already_open": True,
                        "order": ex.ticket, "price": ex.price_open,
                        "sl": ex.sl, "tp": ex.tp,
                        "error": f"{prev['symbol']} {prev['direction']} is "
                                 f"ALREADY OPEN (order #{ex.ticket}) — no "
                                 "duplicate was sent."}), 409

    with EXEC_LOCK:
        EXEC["pending"] = None  # consume the token now
    save_exec_state()  # (writes pending=None) — clears the on-disk copy

    try:
        with sc.mt5_lock:
            mt5.symbol_select(prev["symbol"], True)
            tick = mt5.symbol_info_tick(prev["symbol"])
            direction = sc.direction_of(prev["strategy"])
            order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
            fill_price = tick.ask if direction == "BUY" else tick.bid
            # re-clamp against the LIVE price: a stop can become invalid if the
            # market moved since the preview, and this is what actually gets sent
            send_sl, send_tp, _ = clamp_stops_to_broker(
                prev["symbol"], direction, prev["stop"], prev["target"] or 0.0)
            result = None
            send_err = None
            # order_send() returns a TradeOperationResult (or None). Try the
            # broker's common filling policies in turn; stop at the first accept.
            for filling in (mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC,
                            mt5.ORDER_FILLING_RETURN):
                req = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": prev["symbol"],
                    "volume": prev["lots"],
                    "type": order_type,
                    "price": fill_price,
                    "sl": send_sl,
                    "tp": send_tp,
                    "deviation": 30,
                    "magic": EXEC_MAGIC,
                    # MT5 rejects order comments longer than 31 chars; keep it
                    # short (timeframe + strategy) and hard-clamp as a safety net.
                    "comment": f"{prev['timeframe']} {prev['strategy']}"[:31],
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": filling,
                }
                result = mt5.order_send(req)
                if result is None:
                    send_err = f"order_send failed: {mt5.last_error()}"
                    continue
                if result.retcode == mt5.TRADE_RETCODE_DONE:
                    break
                # a "reject" (e.g. filling policy not allowed) is retryable; a
                # real market rejection (10008 requote, 10019 invalid, ...) is not.
                if result.retcode not in (mt5.TRADE_RETCODE_REJECT,):
                    send_err = (f"retcode {result.retcode} "
                                f"{getattr(result, 'retcode_description', '')}: {result.comment}")
                    break
    except Exception as e:
        # something failed before the send (no tick, symbol select error, ...)
        # — the token was already consumed, so re-arm it. No order was sent.
        err = f"{type(e).__name__}: {e}"
        exec_log({"event": "failed", "symbol": prev["symbol"], "error": err})
        with EXEC_LOCK:
            EXEC["pending"] = prev
        save_exec_state()
        return jsonify({"ok": False, "retryable": True, "error": err}), 400

    if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
        verified = verify_position_stops(prev["symbol"], send_sl, send_tp)
        exec_log({"event": "executed", "symbol": prev["symbol"],
                  "direction": direction,
                  "lots": prev["lots"], "price": result.price,
                  "order": result.order,
                  "sl": verified["sl"], "tp": verified["tp"],
                  "note": verified["note"]})
        snap_account()
        return jsonify({"ok": True, "retcode": result.retcode,
                        "comment": result.comment, "price": result.price,
                        "order": result.order,
                        "sl": verified["sl"], "tp": verified["tp"],
                        "note": verified["note"]})
    if result is not None and not send_err:
        send_err = (f"retcode {result.retcode} "
                    f"{getattr(result, 'retcode_description', '')}: {result.comment}")
    err = send_err or "order not sent"
    exec_log({"event": "failed", "symbol": prev["symbol"], "error": err})
    # no order was placed — put the preview back so the user can simply click
    # "Send order" again without redoing the whole flow. Safe to re-arm: a
    # retry re-checks open positions first (existing_exec_position) and will
    # refuse with 409 if the failed attempt had somehow filled.
    with EXEC_LOCK:
        EXEC["pending"] = prev
    save_exec_state()
    return jsonify({"ok": False, "retryable": True, "error": err}), 400


# ---------------- page ----------------
PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>MT5 Short-TF Screener</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHZpZXdCb3g9JzAgMCAzMiAzMic+CjxyZWN0IHg9JzEnIHk9JzEnIHdpZHRoPSczMCcgaGVpZ2h0PSczMCcgcng9JzcnIGZpbGw9JyMwZDExMTcnIHN0cm9rZT0nIzMwMzYzZCcvPgo8bGluZSB4MT0nMTAnIHkxPSc2JyB4Mj0nMTAnIHkyPScyNicgc3Ryb2tlPScjM2ZiOTUwJyBzdHJva2Utd2lkdGg9JzInIHN0cm9rZS1saW5lY2FwPSdyb3VuZCcvPgo8cmVjdCB4PSc2LjUnIHk9JzEyJyB3aWR0aD0nNycgaGVpZ2h0PSc5JyByeD0nMScgZmlsbD0nIzNmYjk1MCcvPgo8bGluZSB4MT0nMjInIHkxPSc3JyB4Mj0nMjInIHkyPScyNScgc3Ryb2tlPScjZjg1MTQ5JyBzdHJva2Utd2lkdGg9JzInIHN0cm9rZS1saW5lY2FwPSdyb3VuZCcvPgo8cmVjdCB4PScxOC41JyB5PScxMScgd2lkdGg9JzcnIGhlaWdodD0nOCcgcng9JzEnIGZpbGw9JyNmODUxNDknLz4KPC9zdmc+">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<style>
:root { --bg:#0d1117; --panel:#161b22; --line:#2d333b; --txt:#e6edf3;
        --dim:#8b949e; --red:#f85149; --green:#3fb950; --amber:#d29922; }
* { box-sizing:border-box; }
body { background:var(--bg); color:var(--txt); font:14px/1.45 'Segoe UI',system-ui,sans-serif; margin:0; padding:18px; }
.masthead { display:flex; align-items:center; gap:12px; margin:0 0 14px; padding:12px 16px;
            background:linear-gradient(180deg,#161b22,#11161d); border:1px solid var(--line);
            border-radius:10px; border-left:3px solid var(--green); box-shadow:0 1px 3px rgba(0,0,0,.4); }
.masthead h1 { font-size:19px; margin:0; letter-spacing:.01em; }
.masthead h1 .sub { color:var(--dim); font-weight:400; font-size:13px; margin-left:10px; }
.brand { flex:0 0 auto; width:34px; height:34px; display:block; }
.masthead .spacer { flex:1 1 auto; }
.livechip { display:inline-flex; align-items:center; gap:7px; font-size:11px; font-weight:700;
            letter-spacing:.06em; text-transform:uppercase; padding:4px 11px; border-radius:999px;
            border:1px solid var(--line); color:var(--dim); background:#0d1117; }
.livechip .dot { width:8px; height:8px; border-radius:50%; background:var(--dim); }
.livechip.on { color:var(--green); border-color:#1f6f34; background:#0f1a12; }
.livechip.on .dot { background:var(--green); animation:pulse 1.6s infinite; box-shadow:0 0 6px var(--green); }
.livechip.down { color:var(--red); border-color:#6e2430; background:#1a0f11; }
.verchip { font-size:11px; font-weight:700; letter-spacing:.06em; color:var(--dim);
           border:1px solid var(--line); border-radius:999px; padding:4px 10px; background:#0d1117; }
.livechip.down .dot { background:var(--red); }
.grid { display:grid; grid-template-columns: 340px 1fr; gap:14px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; margin-bottom:14px; }
.card h2 { font-size:13px; text-transform:uppercase; letter-spacing:.06em; color:var(--dim); margin:0 0 10px; }
.kv { display:flex; justify-content:space-between; padding:3px 0; }
.kv b { font-variant-numeric:tabular-nums; }
.pos { color:var(--green); } .neg { color:var(--red); }
table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
th { text-align:left; color:var(--dim); font-size:11px; text-transform:uppercase; letter-spacing:.05em;
     padding:5px 8px; border-bottom:1px solid var(--line); position:sticky; top:0; background:var(--panel);}
#tbl thead th { cursor:pointer; user-select:none; }
#tbl thead th:hover { color:var(--txt); }
#tbl thead th.sort-asc::after { content:' \25B2'; font-size:9px; color:var(--amber); }
#tbl thead th.sort-desc::after { content:' \25BC'; font-size:9px; color:var(--amber); }
td { padding:4px 8px; border-bottom:1px solid #1c2129; font-size:13px; }
tr.bust td { opacity:.55; }
tr.bust td.st { color:var(--red); font-weight:600; }
tr.ok td.st { color:var(--green); font-weight:600; }
tr.click { background:#1f2a3a; }
tbody tr { cursor:pointer; }
.progbar { height:8px; background:#21262d; border-radius:4px; overflow:hidden; margin:8px 0 6px; }
.progbar div { height:100%; background:var(--amber); width:0%; transition:width .4s; }
#last { color:var(--dim); font-size:12px; }
#curveWrap { display:none; }
#curveTitle { font-size:13px; color:var(--dim); margin-bottom:6px; }
.hint { color:var(--dim); font-size:12px; margin-top:10px; }
.badge { font-size:11px; padding:1px 7px; border-radius:10px; border:1px solid var(--line); color:var(--dim); }
.execb { background:#8b2635; color:#fff; border:1px solid #f85149; border-radius:5px;
         padding:2px 10px; font-size:11px; font-weight:600; cursor:pointer; }
.execb:hover { background:#b6232f; }
.warnbox { background:#3d1418; border:1px solid #f85149; border-radius:6px;
           padding:10px; margin:10px 0; font-size:13px; }
#execlog div { padding:3px 0; border-bottom:1px solid #1c2129; font-size:12px; }
td.nt { font-size:12px; color:var(--dim); white-space:nowrap; font-variant-numeric:tabular-nums; }
td.nt.now { color:#fff; font-weight:600; }
td.bt { font-size:11.5px; color:var(--dim); white-space:nowrap; font-variant-numeric:tabular-nums; }
td.bt.on.g { color:var(--green); } td.bt.on.a { color:var(--amber); } td.bt.on.r { color:var(--red); }
td.bt span.r { color:var(--red); font-weight:600; }
.badge-now { display:inline-block; background:#238636; color:#fff; border-radius:4px;
             padding:1px 8px; font-size:11px; font-weight:700; letter-spacing:.03em;
             animation:pulse 1.2s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.45} }
</style></head>
<body>
<div class="masthead">
  <svg class="brand" viewBox="0 0 32 32" aria-hidden="true">
    <line x1="10" y1="6" x2="10" y2="26" stroke="#3fb950" stroke-width="2" stroke-linecap="round"/>
    <rect x="6.5" y="12" width="7" height="9" rx="1" fill="#3fb950"/>
    <line x1="22" y1="7" x2="22" y2="25" stroke="#f85149" stroke-width="2" stroke-linecap="round"/>
    <rect x="18.5" y="11" width="7" height="8" rx="1" fill="#f85149"/>
  </svg>
  <h1>MT5 Short-TF Screener <span class="sub">real broker data · real spreads + $0.06 RT comm · 0.01-lot floor · margin-call at $0</span></h1>
  <span class="spacer"></span>
  <span class="verchip" title="MT5 Short-TF Screener build version">v__VER__</span>
  <span class="livechip" id="livechip"><span class="dot"></span><span id="livetext">connecting…</span></span>
</div>
<div class="grid">
<div>
  <div class="card"><h2>Account <span class="badge" id="srv"></span></h2>
    <div class="kv"><span>Balance</span><b id="bal">–</b></div>
    <div class="kv"><span>Equity</span><b id="eq">–</b></div>
    <div class="kv"><span>Margin free</span><b id="mf">–</b></div>
    <div class="kv"><span>Leverage</span><b id="lev">–</b></div>
  </div>
  <div class="card"><h2>Open positions</h2>
    <div id="positions"><i style="color:var(--dim)">loading…</i></div>
  </div>
  <div class="card"><h2>Screen <span id="st"></span></h2>
    <div class="progbar"><div id="bar"></div></div>
    <div id="last"></div>
    <div class="hint" id="doneinfo"></div>
  </div>
  <div class="card"><h2>Model notes</h2>
    <div class="hint">
     $1 risk per trade, floored at the broker's min volume. Costs = real per-bar spread
     + $0.03/side per 0.01 lot (per the account's deal history). Long-only, spot-style
     (no hedging). Window: M15 = 120d, H1 = 365d (as deep as the broker has).
     A row marked <b class="neg">BUST</b>
     means the broker would have margin-called the account at $0.<br><br>
     Click any row to see its equity curve.
    </div>
  </div>
  <div class="card"><h2>Execution log</h2>
    <div id="execlog" class="hint">(no actions yet)</div>
  </div>
</div>
<div>
  <div class="card"><h2>Results — survivors first, then busted</h2>
    <div style="max-height:430px;overflow:auto"><table id="tbl">
      <thead><tr><th>#</th><th data-i="0">Symbol</th><th data-i="1">TF</th><th data-i="2">Strategy</th><th data-i="3">Dir</th><th data-i="4">Bars</th>
      <th data-i="5">Final $</th><th data-i="6">Return</th><th data-i="7">Trades</th><th data-i="8">Win%</th><th data-i="9">PF</th>
      <th data-i="10">MaxDD</th><th data-i="11">AvgHold</th><th data-i="12">Outcome</th><th data-i="13">Next entry</th>
      <th data-i="14">Backtest @ entry</th><th data-i="15">Live</th></tr></thead>
      <tbody id="tb"></tbody>
    </table></div>
  </div>
  <div class="card" id="curveWrap"><h2>Equity curve</h2>
    <div id="curveTitle"></div>
    <canvas id="cv" height="210"></canvas>
  </div>
</div>
</div>
<div id="modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:10;align-items:center;justify-content:center">
  <div class="card" style="width:540px;max-width:92vw;border-color:#f85149">
    <h2 style="color:var(--red)">Execute position — REAL MONEY</h2>
    <div id="execBody"></div>
  </div>
</div>
<script>
let chart=null, selRow=null, lastSig='', ALLROWS=[], sortIdx=null, sortDir=-1;
let BT_START=null, ACC_BAL=null;   // backtest start = live account balance (from /api/status)
const $=id=>document.getElementById(id);
function money(v){return v==null?'–':'$'+v.toFixed(2);}
function pct(v){if(v==null)return'–';const p=(v*100).toFixed(1)+'%';return '<span class="'+(v>=0?'pos':'neg')+'">'+(v>=0?'+':'')+p+'</span>';}
function setLiveChip(on,label){const c=$('livechip');if(!c)return;
  c.className='livechip '+(on?'on':'down'); $('livetext').textContent=label;}
function STATE_ROWS(){ return ALLROWS; }

let wasOffline=false;
async function tick(){
  try{
    const {account:acc,positions:pos,screen:sc,start}=await (await fetch('/api/status')).json();
    if(start!=null) BT_START=start;
    if(acc){ACC_BAL=acc.balance;
      $('bal').textContent=money(acc.balance); $('eq').textContent=money(acc.equity);
      $('mf').textContent=money(acc.margin_free); $('lev').textContent='1:'+acc.leverage;
      $('srv').textContent=acc.server;}
    $('positions').innerHTML = pos.length? pos.map(p=>{
      const noStops = (!p.sl || p.sl==0) && (!p.tp || p.tp==0);
      return `<div class="kv"><span>${p.symbol} ${p.side} ${p.volume}
        <small style="color:${noStops?'var(--red)':'var(--dim)'}">
        ${noStops?'⚠ no SL/TP set':'SL '+p.sl+' / TP '+p.tp}
        · ${p.time}</small></span>
       <b class="${p.pl>=0?'pos':'neg'}">${money(p.pl)} <span style="color:var(--dim)">(${p.open}→${p.current})</span></b></div>`;
    }).join('') : '<i style="color:var(--dim)">none</i>';
    $('st').textContent=sc.state;
    if(sc.total){$('bar').style.width=(100*sc.done/sc.total).toFixed(1)+'%';}
    $('last').textContent=sc.state==='running'? `working: ${sc.last}  (${sc.done}/${sc.total}, ${sc.elapsed}s)` :
                          (sc.state==='done'? `finished in ${sc.elapsed}s` : sc.state);
    setLiveChip(true, sc.state==='running'? 'live · screening' : 'live · connected');
  }catch(e){ setLiveChip(false,'offline'); wasOffline=true; }
}
async function loadResults(){
  let rows; try{ rows=await (await fetch('/api/results')).json(); }catch(e){ return; }
  const sig=rows.length+':'+(rows.length?JSON.stringify([rows[0].final,rows[rows.length-1].final,rows[0].direction]):'');
  if(sig===lastSig) return;  // unchanged: don't re-render (keeps clicks stable)
  lastSig=sig;
  ALLROWS=rows;
  renderRows();
}
function sortVal(r,i){
  const k=`${r.symbol}|${r.timeframe}|${r.strategy}`;
  switch(i){
    case 0:  return r.symbol;
    case 1:  return r.timeframe;
    case 2:  return r.strategy;
    case 3:  return r.direction;
    case 4:  return r.bars;
    case 5:  return r.final;
    case 6:  return r.return_pct;
    case 7:  return r.trades;
    case 8:  return r.win_rate;
    case 9:  return r.profit_factor==null?999:r.profit_factor;
    case 10: return r.max_dd;
    case 11: return r.avg_hold;
    case 12: return r.busted;                       // false(alive) sorts first ascending
    case 13: { const ls=liveState.ready[k];         // ENTRY NOW rows sort to the front
               if(ls&&ls.ready) return 0;
               return nextClose(r.timeframe); }
    case 14: { const ls=liveState.ready[k], b=ls&&ls.bt;  // @-entry PF, ungraded last
               if(!b) return -1;
               return b.pf==null?999:b.pf; }
    case 15: return r.busted?1:0;                   // EXEC column: tradeable first
    default: return 0;
  }
}
function renderRows(){
  const rows=STATE_ROWS();
  let all=rows;
  if(sortIdx!=null){
    const dir=sortDir||-1;
    all=[...rows].sort((a,b)=>{
      let va=sortVal(a,sortIdx), vb=sortVal(b,sortIdx);
      if(typeof va==='string'||typeof vb==='string'){ va=String(va).toLowerCase(); vb=String(vb).toLowerCase(); }
      if(va<vb) return -dir; if(va>vb) return dir;
      return (a.busted?1:0)-(b.busted?1:0);        // tie: survivors before busted
    });
  } else {
    const ok=rows.filter(r=>!r.busted).sort((a,b)=>b.final-a.final);
    const bust=rows.filter(r=>r.busted).sort((a,b)=>a.final-b.final);
    all=[...ok,...bust];
  }
  const ok=rows.filter(r=>!r.busted).length, bust=rows.length-ok;
  $('doneinfo').textContent = rows.length? `${rows.length} results so far — ${ok} alive, ${bust} busted` : '';
  const tbody=$('tb');
  tbody.innerHTML = all.map((r,i)=>{
    const key=`${r.symbol}|${r.timeframe}|${r.strategy}`;
    return `<tr class="${r.busted?'bust':'ok'}" data-k="${key}">
      <td>${i+1}</td><td>${r.symbol}</td><td>${r.timeframe}</td><td>${r.strategy}</td>
      <td>${r.direction==='SELL'?'<span class="neg">SELL</span>':'<span class="pos">BUY</span>'}</td>
      <td>${r.bars}</td><td>${money(r.final)}</td><td>${pct(r.return_pct)}</td>
      <td>${r.trades}</td><td>${r.win_rate?(100*r.win_rate).toFixed(0):'–'}</td>
      <td>${r.profit_factor==null?'∞':r.profit_factor}</td>
      <td>${pct(r.max_dd)}</td><td>${(r.avg_hold*(r.timeframe==='H1'?1:0.25)).toFixed(1)}h</td>
      <td class="st">${r.busted?'BUST':'alive'}</td>
      <td class="nt" data-tf="${r.timeframe}" data-k="${key}" ${r.busted?'data-busted="1"':''}>${r.busted?'--':'…'}</td>
      <td class="bt" ${r.busted?'data-busted="1"':''}></td>
      <td>${r.busted?'':`<button class="execb" data-k="${key}">EXEC</button>`}</td></tr>`;}).join('');
  document.querySelectorAll('#tb tr').forEach(tr=>tr.onclick=()=>select(tr.dataset.k, tr));
  document.querySelectorAll('#tb .execb').forEach(b=>b.onclick=async e=>{
    e.stopPropagation();
    const r=rows.find(x=>`${x.symbol}|${x.timeframe}|${x.strategy}`===b.dataset.k);
    openExec(r);
  });
}

async function select(key, tr){
  document.querySelectorAll('#tb tr').forEach(x=>x.classList.remove('click'));
  if(tr) tr.classList.add('click');
  const d=await (await fetch('/api/curve?key='+encodeURIComponent(key))).json();
  if(!d) return;
  $('curveWrap').style.display='block';
  const [sym,tf,strat]=key.split('|');
  $('curveTitle').textContent=`${sym} · ${tf} · ${strat}  (downsampled every 4th bar)`;
  if(chart) chart.destroy();
  chart=new Chart($('cv'),{type:'line',data:{labels:d.t.map(t=>new Date(t*1000).toLocaleDateString()),
    datasets:[{label:'equity $',data:d.v,borderColor:'#58a6ff',borderWidth:1.4,pointRadius:0,fill:false},
              {label:BT_START?'start $'+BT_START:'start',data:d.v.map(()=>BT_START==null?d.v[0]:BT_START),borderColor:'#484f58',borderDash:[5,5],borderWidth:1,pointRadius:0}]},
    options:{plugins:{legend:{labels:{color:'#8b949e',boxWidth:14}}},
      scales:{x:{ticks:{color:'#8b949e',maxTicksLimit:8},grid:{color:'#21262d'}},
              y:{ticks:{color:'#8b949e'},grid:{color:'#21262d'}}},animation:false}});
  $('curveWrap').scrollIntoView({behavior:'smooth',block:'nearest'});
}

// ---------- live execution ----------
let execToken=null;
function modalShow(v){const m=$('modal');m.style.display=v?'flex':'none';}
// Fetch that survives a brief server restart gap: when the MT5 link freezes,
// the heartbeat self-terminates the process and the watchdog relaunches it in
// a few seconds. A read-only preview re-run is harmless, so retry with backoff
// instead of surfacing a hard "Failed to fetch". Returns parsed JSON; rethrows
// after `tries` attempts. `onRetry(n)` lets the caller update its UI.
async function fetchRetry(url, opts, tries, onRetry){
  let lastErr;
  for(let i=0;i<tries;i++){
    try{
      const res=await fetch(url,opts);
      return await res.json();
    }catch(e){
      lastErr=e;
      if(i<tries-1){ if(onRetry) onRetry(i+1);
        await new Promise(r=>setTimeout(r, 1200*(i+1))); }
    }
  }
  throw lastErr;
}
// was the position from this attempt already opened? (used when the confirm
// reply is lost mid-flight, so we never tell the user "unknown" when we know)
async function checkIfOpen(sym,dir){
  try{ const s=await (await fetch('/api/status')).json();
    return (s.positions||[]).find(p=>p.symbol===sym && p.side===dir)||null;
  }catch(e){ return null; }
}
function renderExecModal(p){
  const body=$('execBody');
  const isSell=p.direction==='SELL';
  const bigRisk=p.big_risk?`<div class="warnbox">⚠ This trade risks <b>$${p.risk_usd}</b> =
       <b>${p.risk_pct_balance}% of your balance</b> (min-lot floor). That is above the 5% safety cap.</div>`:'';
  body.innerHTML=`
    <div class="warnbox">You are about to open a <b>real, live market ${isSell?'SELL':'BUY'}</b>
      on <b>${p.symbol}</b>. This cannot be undone from the dashboard (close it in MT5).
      ${bigRisk}</div>
    <div class="kv"><span>Strategy spec</span><b>${p.strategy} · ${p.timeframe} ·
      ${isSell?'<span class="neg">SELL</span>':'<span class="pos">BUY</span>'}</b></div>
    <div class="kv"><span>Signal on bar</span><b>${p.signal_bar}</b></div>
    <div class="kv"><span>Fill price (${isSell?'bid':'ask'})</span><b>${isSell?p.bid:p.ask}</b></div>
    <div class="kv"><span>Volume</span><b>${p.lots} lots</b></div>
    <div class="kv"><span>Stop loss ${isSell?'(above)':'(below)'}</span><b>${p.stop||'—'}  <span class="neg">(−$${p.risk_usd})</span></b></div>
    <div class="kv"><span>Take profit ${isSell?'(below)':'(above)'}</span><b>${p.target||'—'}</b></div>
    ${p.stop_note?`<div class="warnbox" style="background:#2a2113;border-color:#d29922">⚠ <b>Stops adjusted to broker minimum distance:</b> ${p.stop_note}</div>`:''}
    <div class="kv"><span>Account balance</span><b>$${p.balance}</b></div>
    <div style="margin-top:14px">
      <label style="display:block;margin-bottom:8px;font-size:13px">
        <input type="checkbox" id="ackBox" style="margin-right:6px">
        I understand this uses <b>real money</b> on my live account.
      </label>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <button class="execb" id="ackBtn" style="background:#5a3e00;border-color:#d29922">Acknowledge risk</button>
        <button class="execb" id="sendBtn" disabled style="opacity:.4">Send order</button>
        <button class="execb" onclick="modalShow(false)" style="background:#30363d;border-color:var(--line)">Cancel</button>
      </div>
      <input id="cfm" placeholder="Type EXECUTE to confirm" style="display:none;margin-top:10px;width:220px;
        background:#0d1117;color:var(--txt);border:1px solid var(--line);border-radius:5px;padding:6px">
      <div id="execMsg" style="margin-top:8px;font-size:13px"></div>
    </div>`;
  $('ackBtn').onclick=async()=>{
    if(!$('ackBox').checked){$('execMsg').innerHTML='<span class="neg">Tick the box first.</span>';return;}
    let a; try{ a=await fetchRetry('/api/execute/ack',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({token:execToken})},3); }
    catch(e){ $('execMsg').innerHTML='<span class="neg">ack failed: '+e+' — server restarting, try again</span>'; return; }
    if(a.ok){ $('cfm').style.display='inline-block'; $('sendBtn').disabled=false; $('sendBtn').style.opacity=1;
      $('cfm').focus(); }
    else $('execMsg').innerHTML='<span class="neg">'+(a.error||'ack failed')+'</span>';
  };
  $('sendBtn').onclick=async()=>{
    const t=$('cfm').value.trim().toUpperCase();
    if(t!=='EXECUTE'){ $('execMsg').innerHTML='<span class="neg">Type EXECUTE to confirm.</span>'; return; }
    $('sendBtn').disabled=true;
    $('execMsg').innerHTML='<span style="color:var(--amber)">Sending…</span>';
    let r;
    try{
      r=await (await fetch('/api/execute/confirm',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({token:execToken,confirm:t})})).json();
    }catch(e){
      // the reply was lost (server restarting mid-flight) — the order may or
      // may not have gone through. Verify against live positions before
      // telling the user either way.
      $('execMsg').innerHTML='<span style="color:var(--amber)">reply lost — checking whether the order went through…</span>';
      const pos=await checkIfOpen(p.symbol,p.direction);
      if(pos){ $('execMsg').innerHTML='<span class="pos">ORDER SENT (verified in account) — '+pos.symbol+
        ' '+pos.side+' '+pos.volume+' @ '+pos.open+' · SL '+(pos.sl||'—')+' / TP '+(pos.tp||'—')+'</span>';
        loadResults(); execToken=null;
        setTimeout(()=>modalShow(false), 2500); return; }
      $('execMsg').innerHTML='<span class="neg">No order found in the account — it was NOT sent. Click Send order again.</span>';
      $('sendBtn').disabled=false; return;
    }
    $('sendBtn').disabled=false;
    if(r.ok){ $('execMsg').innerHTML='<span class="pos">ORDER SENT — '+r.price+' · order #'+r.order+
      ' · SL '+(r.sl||'—')+' / TP '+(r.tp||'—')+' <span style="color:var(--dim)">('+r.note+')</span></span>';
      loadResults();
      setTimeout(()=>modalShow(false), 2000);  // show the confirmation, then close
      return; }
    if(r.already_open){ $('execMsg').innerHTML='<span class="pos">Already open — order #'+r.order+
      ' @ '+r.price+' · SL '+(r.sl||'—')+' / TP '+(r.tp||'—')+'. No duplicate was sent.</span>';
      loadResults(); execToken=null;
      setTimeout(()=>modalShow(false), 2500); return; }
    if(r.busy){ $('execMsg').innerHTML='<span style="color:var(--amber)">'+r.error+'</span>'; return; }
    $('execMsg').innerHTML='<span class="neg">'+(r.error||'send failed')+
      (r.retryable?' <span style="color:var(--dim)">— nothing was sent, click Send order again</span>':'')+'</span>';
  };
}
async function openExec(r){
  const body=$('execBody');
  body.innerHTML='<div class="warnbox">Requesting preview from server…</div>';
  modalShow(true);
  let d; try{ d=await fetchRetry('/api/execute/preview',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({symbol:r.symbol,timeframe:r.timeframe,strategy:r.strategy})},
      4, n=>{ if($('livetext')) $('livetext').textContent='server restarting — retrying…'; }); }
  catch(e){ body.innerHTML='<div class="warnbox">preview request failed after retry: '+e+
      ' — the server may still be restarting. Close and re-click EXEC to try again.</div>'; return; }
  if(!d.ok){ body.innerHTML=`<div class="warnbox">${d.error||d.message||'cannot preview'}</div>
       <button class="execb" onclick="modalShow(false)">Close</button>`; return; }
  execToken=d.preview.token;
  renderExecModal(d.preview);
}
// if a restart happened mid-exec-flow, the pending preview survives on disk —
// re-open the modal so the user doesn't have to start over
async function restoreExecModal(){
  if($('modal').style.display==='flex') return;      // modal already open
  let d; try{ d=await (await fetch('/api/exec')).json(); }catch(e){ return; }
  const p=d.pending; if(!p) return;
  if(!p.created || Date.now()/1000 - p.created > 115) return;  // expired
  execToken=p.token;
  renderExecModal(p);
  modalShow(true);
  if(p.real_money_warning_ack){ $('ackBox').checked=true; $('cfm').style.display='inline-block';
    $('sendBtn').disabled=false; $('sendBtn').style.opacity=1; }
  $('execMsg').innerHTML='<span style="color:var(--amber)">Server restarted while you were here — your order setup was restored. Check the prices, then continue.</span>';
}
async function loadExecLog(){
  let d; try{ d=await (await fetch('/api/exec')).json(); }catch(e){ return; }
  const el=$('execlog');
  if(!d.log.length){ el.innerHTML='(no actions yet)'; return; }
  el.innerHTML=d.log.slice().reverse().map(l=>
    `<div><b>${l.ts}</b> ${l.event} — ${l.symbol||''} ${l.tf||''} ${l.strategy||''}
     ${l.direction?'· '+l.direction:''}
     ${l.lots?'· '+l.lots+' lots':''} ${l.risk?'· −$'+l.risk:''} ${l.price?'· @'+l.price:''}
     ${l.error?'<span class="neg">'+l.error+'</span>':''}</div>`).join('');
}

// ---------- next-entry column (live signal watch) ----------
let liveState={ready:{},meta:{}};
const TF_SEC={'M15':900,'H1':3600};
async function loadLiveSignals(){
  try{ const d=await (await fetch('/api/live-signals')).json();
    liveState={ready:d.live||{},meta:d.meta||{}};
  }catch(e){}
  renderEntryCol();
}
function nextClose(tf){
  const D=TF_SEC[tf]||900;
  const now=Date.now()/1000;
  return Math.ceil(now/D)*D - now;   // seconds until this bar closes (signal evaluated)
}
function renderEntryCol(){
  document.querySelectorAll('#tb tr').forEach(tr=>{
    const cell=tr.querySelector('td.nt'); if(!cell) return;
    const bt=tr.querySelector('td.bt');
    if(cell.dataset.busted==='1') return;
    const tf=cell.dataset.tf, k=cell.dataset.k;
    const ls=liveState.ready[k];
    if(ls && ls.ready){
      cell.innerHTML='<span class="badge-now">● ENTRY NOW</span>';
      cell.classList.add('now');
      if(bt && ls.bt){
        const b=ls.bt, pf=(b.pf==null?99:b.pf);
        const base=BT_START!=null?BT_START:(ACC_BAL!=null?ACC_BAL:b.final);
        const grade = pf>=1.2?'g' : (b.trades>=1 && pf>=1.0 && (100*b.win)>=38?'a' : 'r');
        bt.classList.add('on'); bt.classList.remove('g','a','r'); bt.classList.add(grade);
        bt.innerHTML=`${(100*b.win).toFixed(0)}% win · `+
          `PF ${b.pf==null?'∞':b.pf} · `+
          `<span>${b.final>=base?'$':'−$'}${Math.abs(b.final).toFixed(0)}</span> · ${b.trades} trades`+
          (grade==='r'?' · <span class="r">don&#39;t take</span>':'');
      } else if(bt){ bt.classList.remove('on'); bt.textContent='–'; }
    } else {
      const s=nextClose(tf);
      const m=Math.floor(s/60), sec=Math.floor(s%60);
      cell.textContent = `${m}m ${String(sec).padStart(2,'0')}s to next bar`;
      cell.classList.remove('now');
      if(bt){ bt.classList.remove('on'); bt.textContent='–'; }
    }
  });
}

// ---------- sortable table headers (UI only) ----------
document.querySelectorAll('#tbl thead th[data-i]').forEach(th=>{
  th.onclick=()=>{
    const i=+th.dataset.i;
    if(sortIdx!==i){ sortIdx=i; sortDir=-1; }        // new column: best-first
    else if(sortDir===-1){ sortDir=1; }              // 2nd click: reverse
    else { sortIdx=null; sortDir=-1; }               // 3rd click: default order
    document.querySelectorAll('#tbl thead th').forEach(x=>x.classList.remove('sort-asc','sort-desc'));
    if(sortIdx!==null) th.classList.add(sortDir===-1?'sort-desc':'sort-asc');
    renderRows();
  };
});
setInterval(tick,3000); setInterval(loadResults,4000); setInterval(loadExecLog,4000);
setInterval(loadLiveSignals,4000); setInterval(renderEntryCol,1000);
document.addEventListener('keydown', e=>{ if(e.key==='Escape') modalShow(false); });
tick(); loadResults(); loadExecLog(); loadLiveSignals(); restoreExecModal();
</script></body></html>"""


@app.get("/")
def index():
    return PAGE.replace("v__VER__", "v" + __version__)


def supervisor():
    """If a looping background thread dies, restart it (feed keeps running).
    `worker` is one-shot (runs the screen once) and is NOT restarted."""
    targets = {"account_loop": account_loop,
               "live_signal_loop": live_signal_loop}
    alive = {}
    for name, target in targets.items():
        t = threading.Thread(target=target, daemon=True, name=name)
        t.start()
        alive[name] = t
    while True:
        time.sleep(15)
        for name, t in alive.items():
            if not t.is_alive():
                print(f"[supervisor] thread {name} died; restarting", flush=True)
                nt = threading.Thread(target=targets[name], daemon=True,
                                      name=name)
                nt.start()
                alive[name] = nt


def main():
    print(f"=== MT5 Short-TF Screener v{__version__} ===", flush=True)
    load_exec_state()   # restore a pending preview + audit log across restarts
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=supervisor, daemon=True).start()   # starts + guards the loops
    threading.Thread(target=mt5_heartbeat, daemon=True).start()  # kills a frozen MT5 link
    app.run(host="127.0.0.1", port=8765, debug=False, threaded=True)


if __name__ == "__main__":
    main()
