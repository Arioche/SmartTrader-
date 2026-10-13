"""Trade journal + per-strategy live stats for the demo trainer.

Every auto (and manual, same magic) trade the demo account takes is written
to a JSONL journal. Live stats per row (symbol|TF|strategy) — closed trades,
win rate, profit factor, net P&L — are what make the system self-learning:
the dashboard shows them next to the backtest numbers, and the auto-trader
gates entries on them once enough trades exist.
"""
import json
import os
import threading


class Journal:
    def __init__(self, path="journal_demo.jsonl"):
        self.path = path
        self.lock = threading.Lock()
        self.entries = []
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            self.entries.append(json.loads(line))
            except Exception:
                self.entries = []

    def log(self, entry):
        with self.lock:
            self.entries.append(entry)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

    def entries_for(self, key):
        with self.lock:
            return [e for e in self.entries if e.get("key") == key]

    @property
    def open_tickets(self):
        with self.lock:
            return {e["ticket"] for e in self.entries
                    if e.get("event") == "entry" and not e.get("closed")}

    def live_stats(self):
        """Per key: {trades, wins, net, pf} from CLOSED journal entries."""
        with self.lock:
            out = {}
            for e in self.entries:
                if e.get("event") != "exit":
                    continue
                s = out.setdefault(e["key"], {"trades": 0, "wins": 0,
                                              "gw": 0.0, "gl": 0.0, "net": 0.0})
                s["trades"] += 1
                s["net"] += e["pnl"]
                if e["pnl"] > 0:
                    s["wins"] += 1
                    s["gw"] += e["pnl"]
                else:
                    s["gl"] += -e["pnl"]
            for s in out.values():
                s["win"] = s["wins"] / s["trades"] if s["trades"] else 0.0
                s["pf"] = (s["gw"] / s["gl"]) if s["gl"] > 0 else (
                    None if s["gw"] > 0 else 0.0)
                del s["gw"], s["gl"]
            return out
