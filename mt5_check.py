"""Connect to the running MT5 terminal and report account + symbols."""
import MetaTrader5 as mt5

if not mt5.initialize():
    print("INIT FAILED:", mt5.last_error())
    raise SystemExit

acc = mt5.account_info()
print(f"Account {acc.login}  server={acc.server}  balance=${acc.balance:.2f}  equity=${acc.equity:.2f}  leverage=1:{acc.leverage}")

symbols = mt5.symbols_get()
print(f"\n{len(symbols)} symbols in Market Watch:")
for s in symbols[:40]:
    print(f"  {s.name:<18} spread={s.spread}  digits={s.digits}  vol={s.volume_min}-{s.volume_max} {s.volume_step}")
