# MT5 Short-TF Screener — User Manual

**Version:** 1.0 (see `VERSION`; the running build shows `v1.0` in the header)

A live trading dashboard that backtests a set of short-timeframe strategies
across every symbol on your MT5 broker, shows the results as a ranked table,
watches the market for new entry signals in real time, and — only if you
explicitly ask — places a **real, live market order** on your account.

It talks to your **real** MT5 terminal and your **real** money account
(whatever server/account your terminal is logged into). Treat the EXEC button
accordingly:
once an order is sent you can only close it inside MT5, not from the dashboard.

---

## 1. What it does, in one screen

| Area | What it shows |
|---|---|
| **Header** | Brand logo, connection status chip (`live · connected` / `live · screening` / `offline`), and the model's assumptions. |
| **Account** card | Live balance, equity, free margin, leverage — refreshed every 5 s. |
| **Open positions** card | Any currently open positions with SL/TP and live P&L. |
| **Screen** card | Progress bar + how long the backtest took. |
| **Results** table | Every symbol × timeframe × strategy, ranked by backtested final capital. |
| **Equity curve** | Shown when you click a row (Chart.js line of the backtest equity). |
| **Execution log** | Every preview / execution / failure, newest first. |

On start it **runs one full backtest screen** (every symbol, M15 and H1, all
six strategies). That takes about 10–15 s and currently produces ~312 rows.
After that, it keeps two background watches running:
- **Account loop** — refreshes the Account / Positions cards every 5 s.
- **Live-signal loop** — re-checks each "alive" row every 45 s and flags the
  *Next entry* / *Backtest @ entry* columns when a strategy signals a new entry.

The full backtest is **not** re-run automatically — to re-screen (e.g. after a
big market move, or if you changed code), **restart the dashboard** (Section 5).

### The six strategies

| Strategy | Direction |
|---|---|
| Trend Pullback | BUY |
| Mean Reversion | BUY |
| Breakout | BUY |
| Trend Pullback (short) | SELL |
| Mean Reversion (short) | SELL |
| Breakout (short) | SELL |

---

## 2. Requirements

All of these must be true before the app will connect:

1. **Windows** (the startup uses Windows `Start-Process` / `pythonw`).
2. **The MT5 terminal is open and logged in** to your real account. The app
   auto-detects the terminal (`mt5.initialize()` with no path). If MT5 is
   closed, the Screen card will show an `MT5 init failed …` error and retry
   every 10 s.
3. **A Python environment with the app's packages.** Everything the app needs
   is pinned in **`requirements.txt`** in this folder, and a ready-to-use
   virtual environment is created at **`.\.venv`** (Python 3.11). You do **not**
   need any global Python:
   - **Fastest:** just double-click **`start_app.bat`** — it rebuilds `.venv`
     from `requirements.txt` the first time it runs, then starts the app.
   - **Manually:** `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`
     (only if you deleted or want to refresh `.venv`).
   The pinned packages: `Flask 3.1.3`, `Werkzeug 3.1.8`, `numpy 2.2.1`,
   `pandas 2.3.3`, `metatrader5 5.0.6180`.

> The whole app is self-contained in `D:\HTPI\NewTrading`: the code, the
> pinned dependencies (`requirements.txt`), the interpreter (`.\.venv`), and a
> one-click start (`start_app.bat`). Nothing outside this folder is required.

> **One connection per terminal.** MT5 allows only **one** external Python
> client at a time. If another Python process (an old dashboard, a script, the
> old robot) is holding the slot, this app's connection will hang or fail.
> Make sure no other MT5-Python tool is running (see Section 8).

---

## 3. Starting the app

### Easiest: double-click `start_app.bat`

This is the intended way to run it. It lives in this folder, uses the local
`.venv` (building it from `requirements.txt` the first time), stops any stale
copy, launches the watchdog, and opens the browser for you.

### Production start (what the script does, by hand)

Launch the **watchdog**, which starts the dashboard and relaunches it
automatically if it ever crashes (the MT5 DLL can occasionally fault the
interpreter natively — the watchdog is the only layer that recovers from that).
Note it uses the **local** venv in this folder, not any system Python:

```powershell
powershell -NoProfile -Command "Start-Process -FilePath 'D:\HTPI\NewTrading\.venv\Scripts\pythonw.exe' -ArgumentList 'dashboard_watchdog.py' -WorkingDirectory 'D:\HTPI\NewTrading'"
```

Notes:
- `pythonw.exe` = no console window; the process detaches and survives the
  shell session.
- Launch **only once** — a second watchdog is a duplicate (you would get two
  dashboards fighting over the single MT5 slot). `start_app.bat` already stops
  a stale copy before launching, so it's safe to run repeatedly.
- Wait ~15 s, then open **http://127.0.0.1:8765/** in a browser.

### Quick test: run in a console (to see logs)

Good for development — you see stdout/stderr live. It stops when the terminal
closes and does **not** auto-restart:

```powershell
cd D:\HTPI\NewTrading
& ".venv\Scripts\python.exe" dashboard.py
```

Watch for `* Running on http://127.0.0.1:8765 *`, then the Screen card filling
its progress bar, then `Screen … done`.

---

## 4. Reading the dashboard

- **Header status chip** (top-right):
  - green **`live · connected`** — connected, idle between scans.
  - green **`live · screening`** — the backtest is running.
  - red **`offline`** — the app lost contact with the API; check the process
    and that MT5 is open.
- **Account card** — `Balance` is cash; `Equity` = balance + open P&L. With a
  high-leverage account and no open positions, Margin free ≈ Equity.
- **Results table** — ranked **survivors first, then busted**. Key columns:
  - `Final $` — account capital at the end of the backtest (started at your account balance).
  - `Return` — % change over the window.
  - `Trades` / `Win%` / `PF` — number of trades, win rate, and
    **profit factor** (gross wins ÷ gross losses; `∞` = no losses).
  - `MaxDD` — worst drawdown.
  - `Outcome` — `alive` or **`BUST`** (see below).
  - `Next entry` — when the next M15/H1 bar closes (e.g. `45m 39s to next bar`),
    or a green **`● ENTRY NOW`** badge when a strategy is signaling on the
    latest closed bar.
  - `Backtest @ entry` — a quick verdict on the strategy **as of the current
    bar** (green/amber/red), so you can compare the full backtest to "how it
    looks right now."
  - **Last column (EXEC button)** — the red `EXEC` button on surviving rows
    (see Section 6). Its column header currently reads "Live" — a leftover
    label; the cell actually holds the execute button.
- **Click any row** to draw its **equity curve** (downsampled every 4th bar,
  with a dashed start line at your account balance) in the Equity curve card.
- **Execution log** — a running record of every action you took (preview,
  execute, failure), newest first.

### How the backtest scores a strategy

- **$1 risk per trade**, floored at the broker's minimum volume (0.01).
- **Costs** = the real per-bar spread **plus** $0.03 per side per 0.01 lot
  (measured from your account).
- **BUST** = the backtest account hit $0 (a margin call) — i.e. that strategy
  would have blown up the account. Busted rows are ranked last and have no
  EXEC button.
- **Windows:** M15 = last 120 days, H1 = last 365 days (as deep as the broker
  has).
- Signals act on the **next open** after the bar that closed (no lookahead).

> **Reading win-rate:** at a ~2:1 reward-to-risk, the breakeven win rate is
> about 33% before costs (~36–38% after). **Profit factor is the more
> reliable gate** — prefer `PF` well above 1.0 over a high raw win %.

---

## 5. Stopping and restarting

### Stop everything

Kills the watchdog **and** the dashboard (both match `*dashboard*`). Killing
the watchdog matters — otherwise it would immediately relaunch the dashboard:

```powershell
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | Where-Object { $_.CommandLine -like '*dashboard*' } | ForEach-Object { Write-Host ('killing ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }"
```

### Restart

Stop as above, then run the **production start** from Section 3 again. The
dashboard re-runs the full backtest on start.

### "Restart" shortcut (relaunch dashboard, keep watchdog)

If the watchdog is healthy and you just want a fresh screen, you can stop only
the dashboard processes (command line `*dashboard.py*`, not `dashboard_watchdog`)
— the watchdog relaunches it within ~3 s. Killing just the watchdog leaves an
orphan dashboard with no auto-restart, so prefer a full stop.

---

## 6. Executing a real trade (EXEC flow)

This is the only part of the app that spends real money. It is deliberately
three-step so a mistake can't slip through.

1. **Find a row** with a green `● ENTRY NOW` badge (the strategy is signaling
   on the latest closed bar) and click its red **`EXEC`** button.
   - If the strategy is *not* currently signaling, the preview will tell you
     "NOT currently signaling an entry … Nothing to execute" and stop there.
2. **Review the preview modal.** It shows, for the *exact* order that would be
   sent:
   - direction (BUY/SELL), symbol, the signal bar, the fill price (ask for
     buys, bid for sells), volume in lots,
   - **Stop loss** with the dollar amount at risk,
   - take profit (if the strategy defines one),
   - your current balance.
   - If the trade's risk exceeds **5% of your balance**, a red **safety-cap
     warning** is shown (this is a *warning*, not a block — you decide).
3. **Acknowledge and confirm:**
   - Tick **"I understand this uses real money on my live account."**
   - Click **Acknowledge risk**.
   - **Type `EXECUTE`** (exactly, capital) into the box.
   - Click **Send order**.
   - On success the modal reports the fill price, order number, and the
     **verified SL/TP** (the app re-reads the position and confirms the stops
     actually landed — it shows a note if they couldn't be verified).

**Important:** a sent order cannot be undone from the dashboard. Close it in
MT5. The SL/TP are set on the order itself, so a losing trade exits on its
stop even if you close the dashboard.

---

## 7. Confirming it's healthy

Open **http://127.0.0.1:8765/** — or hit the status endpoint:

```powershell
curl http://127.0.0.1:8765/api/status
```

A healthy response looks like:
`"state":"done"`, `"done":312, "total":312`, and your account balance/server.
The header chip should read `live · connected`.

Useful endpoints (mostly for debugging, the UI already calls them):
- `GET /api/status` — account, positions, screen state.
- `GET /api/results` — the ranked rows.
- `GET /api/live-signals` — current entry flags + meta (watching N rows, last scan).
- `GET /api/curve?key=SYMBOL|TF|STRATEGY` — equity-curve series for a row.
- `GET /api/exec` — last 20 log entries + pending preview.

The watchdog writes its own log to `dashboard_watchdog.log` (every launch and
every crash + exit code). Check it if the app ever fails to come back up.

---

## 8. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Port 8765 won't open / "Address already in use" | A dashboard (or its stray child) is already running. Stop everything (Section 5) and start once. |
| Screen stuck at `MT5 init failed` or `initialize hung` | MT5 terminal is closed, or another Python process holds the single MT5 slot. Open MT5, confirm it's logged in, and make sure no other MT5-Python tool is running. |
| `ModuleNotFoundError: No module named 'flask'` | Started with the wrong Python. Use the local venv (`.venv\Scripts\python.exe` — Section 3), not the bare system/uv Python. Rebuild it with `start_app.bat` or `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`. |
| Two dashboards / duplicate rows | Two watchdogs were started. Stop everything and start exactly one watchdog. |
| Process list shows "extra" python processes | Normal on this machine: `pythonw.exe` is a launcher shim that spawns a `python.exe` child doing the real work. Each logical process appears as a shim→child **pair**. One watchdog + one dashboard = 4 processes. |
| No `ENTRY NOW` badges | No strategy is currently signaling on a closed bar — that's normal; badges appear only on a fresh signal. |
| A row is `BUST` and has no EXEC button | Expected — the backtest blew the account, so the app won't offer to trade it. |

### Quick "is a stray holding the MT5 slot?" check

```powershell
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | Where-Object { $_.CommandLine -match 'MetaTrader|dashboard|autotrader' } | Select-Object ProcessId, CommandLine | Format-List"
```

Anything matching that isn't your current dashboard/watchdog should be killed
before (re)starting.

---

## 9. Safety notes

- **This is a real-money account.** Every executed order is a real market
  order on your live account. The three-step EXEC flow (preview → acknowledge →
  typed `EXECUTE`) is the only protection between you and a filled order — use
  it every time, and never skip the acknowledgement.
- **Stops are set at order time.** SL/TP are attached to the order, and the
  app re-verifies they landed. Even so, if your MT5 terminal / internet drops,
  a live position can still move against you — the dashboard does not
  monitor or close positions for you.
- **The 5% safety cap is advisory.** It warns you when a single trade's risk
  exceeds 5% of balance (usually because the min-lot floor forces a bigger
  risk on a small account), but it does not block the order.
- **The backtest is not a guarantee.** Small sample sizes, and strategies that
  look great in a backtest can still lose live. Judge `PF` and `MaxDD` over a
  single big `Return`, and size down when unsure.

---

*Files (all in `D:\HTPI\NewTrading`): `dashboard.py` (web app + execution),
`screen.py` (backtest engine + signals), `backtest.py` (strategy definitions),
`dashboard_watchdog.py` (process supervisor), `requirements.txt` (pinned
dependencies), `.venv\` (self-contained Python environment), `start_app.bat`
(one-click start). Startup and stop commands live in Sections 3 and 5.*
