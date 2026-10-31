# pump-board-engine

**PAPER ONLY.** No wallet signing and no live spends. This tree records simulated fills only.

Rule + sentiment + social-crawl engine behind the checking → holding → done pump.fun board.
**No private keys. No signing. No live orders.** `mode: live` is a stub that refuses.

## Run (paper)
```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml   # optional; falls back to example
make test                            # pytest
make once                            # .venv/bin/python -m src.run --once
make loop                            # --paper-loop --max-ticks 3
```
Outputs: `out/board.json` (board columns for the Page), `out/ledger/{fills,skips}.csv`, `out/ledger/do_not_rebuy.json`.

## Pipeline (`src/engine.py`)
new coin → `holders.py` metrics → `rules.entry_checks` → `social_crawl` fetch → `sentiment.score_posts` + `velocity`
→ `rules.social_ok` → BUY (holding) / SKIP (done) → each tick: bundle moved → EXIT, stop-loss, take profit at top,
equity ≥ goal × bankroll → close all.

Rules (config `rules:`): dev > 10% skip · bundle > 25% skip · known-rug dev skip forever (added to do_not_rebuy)
· honeypot / dev_sold skip · bundle moves exit · 4× goal close all · min X posts in 15m + min sentiment.

## Social crawl (`src/social_crawl.py`, `social.source`)
- `fixture` — `fixtures/posts.json`, offline.
- `x_api` — X API v2 `tweets/search/recent` + `tweets/counts/recent`, needs `X_BEARER_TOKEN` (paid tier).
- `grok_mcp` — Grok Bot runs the x MCP tools (`search_posts_all`, `get_posts_counts_recent`,
  `get_users_by_username`) and writes `out/social/<SYMBOL>.json` as `{"posts":[...],"counts":[...]}`; engine reads it.

Sentiment: crypto lexicon (moon/🚀/gem vs rug/scam/🚩), negation window, engagement + follower weighting, tanh to [-1,1].

## Holders (`src/holders.py`)
Fixture now. `HeliusHolders` stub documents the plan: largest accounts → dev %, same-slot funded wallets → bundle %,
websocket on bundle wallets → bundle_moved, dev ATA delta → dev_sold.

## Going live later (not enabled)
`src/live.py::execute_live` refuses unless `mode: live` AND `PUMP_ENGINE_ENABLE_LIVE=true`, and even then raises
because no executor exists. Plug-in point: route each order to Grok Bot's Phantom flow (Phantom MCP swap, or pump.fun
in the box browser with Phantom confirm) with Simran approving every trade. Keys never enter this repo.

## Risk
Fixture numbers are made up. Most pump.fun coins go to zero; rugs, bundles, and MEV beat simple rules.
Sentiment on X is easy to fake with bots. This isn't financial advice. Only trade what you can lose.

## Multi-platform social: LunarCrush aggregator (default `social.primary: lunarcrush`)
Platforms covered: X, Reddit, YouTube, TikTok, Instagram, and news.
- `src/aggregators/lunarcrush.py` wraps API v4 (`https://lunarcrush.com/api4`, `Authorization: Bearer $LUNARCRUSH_API_KEY`):
  `/public/topic/:topic/v1`, `/public/topic/:topic/posts/v1`, `/public/topic/:topic/news/v1`, `/public/coins/list/v2`.
- `src/aggregators/fuse.py` turns each network's `types_count`, `types_interactions` and `types_sentiment` into one
  `SocialSnapshot` with: mention_volume, interactions, sentiment_0_100, sentiment_-1_1, trend, by_platform, headlines,
  top_posts, and reasons. A volume spike means a platform has at least 2× its usual share of mentions.
  Sentiment of 60 or more counts as positive; 40 or less counts as negative.
- The engine checks `social.min_mentions` and `social.min_sentiment` (0-100 scale). Each decision reason looks like
  `social ok 85.1/100, 7486 mentions | tiktok volume spike + tiktok positive + x positive`. Those reasons, plus the
  per-platform breakdown, go into `out/board.json` and `out/ledger/skips.csv`.

**Fixture vs. live**
- No `LUNARCRUSH_API_KEY` set: the client reads `fixtures/lunarcrush.json`, which works offline and is used by `make once` and the tests.
- `LUNARCRUSH_API_KEY` set: the client makes real HTTP calls only and never falls back to the fixture. HTTP errors raise
  `LunarCrushError`. A missing posts or news endpoint, or one your plan doesn't include, comes back as an empty list.
  Run `export LUNARCRUSH_API_KEY=...; make once`. The live test (`tests/test_lunarcrush.py::test_live_topic`) is skipped when no key is set.

**Getting a key:** sign up at https://lunarcrush.com/developers/api and create an API key in the dashboard. Plans:
the free Hobby tier is mostly market data with tight rate limits, and the topic and social endpoints need a paid plan.
Check the current limits on the pricing page and expect 429 errors if you poll every coin. LunarCrush ToS: the data is
for your own use and may not be resold or redistributed, so check their terms before showing it publicly.

**Not scraped (future, opt-in only):** Telegram and Discord. Groups are often private, and scraping them raises ToS and legal problems.
They could be added later only through bots invited into public channels with admin consent. The same applies to StockTwits,
Bitcointalk, 4chan /biz/, Farcaster, and TradingView: possible extra sources later, none implemented.

Trading is still paper only. Social data never triggers a real order.
