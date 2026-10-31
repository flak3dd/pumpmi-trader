"""Build done-trade chart data (paper) from ledger fills. No network.
Usage: python -m src.trade_charts  -> writes out/trade_charts.json"""
import csv, json, re, os
from datetime import datetime
from zoneinfo import ZoneInfo
TZ = ZoneInfo("Australia/Melbourne")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILLS = os.path.join(ROOT, "out/ledger_live/fills.csv")
OUT = os.path.join(ROOT, "out/trade_charts.json")

def hm(ts, sec=True): return datetime.fromtimestamp(ts, TZ).strftime("%H:%M:%S" if sec else "%H:%M")
def split_top(s):
    out, d, cur = [], 0, ""
    for ch in s:
        d += ch == "("; d -= ch == ")"
        if ch == "," and d == 0: out.append(cur.strip()); cur = ""
        else: cur += ch
    if cur.strip(): out.append(cur.strip())
    return out

def gates_buy(r):
    g = []
    m = re.match(r"^(v\d+) PASS:\s*(.*)$", r)
    if m: g.append({"name": f"{m.group(1)} checklist", "detail": "all hard gates passed", "pass": True}); r = m.group(2)
    for p in split_top(r):
        if p.startswith("curve"):
            mm = re.search(r"([\d.]+)->([\d.]+)%", p)
            g.append({"name": "P1 curve rise", "detail": p + (" (in 25–85% band, rising)" if mm else ""), "pass": True})
        elif p.startswith("flow"):
            g.append({"name": "P2 flow" + (" (fallback)" if "fallback" in p else ""), "detail": p, "pass": True})
        elif p.startswith("social") or p.startswith("no social"):
            g.append({"name": "P3 social", "detail": p, "pass": True})
        elif re.match(r"\d+ mentions", p):
            g.append({"name": "P3 mentions", "detail": p, "pass": True})
        elif p.startswith("top10"):
            g.append({"name": "Holders top10", "detail": p.replace("None%", "n/a (not blocking)"), "pass": True})
        elif p.startswith("dev"):
            g.append({"name": "Dev wallet", "detail": p.replace("None%", "n/a (not blocking)"), "pass": True})
        elif p.startswith("v7 SIM") or p.startswith("v6 SIM"):
            g.append({"name": "Similarity gate", "detail": p, "pass": "skip" not in p.lower()})
        else:
            g.append({"name": "Note", "detail": p, "pass": True})
    return g

def rule_for(r):
    if r.startswith("scale-out"): return "Scale-out: sell half at +25%+ target"
    if r.startswith("trail"): return "Trailing stop: gave back trail % from peak"
    if r.startswith("emergency"): return "Emergency exit: spot dropped at least 10% in one check"
    if r.startswith("break-even"): return "Break-even stop after +15% net of paper fees"
    if r.startswith("profit lock"): return "Profit lock: pre-scale net gave back 6 points, exit above zero"
    if r.startswith("vol stop"): return "Volatility stop: tighter than the hard −15% cap"
    if r.startswith("flow exit"): return "Exit: post-entry net SOL flow reversed"
    if r.startswith("stop"): return "Hard stop: −15% net of paper fees"
    if r.startswith("time"): return "Time exit: soft 12m flat / hard 40m"
    if "curve" in r: return "Flatten near curve completion (≥95%)"
    return "Exit rule"

def label_of(kind, r):
    if kind == "buy": return "Buy"
    m = re.search(r"([+-]\d+)%", r)
    pct = m.group(1) + "%" if m else ""
    if kind == "scale": return f"½ {pct}"
    if r.startswith("trail"): return f"Trail {pct}"
    if r.startswith("stop"): return f"Stop {pct}"
    if r.startswith("time"): return f"Time {pct}"
    return f"Sell {pct}"

def build():
    rows = list(csv.DictReader(open(FILLS)))
    by = {}
    for r in rows: by.setdefault(r["symbol"], []).append(r)
    trades = {}
    for sym, fs in by.items():
        bi = max((i for i, f in enumerate(fs) if f["side"] == "BUY"), default=None)
        if bi is None: continue
        buy, sells = fs[bi], [f for f in fs[bi+1:] if f["side"] == "SELL"]
        if not sells or sells[-1]["reason"].startswith("scale-out"): continue
        t0, e = float(buy["ts"]), float(buy["price"])
        sig = [{"id": "b", "kind": "buy", "ts": t0, "mcap": e, "usd": float(buy["usd"]), "pnl": None, "reason": buy["reason"]}]
        for k, s in enumerate(sells):
            kind = "scale" if s["reason"].startswith("scale-out") else "sell"
            sig.append({"id": f"s{k}", "kind": kind, "ts": float(s["ts"]), "mcap": float(s["price"]), "usd": float(s["usd"]), "pnl": float(s["pnl"] or 0), "reason": s["reason"]})
        pts = [(x["ts"], x["mcap"], x["id"]) for x in sig]
        pk = re.search(r"peak \+([\d.]+)%", sells[-1]["reason"])
        if pk:
            prev = pts[-2]; pts.insert(-1, ((prev[0] + pts[-1][0]) / 2, e * (1 + float(pk.group(1)) / 100), None))
        tN = pts[-1][0]; span = max(tN - t0, 1)
        N = 16
        idx_of = {}
        # assign each key point a unique index proportional to time
        keyidx = []; prev = -1; K = len(pts)
        for n, (ts, v, sid) in enumerate(pts):
            i = 0 if n == 0 else (N - 1 if n == K - 1 else round((ts - t0) / span * (N - 1)))
            i = min(max(i, prev + 1), N - 1 - (K - 1 - n)); prev = i
            keyidx.append((i, v, sid))
        keyidx.sort()
        vals = [None] * N
        for i, v, sid in keyidx:
            vals[i] = v
            if sid: idx_of[sid] = i
        for a, b in zip(keyidx, keyidx[1:]):
            for j in range(a[0] + 1, b[0]):
                vals[j] = a[1] + (b[1] - a[1]) * (j - a[0]) / (b[0] - a[0])
        for j in range(N):
            if vals[j] is None: vals[j] = vals[j-1] if j and vals[j-1] is not None else next(v for v in vals if v is not None)
        labs = [""] * N
        for j in (0, N // 3, 2 * N // 3, N - 1): labs[j] = hm(t0 + span * j / (N - 1), span < 300)
        series = [{"label": labs[j], "value": round(vals[j])} for j in range(N)]
        out_sig = []
        for x in sig:
            out_sig.append({"id": x["id"], "kind": x["kind"], "at": idx_of[x["id"]], "label": label_of(x["kind"], x["reason"]),
                            "t": hm(x["ts"]), "mcap": round(x["mcap"]), "usd": round(x["usd"], 2),
                            "pnl": None if x["pnl"] is None else round(x["pnl"], 2), "reason": x["reason"],
                            "rule": None if x["kind"] == "buy" else rule_for(x["reason"])})
        tot = round(sum(x["pnl"] or 0 for x in sig), 2)
        trades[sym] = {"series": series, "signals": out_sig, "gatesBuy": gates_buy(buy["reason"]),
                       "gatesSell": [{"name": rule_for(s["reason"]), "detail": s["reason"], "pass": True} for s in sells],
                       "entry": round(e), "exitPnl": tot, "exitPp": round(tot / sig[0]["usd"] * 100, 1), "dur": round(span / 60, 1), "_exitTs": tN}
    return trades

if __name__ == "__main__":
    t = build(); json.dump(t, open(OUT, "w"), separators=(",", ":"))
    print(len(t), "trades ->", OUT)

def page_payload(trades, only=None, cap=40):
    """Compact form embedded in the Page's DATA.trades:
    {sym:{e:entry,pnl,pp,d:durMin,v:[mcap x16],l:{idx:"HH:MM"},g:[[k,at,t,mc,usd,pnl,reason]]}}
    k: b=buy, h=scale(half), s=final sell. Gates/rules are parsed from reason in the UI."""
    # Default: ALL closed trades (most recent `cap` by exit), plus any `only` symbols (board cards) always kept.
    keys = sorted(trades, key=lambda k: trades[k].get("_exitTs", 0), reverse=True)[:cap]
    keep = set(keys) | (set(only) & set(trades) if only else set())
    out = {}
    for sym in sorted(keep, key=lambda k: trades[k].get("_exitTs", 0), reverse=True):
        x = trades[sym]
        out[sym] = {"e": x["entry"], "pnl": x["exitPnl"], "pp": x["exitPp"], "d": x["dur"],
                    "v": [p["value"] for p in x["series"]],
                    "l": {str(i): p["label"] for i, p in enumerate(x["series"]) if p["label"]},
                    "g": [[{"buy": "b", "scale": "h", "sell": "s"}[s["kind"]], s["at"], s["t"], s["mcap"], s["usd"], s["pnl"], s["reason"]] for s in x["signals"]]}
    return out
