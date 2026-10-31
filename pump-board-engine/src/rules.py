"""Pure rule checks. No I/O. Each returns (ok: bool, reason: str)."""
from dataclasses import dataclass, field

@dataclass
class CoinMetrics:
    mint: str
    symbol: str
    dev_wallet: str
    dev_pct: float
    bundle_pct: float
    curve_pct: float = 0.0
    dev_sold: bool = False
    honeypot: bool = False
    bundle_moved: bool = False
    bundle_move_step: int = 1

def check_dev(m, max_pct=10):
    return (m.dev_pct <= max_pct, f"dev holds {m.dev_pct}% > {max_pct}%" if m.dev_pct > max_pct else "dev ok")
def check_bundle(m, max_pct=25):
    return (m.bundle_pct <= max_pct, f"bundled {m.bundle_pct}% > {max_pct}%" if m.bundle_pct > max_pct else "bundle ok")
def check_known_rug(m, known_rug_devs):
    bad = m.dev_wallet in set(known_rug_devs)
    return (not bad, "same dev as known rug (skip forever)" if bad else "dev clean")
def check_flags(m):
    if m.honeypot: return False, "honeypot flag"
    if m.dev_sold: return False, "dev sold"
    return True, "flags ok"
def should_exit_bundle(m):
    return m.bundle_moved
def goal_hit(equity, bankroll, multiple=4.0):
    return equity >= bankroll * multiple
def social_ok(posts_15m, sentiment, min_posts=20, min_sent=0.1):
    if posts_15m < min_posts: return False, f"low X volume ({posts_15m})"
    if sentiment < min_sent: return False, f"weak sentiment ({sentiment:.2f})"
    return True, "social ok"

def entry_checks(m, known_rug_devs, rules):
    """Returns (ok, reason, forever) — forever=True means add to do_not_rebuy."""
    ok, r = check_known_rug(m, known_rug_devs)
    if not ok: return False, r, True
    for fn in (lambda: check_dev(m, rules.get("max_dev_pct", 10)),
               lambda: check_bundle(m, rules.get("max_bundle_pct", 25)),
               lambda: check_flags(m)):
        ok, r = fn()
        if not ok: return False, r, False
    return True, "holder checks pass", False
