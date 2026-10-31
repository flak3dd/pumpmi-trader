"""v7 similarity gate (PAPER ONLY). Compares a sentiment-affirmed candidate to labeled historical paper EXITs
in out/feature_history.jsonl and returns a decision: boost | skip | inconclusive | disagree.

Feature vector FEAT_VERSION=2 (17 dims, order is stable; append-only for future versions):
 0 curve_pct/100           1 min(mcap/250k,1)          2 min(replies/20,1)        3 top10_pct/100
 4 dev_pct/100             5 bundle_pct/100            6 clip(rise_pts/10,-1,1)   7 sentiment_0_100/100
 8 log1p(mentions)/10      9 min(size_pct_equity/10,1) 10 min(age_s/3600,1) (age-on-curve proxy)
11 min(log1p(mentions/max(mcap,1)*1e4)/5,1) (mention density)   12 sentiment*log-mentions interaction
13-16 missingness flags (1 if null): replies, top10, dev, bundle  -> nulls never silently look like zeros
Scores: (a) weighted mean cosine to winners minus losers; (b) cosine kNN majority vote.
Weights: (1+|pnl_pct|/25) * 0.5**(age_days/half_life), capped at w_cap per trade. Exits w/o sentiment ignored.
Gate: need >=min_labeled exits AND >=min_each winners AND losers, else inconclusive (never 3x, never skip).
"""
import json, math, time
from pathlib import Path
HIST = Path("out/feature_history.jsonl")
FEAT_VERSION = 2
SIM = dict(min_labeled=5, min_each=2, score_min=0.05, knn_k=5, half_life_days=7.0, w_cap=3.0)

def append(rec, path=None):
    p = Path(path or HIST); p.parent.mkdir(exist_ok=True)
    with p.open("a") as f: f.write(json.dumps({**rec, "feat_v": FEAT_VERSION, "ts": rec.get("ts", time.time())}) + "\n")

def load(path=None):
    p = Path(path or HIST)
    if not p.exists(): return []
    out = []
    for l in p.read_text().splitlines():
        try: out.append(json.loads(l))
        except Exception: pass
    return out

def vec(r):
    def g(k, d=0.0):
        v = r.get(k); return d if v is None else float(v)
    miss = lambda k: 1.0 if r.get(k) is None else 0.0
    mc, men, sen = g("mcap"), g("mention_volume"), g("sentiment_0_100") / 100
    lm = math.log1p(men) / 10
    return [g("curve_pct") / 100, min(mc / 250_000, 1), min(g("replies") / 20, 1), g("top10_pct") / 100,
            g("dev_pct") / 100, g("bundle_pct") / 100, max(-1, min(g("rise_pts") / 10, 1)), sen, lm,
            min(g("size_pct_equity") / 10, 1), min(g("age_s") / 3600, 1),
            min(math.log1p(men / max(mc, 1) * 1e4) / 5, 1), sen * lm,
            miss("replies"), miss("top10_pct"), miss("dev_pct"), miss("bundle_pct")]

def cos(a, b):
    na = math.sqrt(sum(x * x for x in a)); nb = math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0

def weight(r, now, C=SIM):
    age_d = max(0.0, (now - float(r.get("ts", now))) / 86400)
    return min(C["w_cap"], (1 + abs(r["pnl_pct"]) / 25) * 0.5 ** (age_d / C["half_life_days"]))

def decide(cand, hist=None, C=SIM, now=None):
    """-> dict(decision, score, knn_win, knn_n, n_hist, nw, nl)."""
    now = now or time.time()
    hist = [r for r in (load() if hist is None else hist) if r.get("side") == "EXIT"
            and r.get("sentiment_0_100") is not None and r.get("pnl_pct") is not None]
    win = [r for r in hist if r["pnl_pct"] > 0]; loss = [r for r in hist if r["pnl_pct"] <= 0]
    out = dict(decision="inconclusive", score=None, knn_win=0, knn_n=0, n_hist=len(hist), nw=len(win), nl=len(loss))
    if len(hist) < C["min_labeled"] or len(win) < C["min_each"] or len(loss) < C["min_each"]: return out
    v = vec(cand)
    def wmean(rs):
        ws = [weight(r, now, C) for r in rs]
        return sum(cos(v, vec(r)) * w for r, w in zip(rs, ws)) / (sum(ws) or 1)
    sc = round(wmean(win) - wmean(loss), 3)
    k = min(C["knn_k"], len(hist)); nn = sorted(hist, key=lambda r: -cos(v, vec(r)))[:k]
    kw = sum(1 for r in nn if r["pnl_pct"] > 0); out.update(score=sc, knn_win=kw, knn_n=k)
    a_buy, b_buy = sc >= C["score_min"], kw * 2 > k
    a_sell, b_sell = sc < 0, kw * 2 < k
    out["decision"] = "boost" if a_buy and b_buy else "skip" if a_sell and b_sell else "disagree"
    return out

def score(cand, min_n=None):  # back-compat
    d = decide(cand); return (None if d["decision"] == "inconclusive" else d["score"]), d["nw"], d["nl"]
