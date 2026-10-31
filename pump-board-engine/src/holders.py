"""Holder / dev / bundle metrics. Fixture now; Helius stub for later.

The live paper feed's concentration helper drops known bonding-curve token
accounts. It does not assume the largest balance is the curve. That proxy
hides a whale once the whale outsizes the unsold curve inventory.
"""
from __future__ import annotations

import json, math, os
from pathlib import Path
from .rules import CoinMetrics

# Same public coin fields the optimizer holder read already trusts.
_CURVE_TOKEN_KEYS = (
    "associated_bonding_curve", "associatedBondingCurve", "curve_token_account",
    "curveTokenAccount", "bonding_curve", "bondingCurve",
)


def curve_token_accounts(coin: dict) -> set:
    """Token-account addresses on the public coin record. Empty when none are named."""
    found = set()
    for key in _CURVE_TOKEN_KEYS:
        value = coin.get(key)
        if isinstance(value, str) and value.strip():
            found.add(value.strip())
    return found


def account_ui_amount(row: dict) -> float | None:
    """Finite ui amount. ``uiAmount`` null falls through to ``uiAmountString``, never to zero."""
    if not isinstance(row, dict):
        return None
    amount = row.get("uiAmount")
    if isinstance(amount, bool) or amount is None:
        text = row.get("uiAmountString")
        if not isinstance(text, str):
            return None
        try:
            amount = float(text)
        except ValueError:
            return None
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def largest_account_rows(rows) -> list | None:
    """``(address, ui_amount)`` from getTokenLargestAccounts. None if any row is unreadable."""
    if not isinstance(rows, list) or not rows:
        return None
    parsed = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        address = row.get("address")
        if not isinstance(address, str) or not address.strip():
            return None
        amount = account_ui_amount(row)
        if amount is None:
            return None
        parsed.append((address.strip(), amount))
    return parsed


def concentration(accounts: list, supply: float, curve_accounts: set) -> tuple | None:
    """``(top1_pct, top5_pct, top10_pct)`` after removing known curve token accounts.

    None when supply is unusable or none of the known curve addresses appear in
    ``accounts``. Callers skip. Dropping the largest balance instead would delete
    a whale that holds more than the curve and leave the curve inside the top 10.
    """
    try:
        supply = float(supply)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(supply) or supply <= 0:
        return None
    known = {item.strip() for item in curve_accounts if isinstance(item, str) and item.strip()}
    if not known:
        return None
    matched = False
    rest = []
    for address, amount in accounts:
        if address in known:
            matched = True
            continue
        rest.append(float(amount))
    if not matched:
        return None
    ranked = sorted(rest, reverse=True)

    def pct(count: int) -> float:
        return round(100.0 * sum(ranked[:count]) / supply, 1)

    top1 = round(100.0 * ranked[0] / supply, 1) if ranked else 0.0
    return top1, pct(5), pct(10)

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
