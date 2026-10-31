# Pumpmi live-market paper terminal

Pumpmi's high-voltage dashboard connects to the **running live-market paper engine**, not the repository's saved board. It receives actual public pump.fun market-cap observations and displays current checking/holding/done activity, real observed market paths, and paper executions from the durable forward portfolio. It never connects a wallet, signs a transaction, or executes a real-money order.

## Live connection

`GET /api/live-feed` bootstraps the current live engine state. `GET /api/live-feed/events` is a server-sent-event stream emitting named `feed` events and connection heartbeats. The dashboard consumes these events automatically, with reconnect and display-pause controls. Pausing the display does not stop the paper worker.

There is **no historical-file fallback** in normal browsing. If neither the durable optimizer nor a running file-backed paper worker with a fresh board is available, `/api/live-feed` and `/api/live-feed/events` return `503`. The interface shows connecting/disconnected/stale states instead of displaying an archived dataset. Opening a stream does not itself prove fresh prices: frames must carry verified source timestamps. The old `/api/snapshot` route is a compatibility alias for live-only data, not a saved-file response.

Watched tokens have market-cap paths derived from real observations. A path without paper execution is marked as a market watch, has no fabricated BUY/SELL signals, and is excluded from closed-trade counts, realized P&L, and the journal. Completed paper trades use actual modeled entry/exit amounts and observed prices. These sampled paths are not exchange candle histories or measured real-money fills.

## Run locally

Install the pinned dependencies:

```bash
corepack enable
pnpm install --frozen-lockfile
python3 -m pip install -r pump-board-engine/requirements.txt
```

Provide a MySQL `DATABASE_URL` through protected server-side configuration. The managed project already supplies its database connection. Never put actual database credentials or authenticated RPC URLs into frontend code or Git commits.

Run the frontend on port 3000:

```bash
pnpm dev
```

In a second terminal, run the real-data paper API on port 3001:

```bash
PUMPMI_OPTIMIZER_ENABLED=1 pnpm dev:api
```

Vite proxies the relative `/api` endpoints to the adapter. For production, the Dockerfile builds the frontend and serves a live-data dashboard replica on port 3000 with `PUMPMI_WORKER_ENABLED=0`. The selected Cloud Computer runs the continuous worker under systemd with the worker flag enabled. `GET /health` is a cheap liveness check; `/api/optimizer` exposes actual worker ownership, source freshness/errors, and portfolio status.

### File-backed paper worker without MySQL

When `DATABASE_URL` is unset, the adapter serves the file-backed paper worker instead of the optimizer. That path is still paper-only: public pump.fun ticks and modeled fills, with no wallet, signature, or live order. `POST /api/simulate` and the checked-in fixtures stay test utilities and are not used as a live feed.

The adapter imports the engine, which needs PyYAML (`config.py`). Use the engine virtualenv for the API and the worker when system Python does not already have the engine requirements:

```bash
python3 -m venv pump-board-engine/.venv
pump-board-engine/.venv/bin/pip install -r pump-board-engine/requirements.txt
```

Start the worker with the engine as the working directory so Python can import `src`. On startup it switches to `pump-board-engine` and publishes `out/board.json`, `out/trade_charts.json`, and `out/live_feed.pid`:

```bash
cd pump-board-engine
.venv/bin/python -m src.live_feed
```

In another terminal, run the API with that same interpreter and no database URL:

```bash
DATABASE_URL= pump-board-engine/.venv/bin/python scripts/serve.py
```

`/api/live-feed` and the SSE stream `/api/live-feed/events` return the board only while that pid is a live `src.live_feed` process and `out/board.json` was published within 180 seconds. A current `board.ts` is `meta.kind` `live`; a missing timestamp is `awaiting`; a timestamp more than a few seconds ahead is `stale`. A stopped worker, a file older than 180 seconds, or a `board.ts` older than 180 seconds returns `503`. `meta.simulationAvailable` stays false. `pnpm dev` on port 3000 proxies `/api` to this adapter. Restart the worker after upgrading so it writes the pid file.

## Three watch periods and six positions

Every token displays Short, Medium, and Long watch checkpoints. Three sliders save the actual duration parameters, with increasing 15-second increments, 350-millisecond debouncing, revision checks, and visible saving/failure/retry states. The engine captures settings when a new watch starts, so changing a slider does not rewrite an in-progress watch. Maximum open paper positions stays fixed at **six**.

`GET /api/strategy` reads the effective settings. `PUT /api/strategy` accepts only:

```json
{"watchPeriods":{"short":90,"medium":180,"long":300},"revision":0}
```

Use the current returned revision, not this illustrative zero. The user's currently saved settings are preserved. MySQL persists configuration, observations, positions, reviews, and worker coordination; restart does not reset the bankroll or champion. Local configuration editing without a database remains available. Editing that file does not itself publish a live feed; start `python -m src.live_feed` from `pump-board-engine` for the file-backed path.

## Holder-risk RPC

Set `SOLANA_RPC_URL` as a protected server-side secret to a reliable Solana mainnet HTTPS endpoint supporting `getTokenLargestAccounts`, `getTokenSupply`, and `getTokenAccountsByOwner`. Shared public RPC can return 429 or 403. Unknown or unverifiable concentrations remain entry-blocking; they are not silently treated as safe zero values.

Real live prices were verified in this session, but public holder RPC access is rate-limited and no paper fills are claimed from that readiness check. The configured watch windows and risk gates must pass before entry. The UI can therefore show real market paths with zero paper trades, without substituting offline test fills.

## Strategy and hourly analysis

The worker reviews at 30-minute boundaries, considering at most seven neighboring watch configurations. Chronological training and held-out cohorts have no future-data leakage. Promotion requires at least 30 completed holdout trades, positive modeled net expectancy, profit factor above 1.1, improvement over the champion on the same held-out set, and drawdown no greater than 10%. Insufficient evidence produces a waiting decision, not an arbitrary change.

Costs explicitly assume **1% fees plus 1% slippage per side**, not measured fills or a provider quote. Gross and modeled net results remain separate. Audited rollback can restore a previously validated champion after excessive forward drawdown. Repeated testing can still overfit; neither a software check nor a profitable paper window guarantees future profit.

Hourly capability reports evaluate contiguous 3,600-second operation windows on the same portfolio. They record completed-trade gross/net results, modeled costs, equity, observed drawdown, fresh-source coverage, and strategy revisions. Long data gaps are insufficient evidence. A no-trade hour is not a win, and mixed strategy revisions are not a controlled enhancement comparison.

The user stopped the ten-minute agent-led cycle; it remains paused. Application-native candidate review runs every 30 minutes on the systemd-hosted Cloud Computer worker and retains the incumbent when fresh chronological holdout evidence is insufficient. The managed HTTPS site is a reader, not the continuous worker. No additional Reserved hosting purchase is required for this selected existing VM; normal computer traffic/budget conditions apply.

## Validation and preserved archives

```bash
pnpm build
pnpm test:ui
python3 scripts/test_api.py
python3 scripts/test_optimizer.py
python3 scripts/test_live_stream.py
python3 scripts/test_hourly.py
python3 scripts/test_rpc_config.py
python3 scripts/test_state_cache.py
cd pump-board-engine && python3 -m pytest -q tests
```

Checks use explicit mocks, injected SQLite, or isolated temporary fixture runs—not disposable writes to the shared managed database. Original captured boards, ledgers, and fixtures are preserved as archives/test evidence and are not normal dashboard sources. `POST /api/simulate` remains an isolated offline test utility and is not offered as the dashboard's live paper-trading action.

Reduced-motion preferences, keyboard navigation, inspection, live paper journal filtering, and strategy controls remain available. A short committed-state telemetry cache and non-overlapping client requests reduce database contention; actual observation queries and strategy revision checks remain uncached.

## Improvement-cycle Git discipline

Each authorized change pulls the latest GitHub `main` first, implements and validates the changes, fetches `main` again before delivery, merges any intervening updates, reruns affected checks, and pushes the integrated result back to `main`. There is no force-push or overwrite of independent edits. The ten-minute strategy task remains paused; it is not restarted by a dashboard change.

The managed dashboard uses the durable forward-paper optimizer when `DATABASE_URL` is configured. Without that database, `/api/live-feed` and `/api/live-feed/events` serve the file-backed `src.live_feed` paper worker only while its pid is alive and its board is fresh. Active strategy telemetry still reports the durable worker's actual limits when that runtime is enabled. A future strategy integration must be validated against the runner that is actually serving the feed.

## Health check from Manus Preview

Open this project's Preview and use `/health` on that same origin. The development proxy forwards it to the real API and returns JSON, just as production does. `http://127.0.0.1:3001/health` is a sandbox-local address; opening it in a browser on your Mac does not reach this cloud server. A healthy response is `{"ok":true,"optimizer":"requested"}`. This is liveness only, not proof of a profitable strategy or fresh market data.

## Slider-save concurrency

Local file-backed saves now lock a stable sibling advisory file across processes for the entire read/revision-check/replace operation. Atomic JSON replacement alone does not provide compare-and-swap; the advisory lock closes that race. A simultaneous stale save receives a revision conflict instead of silently overwriting another value. Durable MySQL saves use the running adapter's transactional revision checks. An already-applied draft retry clears obsolete error text, while dragging again during an in-flight save preserves the newer draft and sends it against the returned revision.

Run `pnpm test:sliders` for mocked browser-save races and `python3 -m pytest -q tests/test_strategy_cas.py` from `pump-board-engine` for local cross-process CAS, failure recovery, strict 15-second/increasing-duration validation, and immutable six-position limit checks. These commands never edit the real strategy or create production paper fills.

## Worker handoff and atomic promotion

Each durable worker write unit starts with a root lease fence. SQLite reserves its writer transaction with `BEGIN IMMEDIATE`; MySQL locks the lease row with `SELECT ... FOR UPDATE`. Ownership and expiry are checked again immediately before the root commit. Nested store operations use savepoints and cannot independently commit portfolio, observations, closed trades, counters, health, or hourly reports. Failed transaction setup restores transaction depth and closes its cursor; an attempt to place a worker fence inside an ordinary unfenced outer transaction is rejected.

Collection happens outside database locks. Post-collection renewal can only extend an unexpired lease still belonging to that collector; it cannot reacquire an expired or handed-off lease to commit an old batch. The next worker cycle can acquire fresh authority normally. Trading inputs bypass telemetry caching, initialization inserts only missing state, and a persisted over-cap portfolio is rejected rather than reset or silently liquidated. At the separate 64-token watch cap, admission can safely retire the oldest decided non-held watch instead of permanently blocking new discovery; pending timelines and held tokens are protected.

Candidate validation captures one strategy document before it begins. Promotion must CAS against that same revision. Config, audit, and promotion/rollback markers commit atomically, and a user save clears obsolete promotion rollback lineage. Newly watched tokens use settings re-read after collection; already-started watches retain their original durations. Frame freshness uses response-time wall-clock checking so a delayed read of a newer real quote is not falsely classified as future data.

Run `pnpm test:concurrency` for two-connection SQLite handoff, six-position/seventh-entry denial, expiry, stale-cache, config/validation races, atomic failure rollback, initialization, admission, transaction-setup recovery, and old-batch rejection checks. MySQL lock SQL is checked in software, but destructive MySQL handoff/failure tests are not run against the shared live database. `scripts/verify_live_readonly.py` uses GET requests only to verify a running worker, current source state, preserved settings/bankroll, and at most six positions. These safeguards do not supply missing holder RPC data or establish a profitable strategy.

### Reconciled reserve-curve data

Concurrent main PR #5 is retained in the repaired runtime. Curve progress comes from actual bonding-curve reserve account data via one batched `getMultipleAccounts` read, not market capitalization divided by a guessed graduation threshold. A missing reserve read does not create a zero-percent watch anchor, qualify an entry, or falsely flatten a held position. Market-cap observations still update held paper positions; pending watches preserve their captured durations. An adequately provisioned `SOLANA_RPC_URL` is needed for both reserve and holder validation. Historical proxy-curve statistics in older diagnostics should not be treated as reserve measurements for this version.

## Price-feed activity versus paper-entry readiness

The price feed can be live while no paper entry qualifies. `entryReadiness` now separates pending watches, unverified holder/reserve data, overdue watches, and cumulative expired watches; the dashboard explicitly shows entry blocking rather than implying an inactive price feed. Local replicas may be standby readers of a shared lease-owned worker and must not force a takeover.

An undecided non-held watch expires once its own captured long duration plus 120 seconds passes. Expiry is a lease-fenced SKIP decision, not a closed trade, cash mutation, or profit/loss. Held positions are protected, and expiry works even for empty/source-failed batches. Decided expired watches can release bounded admission slots without rewriting the timing of surviving watches.

Holder checks interleave listing and refreshed tracked candidates and prefer the least recently attempted tracked member. This avoids repeatedly selecting the first member of stable eight-token refresh chunks. Metadata remains bounded to current candidates, and per-poll limits remain two holder checks, eight details, and one reserve batch. Failed risk-cache values use short failure backoff; known values retain their positive TTL.

The configured protected provider was tested without logging its URL/key: version and real reserve reads succeeded, but holder verification returned HTTP 403. Provider permission for `getTokenLargestAccounts` (and the subsequent supply/owner methods) must be enabled, or a compatible provider supplied, before those risk checks can qualify paper entries. No alternate route, unknown-as-safe value, fake fill, or risk relaxation is used to bypass that denial.

Run `pnpm test:liveness` and `pnpm test:entry-status` for isolated expiry/scheduling and readiness-display regressions. The selected VM now runs the writer continuously with automatic service restart; the on-demand managed site reads its durable data. The replacement provider's holder method remains rate-limited (HTTP429), not proven fully operational.

If the event transport repeatedly fails, the client falls back to one in-flight current-live JSON read every 15 seconds while EventSource retries. Repeated stream errors cannot continually cancel those reads; a valid event resumes streaming and stops polling. The status caption explicitly labels live polling. This is still fresh data from the running engine, never a stored snapshot or test fixture. Display pause/unmount cancel recovery.

The browser-side live JSON request allowance is 35 seconds: a measured valid live response took about 21 seconds and was previously aborted by a 20-second limit. Polling still allows only one in-flight read, and failures remain explicit rather than reverting to archives.

## Live signal reasoning and near-trigger percentages

The dashboard's signal panel reads bounded, durable per-token diagnostics from the running worker. It ranks tokens approaching buy or sell conditions and displays actual measured values, required thresholds, missing-data blockers and timestamped operational reasons. Filter by near-buy/near-sell or show all actually screened tokens, expand gate facts, and inspect a matching token's live chart. The reason log distinguishes a diagnostic READY from an actual paper BUY, partial SCALE, full SELL or skipped/expired watch. Display pause freezes the log only, not the trader.

**Buy readiness** is progress across the current strategy's required gates. **Sell proximity** is progress toward the nearest applicable exit trigger for an actual open position. These are deterministic heuristic scores, **not calibrated probabilities of buying, selling or profit**. A high score with a BLOCKED badge cannot justify a trade; unknown or stale data remain explicit, no open position means no sell candidate, and 100% readiness requires current data satisfying the authoritative policy. With no eligible/near candidates, the panel says so instead of fabricating percentages or trades.

Signal diagnostics use existing observations and unchanged policy rules, no extra RPC requests. They persist inside the same lease-fenced transaction as the paper state, remain bounded/deduplicated across handoffs, and do not change user watch settings, bankroll, fees/slippage, risk filters or the six-position cap.
