"""Holder / dev / bundle metrics. Fixture now; Helius stub for later."""
import json, os
from pathlib import Path
from .rules import CoinMetrics

class FixtureHolders:
    def __init__(self, path):
        self.coins = json.loads(Path(path).read_text())
        self.by_mint = {c["mint"]: c for c in self.coins}
    def new_coins(self): return [c for c in self.coins]
    def metrics(self, mint):
        c = self.by_mint[mint]
        return CoinMetrics(**{k: c[k] for k in CoinMetrics.__dataclass_fields__ if k in c})
    def price_path(self, mint): return self.by_mint[mint].get("price_path", [1.0])

class HeliusHolders:
    """TODO: implement with Helius DAS/RPC (HELIUS_API_KEY):
    - getTokenLargestAccounts(mint) -> top holders; dev_pct = dev ATA / supply
    - bundle_pct: sum of wallets funded in same slot as create tx (parse signatures for create slot)
    - bundle_moved: websocket accountSubscribe on bundle wallets
    - dev_sold: dev ATA balance decreased since launch
    - honeypot: pump.fun bonding-curve tokens are sellable; post-graduation check mint/freeze authority"""
    def __init__(self):
        if not os.environ.get("HELIUS_API_KEY"): raise RuntimeError("HELIUS_API_KEY not set")
    def new_coins(self): raise NotImplementedError
    def metrics(self, mint): raise NotImplementedError
    def price_path(self, mint): raise NotImplementedError

def make_holders(cfg):
    s = cfg["holders"]["source"]
    if s == "fixture": return FixtureHolders(cfg["holders"]["fixture_path"])
    if s == "helius": return HeliusHolders()
    raise ValueError(s)
