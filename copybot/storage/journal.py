"""Журнал сделок в CSV (открывается в Excel / Google Sheets)."""
import csv
import os
import time

HEADER = ["time", "mode", "action", "reason", "wallet", "mint", "sol", "pnl_sol", "tx"]


class Journal:
    def __init__(self, path: str, dry_run: bool):
        self.path, self.mode = path, "TEST" if dry_run else "LIVE"

    def write(self, action: str, reason: str, wallet: str, mint: str, sol: float,
              pnl: float | None = None, tx: str | None = None):
        new = not os.path.exists(self.path)
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(HEADER)
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), self.mode, action, reason, wallet, mint,
                        f"{sol:.6f}", "" if pnl is None else f"{pnl:.6f}", tx or ""])
