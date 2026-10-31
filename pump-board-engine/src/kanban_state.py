import json, time
from pathlib import Path
class Board:
    def __init__(self): self.cards = {}
    def put(self, symbol, column, **info):
        assert column in ("checking", "holding", "done")
        self.cards[symbol] = {"symbol": symbol, "column": column, **{**self.cards.get(symbol, {}), **info, "column": column}}
    def state(self, equity, bankroll, mode):
        cols = {c: [v for v in self.cards.values() if v["column"] == c] for c in ("checking", "holding", "done")}
        return {"ts": time.time(), "mode": mode, "bankroll": bankroll, "equity": round(equity, 2),
                "multiple": round(equity / bankroll, 3), "columns": cols}
    def write(self, path, **kw):
        p = Path(path); p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.state(**kw), indent=1)); return p
