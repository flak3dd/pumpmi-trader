import csv, json, time
from pathlib import Path
class Ledger:
    def __init__(self, d):
        self.d = Path(d); self.d.mkdir(parents=True, exist_ok=True)
        self.dnr_f = self.d / "do_not_rebuy.json"
        self.do_not_rebuy = set(json.loads(self.dnr_f.read_text())) if self.dnr_f.exists() else set()
    def _row(self, name, row):
        f = self.d / f"{name}.csv"; new = not f.exists()
        with f.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(row)); new and w.writeheader(); w.writerow(row)
    def fill(self, symbol, side, usd, price, pnl=0.0, reason=""):
        self._row("fills", {"ts": time.time(), "symbol": symbol, "side": side, "usd": round(usd,2),
                            "price": price, "pnl": round(pnl,2), "reason": reason, "mode": "paper"})
    def skip(self, symbol, reason, forever=False, dev=None):
        self._row("skips", {"ts": time.time(), "symbol": symbol, "reason": reason, "forever": forever})
        if forever and dev: self.add_dnr(dev)
    def add_dnr(self, dev):
        self.do_not_rebuy.add(dev); self.dnr_f.write_text(json.dumps(sorted(self.do_not_rebuy), indent=1))
