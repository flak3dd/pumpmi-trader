"""Pipeline: new coin -> holders -> social crawl -> sentiment -> verdict -> ledger/board."""
import json
from pathlib import Path
from . import rules as R, sentiment as S
from .ledger import Ledger
from .kanban_state import Board
from .live import execute_live

class Engine:
    def __init__(self, cfg, holders, social):
        self.cfg, self.h, self.s = cfg, holders, social
        self.rules = cfg["rules"]; self.bankroll = cfg["bankroll_usd"]; self.cash = self.bankroll
        self.ledger = Ledger(cfg["paths"]["ledger_dir"]); self.board = Board()
        kr = Path(cfg["paths"]["known_rugs"])
        self.known_rugs = set(json.loads(kr.read_text())) if kr.exists() else set()
        self.positions = {}  # symbol -> {usd, entry, mint, step}
        self.closed = False

    def _order(self, side, sym, usd, price, pnl=0, reason=""):
        if self.cfg["mode"] == "live": execute_live({"side": side, "symbol": sym, "usd": usd}, self.cfg)
        self.ledger.fill(sym, side, usd, price, pnl, reason)

    def equity(self):
        return self.cash + sum(p["usd"] * self._px(p) / p["entry"] for p in self.positions.values())
    def _px(self, p):
        path = self.h.price_path(p["mint"]); return path[min(p["step"], len(path) - 1)]

    def evaluate(self, coin):
        sym, mint = coin["symbol"], coin["mint"]
        self.board.put(sym, "checking", mint=mint)
        m = self.h.metrics(mint)
        ok, reason, forever = R.entry_checks(m, self.known_rugs | self.ledger.do_not_rebuy, self.rules)
        soc = self.s.fetch(sym, mint)
        sent = S.score_posts(soc["posts"]); vel = S.velocity(soc["counts"])
        info = dict(dev_pct=m.dev_pct, bundle_pct=m.bundle_pct, curve_pct=m.curve_pct, posts_15m=soc["posts_15m"],
                    sentiment=round(sent, 3), velocity=round(vel, 2), dev_sold=m.dev_sold, honeypot=m.honeypot)
        snap = soc.get("snapshot")
        if snap:
            sent = snap["sentiment_-1_1"]
            info.update(sentiment=sent, sentiment_0_100=snap["sentiment_0_100"], mentions=snap["mention_volume"],
                        interactions=snap["interactions"], trend=snap["trend"], social_signal=" + ".join(snap["reasons"][:3]),
                        by_platform=snap["by_platform"], headlines=snap["headlines"])
        if ok and snap:
            sc = self.cfg["social"]; sig = " + ".join(snap["reasons"][:3]) or "no platform signal"
            if snap["mention_volume"] < sc.get("min_mentions", 0):
                ok, reason = False, f"low mentions ({snap['mention_volume']}) | {sig}"
            elif snap["sentiment_0_100"] < sc.get("min_sentiment", 50):
                ok, reason = False, f"weak sentiment {snap['sentiment_0_100']}/100 | {sig}"
            else:
                reason = f"social ok {snap['sentiment_0_100']}/100, {snap['mention_volume']} mentions | {sig}"
        elif ok:
            ok, reason = R.social_ok(soc["posts_15m"], sent, self.rules.get("min_posts_15m", 20), self.rules.get("min_sentiment", 0.1))
        if not ok:
            self.ledger.skip(sym, reason, forever, m.dev_wallet)
            self.board.put(sym, "done", verdict="SKIP", reason=reason, **info); return "SKIP", reason
        max_open = self.cfg.get("max_open", 6)
        if len(self.positions) >= max_open:
            reason = f"max {max_open} open"
            self.board.put(sym, "done", verdict="SKIP", reason=reason, **info); return "SKIP", reason
        usd = min(self.cfg["position_usd"], self.cash)
        if usd <= 0:
            self.board.put(sym, "done", verdict="SKIP", reason="no cash", **info); return "SKIP", "no cash"
        price = self.h.price_path(mint)[0]
        self.cash -= usd; self.positions[sym] = {"usd": usd, "entry": price, "mint": mint, "step": 0}
        self._order("BUY", sym, usd, price, reason=reason)
        self.board.put(sym, "holding", verdict="BUY", reason=reason, **info); return "BUY", reason

    def _close(self, sym, why):
        p = self.positions.pop(sym); px = self._px(p); value = p["usd"] * px / p["entry"]
        pnl = value - p["usd"]; self.cash += value
        self._order("SELL", sym, value, px, pnl, why)
        self.board.put(sym, "done", verdict="EXIT", reason=why, pnl=round(pnl, 2), pnl_pct=round(100 * pnl / p["usd"], 1))

    def tick(self):
        """Advance prices one step and apply exit rules."""
        for sym, p in list(self.positions.items()):
            p["step"] += 1
            m = self.h.metrics(p["mint"])
            path = self.h.price_path(p["mint"]); chg = 100 * (self._px(p) / p["entry"] - 1)
            if R.should_exit_bundle(m) and p["step"] >= getattr(m, "bundle_move_step", 1):
                self._close(sym, "bundle moved -> exit now")
            elif chg <= self.cfg["stop_loss_pct"]: self._close(sym, f"stop loss {chg:.0f}%")
            elif p["step"] >= len(path) - 1: self._close(sym, f"take profit at top {chg:+.0f}%")
        if not self.closed and R.goal_hit(self.equity(), self.bankroll, self.cfg["goal_multiple"]):
            for sym in list(self.positions): self._close(sym, "goal hit -> close all")
            self.closed = True

    def run_feed(self, coins, log=print):
        """Sequential board run: each new coin is evaluated, then open positions advance one tick."""
        for c in coins:
            if self.closed: break
            v, r = self.evaluate(c); log(f"{c['symbol']:7} {v:5} {r}")
            self.tick()
        while self.positions and not self.closed: self.tick()

    def write_board(self):
        return self.board.write(self.cfg["paths"]["board_json"], equity=self.equity(), bankroll=self.bankroll, mode=self.cfg["mode"])
