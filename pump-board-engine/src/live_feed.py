"""Continuous PAPER feed on public market data. Never signs and never sends.

v8 expected-edge revision (not a profitability claim):
  H1 swap tape from curve SOL deltas — buyers, net SOL, top-buyer share. A present tape still has to pass.
     A missing tape does not consult replies: zero and absent counts are unavailable, so only a curve
     rise of at least flow_fallback_rise_pts can clear P2. A smaller rise still skips. The 70–85% band still needs the tape.
  H2 position checks every ~2.5s before coin scans; parallel marks; 10% one-check emergency exit.
  H3 paper fills include the fee and, when reserves are known, bonding-curve impact.
  M1 each watch window's SOL per second must rise; a longer window is not acceleration. Material post-entry SOL outflow exits.
  M2 hard curve floor is 2%, so the GeckoTerminal fresh-pool mass is not rejected on the band.
     2–30% is the early band at a smaller size. Above 85% still skips. 70–85% still needs buyer breadth.
  S1 LunarCrush is a primary buy weight, not only a veto. Strong means sentiment >= 65, mentions >= 150, trend not down.
     Strong sentiment removes the early-curve and missing-holder size cuts and can lift a clean ticket from $130 to $160.
     A missing, fixture, or rate-limited reading keeps the smaller size. It does not qualify. High-mention weak sentiment still skips.
  M3 volatility can tighten the stop on the spot move, not the fee-inclusive net, and not past -15%. +15% net arms fee-inclusive break-even.
  H4 top-10 and top-5 drop the known curve token account. The largest balance is not treated as the curve, so a whale that outsizes it still counts. A missing curve account skips.
  M4 similarity boost needs >=30 prior exits, is capped at 1.5x, and ignores future rows.
  L1 an unreadable holder payload still skips. A rate limit or timeout does not invent percents:
     the coin can still enter at holder_gap_size_mult, strong sentiment can clear that cut, and a later real over-cap reading still skips.
  L2 curve progress is reserve-based.
  L3 signal strength sizes inside the $130–160 band (loss-streak halving still applies after).
  D1 top-3 buyers above 45% of buy SOL skip. The 20% single-wallet cap still applies.
  D2 fewer than 8 buyers of at least 0.05 SOL skip; the 70–85% curve band requires 12.
  Both distribution terms also scale size inside the same $130–160 band. Fees stay 100 bps.
  L5 the last watch window needs 3 buyers and no wallet above 50% of that window's buy SOL.
  L6 that same window must itself clear the 1.3 buy/sell ratio. A net bid sizes slightly larger inside $130–160.
  L7 same-wallet round trip above 50% of buy SOL skips. Sells from other wallets do not count. A cleaner tape sizes slightly larger inside $130–160.
  L8 last-window sells of at least 0.15 SOL cannot be one wallet above 60%. The seller does not have to have bought. A spread-out offer sizes slightly larger inside $130–160.
  M5 once net peaks at +8%, a 6-point giveback exits before the +25% scale. The scale still comes first.
  M8 a position that never printed +2% spot exits once spot is −8%. A widened vol band no longer holds that path to the −15% net stop. A bounce of +2% spot keeps the hard stop.
  M6 a flat round trip worse than -10% at the intended size skips. The curve is priced with the SOL quote, or the price implied by reserves when the quote is missing.
  M7 after that skip, size above the $130 floor is cut until a flat round trip is no worse than -4%. A deep book keeps the signal size, including a similarity boost.
"""
import base64
import json
import os
import time
import signal
from pathlib import Path

from .config import load
from .file_feed import publish_pid, refresh_pid, release_pid
from .strategy_config import effective_strategy
from .aggregators.lunarcrush import LunarCrushClient, LunarCrushError
from .aggregators.fuse import fuse, PLATFORMS
from .ledger import Ledger
from . import similarity as SIM
from .kanban_state import Board
from .holders import (
    concentration, curve_token_accounts, largest_account_rows,
    rpc_failure_kind, rpc_payload_kind,
)
from .curve import parse_curve_account, buy_fill, sell_eff, flat_round_trip_pct, size_for_curve_depth, sol_price_usd
from .flow import (
    summarize, trade_from_tx, flow_allows, accelerating, flow_reversed,
    signal_strength, size_for_signal, with_last_window, last_window_allows,
    unavailable_tape_fallback,
)
from .exits import emergency_drop, failed_impulse_hit, net_change_pct, vol_stop_hit, vol_stop_pct, profit_lock_hit
from .market_scan import (
    GECKO_HEADERS,
    apply_account_meta,
    coins_from_listing,
    listing_url,
    mcap_from_reserves,
)

RPC = "https://api.mainnet-beta.solana.com"
UA = {"User-Agent": "Mozilla/5.0 (PumpBoardEngine paper)", "Accept": "application/json"}
STRAT = dict(
    min_curve_pct=2, max_curve_pct=85, curve_breadth_from_pct=70, early_curve_pct=30,
    early_curve_size_mult=0.45, max_mcap_usd=250_000,
    p0_top10_max=30, p0_dev_max=5, p0_bundle_max=25, holder_gap_size_mult=0.65,
    watch_short_s=90, watch_medium_s=180, watch_long_s=300, rescan_s=180,
    flow_min_buyers=15, flow_strong_buyers=25, flow_min_bs_ratio=1.3, flow_max_whale_pct=20,
    flow_max_top3_pct=45, flow_meaningful_sol=0.05, flow_min_meaningful_buyers=8,
    flow_strong_meaningful_buyers=12,
    flow_sig_limit=25, flow_slice_dust_sol=0.01, flow_exit_dust_sol=0.05, flow_exit_giveback=0.25,
    flow_last_window_min_buyers=3, flow_last_window_max_whale_pct=50, flow_last_window_min_bs_ratio=1.3,
    flow_last_window_max_seller_pct=60, flow_last_window_min_sell_sol=0.15,
    flow_max_round_trip_pct=50, flow_fallback_rise_pts=3, impact_max_drag_pct=-10, impact_size_budget_pct=-4,
    p3_min_sentiment=50, p3_min_mentions=100,
    p3_strong_sentiment=65, p3_strong_mentions=150,
    sentiment_border_size_mult=1.0, sentiment_size_weight=1.0,
    size_pct_equity=1.5, size_min=130, size_max=160, max_open=6, loss_streak_halve=3,
    curve_fee_bps=100, pos_check_s=2.5, scan_s=10, emergency_drop_pct=10,
    stop_loss_pct=-15, vol_stop_floor_pct=4, vol_stop_mult=2, vol_stop_min_samples=4, be_arm_pct=15,
    profit_lock_arm_pct=8, profit_lock_giveback_pct=6,
    fail_impulse_drop_pct=8, fail_impulse_max_peak_pct=2,
    scale_out_pct=25, trail_giveback_pct=12, flat_exit_s=720, flat_exit_below_pct=5,
    hard_exit_s=2400, flatten_curve_pct=95,
    v6_size_mult=1.5, v6_trail_relax=1.20, v6_min_hist=30, v7_boost_eq_pct=8, v7_boost_usd_max=500,
)


def public_sentiment_tier(snap, rules) -> str:
    """``strong``, ``neutral``, ``veto``, or ``missing`` from one already-fetched snapshot.

    ``strong`` is the buy signal: enough mentions, sentiment at the higher bar, and
    the topic is not trending down. ``veto`` is the old high-mention weak reading.
    Anything absent, non-numeric, or too thin to judge is ``missing``. Missing is
    not a pass and it is not a zero score.
    """
    if not isinstance(snap, dict):
        return "missing"
    mentions = snap.get("mention_volume")
    score = snap.get("sentiment_0_100")
    if isinstance(mentions, bool) or isinstance(score, bool):
        return "missing"
    try:
        mentions_n = float(mentions)
        score_n = float(score)
    except (TypeError, ValueError):
        return "missing"
    if mentions_n != mentions_n or score_n != score_n:
        return "missing"
    if mentions_n < float(rules["p3_min_mentions"]) or score_n < 0 or score_n > 100:
        return "missing"
    if score_n < float(rules["p3_min_sentiment"]):
        return "veto"
    trend = str(snap.get("trend") or "flat").strip().lower()
    if (
        mentions_n >= float(rules["p3_strong_mentions"])
        and score_n >= float(rules["p3_strong_sentiment"])
        and trend != "down"
    ):
        return "strong"
    return "neutral"


def sentiment_size_margin(score, rules) -> float:
    """Share of the $130–160 span a strong reading adds. Half at the strong bar, all of it at 100."""
    span = 100.0 - float(rules["p3_strong_sentiment"])
    if span <= 0:
        return 1.0
    frac = (float(score) - float(rules["p3_strong_sentiment"])) / span
    return max(0.0, min(1.0, 0.5 + 0.5 * frac))


def http(url, body=None, timeout=12, headers=None):
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    merged = {**UA, **(headers or {})}
    if body is not None:
        merged.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, headers=merged)
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
        self.sol_usd = None
        self._listing_page = 1
        self._listed_once = False
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

    def _accounts(self, addresses):
        """One getMultipleAccounts read. None when the RPC call itself fails."""
        addresses = [address for address in addresses if address]
        if not addresses:
            return {}
        try:
            payload = self._rpc("getMultipleAccounts", [addresses, {"encoding": "base64", "commitment": "confirmed"}])
        except Exception:
            return None
        values = ((payload or {}).get("result") or {}).get("value") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            return None
        found = {}
        for address, value in zip(addresses, values):
            if not isinstance(value, dict) or not value.get("data"):
                continue
            raw = value["data"]
            try:
                blob = base64.b64decode(raw[0] if isinstance(raw, list) else raw)
            except Exception:
                continue
            found[address] = {"owner": value.get("owner"), "data": blob}
        return found

    def _enrich_coins(self, coins):
        """Attach on-chain creator, supply, and the curve token account. Gates stay fail-closed."""
        addresses = []
        for coin in coins:
            for key in ("bonding_curve", "mint"):
                value = coin.get(key)
                if value and value not in addresses:
                    addresses.append(value)
        accounts = self._accounts(addresses)
        if accounts is None:
            return list(coins)
        enriched = []
        for coin in coins:
            updated = apply_account_meta(coin, accounts)
            if updated is not None:
                enriched.append(updated)
        return enriched

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
        """Holder snapshot with the known curve token account removed.

        ``status`` is ``known`` only when supply and the curve account are both
        real. ``transient`` is a rate limit or timeout: percents are omitted, not
        zeroed. ``corrupt`` is an unreadable body or a curve account that is not
        in the largest-account list. Callers skip that case. The largest balance
        is never assumed to be the curve.
        """
        try:
            payload = self._rpc("getTokenLargestAccounts", [coin["mint"], {"commitment": "confirmed"}])
        except Exception as exc:
            kind = rpc_failure_kind(exc)
            return {
                "status": kind,
                "reason": "holder RPC unavailable" if kind == "transient" else "P0 holder data missing",
            }
        kind = rpc_payload_kind(payload)
        if kind:
            return {
                "status": kind,
                "reason": "holder RPC unavailable" if kind == "transient" else "P0 holder data missing",
            }
        try:
            parsed = largest_account_rows((payload.get("result") or {}).get("value"))
            supply = float(coin.get("total_supply") or 0) / 10 ** int(coin.get("base_decimals", 6))
            stats = concentration(parsed or [], supply, curve_token_accounts(coin))
        except Exception:
            return {"status": "corrupt", "reason": "P0 holder data missing"}
        if stats is None or supply <= 0:
            return {"status": "corrupt", "reason": "P0 holder data missing"}
        top, bundle, top10 = stats
        return {
            "status": "known", "top": top, "bundle": bundle, "top10": top10,
            "supply": supply, "reason": None,
        }

    def dev_pct(self, coin, supply):
        """Creator token balance as % of supply. None if it cannot be read.

        Sets ``_dev_failure`` to ``transient`` or ``corrupt`` when the result is
        None, so a timeout is not stored as a 0% developer. A mock that returns
        a number never has to set the flag.
        """
        self._dev_failure = None
        try:
            if not coin.get("creator") or not supply:
                self._dev_failure = "corrupt"
                return None
            payload = self._rpc("getTokenAccountsByOwner", [
                coin["creator"], {"mint": coin["mint"]}, {"encoding": "jsonParsed", "commitment": "confirmed"},
            ])
            kind = rpc_payload_kind(payload)
            if kind:
                self._dev_failure = kind
                return None
            amount = sum(
                float(account["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
                for account in payload["result"]["value"]
            )
            return round(100 * amount / supply, 2)
        except Exception as exc:
            self._dev_failure = rpc_failure_kind(exc)
            return None

    @staticmethod
    def _holder_fields(raw):
        """Accept the snapshot dict or the older ``(top, bundle, top10, supply)`` tuple.

        A tuple with any missing percent is corrupt. Tests and a caller that
        already measured concentration keep working. Nothing here fills a gap with 0.
        """
        if isinstance(raw, dict) and raw.get("status") in ("known", "transient", "corrupt"):
            return raw
        if isinstance(raw, tuple) and len(raw) == 4:
            top, bundle, top10, supply = raw
            if top10 is None or bundle is None or supply is None:
                return {
                    "status": "corrupt", "reason": "P0 holder data missing",
                    "top": top, "bundle": bundle, "top10": top10, "supply": supply,
                }
            return {
                "status": "known", "reason": None,
                "top": top, "bundle": bundle, "top10": top10, "supply": supply,
            }
        return {"status": "corrupt", "reason": "P0 holder data missing"}

    def holder_verdict(self, coin):
        """``(clear|gap|skip, reason, fields)``.

        A measured top-10, developer, or top-5 over its cap skips. An unreadable
        payload skips. A rate limit or timeout is a gap: the missing percents
        stay missing, and the entry size is cut later. Denial is not a pass.
        """
        rules = self.strategy
        snap = self._holder_fields(self.holders(coin))
        top10 = snap.get("top10")
        bundle = snap.get("bundle")
        supply = snap.get("supply")
        if snap.get("status") == "corrupt":
            return "skip", "P0 holder data missing", snap
        if snap.get("status") == "known":
            if top10 is None or bundle is None or not supply:
                return "skip", "P0 holder data missing", snap
            if top10 > rules["p0_top10_max"]:
                return "skip", f"P0 top10 {top10}% > {rules['p0_top10_max']}%", snap
            self._dev_failure = None
            dev = self.dev_pct(coin, supply)
            fields = {**snap, "dev": dev}
            if dev is None:
                # A real top-5 reading still vetoes. A missing dev does not become 0%.
                if bundle > rules["p0_bundle_max"]:
                    return "skip", f"P0 top-5 cluster {bundle}% > {rules['p0_bundle_max']}%", fields
                if getattr(self, "_dev_failure", None) == "transient":
                    return "gap", "holders unverified (RPC unavailable)", fields
                return "skip", "P0 holder data missing", fields
            if dev > rules["p0_dev_max"]:
                return "skip", f"P0 dev {dev}% > {rules['p0_dev_max']}%", fields
            if bundle > rules["p0_bundle_max"]:
                return "skip", f"P0 top-5 cluster {bundle}% > {rules['p0_bundle_max']}%", fields
            return "clear", None, fields
        return "gap", "holders unverified (RPC unavailable)", snap

    def social(self, symbol):
        """Live LunarCrush snapshot, ``RATE``, or None.

        Fixture mode is not a public reading. A canned topic must not qualify a
        borderline curve or lift a size cut.
        """
        if getattr(self.lc, "mode", None) != "live":
            return None
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
        # 2% keeps the untouched 0–2% launches out. The rest of the fresh-pool mass is in band
        # and is sized by sentiment instead of being rejected here.
        if not (rules["min_curve_pct"] <= curve <= rules["max_curve_pct"] and mcap <= rules["max_mcap_usd"]):
            return self.skip(
                key,
                f"P1 curve {curve}% not in {rules['min_curve_pct']}-{rules['max_curve_pct']}% / mcap ${mcap:,.0f}",
                False, coin,
            )
        verdict, holder_reason, fields = self.holder_verdict(coin)
        top = fields.get("top")
        bundle = fields.get("bundle")
        top10 = fields.get("top10")
        dev = fields.get("dev")
        supply = fields.get("supply")
        holder_gap = verdict == "gap"
        old = self.board.cards.get(key)
        seen = old["ts_seen"] if old and "ts_seen" in old else time.time()
        self.board.put(
            key, "checking", mint=mint, name=coin.get("name"), mcap=round(mcap), curve_pct=curve,
            top1_pct=top, top10_pct=top10, dev_pct=dev, bundle_pct=bundle, replies=replies, ts_seen=seen,
        )
        if verdict == "skip":
            return self.skip(key, holder_reason, False, coin)
        watch = self.watch.get(mint)
        if watch is None:
            periods = self._new_watch_periods()
            started = time.time()
            watch = dict(
                t=started, curve=curve, baseline_curve=curve, key=key, real_sol=state["real_sol"],
                watch_periods=periods, coin=dict(coin), holder_gap=holder_gap,
            )
            self.watch[mint] = watch
            self.board.put(
                key, "checking", reason=self._watch_reason(watch), watch_started_ts=started,
                watch_periods=self._copy_watch_periods(periods),
            )
            gap_note = " holders unverified, retrying" if holder_gap else ""
            self.ev(f"WATCH {key} curve {curve}%{gap_note} ({self._watch_reason(watch)})")
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
        rise_pts = curve - watch["curve"]
        flow = self.flow_for(coin, watch["t"], self._last_window_start(watch))
        if flow is None:
            # 70–85% still needs the tape's stronger buyer breadth. Replies cannot stand in for it.
            if strong:
                return self.skip(key, "flow unavailable: upper curve band needs buyer breadth", False, coin)
            flow_ok, flow_why = unavailable_tape_fallback(
                coin.get("reply_count"), rise_pts, rules["flow_fallback_rise_pts"],
            )
            if not flow_ok:
                return self.skip(key, flow_why, False, coin)
            late_why = "flow last window n/a (tape missing)"
            strength = 0.0
        else:
            flow_ok, flow_why = flow_allows(flow, rules, strong=strong)
            if not flow_ok:
                return self.skip(key, flow_why, False, coin)
            late_ok, late_why = last_window_allows(flow, rules)
            if not late_ok:
                return self.skip(key, late_why, False, coin)
            strength = signal_strength(flow, slices, rules, strong=strong, durations=durations)
        snap = self.social(symbol)
        half = snap == "RATE"
        if half:
            snap = None
        sentiment_tier = "missing" if half else public_sentiment_tier(snap, rules)
        if sentiment_tier == "veto":
            return self.skip(key, f"P3 sentiment {snap['sentiment_0_100']}/100", False, coin)
        if sentiment_tier == "strong":
            social_why = f"social buy {snap['sentiment_0_100']}/100, {int(snap['mention_volume'])} mentions"
        elif sentiment_tier == "neutral":
            social_why = f"social {snap['sentiment_0_100']}/100 below strong bar"
        elif half:
            social_why = "LunarCrush rate-limited -> half size"
        else:
            social_why = "no social topic (not a buy signal)"
        self.watch.pop(mint, None)
        if len(self.pos) >= rules["max_open"]:
            return self.skip(key, f"max {rules['max_open']} open", False, coin)
        usd = size_for_signal(strength, rules)
        if self.loss_streak >= rules["loss_streak_halve"]:
            usd /= 2
        if half:
            usd /= 2
        usd = min(usd, self.cash)
        if usd < 20:
            return self.skip(key, "no cash", False, coin)
        sentiment_buy = sentiment_tier in ("strong", "neutral")
        social_values = snap if isinstance(snap, dict) else {}
        created = coin.get("created_timestamp")
        quote = float(coin.get("market_cap_quote") or 0)
        sol_px = (mcap / quote) if mcap and quote else 0.0
        if sol_px <= 0:
            sol_px = sol_price_usd(mcap, state.get("virtual_sol"), state.get("virtual_token")) or 0.0
        if flow:
            flow_fields = dict(
                flow_buyers=flow["unique_buyers"], flow_signed_sol=round(flow["signed_quote_sol"], 4),
                flow_top_buyer_pct=round(flow["top_buyer_pct"], 2),
                flow_top3_buyer_pct=round(float(flow["top3_buyer_pct"]), 2),
                flow_meaningful_buyers=int(flow["meaningful_buyers"]),
            )
        else:
            # Unmeasured tape stays null so a fallback entry is not recorded as zero buyers.
            flow_fields = dict(
                flow_buyers=None, flow_signed_sol=None, flow_top_buyer_pct=None,
                flow_top3_buyer_pct=None, flow_meaningful_buyers=None,
            )
        feat = dict(
            key=key, mint=mint, curve_pct=curve, mcap=round(mcap), replies=replies, top10_pct=top10,
            dev_pct=dev, bundle_pct=bundle, rise_pts=round(rise_pts, 2),
            sentiment_0_100=social_values.get("sentiment_0_100") if sentiment_buy else None,
            mention_volume=social_values.get("mention_volume") if sentiment_buy else None,
            sentiment_buy=sentiment_buy, age_s=round(time.time() - created / 1000) if created else None,
            signal_strength=round(strength, 3), **flow_fields,
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
        # Clean in-band names get a bounded lift inside $130–160. Borderline names
        # do not: their haircut below is the risk control, only eased when sentiment is strong.
        if sentiment_tier == "strong" and not holder_gap and curve >= float(rules["early_curve_pct"]):
            score_n = float(snap["sentiment_0_100"])
            span = float(rules["size_max"]) - float(rules["size_min"])
            margin = sentiment_size_margin(score_n, rules)
            lift = min(float(rules["size_max"]) - usd, float(rules["sentiment_size_weight"]) * margin * span)
            if lift > 0.5:
                usd += lift
                social_why += f", sentiment weight +${lift:.0f}"
        # These multipliers are the intended ticket. They are not a depth resize,
        # and they do not turn a missing holder read into a measured pass.
        border_mult = float(rules["sentiment_border_size_mult"]) if sentiment_tier == "strong" else None
        size_notes = []
        if holder_gap:
            mult = border_mult if border_mult is not None else float(rules["holder_gap_size_mult"])
            usd *= mult
            note = f"holders unverified (RPC unavailable), size x{mult:.2f}"
            if border_mult is not None:
                note += f", lifted by social {snap['sentiment_0_100']}/100"
            size_notes.append(note)
        if curve < float(rules["early_curve_pct"]):
            mult = border_mult if border_mult is not None else float(rules["early_curve_size_mult"])
            usd *= mult
            note = f"early curve {curve}% < {rules['early_curve_pct']}, size x{mult:.2f}"
            if border_mult is not None:
                note += f", lifted by social {snap['sentiment_0_100']}/100"
            size_notes.append(note)
        if size_notes:
            sim_why += ", " + ", ".join(size_notes)
        usd = min(usd, self.cash)
        if usd < 20:
            return self.skip(key, "no cash", False, coin)
        drag_limit = float(rules["impact_max_drag_pct"])
        if drag_limit > -100:
            drag = flat_round_trip_pct(
                mcap or 1, usd, sol_px, state.get("virtual_sol"), state.get("virtual_token"), rules["curve_fee_bps"],
            )
            if drag is not None and drag < drag_limit:
                return self.skip(
                    key, f"curve impact {drag:.1f}% worse than {drag_limit:g}%", False, coin,
                )
        # The hard skip above used the intended size. Only a size that already
        # cleared it can be cut, so a thin book is never resized into a pass.
        intended = usd
        usd = size_for_curve_depth(
            intended, mcap or 1, sol_px, state.get("virtual_sol"), state.get("virtual_token"),
            rules["curve_fee_bps"], budget_pct=float(rules["impact_size_budget_pct"]),
            floor_usd=min(intended, float(rules["size_min"])),
        )
        if usd + 0.5 < intended:
            sim_why += f", depth size ${usd:.0f} from ${intended:.0f}"
        filled = buy_fill(
            mcap or 1, usd, sol_px, state.get("virtual_sol"), state.get("virtual_token"), rules["curve_fee_bps"],
        )
        entry_eff = filled["entry_eff"]
        feat["size_pct_equity"] = round(100 * usd / max(self.equity(), 1), 2)
        top10_why = f"top10 {top10}%" if top10 is not None else "top10 unverified"
        dev_why = f"dev {dev}%" if dev is not None else "dev unverified"
        why = (
            f"v8 PASS: curve {watch['curve']}->{curve}%, {accel_why}, {flow_why}, {late_why}, {social_why}, "
            f"{top10_why}, {dev_why}, fill eff ${entry_eff:,.0f} vs spot ${mcap:,.0f}{sim_why}"
        )
        self.traded.add(key)
        self.cash -= usd
        self.pos[key] = dict(
            mint=mint, usd=usd, entry=entry_eff, entry_eff=entry_eff, entry_spot=mcap or 1, peak=mcap or 1,
            last_spot=mcap or 1, t=time.time(), creator=coin.get("creator"), dev0=dev, sup=supply,
            scaled=False, boost=boost, trail_giveback_pct=trail, sentiment=feat["sentiment_0_100"], feat=feat,
            tokens=filled["tokens"], fee_bps=rules["curve_fee_bps"], curve_addr=coin.get("bonding_curve"),
            entry_real_sol=state["real_sol"], entry_flow_sol=sum(slices), entry_usd=usd, sol_px=sol_px,
            supply_raw=state.get("token_supply") or coin.get("total_supply"),
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
            coin = dict(watch.get("coin") or {})
            if not coin.get("mint"):
                self.ev(f"recheck {watch['key']} coin metadata missing")
                continue
            state = self.curve_state(coin)
            if not state:
                self.ev(f"recheck {watch['key']} curve reserves unavailable")
                continue
            if state.get("complete"):
                self.watch.pop(mint, None)
                continue
            priced = mcap_from_reserves(
                self.sol_usd, state.get("virtual_sol"), state.get("virtual_token"),
                state.get("token_supply") or coin.get("total_supply"),
            )
            if priced:
                coin["usd_market_cap"], coin["market_cap_quote"] = priced
            watch["coin"] = dict(coin)
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
            else:
                # Retry before the decision. A real over-cap reading skips. A still-missing
                # rate limit stays a gap and does not invent holder percents.
                retry, retry_reason, _retry_fields = self.holder_verdict(coin)
                if retry == "skip":
                    self.skip(watch["key"], retry_reason, False, coin)
                    continue
                if retry == "gap" and not watch.get("holder_gap"):
                    self.ev(f"HOLD {watch['key']} holders unverified (RPC), still watching")
                watch["holder_gap"] = retry == "gap"

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
            entry_spot = position.get("entry_spot") or position.get("entry")
            if vol_stop_hit(entry_spot, spot, band, rules["stop_loss_pct"]):
                spot_change = net_change_pct(entry_spot, spot)
                self.close(
                    key, spot,
                    f"vol stop {net:.0f}% (spot {spot_change:.0f}%, band {band:.0f}%)",
                    exit_eff=exit_price,
                )
            elif not position.get("scaled") and failed_impulse_hit(
                entry_spot, spot, position.get("peak"),
                rules["fail_impulse_drop_pct"], rules["fail_impulse_max_peak_pct"],
            ):
                # Quiet tapes already left through the −4% spot band above. This is the
                # widened-band path: spot is down and the trade never bounced.
                spot_change = net_change_pct(entry_spot, spot)
                peak_change = net_change_pct(entry_spot, position.get("peak") or spot)
                self.close(
                    key, spot,
                    f"failed impulse spot {spot_change:.0f}% (peak {peak_change:.0f}%)",
                    exit_eff=exit_price,
                )
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
        """Mark every open paper position from curve reserves and the latest SOL quote.

        The quote comes from the public pool listing. A missing quote or a missing
        curve account skips the mark instead of inventing a price.
        """
        self.last_pos_check = time.time()
        self.stats["pos_checks"] += 1
        if not self.pos:
            return
        keys = list(self.pos)
        curves = self.curves_for([self.pos[key].get("curve_addr") for key in keys])
        for key in keys:
            if key not in self.pos:
                continue
            position = self.pos[key]
            curve = curves.get(position.get("curve_addr")) or {}
            priced = mcap_from_reserves(
                self.sol_usd, curve.get("virtual_sol"), curve.get("virtual_token"),
                curve.get("token_supply") or position.get("supply_raw"),
            )
            if not priced:
                continue
            self.apply_mark(key, {
                "spot": priced[0],
                "sol_px": self.sol_usd or 0,
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
            listing="geckoterminal pump-fun pools",
            stats={**self.stats, "realized": round(self.stats["realized"], 2)},
            log=self.log, lunarcrush=self.lc.mode, updated=time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        )
        out = Path("out")
        out.mkdir(exist_ok=True)
        temporary = out / "board.json.tmp"
        temporary.write_text(json.dumps(state, indent=1))
        temporary.replace(out / "board.json")
        self._write_trade_charts(out)
        refresh_pid(out)

    def _write_trade_charts(self, out: Path) -> None:
        """Publish closed-trade charts beside the board. A missing ledger is an empty object.

        A parse failure leaves the previous chart file in place so a bad row cannot wipe it.
        """
        try:
            from .trade_charts import build

            built = build()
        except FileNotFoundError:
            built = {}
        except Exception:
            return
        if not isinstance(built, dict):
            return
        temporary = out / "trade_charts.json.tmp"
        temporary.write_text(json.dumps(built, separators=(",", ":")))
        temporary.replace(out / "trade_charts.json")

    def scan(self):
        """One public pump-fun pool page, then the same on-chain curve and holder gates.

        frontend-api-v3.pump.fun answers this client with HTTP 403 (Cloudflare
        challenge), so the listing is GeckoTerminal's pump-fun pools. Those pool
        addresses are the bonding-curve accounts. Pages alternate so a scan stays
        inside the public ~10 requests/minute budget.
        """
        self.stats["ticks"] += 1
        page = self._listing_page
        self._listing_page = 1 if page >= 2 else page + 1
        payload = http(listing_url(page), headers=GECKO_HEADERS, timeout=12)
        coins, sol_usd = coins_from_listing(payload)
        if sol_usd:
            self.sol_usd = sol_usd
        coins = self._enrich_coins(coins)
        if not self._listed_once:
            self._listed_once = True
            shown = f"{self.sol_usd:.2f}" if self.sol_usd else "unknown"
            self.ev(f"scan ok {len(coins)} pump-fun pools via geckoterminal (sol ${shown})")
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
            delay = float(self.strategy["scan_s"])
            try:
                self.scan()
                scanned = True
            except Exception as exc:
                self.ev(f"scan error {type(exc).__name__}: {str(exc)[:120]}")
                if getattr(exc, "code", None) == 429:
                    delay = max(delay, 60.0)
            self.next_scan = now + delay
        self.write()
        return scanned

    def tick(self):
        """Backward-compatible single step: positions, then a scan."""
        self.next_scan = 0
        return self.cycle()


def main():
    # Board, charts, ledger, and the pid file all use this working directory.
    os.chdir(Path(__file__).resolve().parents[1])
    load_env()
    cfg = load()
    cfg["mode"] = "paper"
    try:
        publish_pid(Path("out"))
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    try:
        feed = Feed(cfg)
        stop_delay = feed.strategy["pos_check_s"]
        signal.signal(signal.SIGTERM, lambda *args: (_ for _ in ()).throw(SystemExit))
        feed.ev(
            f"feed start PAPER strategy v8+distribution, curve "
            f"{feed.strategy['min_curve_pct']}-{feed.strategy['max_curve_pct']}% "
            f"(early <{feed.strategy['early_curve_pct']}% at x{feed.strategy['early_curve_size_mult']}, "
            f"holder RPC gap at x{feed.strategy['holder_gap_size_mult']}, "
            f"strong sentiment clears those cuts), "
            f"position check {stop_delay}s, "
            f"lunarcrush={feed.lc.mode}, listing=geckoterminal pump-fun pools"
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
    finally:
        release_pid(Path("out"))


if __name__ == "__main__":
    main()
