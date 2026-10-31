"""Continuous PAPER feed on public market data. Never signs and never sends.

v8 expected-edge revision (not a profitability claim):
  H1 swap tape from curve SOL deltas — buyers, net SOL, top-buyer share; missing tape skips.
  H2 position checks every ~2.5s before coin scans; parallel marks; 10% one-check emergency exit.
  H3 paper fills include the fee and, when reserves are known, bonding-curve impact.
  M1 each watch window's SOL per second must rise; a longer window is not acceleration. Material post-entry SOL outflow exits.
  M2 curve band 30–70%, or 70–85% only with stronger buyer breadth. Above 85% skips.
  M3 volatility can tighten the stop but not past -15%; +15% net arms fee-inclusive break-even.
  M4 similarity boost needs >=30 prior exits, is capped at 1.5x, and ignores future rows.
  L1 missing holder/dev/cluster reads skip. L2 curve progress is reserve-based.
  L3 signal strength sizes inside the $130–160 band (loss-streak halving still applies after).
  D1 top-3 buyers above 45% of buy SOL skip. The 20% single-wallet cap still applies.
  D2 fewer than 8 buyers of at least 0.05 SOL skip; the 70–85% curve band requires 12.
  Both distribution terms also scale size inside the same $130–160 band. Fees stay 100 bps.
  L5 the last watch window needs 3 buyers and no wallet above 50% of that window's buy SOL.
  L6 that same window must itself clear the 1.3 buy/sell ratio. A net bid sizes slightly larger inside $130–160.
  L7 same-wallet round trip above 50% of buy SOL skips. Sells from other wallets do not count. A cleaner tape sizes slightly larger inside $130–160.
  M5 once net peaks at +8%, a 6-point giveback exits before the +25% scale. The scale still comes first.
  M6 a flat round trip worse than -10% at the intended size skips. The curve is priced with the SOL quote, or the price implied by reserves when the quote is missing.
"""
import base64
import json
import os
import time
import signal
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import load
from .strategy_config import effective_strategy
from .aggregators.lunarcrush import LunarCrushClient, LunarCrushError
from .aggregators.fuse import fuse, PLATFORMS
from .ledger import Ledger
from . import similarity as SIM
from .kanban_state import Board
from .curve import parse_curve_account, buy_fill, sell_eff, flat_round_trip_pct, sol_price_usd
from .flow import (
    summarize, trade_from_tx, flow_allows, accelerating, flow_reversed,
    signal_strength, size_for_signal, with_last_window, last_window_allows,
)
from .exits import emergency_drop, net_change_pct, vol_stop_pct, profit_lock_hit

PF = "https://frontend-api-v3.pump.fun"
RPC = "https://api.mainnet-beta.solana.com"
UA = {"User-Agent": "Mozilla/5.0 (PumpBoardEngine paper)", "Accept": "application/json"}
STRAT = dict(
    min_curve_pct=30, max_curve_pct=85, curve_breadth_from_pct=70, max_mcap_usd=250_000,
    p0_top10_max=30, p0_dev_max=5, p0_bundle_max=25,
    watch_short_s=90, watch_medium_s=180, watch_long_s=300, rescan_s=180,
    flow_min_buyers=15, flow_strong_buyers=25, flow_min_bs_ratio=1.3, flow_max_whale_pct=20,
    flow_max_top3_pct=45, flow_meaningful_sol=0.05, flow_min_meaningful_buyers=8,
    flow_strong_meaningful_buyers=12,
    flow_sig_limit=25, flow_slice_dust_sol=0.01, flow_exit_dust_sol=0.05, flow_exit_giveback=0.25,
    flow_last_window_min_buyers=3, flow_last_window_max_whale_pct=50, flow_last_window_min_bs_ratio=1.3,
    flow_max_round_trip_pct=50, impact_max_drag_pct=-10,
    p3_min_sentiment=50, p3_min_mentions=100,
    size_pct_equity=1.5, size_min=130, size_max=160, max_open=6, loss_streak_halve=3,
    curve_fee_bps=100, pos_check_s=2.5, scan_s=10, emergency_drop_pct=10,
    stop_loss_pct=-15, vol_stop_floor_pct=4, vol_stop_mult=2, vol_stop_min_samples=4, be_arm_pct=15,
    profit_lock_arm_pct=8, profit_lock_giveback_pct=6,
    scale_out_pct=25, trail_giveback_pct=12, flat_exit_s=720, flat_exit_below_pct=5,
    hard_exit_s=2400, flatten_curve_pct=95,
    v6_size_mult=1.5, v6_trail_relax=1.20, v6_min_hist=30, v7_boost_eq_pct=8, v7_boost_usd_max=500,
)


def http(url, body=None, timeout=12):
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    headers = {**UA, **({"Content-Type": "application/json"} if body is not None else {})}
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def load_env(path=".env"):
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                if value.strip():
                    os.environ.setdefault(key.strip(), value.strip())


class Feed:
    def __init__(self, cfg, strategy_config_path=None):
        assert cfg["mode"] == "paper", "live feed is PAPER ONLY"
        self.cfg = cfg
        self.bank = cfg["bankroll_usd"]
        self.cash = self.bank
        self.pos = {}
        self.seen = {}
        self.board = Board()
        self.log = []
        self.ledger = Ledger("out/ledger_live")
        self.lc = LunarCrushClient()
        self.lc_backoff = 0
        self.stats = dict(ticks=0, scanned=0, buys=0, wins=0, losses=0, skips=0, realized=0.0, pos_checks=0)
        self.lc_cache = {}
        self.traded = set()
        self.watch = {}
        self.loss_streak = 0
        self.strategy_config_path = strategy_config_path
        self.strategy = effective_strategy(STRAT, strategy_config_path)
        self.next_scan = 0.0
        self.last_pos_check = 0.0
        self.restore()

    def refresh_strategy(self):
        """Read the local slider configuration once per cycle for newly discovered coins."""
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
    def _watch_complete(watch):
        return all(checkpoint["status"] == "observed" for checkpoint in watch["watch_periods"].values())

    @staticmethod
    def _watch_reason(watch):
        parts = []
        for tier in ("short", "medium", "long"):
            checkpoint = watch["watch_periods"][tier]
            label = f"{tier} {checkpoint['duration_s']}s {checkpoint['status']}"
            if checkpoint["status"] == "observed":
                label += f" ({checkpoint['curve_delta']:+.1f}pts)"
            parts.append(label)
        return "watch tiers: " + "; ".join(parts)

    @staticmethod
    def _flow_slices(watch):
        previous = watch.get("real_sol")
        slices = []
        for tier in ("short", "medium", "long"):
            sol = watch["watch_periods"][tier].get("real_sol")
            if previous is None or sol is None:
                return None
            slices.append((sol - previous) / 1e9)
            previous = sol
        return slices

    @staticmethod
    def _flow_window_seconds(watch: dict) -> list[float] | None:
        """Seconds covered by each checkpoint delta. None if a window is not usable."""
        periods = watch.get("watch_periods") or {}
        try:
            short = float(periods["short"]["duration_s"])
            medium = float(periods["medium"]["duration_s"])
            long = float(periods["long"]["duration_s"])
        except (KeyError, TypeError, ValueError):
            return None
        windows = [short, medium - short, long - medium]
        if any(item <= 0 for item in windows):
            return None
        return windows

    def restore(self):
        """Rebuild cash/realized/open positions from the live ledger so restarts keep stats continuous."""
        import csv
        fills = Path("out/ledger_live/fills.csv")
        if not fills.exists():
            return
        for row in csv.DictReader(fills.open()):
            key = row["symbol"]
            usd = float(row["usd"])
            self.traded.add(key)
            if row["side"] == "BUY":
                self.cash -= usd
                self.stats["buys"] += 1
                price = float(row["price"]) or 1
                self.pos[key] = dict(
                    mint=None, usd=usd, entry=price, entry_eff=price, entry_spot=price, peak=price,
                    t=float(row["ts"]), creator=None, key=key, entry_usd=usd, last_spot=price,
                )
            elif row["side"] == "SELL":
                pnl = float(row["pnl"])
                self.cash += usd
                self.stats["realized"] += pnl
                if row.get("reason", "").startswith("scale-out"):
                    if key in self.pos:
                        self.pos[key]["usd"] /= 2
                        self.pos[key]["scaled"] = True
                        self.pos[key]["scaled_pnl"] = self.pos[key].get("scaled_pnl", 0) + pnl
                    continue
                self.stats["wins" if pnl > 0 else "losses"] += 1
                self.pos.pop(key, None)
                self.loss_streak = 0 if pnl > 0 else self.loss_streak + 1
        try:
            board = json.loads(Path("out/board.json").read_text())
            mints = {card["symbol"]: card.get("mint") for col in board.get("columns", {}).values() for card in col}
        except Exception:
            mints = {}
        for key in list(self.pos):
            self.pos[key]["mint"] = mints.get(key)
            if not self.pos[key]["mint"]:
                self.pos.pop(key)
            else:
                self.board.put(key, "holding", verdict="BUY", reason="restored from ledger", pnl=0, pnl_pct=0)
        buys = {row["key"]: row for row in SIM.load() if row.get("side") == "BUY"}
        for key, position in self.pos.items():
            buy = buys.get(key, {})
            position["boost"] = buy.get("boost", False)
            position["sentiment"] = buy.get("sentiment_0_100")
            if buy.get("trail_giveback_pct"):
                position["trail_giveback_pct"] = buy["trail_giveback_pct"]
            if buy.get("fee_bps"):
                position["fee_bps"] = buy["fee_bps"]
            if buy.get("curve_addr"):
                position["curve_addr"] = buy["curve_addr"]
            if buy.get("entry_real_sol") is not None:
                position["entry_real_sol"] = buy["entry_real_sol"]
            if buy.get("entry_flow_sol") is not None:
                position["entry_flow_sol"] = buy["entry_flow_sol"]
        self.cash = self.bank + self.stats["realized"] - sum(position["usd"] for position in self.pos.values())
        self.restored = list(self.pos)

    def ev(self, msg):
        line = time.strftime("%H:%M:%S") + " " + msg
        print(line, flush=True)
        self.log = ([line] + self.log)[:60]

    def _rpc(self, method, params, timeout=20):
        return http(RPC, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=timeout)

    def curve_state(self, coin):
        """Bonding-curve reserves for one coin, or None when the account cannot be read."""
        address = coin.get("bonding_curve")
        if not address:
            return None
        try:
            payload = self._rpc("getAccountInfo", [address, {"encoding": "base64", "commitment": "confirmed"}])
        except Exception:
            return None
        value = ((payload or {}).get("result") or {}).get("value") if isinstance(payload, dict) else None
        if not value or not value.get("data"):
            return None
        raw = value["data"]
        try:
            blob = base64.b64decode(raw[0] if isinstance(raw, list) else raw)
        except Exception:
            return None
        return parse_curve_account(blob)

    def curves_for(self, addresses):
        """One getMultipleAccounts call for every open curve. Failures yield an empty map."""
        addresses = [address for address in addresses if address]
        if not addresses:
            return {}
        try:
            payload = self._rpc("getMultipleAccounts", [addresses, {"encoding": "base64", "commitment": "confirmed"}])
        except Exception:
            return {}
        values = ((payload or {}).get("result") or {}).get("value") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            return {}
        found = {}
        for address, value in zip(addresses, values):
            if not value or not value.get("data"):
                continue
            raw = value["data"]
            try:
                blob = base64.b64decode(raw[0] if isinstance(raw, list) else raw)
            except Exception:
                continue
            parsed = parse_curve_account(blob)
            if parsed:
                found[address] = parsed
        return found

    def marks_for(self, mints):
        """Parallel public mcap reads. A failed mint is omitted; it does not fail the others."""
        mints = [mint for mint in mints if mint]
        if not mints:
            return {}

        def one(mint):
            try:
                payload = http(f"{PF}/coins-v2/{mint}")
                spot = float(payload.get("usd_market_cap") or 0)
                quote = float(payload.get("market_cap_quote") or 0)
                sol_px = spot / quote if spot and quote else 0.0
                return mint, {"spot": spot, "sol_px": sol_px}
            except Exception:
                return mint, None

        workers = min(8, len(mints))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return {mint: snap for mint, snap in pool.map(one, mints) if snap and snap.get("spot")}

    @staticmethod
    def _last_window_start(watch: dict) -> float | None:
        """Timestamp where the long slice starts: the moment the medium tier was actually observed.

        The scheduled medium duration is not used. A late sample would otherwise
        count trades whose SOL was already inside the medium slice. Missing or
        non-positive elapsed time fails closed.
        """
        medium = (watch.get("watch_periods") or {}).get("medium") or {}
        if medium.get("status") != "observed":
            return None
        try:
            elapsed = float(medium["elapsed_s"])
        except (KeyError, TypeError, ValueError):
            return None
        if elapsed <= 0:
            return None
        try:
            return float(watch["t"]) + elapsed
        except (KeyError, TypeError, ValueError):
            return None

    def flow_for(self, coin, since_ts, last_window_ts=None):
        """Swap tape since ``since_ts``. None means the tape is missing or too incomplete to trust.

        ``last_window_ts`` marks the long slice. The summary then carries that
        slice's buyer count and top-buyer share. A missing cutoff leaves those
        fields empty so the entry gate skips.
        """
        curve = coin.get("bonding_curve")
        if not curve:
            return None
        limit = int(self.strategy["flow_sig_limit"])
        try:
            payload = self._rpc("getSignaturesForAddress", [curve, {"limit": limit, "commitment": "confirmed"}])
        except Exception:
            return None
        if not isinstance(payload, dict) or payload.get("error"):
            return None
        rows = [
            row for row in (payload.get("result") or [])
            if isinstance(row, dict) and row.get("signature") and not row.get("err")
            and row.get("blockTime") and row["blockTime"] >= since_ts
        ]
        if not rows:
            return with_last_window(summarize([]), [], last_window_ts)
        batch = [
            {"jsonrpc": "2.0", "id": index, "method": "getTransaction",
             "params": [row["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}]}
            for index, row in enumerate(rows)
        ]
        try:
            results = http(RPC, batch, timeout=20)
        except Exception:
            return None
        if not isinstance(results, list):
            return None
        trades = []
        missing = 0
        for item in results:
            if not isinstance(item, dict) or item.get("error") or not item.get("result"):
                missing += 1
                continue
            trade = trade_from_tx(item["result"], curve)
            if trade:
                trades.append(trade)
        if results and missing * 2 > len(results):
            return None
        summary = summarize(trades, meaningful_sol=float(self.strategy["flow_meaningful_sol"]))
        return with_last_window(summary, trades, last_window_ts)

    def holders(self, coin):
        try:
            payload = self._rpc("getTokenLargestAccounts", [coin["mint"], {"commitment": "confirmed"}])
            accounts = [float(account["uiAmount"] or 0) for account in payload["result"]["value"]]
            supply = float(coin.get("total_supply") or 1e15) / 10 ** int(coin.get("base_decimals", 6))
            rest = sorted(accounts, reverse=True)[1:]
            top10 = round(100 * sum(rest[:10]) / supply, 1)
            return (round(100 * rest[0] / supply, 1) if rest else 0), round(100 * sum(rest[:5]) / supply, 1), top10, supply
        except Exception:
            return None, None, None, None

    def dev_pct(self, coin, supply):
        """Creator token balance as % of supply. None if it cannot be read."""
        try:
            if not coin.get("creator") or not supply:
                return None
            payload = self._rpc("getTokenAccountsByOwner", [
                coin["creator"], {"mint": coin["mint"]}, {"encoding": "jsonParsed", "commitment": "confirmed"},
            ])
            amount = sum(
                float(account["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
                for account in payload["result"]["value"]
            )
            return round(100 * amount / supply, 2)
        except Exception:
            return None

    def social(self, symbol):
        if symbol in self.lc_cache:
            return self.lc_cache[symbol]
        if time.time() < self.lc_backoff:
            return "RATE"
        try:
            snap = fuse(symbol, self.lc.bundle(symbol), PLATFORMS).to_dict()
        except LunarCrushError as exc:
            text = str(exc)
            if text.startswith("429") or "403" in text[:4]:
                self.lc_backoff = time.time() + 60
                self.ev(f"LunarCrush {text[:3]} -> backoff 60s")
            snap = None
            if text.startswith("429"):
                return "RATE"
        except Exception:
            snap = None
        self.lc_cache[symbol] = snap
        return snap

    def evaluate(self, coin, first=True):
        symbol = (coin.get("symbol") or "?")[:10].upper()
        mint = coin["mint"]
        key = f"{symbol}·{mint[:4]}"
        rules = self.strategy
        if first:
            self.stats["scanned"] += 1
        mcap = float(coin.get("usd_market_cap") or 0)
        replies = coin.get("reply_count", 0) or 0
        if coin.get("creator") in self.ledger.do_not_rebuy:
            return self.skip(key, "same dev as rug -> skip forever", True, coin)
        if key in self.traded:
            return self.skip(key, "already traded this coin (no re-entry)", False, coin)
        if coin.get("complete"):
            return self.skip(key, "graduated curve", False, coin)
        state = self.curve_state(coin)
        if not state:
            return self.skip(key, "curve reserves unavailable", False, coin)
        curve = state["curve_pct"]
        if not (rules["min_curve_pct"] <= curve <= rules["max_curve_pct"] and mcap <= rules["max_mcap_usd"]):
            return self.skip(
                key,
                f"P1 curve {curve}% not in {rules['min_curve_pct']}-{rules['max_curve_pct']}% / mcap ${mcap:,.0f}",
                False, coin,
            )
        top, bundle, top10, supply = self.holders(coin)
        dev = self.dev_pct(coin, supply)
        old = self.board.cards.get(key)
        seen = old["ts_seen"] if old and "ts_seen" in old else time.time()
        self.board.put(
            key, "checking", mint=mint, name=coin.get("name"), mcap=round(mcap), curve_pct=curve,
            top1_pct=top, top10_pct=top10, dev_pct=dev, bundle_pct=bundle, replies=replies, ts_seen=seen,
        )
        if top10 is None or dev is None or bundle is None:
            return self.skip(key, "P0 holder data missing", False, coin)
        if top10 > rules["p0_top10_max"]:
            return self.skip(key, f"P0 top10 {top10}% > {rules['p0_top10_max']}%", False, coin)
        if dev > rules["p0_dev_max"]:
            return self.skip(key, f"P0 dev {dev}% > {rules['p0_dev_max']}%", False, coin)
        if bundle > rules["p0_bundle_max"]:
            return self.skip(key, f"P0 top-5 cluster {bundle}% > {rules['p0_bundle_max']}%", False, coin)
        watch = self.watch.get(mint)
        if watch is None:
            periods = self._new_watch_periods()
            started = time.time()
            watch = dict(
                t=started, curve=curve, baseline_curve=curve, key=key, real_sol=state["real_sol"],
                watch_periods=periods,
            )
            self.watch[mint] = watch
            self.board.put(
                key, "checking", reason=self._watch_reason(watch), watch_started_ts=started,
                watch_periods=self._copy_watch_periods(periods),
            )
            self.ev(f"WATCH {key} curve {curve}% ({self._watch_reason(watch)})")
            return
        long_duration = watch["watch_periods"]["long"]["duration_s"]
        if time.time() - watch["t"] < long_duration or not self._watch_complete(watch):
            self.board.put(
                key, "checking", reason=self._watch_reason(watch),
                watch_periods=self._copy_watch_periods(watch["watch_periods"]),
            )
            return
        strong = curve > rules["curve_breadth_from_pct"]
        slices = self._flow_slices(watch)
        durations = self._flow_window_seconds(watch)
        # Always pass lengths. An unusable window fails closed instead of comparing absolute SOL.
        accel_ok, accel_why = accelerating(
            slices, rules["flow_slice_dust_sol"], durations if durations is not None else (),
        )
        if not accel_ok:
            return self.skip(key, accel_why, False, coin)
        flow = self.flow_for(coin, watch["t"], self._last_window_start(watch))
        if flow is None:
            return self.skip(key, "flow unavailable", False, coin)
        flow_ok, flow_why = flow_allows(flow, rules, strong=strong)
        if not flow_ok:
            return self.skip(key, flow_why, False, coin)
        late_ok, late_why = last_window_allows(flow, rules)
        if not late_ok:
            return self.skip(key, late_why, False, coin)
        snap = self.social(symbol)
        half = False
        if snap == "RATE":
            snap = None
            half = True
        if snap is not None and snap["mention_volume"] >= rules["p3_min_mentions"]:
            if snap["sentiment_0_100"] < rules["p3_min_sentiment"]:
                return self.skip(key, f"P3 sentiment {snap['sentiment_0_100']}/100", False, coin)
            social_why = f"social {snap['sentiment_0_100']}/100"
        else:
            social_why = "LunarCrush rate-limited -> half size" if half else "no social topic (neutral)"
        self.watch.pop(mint, None)
        if len(self.pos) >= rules["max_open"]:
            return self.skip(key, f"max {rules['max_open']} open", False, coin)
        strength = signal_strength(flow, slices, rules, strong=strong, durations=durations)
        usd = size_for_signal(strength, rules)
        if self.loss_streak >= rules["loss_streak_halve"]:
            usd /= 2
        if half:
            usd /= 2
        usd = min(usd, self.cash)
        if usd < 20:
            return self.skip(key, "no cash", False, coin)
        sentiment_buy = (
            isinstance(snap, dict) and not half and snap["mention_volume"] >= rules["p3_min_mentions"]
            and snap["sentiment_0_100"] >= rules["p3_min_sentiment"]
        )
        social_values = snap if isinstance(snap, dict) else {}
        created = coin.get("created_timestamp")
        quote = float(coin.get("market_cap_quote") or 0)
        sol_px = (mcap / quote) if mcap and quote else 0.0
        if sol_px <= 0:
            sol_px = sol_price_usd(mcap, state.get("virtual_sol"), state.get("virtual_token")) or 0.0
        feat = dict(
            key=key, mint=mint, curve_pct=curve, mcap=round(mcap), replies=replies, top10_pct=top10,
            dev_pct=dev, bundle_pct=bundle, rise_pts=round(curve - watch["curve"], 2),
            sentiment_0_100=social_values.get("sentiment_0_100") if sentiment_buy else None,
            mention_volume=social_values.get("mention_volume") if sentiment_buy else None,
            sentiment_buy=sentiment_buy, age_s=round(time.time() - created / 1000) if created else None,
            flow_buyers=flow["unique_buyers"], flow_signed_sol=round(flow["signed_quote_sol"], 4),
            flow_top_buyer_pct=round(flow["top_buyer_pct"], 2),
            flow_top3_buyer_pct=round(float(flow["top3_buyer_pct"]), 2),
            flow_meaningful_buyers=int(flow["meaningful_buyers"]),
            signal_strength=round(strength, 3),
        )
        boost = False
        trail = rules["trail_giveback_pct"]
        sim_why = ""
        if sentiment_buy:
            decision = SIM.decide(feat, C={**SIM.SIM, "min_labeled": rules["v6_min_hist"]})
            tag = (
                f"score={decision['score']} knn={decision['knn_win']}/{decision['knn_n']} "
                f"n_hist={decision['n_hist']} ({decision['nw']}W/{decision['nl']}L)"
            )
            if str(decision["decision"]) == "skip":
                return self.skip(key, f"v8 SIM skip {tag}", False, coin)
            if str(decision["decision"]) == "boost":
                cap = min(self.cash, self.equity() * rules["v7_boost_eq_pct"] / 100, rules["v7_boost_usd_max"])
                usd = min(cap, usd * rules["v6_size_mult"])
                boost = True
                trail = round(rules["trail_giveback_pct"] * rules["v6_trail_relax"], 2)
                sim_why = f", v8 SIM boost {tag} size ${usd:.0f} trail {trail}%"
                self.ev(f"v8 SIM BOOST {key} {tag} -> size ${usd:.0f} (cap ${cap:.0f}) trail {trail}%")
            else:
                sim_why = f", v8 SIM {decision['decision']} {tag}"
                self.ev(f"v8 SIM {str(decision['decision']).upper()} {key} {tag} -> size ${usd:.0f} trail {trail}%")
        drag_limit = float(rules["impact_max_drag_pct"])
        if drag_limit > -100:
            drag = flat_round_trip_pct(
                mcap or 1, usd, sol_px, state.get("virtual_sol"), state.get("virtual_token"), rules["curve_fee_bps"],
            )
            if drag is not None and drag < drag_limit:
                return self.skip(
                    key, f"curve impact {drag:.1f}% worse than {drag_limit:g}%", False, coin,
                )
        filled = buy_fill(
            mcap or 1, usd, sol_px, state.get("virtual_sol"), state.get("virtual_token"), rules["curve_fee_bps"],
        )
        entry_eff = filled["entry_eff"]
        feat["size_pct_equity"] = round(100 * usd / max(self.equity(), 1), 2)
        why = (
            f"v8 PASS: curve {watch['curve']}->{curve}%, {accel_why}, {flow_why}, {late_why}, {social_why}, "
            f"top10 {top10}%, dev {dev}%, fill eff ${entry_eff:,.0f} vs spot ${mcap:,.0f}{sim_why}"
        )
        self.traded.add(key)
        self.cash -= usd
        self.pos[key] = dict(
            mint=mint, usd=usd, entry=entry_eff, entry_eff=entry_eff, entry_spot=mcap or 1, peak=mcap or 1,
            last_spot=mcap or 1, t=time.time(), creator=coin.get("creator"), dev0=dev, sup=supply,
            scaled=False, boost=boost, trail_giveback_pct=trail, sentiment=feat["sentiment_0_100"], feat=feat,
            tokens=filled["tokens"], fee_bps=rules["curve_fee_bps"], curve_addr=coin.get("bonding_curve"),
            entry_real_sol=state["real_sol"], entry_flow_sol=sum(slices), entry_usd=usd, sol_px=sol_px,
            scaled_pnl=0.0, rets=[], be_armed=False,
        )
        SIM.append({
            **feat, "side": "BUY", "usd": round(usd, 2), "boost": boost, "trail_giveback_pct": trail,
            "fee_bps": rules["curve_fee_bps"], "curve_addr": coin.get("bonding_curve"),
            "entry_real_sol": state["real_sol"], "entry_flow_sol": sum(slices),
        })
        self.ledger.fill(key, "BUY", usd, entry_eff, 0, why)
        self.stats["buys"] += 1
        self.board.put(key, "holding", verdict="BUY", reason=why, pnl=0, pnl_pct=0)
        self.ev(f"BUY  {key} ${usd:.0f} @ eff mcap ${entry_eff:,.0f} ({why})")

    def skip(self, key, reason, forever, coin, keep_watch=False):
        if not keep_watch:
            self.watch.pop(coin["mint"], None)
        self.stats["skips"] += 1
        self.ledger.skip(key, reason, forever, coin.get("creator"))
        self.board.put(key, "done", verdict="SKIP", reason=reason)
        self.ev(f"SKIP {key} {reason}")

    def recheck(self):
        for mint, watch in list(self.watch.items()):
            age = time.time() - watch["t"]
            periods = watch["watch_periods"]
            long_duration = periods["long"]["duration_s"]
            if age > long_duration + 120:
                self.watch.pop(mint, None)
                self.ev(f"WATCH expired {watch['key']} after {age:.0f}s ({self._watch_reason(watch)})")
                continue
            due = [
                tier for tier in ("short", "medium", "long")
                if periods[tier]["status"] == "watching" and age >= periods[tier]["duration_s"]
            ]
            if not due:
                continue
            try:
                coin = http(f"{PF}/coins-v2/{mint}")
            except Exception as exc:
                self.ev(f"recheck {watch['key']} fetch failed {type(exc).__name__}")
                continue
            if coin.get("complete"):
                self.watch.pop(mint, None)
                continue
            state = self.curve_state(coin)
            if not state:
                self.ev(f"recheck {watch['key']} curve reserves unavailable")
                continue
            # One reading cannot prove three windows. Observe a single due tier per fetch.
            tier = due[0]
            periods[tier].update(
                elapsed_s=round(max(0, age), 1), status="observed",
                curve_delta=round(state["curve_pct"] - watch["baseline_curve"], 1),
                real_sol=state["real_sol"],
            )
            self.board.put(
                watch["key"], "checking", reason=self._watch_reason(watch),
                watch_periods=self._copy_watch_periods(periods),
            )
            self.ev(f"RECHECK {watch['key']} after {age:.0f}s observed {tier} ({self._watch_reason(watch)})")
            if self._watch_complete(watch) and age >= long_duration:
                self.evaluate(coin, first=False)

    def close(self, key, spot, why, frac=1.0, exit_eff=None):
        position = self.pos[key]
        entry = position.get("entry_eff") or position["entry"]
        if exit_eff is None:
            exit_eff = sell_eff(spot, None, None, None, position.get("fee_bps", self.strategy["curve_fee_bps"]))
        part = position["usd"] * frac
        value = part * exit_eff / entry if entry else part
        pnl = value - part
        self.cash += value
        self.stats["realized"] += pnl
        if frac < 1 and position.get("tokens"):
            position["tokens"] *= (1 - frac)
        if frac >= 1:
            total = pnl + position.get("scaled_pnl", 0)
            basis = position.get("entry_usd", part) or part
            self.pos.pop(key)
            won = total > 0
            self.stats["wins" if won else "losses"] += 1
            self.loss_streak = 0 if won else self.loss_streak + 1
            if total < -0.5 * basis and position.get("creator"):
                self.ledger.add_dnr(position["creator"])
            feature = position.get("feat") or {"key": key, "mint": position.get("mint"), "sentiment_0_100": position.get("sentiment")}
            SIM.append({
                **feature, "side": "EXIT", "exit_mcap": round(spot), "boost": position.get("boost", False),
                "pnl_pct": round(100 * total / basis, 1), "reason": why,
            })
            self.board.put(
                key, "done", verdict="EXIT", reason=why, pnl=round(total, 2),
                pnl_pct=round(100 * total / basis, 1), mcap=round(spot),
            )
        else:
            position["usd"] -= part
            position["scaled"] = True
            position["scaled_pnl"] = position.get("scaled_pnl", 0) + pnl
            why = "scale-out " + why
        self.ledger.fill(key, "SELL", value, exit_eff, pnl, why)
        self.ev(f"EXIT {key} {why} pnl {pnl:+.2f}")

    def apply_mark(self, key, snap):
        """Apply one paper mark. ``snap`` carries spot and, when known, current reserves."""
        if key not in self.pos:
            return
        position = self.pos[key]
        rules = self.strategy
        spot = float(snap.get("spot") or 0)
        if spot <= 0:
            return
        previous = position.get("last_spot") or position.get("entry_spot") or position.get("entry")
        exit_price = sell_eff(
            spot, position.get("tokens"), snap.get("virtual_sol"), snap.get("virtual_token"),
            position.get("fee_bps", rules["curve_fee_bps"]),
        )
        entry = position.get("entry_eff") or position["entry"]
        net = net_change_pct(entry, exit_price)
        if emergency_drop(previous, spot, rules["emergency_drop_pct"]):
            self.close(key, spot, f"emergency drop {100 * (spot / previous - 1):.0f}% in one check", exit_eff=exit_price)
            return
        position["last_spot"] = spot
        position["last_eff"] = exit_price
        if previous:
            position.setdefault("rets", []).append(spot / previous - 1)
            position["rets"] = position["rets"][-30:]
        position["peak"] = max(position.get("peak", spot), spot)
        position["peak_net"] = max(float(position.get("peak_net", net)), net)
        self.board.put(key, "holding", pnl=round(position["usd"] * net / 100, 2), pnl_pct=round(net, 1), mcap=round(spot))
        if flow_reversed(
            position.get("entry_real_sol"), snap.get("real_sol"), position.get("entry_flow_sol") or 0,
            rules["flow_exit_dust_sol"], rules["flow_exit_giveback"],
        ):
            self.close(key, spot, f"flow exit: post-entry net SOL reversed ({net:+.0f}%)", exit_eff=exit_price)
            return
        curve_pct = snap.get("curve_pct")
        dev_sold = False
        age = time.time() - position["t"]
        if position.get("dev0") and position.get("sup") and int(age) // 60 != position.get("devchk"):
            position["devchk"] = int(age) // 60
            dev_now = self.dev_pct({"creator": position.get("creator"), "mint": position.get("mint")}, position["sup"])
            dev_sold = dev_now is not None and dev_now < position["dev0"] * 0.8
        if net <= rules["stop_loss_pct"]:
            self.close(key, spot, f"stop loss {net:.0f}%", exit_eff=exit_price)
        elif curve_pct is not None and curve_pct >= rules["flatten_curve_pct"]:
            self.close(key, spot, f"flatten: curve {curve_pct:.0f}% (pre-graduation) {net:+.0f}%", exit_eff=exit_price)
        elif dev_sold:
            self.close(key, spot, f"flatten: dev sold {net:+.0f}%", exit_eff=exit_price)
        elif position.get("be_armed") and net <= 0:
            self.close(key, spot, f"break-even fees {net:.0f}%", exit_eff=exit_price)
        elif net >= rules["be_arm_pct"] and not position.get("be_armed"):
            position["be_armed"] = True
            self._scale_or_time(key, position, spot, net, exit_price, age, rules)
        else:
            band = vol_stop_pct(position.get("rets") or [], rules)
            if band > rules["stop_loss_pct"] and net <= band:
                self.close(key, spot, f"vol stop {net:.0f}% (band {band:.0f}%)", exit_eff=exit_price)
            else:
                self._scale_or_time(key, position, spot, net, exit_price, age, rules)

    def _scale_or_time(self, key, position, spot, net, exit_price, age, rules):
        if key not in self.pos:
            return
        if not position.get("scaled") and net >= rules["scale_out_pct"]:
            # The trigger used full-size impact, so the half is filled at that same effective mark.
            self.close(key, spot, f"half at {net:+.0f}%", frac=0.5, exit_eff=exit_price)
            return
        peak_net = float(position.get("peak_net", net))
        if not position.get("scaled") and profit_lock_hit(
            peak_net, net, rules["profit_lock_arm_pct"], rules["profit_lock_giveback_pct"],
        ):
            self.close(
                key, spot,
                f"profit lock {net:+.0f}% (peak {peak_net:+.0f}%)",
                exit_eff=exit_price,
            )
            return
        trail = position.get("trail_giveback_pct", rules["trail_giveback_pct"])
        if position.get("scaled") and spot < position["peak"] * (1 - trail / 100):
            peak = 100 * (position["peak"] / position.get("entry_spot", position["entry"]) - 1)
            self.close(key, spot, f"trail {trail}% {net:+.0f}% (peak {peak:+.0f}%)", exit_eff=exit_price)
        elif age > rules["flat_exit_s"] and net < rules["flat_exit_below_pct"] and not position.get("scaled"):
            self.close(key, spot, f"time exit {rules['flat_exit_s'] // 60}m flat {net:+.0f}%", exit_eff=exit_price)
        elif age > rules["hard_exit_s"]:
            self.close(key, spot, f"time exit {rules['hard_exit_s'] // 60}m {net:+.0f}%", exit_eff=exit_price)

    def manage_fast(self):
        """Mark every open paper position. Prices are fetched together; reserves are one RPC batch."""
        self.last_pos_check = time.time()
        self.stats["pos_checks"] += 1
        if not self.pos:
            return
        keys = list(self.pos)
        marks = self.marks_for([self.pos[key]["mint"] for key in keys])
        curves = self.curves_for([self.pos[key].get("curve_addr") for key in keys])
        for key in keys:
            if key not in self.pos:
                continue
            position = self.pos[key]
            mark = marks.get(position.get("mint")) or {}
            curve = curves.get(position.get("curve_addr")) or {}
            if not mark.get("spot"):
                continue
            self.apply_mark(key, {
                "spot": mark["spot"],
                "sol_px": mark.get("sol_px") or 0,
                "virtual_sol": curve.get("virtual_sol"),
                "virtual_token": curve.get("virtual_token"),
                "real_sol": curve.get("real_sol"),
                "curve_pct": curve.get("curve_pct"),
            })

    def manage(self):
        self.manage_fast()

    def _maybe_manage(self):
        if time.time() - self.last_pos_check >= float(self.strategy["pos_check_s"]):
            self.manage_fast()

    def equity(self):
        total = self.cash
        for position in self.pos.values():
            entry = position.get("entry_eff") or position["entry"]
            mark = position.get("last_eff") or entry
            total += position["usd"] * mark / entry if entry else 0
        return total

    def write(self):
        cards = self.board.cards
        if len(cards) > 80:
            old = sorted(
                [key for key, card in cards.items() if card["column"] == "done"],
                key=lambda key: cards[key].get("ts_seen", 0),
            )[:len(cards) - 80]
            for key in old:
                cards.pop(key, None)
        state = self.board.state(self.equity(), self.bank, "paper")
        state.update(
            source="LIVE MARKET DATA · PAPER TRADES",
            stats={**self.stats, "realized": round(self.stats["realized"], 2)},
            log=self.log, lunarcrush=self.lc.mode, updated=time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        )
        Path("out").mkdir(exist_ok=True)
        temporary = Path("out/board.json.tmp")
        temporary.write_text(json.dumps(state, indent=1))
        temporary.replace("out/board.json")

    def scan(self):
        self.stats["ticks"] += 1
        coins = http(f"{PF}/coins?offset=0&limit=30&sort=last_trade_timestamp&order=DESC&includeNsfw=false")
        try:
            coins += http(f"{PF}/coins?offset=0&limit=30&sort=market_cap&order=DESC&includeNsfw=false")
        except Exception:
            pass
        now = time.time()
        fresh = [
            coin for coin in coins
            if not coin.get("complete") and coin["mint"] not in self.watch
            and now - self.seen.get(coin["mint"], 0) > self.strategy["rescan_s"]
        ][:15]
        for coin in fresh:
            self.seen[coin["mint"]] = now
            self.evaluate(coin)
            self._maybe_manage()
        self.recheck()

    def cycle(self, now=None):
        """Check open paper positions first. Scan for new coins on the slower cadence."""
        now = time.time() if now is None else now
        self.refresh_strategy()
        self.manage_fast()
        scanned = False
        if now >= self.next_scan:
            try:
                self.scan()
                scanned = True
            except Exception as exc:
                self.ev(f"scan error {type(exc).__name__}: {str(exc)[:120]}")
            self.next_scan = now + float(self.strategy["scan_s"])
        self.write()
        return scanned

    def tick(self):
        """Backward-compatible single step: positions, then a scan."""
        self.next_scan = 0
        return self.cycle()


def main():
    load_env()
    cfg = load()
    cfg["mode"] = "paper"
    feed = Feed(cfg)
    stop_delay = feed.strategy["pos_check_s"]
    signal.signal(signal.SIGTERM, lambda *args: (_ for _ in ()).throw(SystemExit))
    feed.ev(
        f"feed start PAPER strategy v8+distribution, position check {stop_delay}s, lunarcrush={feed.lc.mode}"
    )
    while True:
        try:
            feed.cycle()
            stop_delay = feed.strategy["pos_check_s"]
        except SystemExit:
            break
        except Exception as exc:
            stop_delay = min(max(stop_delay * 2, feed.strategy["pos_check_s"]), 30)
            feed.ev(f"tick error {type(exc).__name__}: {str(exc)[:120]} -> backoff {stop_delay}s")
        time.sleep(stop_delay)


if __name__ == "__main__":
    main()
