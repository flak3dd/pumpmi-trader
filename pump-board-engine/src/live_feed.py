"""Continuous PAPER feed on live market data. pump.fun public API (new coins + live mcap prices),
Solana public RPC (top-holder concentration, best-effort), LunarCrush live social. NEVER signs/sends."""
import json, os, time, signal, traceback, urllib.request
from pathlib import Path
from .config import load
from .strategy_config import effective_strategy
from .aggregators.lunarcrush import LunarCrushClient, LunarCrushError
from .aggregators.fuse import fuse, PLATFORMS
from .ledger import Ledger
from . import similarity as SIM
from .kanban_state import Board

PF = "https://frontend-api-v3.pump.fun"; RPC = "https://api.mainnet-beta.solana.com"
UA = {"User-Agent": "Mozilla/5.0 (PumpBoardEngine paper)", "Accept": "application/json"}
# Strategy v7 (2026-10-31): hardened similarity, see src/similarity.py (17-dim feats, 5 labeled/2W/2L gate,
#  cosine score>=0.05 AND kNN majority to boost; both negative to skip; else v5). Boost capped min(cash,8% eq,$500);
#  only trail relaxed (12->14.4%), stop stays -15% by choice. Prior v6 (2026-10-31): sentiment-affirmed buys pass a cosine-similarity gate vs historical paper exits
#  (out/feature_history.jsonl); score<0 -> SKIP, else BUY 3x size + trail giveback x1.2 (12->14.4%) for that position.
#  Thin history (<3 labeled w/ sentiment) -> inconclusive -> v5 buy. LC rate-limit/neutral -> v5 path, no boost.
# was v5 (loosened: curve 25-85, rise>=0, rescan seen coins every 3m, LC 429 -> half size)
# was v4 (2026-10-31, research brief top 5). Paper only.
#  P0 dump block: top10 (ex-curve) >30% OR dev(creator) >5% OR top-5 cluster (early-bundle proxy) >25%; nulls logged, never block alone
#  P1 curve/mcap cap, then independently observe live curve deltas at short, medium, and long watch tiers before entry gates.
#  P2 flow: unique buyers>=15/5m, buy/sell SOL>=1.3, top buyer<=20% vol when trade data exists; pump.fun trades API
#     is unavailable (400/410) -> logged fallback: replies>=2 OR curve rising >= flow_fallback_rise_pts over the watch
#  P3 social: LunarCrush sentiment if available; 429/backoff -> SKIP (no half-size); no topic -> neutral pass
#  Exits: SL -15%, scale out half at +25% then trail 12% from peak, soft 12m if <+5%, hard 40m, flatten at curve>=95%
#     or if creator token balance drops (dev sell). Size 1.5% equity clamp $130-160, max 6 open, halve after 3 straight losses.
STRAT = dict(min_curve_pct=25, max_curve_pct=85, max_mcap_usd=250_000, p0_top10_max=30, p0_dev_max=5, p0_bundle_max=25,
             watch_short_s=90, watch_medium_s=180, watch_long_s=300, rise_min_pts=0.0, rescan_s=180, flow_min_buyers=15, flow_min_bs_ratio=1.3, flow_max_whale_pct=20,
             p2_fallback_replies=2, flow_fallback_rise_pts=1, p3_min_sentiment=50, p3_min_mentions=100,
             size_pct_equity=1.5, size_min=130, size_max=160, max_open=6, loss_streak_halve=3,
             stop_loss_pct=-15, scale_out_pct=25, trail_giveback_pct=12, flat_exit_s=720, flat_exit_below_pct=5,
             hard_exit_s=2400, flatten_curve_pct=95, v6_size_mult=3, v6_trail_relax=1.20, v6_min_hist=5, v7_boost_eq_pct=8, v7_boost_usd_max=500)

def http(url, body=None, timeout=12):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                 headers={**UA, **({"Content-Type": "application/json"} if body else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r: return json.load(r)

def load_env(p=".env"):
    if Path(p).exists():
        for l in Path(p).read_text().splitlines():
            if "=" in l and not l.startswith("#"):
                k, v = l.split("=", 1)
                if v.strip(): os.environ.setdefault(k.strip(), v.strip())

class Feed:
    def __init__(self, cfg, strategy_config_path=None):
        assert cfg["mode"] == "paper", "live feed is PAPER ONLY"
        self.cfg = cfg; self.bank = cfg["bankroll_usd"]; self.cash = self.bank
        self.pos = {}; self.seen = {}; self.board = Board(); self.log = []
        self.ledger = Ledger("out/ledger_live"); self.lc = LunarCrushClient()
        self.lc_backoff = 0; self.stats = dict(ticks=0, scanned=0, buys=0, wins=0, losses=0, skips=0, realized=0.0)
        self.lc_cache = {}; self.traded = set(); self.watch = {}; self.loss_streak = 0
        self.strategy_config_path = strategy_config_path; self.strategy = effective_strategy(STRAT, strategy_config_path)
        self.restore()
    def refresh_strategy(self):
        """Read the local slider configuration once per tick for newly discovered coins."""
        self.strategy = effective_strategy(STRAT, self.strategy_config_path)
    def _new_watch_periods(self):
        return {
            "short": {"duration_s": self.strategy["watch_short_s"], "elapsed_s": 0, "status": "watching"},
            "medium": {"duration_s": self.strategy["watch_medium_s"], "elapsed_s": 0, "status": "watching"},
            "long": {"duration_s": self.strategy["watch_long_s"], "elapsed_s": 0, "status": "watching"},
        }
    @staticmethod
    def _copy_watch_periods(watch_periods):
        return {tier: dict(checkpoint) for tier, checkpoint in watch_periods.items()}
    @staticmethod
    def _watch_complete(w):
        return all(checkpoint["status"] == "observed" for checkpoint in w["watch_periods"].values())
    @staticmethod
    def _watch_reason(w):
        parts = []
        for tier in ("short", "medium", "long"):
            checkpoint = w["watch_periods"][tier]
            label = f"{tier} {checkpoint['duration_s']}s {checkpoint['status']}"
            if checkpoint["status"] == "observed": label += f" ({checkpoint['curve_delta']:+.1f}pts)"
            parts.append(label)
        return "watch tiers: " + "; ".join(parts)
    def restore(self):
        """Rebuild cash/realized/open positions from the live ledger so restarts keep stats continuous."""
        import csv
        f = Path("out/ledger_live/fills.csv")
        if not f.exists(): return
        for row in csv.DictReader(f.open()):
            k = row["symbol"]; usd = float(row["usd"]); self.traded.add(k)
            if row["side"] == "BUY":
                self.cash -= usd; self.stats["buys"] += 1
                self.pos[k] = dict(mint=None, usd=usd, entry=float(row["price"]) or 1, peak=float(row["price"]) or 1,
                                   t=float(row["ts"]), creator=None, key=k)
            elif row["side"] == "SELL":
                pnl = float(row["pnl"]); self.cash += usd; self.stats["realized"] += pnl
                if row.get("reason", "").startswith("scale-out"):
                    if k in self.pos: self.pos[k]["usd"] /= 2; self.pos[k]["scaled"] = True
                    continue
                self.stats["wins" if pnl > 0 else "losses"] += 1; self.pos.pop(k, None)
                self.loss_streak = 0 if pnl > 0 else self.loss_streak + 1
        try:
            bj = json.loads(Path("out/board.json").read_text())
            mints = {c["symbol"]: c.get("mint") for col in bj.get("columns", {}).values() for c in col}
        except Exception: mints = {}
        for k in list(self.pos):
            self.pos[k]["mint"] = mints.get(k)
            if not self.pos[k]["mint"]: self.pos.pop(k)  # can't price it; leave as-is in ledger
            else: self.board.put(k, "holding", verdict="BUY", reason="restored from ledger", pnl=0, pnl_pct=0)
        # orphan BUYs from earlier restarts (no SELL, not on board) are ignored; cash = bank + realized - open
        buys = {r["key"]: r for r in SIM.load() if r.get("side") == "BUY"}
        for k, p in self.pos.items():
            b = buys.get(k, {}); p["boost"] = b.get("boost", False); p["sentiment"] = b.get("sentiment_0_100")
            if b.get("trail_giveback_pct"): p["trail_giveback_pct"] = b["trail_giveback_pct"]
        self.cash = self.bank + self.stats["realized"] - sum(p["usd"] for p in self.pos.values())
        self.restored = list(self.pos)
    def ev(self, msg):
        line = time.strftime("%H:%M:%S") + " " + msg; print(line, flush=True); self.log = ([line] + self.log)[:60]
    def mcap(self, mint):
        return float(http(f"{PF}/coins-v2/{mint}").get("usd_market_cap") or 0)
    def holders(self, c):
        try:
            r = http(RPC, {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [c["mint"]]})
            accts = [float(a["uiAmount"] or 0) for a in r["result"]["value"]]
            sup = float(c.get("total_supply") or 1e15) / 10 ** int(c.get("base_decimals", 6))
            rest = sorted(accts, reverse=True)[1:]  # drop largest = bonding curve
            t10 = round(100 * sum(rest[:10]) / sup, 1)
            return (round(100 * rest[0] / sup, 1) if rest else 0), round(100 * sum(rest[:5]) / sup, 1), t10, sup
        except Exception: return None, None, None, None
    def dev_pct(self, c, sup):
        """Creator's token balance as % supply via getTokenAccountsByOwner. None if unknown."""
        try:
            if not c.get("creator") or not sup: return None
            r = http(RPC, {"jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                           "params": [c["creator"], {"mint": c["mint"]}, {"encoding": "jsonParsed"}]})
            amt = sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0) for a in r["result"]["value"])
            return round(100 * amt / sup, 2)
        except Exception: return None
    def social(self, sym):
        if sym in self.lc_cache: return self.lc_cache[sym]
        if time.time() < self.lc_backoff: return "RATE"
        try: snap = fuse(sym, self.lc.bundle(sym), PLATFORMS).to_dict()
        except LunarCrushError as e:
            s = str(e)
            if s.startswith("429") or "403" in s[:4]: self.lc_backoff = time.time() + 60; self.ev(f"LunarCrush {s[:3]} -> backoff 60s")
            snap = None
            if s.startswith("429"): return "RATE"
        except Exception: snap = None
        self.lc_cache[sym] = snap; return snap
    def evaluate(self, c, first=True):
        sym = (c.get("symbol") or "?")[:10].upper(); mint = c["mint"]; key = f"{sym}·{mint[:4]}"; E = self.strategy
        if first: self.stats["scanned"] += 1
        mc = float(c.get("usd_market_cap") or 0); curve = min(100, round(100 * mc / 69000, 1))
        reps = c.get("reply_count", 0) or 0
        reason = None; forever = False
        if c.get("creator") in self.ledger.do_not_rebuy: reason, forever = "same dev as rug -> skip forever", True
        elif key in self.traded: reason = "already traded this coin (no re-entry)"
        elif not (E["min_curve_pct"] <= curve <= E["max_curve_pct"] and mc <= E["max_mcap_usd"]):
            reason = f"P1 curve {curve}% not in {E['min_curve_pct']}-{E['max_curve_pct']}% / mcap ${mc:,.0f}"
        if reason: return self.skip(key, reason, forever, c)
        # P0 holder dump block
        top, bund, t10, sup = self.holders(c); dev = self.dev_pct(c, sup)
        old_card = self.board.cards.get(key)
        ts_seen = old_card["ts_seen"] if old_card and "ts_seen" in old_card else time.time()
        info = dict(mint=mint, name=c.get("name"), mcap=round(mc), curve_pct=curve, top1_pct=top, top10_pct=t10, dev_pct=dev,
                    bundle_pct=bund, replies=reps, ts_seen=ts_seen)
        self.board.put(key, "checking", **info)
        nulls = [n for n, v in (("top10", t10), ("dev", dev), ("bundle", bund)) if v is None]
        if nulls: self.ev(f"P0 {key}: missing {','.join(nulls)} (not blocking on nulls)")
        if t10 is not None and t10 > E["p0_top10_max"]: reason = f"P0 top10 {t10}% > {E['p0_top10_max']}%"
        elif dev is not None and dev > E["p0_dev_max"]: reason = f"P0 dev {dev}% > {E['p0_dev_max']}%"
        elif bund is not None and bund > E["p0_bundle_max"]: reason = f"P0 top-5 cluster {bund}% > {E['p0_bundle_max']}%"
        if reason: return self.skip(key, reason, False, c)
        # P1 rising-curve watch. Each token keeps its initial slider settings, so a
        # later slider save cannot make an in-progress long observation complete early.
        w = self.watch.get(mint)
        if w is None:
            periods = self._new_watch_periods(); started = time.time()
            w = dict(t=started, curve=curve, baseline_curve=curve, key=key, watch_periods=periods)
            self.watch[mint] = w
            self.board.put(key, "checking", reason=self._watch_reason(w), watch_started_ts=started, watch_periods=self._copy_watch_periods(periods))
            self.ev(f"WATCH {key} curve {curve}% ({self._watch_reason(w)})"); return
        long_duration = w["watch_periods"]["long"]["duration_s"]
        # A direct/repeated evaluate call must not bypass the long tier; only recheck
        # records observations, and it does so from the fetched market payload.
        if time.time() - w["t"] < long_duration or not self._watch_complete(w):
            self.board.put(key, "checking", reason=self._watch_reason(w), watch_periods=self._copy_watch_periods(w["watch_periods"]))
            return
        rise = curve - w["curve"]
        if rise < E["rise_min_pts"]: self.watch.pop(mint, None); return self.skip(key, f"P1 curve not rising over watch ({w['curve']}%->{curve}%)", False, c)
        # P2 flow (trade API unavailable -> logged fallback)
        flow = None  # pump.fun trades endpoint returns 400/410 for public clients; no per-trade data
        p2 = False; p2why = "flow unavailable"
        if flow is None:
            p2 = reps >= E["p2_fallback_replies"] or rise >= E["flow_fallback_rise_pts"]
            p2why = f"flow n/a -> fallback ({reps} replies, curve +{rise:.1f}pts)"
        if not p2: self.watch.pop(mint, None); return self.skip(key, f"P2 {p2why}", False, c)
        # P3 social
        snap = self.social(sym)
        half = False
        if snap == "RATE": snap = None; half = True  # v5: rate-limited -> neutral pass at half size
        if snap is not None and snap["mention_volume"] >= E["p3_min_mentions"]:
            if snap["sentiment_0_100"] < E["p3_min_sentiment"]:
                self.watch.pop(mint, None); return self.skip(key, f"P3 sentiment {snap['sentiment_0_100']}/100", False, c)
            p3why = f"social {snap['sentiment_0_100']}/100"
        else: p3why = "LunarCrush rate-limited -> half size" if half else "no social topic (neutral)"
        self.watch.pop(mint, None)
        if len(self.pos) >= E["max_open"]: return self.skip(key, f"max {E['max_open']} open", False, c)
        usd = max(E["size_min"], min(E["size_max"], self.equity() * E["size_pct_equity"] / 100))
        if self.loss_streak >= E["loss_streak_halve"]: usd /= 2
        if half: usd /= 2
        usd = min(usd, self.cash)
        if usd < 20: return self.skip(key, "no cash", False, c)
        sentiment_buy = isinstance(snap, dict) and not half and snap["mention_volume"] >= E["p3_min_mentions"] \
            and snap["sentiment_0_100"] >= E["p3_min_sentiment"]
        social_values = snap if isinstance(snap, dict) else {}
        feat = dict(key=key, mint=mint, curve_pct=curve, mcap=round(mc), replies=reps, top10_pct=t10, dev_pct=dev, bundle_pct=bund,
                    rise_pts=round(rise, 2), sentiment_0_100=social_values.get("sentiment_0_100") if sentiment_buy else None,
                    mention_volume=social_values.get("mention_volume") if sentiment_buy else None, sentiment_buy=sentiment_buy)
        boost = False; trail = E["trail_giveback_pct"]; v6why = ""
        feat["size_pct_equity"] = round(100 * usd / max(self.equity(), 1), 2)
        ct = c.get("created_timestamp"); feat["age_s"] = round(time.time() - ct / 1000) if ct else None
        if sentiment_buy:
            d = SIM.decide(feat); dec = str(d["decision"])
            tag = f"score={d['score']} knn={d['knn_win']}/{d['knn_n']} n_hist={d['n_hist']} ({d['nw']}W/{d['nl']}L)"
            if dec == "skip": return self.skip(key, f"v7 SIM skip {tag}", False, c)
            if dec == "boost":
                cap = min(self.cash, self.equity() * E["v7_boost_eq_pct"] / 100, E["v7_boost_usd_max"])
                usd = min(cap, usd * E["v6_size_mult"]); boost = True
                trail = round(E["trail_giveback_pct"] * E["v6_trail_relax"], 2)
                v6why = f", v7 SIM boost {tag} size ${usd:.0f} trail {trail}%"
                self.ev(f"v7 SIM BOOST {key} {tag} -> size ${usd:.0f} (cap ${cap:.0f}) trail {trail}%")
            else:
                v6why = f", v7 SIM {dec} {tag}"
                self.ev(f"v7 SIM {dec.upper()} {key} {tag} -> v5 size ${usd:.0f} trail {trail}%")
        why = f"v7 PASS: curve {w['curve']}->{curve}%, {p2why}, {p3why}, top10 {t10}%, dev {dev}%{v6why}"
        self.traded.add(key); self.cash -= usd
        self.pos[key] = dict(mint=mint, usd=usd, entry=mc or 1, peak=mc or 1, t=time.time(), creator=c.get("creator"),
                             dev0=dev, sup=sup, scaled=False, boost=boost, trail_giveback_pct=trail,
                             sentiment=feat["sentiment_0_100"], feat=feat)
        SIM.append({**feat, "side": "BUY", "usd": round(usd, 2), "boost": boost, "trail_giveback_pct": trail})
        self.ledger.fill(key, "BUY", usd, mc, 0, why); self.stats["buys"] += 1
        self.board.put(key, "holding", verdict="BUY", reason=why, pnl=0, pnl_pct=0); self.ev(f"BUY  {key} ${usd:.0f} @ mcap ${mc:,.0f} ({why})")
    def skip(self, key, reason, forever, c, keep_watch=False):
        if not keep_watch: self.watch.pop(c["mint"], None)
        self.stats["skips"] += 1; self.ledger.skip(key, reason, forever, c.get("creator"))
        self.board.put(key, "done", verdict="SKIP", reason=reason); self.ev(f"SKIP {key} {reason}")
    def recheck(self):
        for mint, w in list(self.watch.items()):
            age = time.time() - w["t"]
            periods = w["watch_periods"]
            long_duration = periods["long"]["duration_s"]
            if age > long_duration + 120:
                self.watch.pop(mint); self.ev(f"WATCH expired {w['key']} after {age:.0f}s ({self._watch_reason(w)})"); continue
            due = [tier for tier in ("short", "medium", "long")
                   if periods[tier]["status"] == "watching" and age >= periods[tier]["duration_s"]]
            if not due: continue
            try: c = http(f"{PF}/coins-v2/{mint}")
            except Exception as e: self.ev(f"recheck {w['key']} fetch failed {type(e).__name__}"); continue
            if c.get("complete"): self.watch.pop(mint); continue
            mc = float(c.get("usd_market_cap") or 0)
            curve = min(100, round(100 * mc / 69000, 1))
            for tier in due:
                periods[tier].update(elapsed_s=round(max(0, age), 1), status="observed",
                                     curve_delta=round(curve - w["baseline_curve"], 1))
            self.board.put(w["key"], "checking", reason=self._watch_reason(w), watch_periods=self._copy_watch_periods(periods))
            self.ev(f"RECHECK {w['key']} after {age:.0f}s observed {','.join(due)} ({self._watch_reason(w)})")
            if self._watch_complete(w) and age >= long_duration:
                self.evaluate(c, first=False)
    def close(self, key, mc, why, frac=1.0):
        p = self.pos[key]; part = p["usd"] * frac; val = part * mc / p["entry"]; pnl = val - part; self.cash += val
        self.stats["realized"] += pnl
        if frac >= 1:
            self.pos.pop(key); self.stats["wins" if pnl > 0 else "losses"] += 1
            self.loss_streak = 0 if pnl > 0 else self.loss_streak + 1
            if pnl < -0.5 * part: self.ledger.add_dnr(p["creator"])
            f = p.get("feat") or {"key": key, "mint": p.get("mint"), "sentiment_0_100": p.get("sentiment")}
            SIM.append({**f, "side": "EXIT", "exit_mcap": round(mc), "boost": p.get("boost", False),
                        "pnl_pct": round(100 * (mc / p["entry"] - 1), 1), "reason": why})
        else: p["usd"] -= part; p["scaled"] = True; why = "scale-out " + why
        self.ledger.fill(key, "SELL", val, mc, pnl, why)
        if frac >= 1: self.board.put(key, "done", verdict="EXIT", reason=why, pnl=round(pnl, 2), pnl_pct=round(100 * pnl / part, 1), mcap=round(mc))
        self.ev(f"EXIT {key} {why} pnl {pnl:+.2f}")
    def manage(self):
        E = self.strategy
        for k, p in list(self.pos.items()):
            try: mc = self.mcap(p["mint"])
            except Exception: continue
            p["last"] = mc; p["peak"] = max(p["peak"], mc); chg = 100 * (mc / p["entry"] - 1)
            curve = 100 * mc / 69000; age = time.time() - p["t"]
            self.board.put(k, "holding", pnl=round(p["usd"] * chg / 100, 2), pnl_pct=round(chg, 1), mcap=round(mc))
            dev_sold = False
            if p.get("dev0") and p.get("sup") and int(age) // 60 != p.get("devchk"):
                p["devchk"] = int(age) // 60; d = self.dev_pct({"creator": p["creator"], "mint": p["mint"]}, p["sup"])
                dev_sold = d is not None and d < p["dev0"] * 0.8
            if chg <= E["stop_loss_pct"]: self.close(k, mc, f"stop loss {chg:.0f}%")
            elif curve >= E["flatten_curve_pct"]: self.close(k, mc, f"flatten: curve {curve:.0f}% (pre-graduation) {chg:+.0f}%")
            elif dev_sold: self.close(k, mc, f"flatten: dev sold {chg:+.0f}%")
            elif not p.get("scaled") and chg >= E["scale_out_pct"]: self.close(k, mc, f"half at {chg:+.0f}%", frac=0.5)
            elif p.get("scaled") and mc < p["peak"] * (1 - p.get("trail_giveback_pct", E["trail_giveback_pct"]) / 100):
                self.close(k, mc, f"trail {p.get('trail_giveback_pct', E['trail_giveback_pct'])}% {chg:+.0f}% (peak {100*(p['peak']/p['entry']-1):+.0f}%)")
            elif age > E["flat_exit_s"] and chg < E["flat_exit_below_pct"] and not p.get("scaled"): self.close(k, mc, f"time exit {E['flat_exit_s']//60}m flat {chg:+.0f}%")
            elif age > E["hard_exit_s"]: self.close(k, mc, f"time exit {E['hard_exit_s']//60}m {chg:+.0f}%")
    def equity(self): return self.cash + sum(p["usd"] * p.get("last", p["entry"]) / p["entry"] for p in self.pos.values())
    def write(self):
        done = self.board.cards
        if len(done) > 80:  # trim oldest done cards
            old = sorted([k for k, v in done.items() if v["column"] == "done"], key=lambda k: done[k].get("ts_seen", 0))[:len(done) - 80]
            for k in old: done.pop(k)
        st = self.board.state(self.equity(), self.bank, "paper")
        st.update(source="LIVE MARKET DATA · PAPER TRADES", stats={**self.stats, "realized": round(self.stats["realized"], 2)},
                  log=self.log, lunarcrush=self.lc.mode, updated=time.strftime("%Y-%m-%d %H:%M:%S %Z"))
        Path("out").mkdir(exist_ok=True); tmp = Path("out/board.json.tmp"); tmp.write_text(json.dumps(st, indent=1)); tmp.replace("out/board.json")
    def tick(self):
        self.refresh_strategy()
        self.stats["ticks"] += 1
        coins = http(f"{PF}/coins?offset=0&limit=30&sort=last_trade_timestamp&order=DESC&includeNsfw=false")
        try: coins += http(f"{PF}/coins?offset=0&limit=30&sort=market_cap&order=DESC&includeNsfw=false")
        except Exception: pass
        now = time.time()
        new = [c for c in coins if not c.get("complete") and c["mint"] not in self.watch
               and now - self.seen.get(c["mint"], 0) > self.strategy["rescan_s"]][:15]
        for c in new: self.seen[c["mint"]] = now; self.evaluate(c); self.manage()
        self.recheck(); self.manage(); self.write()

def main():
    load_env(); cfg = load(); cfg["mode"] = "paper"
    f = Feed(cfg); stop = False; delay = 10
    signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(SystemExit))
    f.ev(f"feed start PAPER strategy v7, lunarcrush={f.lc.mode}")
    while True:
        try: f.tick(); delay = 10
        except SystemExit: break
        except Exception as e:
            delay = min(delay * 2, 300); f.ev(f"tick error {type(e).__name__}: {str(e)[:120]} -> backoff {delay}s")
        time.sleep(delay)
if __name__ == "__main__": main()
