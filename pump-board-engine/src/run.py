import argparse, json, time
from .config import load
from .holders import make_holders
from .social_crawl import make_source
from .engine import Engine

def main(argv=None):
    ap = argparse.ArgumentParser(description="pump board engine (paper by default)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true"); g.add_argument("--paper-loop", action="store_true")
    ap.add_argument("--config"); ap.add_argument("--max-ticks", type=int, default=20); ap.add_argument("--sleep", type=float, default=0)
    a = ap.parse_args(argv)
    cfg = load(a.config)
    if a.paper_loop and cfg["mode"] != "paper": raise SystemExit("--paper-loop requires mode: paper")
    h = make_holders(cfg); e = Engine(cfg, h, make_source(cfg))
    if a.once:
        e.run_feed(h.new_coins())
    else:  # paper loop: re-poll the feed each tick (fixture feed is static, so de-dupe by symbol)
        seen = set()
        for _ in range(a.max_ticks):
            new = [c for c in h.new_coins() if c["symbol"] not in seen]; seen |= {c["symbol"] for c in new}
            e.run_feed(new)
            if e.closed: break
            time.sleep(a.sleep)
    p = e.write_board(); st = json.loads(p.read_text())
    print(f"\nmode={cfg['mode']} equity=${st['equity']} ({st['multiple']}x) board -> {p}")
    for card in st["columns"]["done"]:
        print(f"  {card['symbol']:7} {card.get('verdict')} {card.get('reason')} {card.get('pnl','')}")
if __name__ == "__main__": main()
