# Pumpmi live-market paper terminal

Pumpmi's high-voltage dashboard connects to the **running live-market paper engine**, not the repository's saved board. It receives actual public pump.fun market-cap observations and displays current checking/holding/done activity, real observed market paths, and paper executions from the durable forward portfolio. It never connects a wallet, signs a transaction, or executes a real-money order.

## Live connection

`GET /api/live-feed` bootstraps the current live engine state. `GET /api/live-feed/events` is a server-sent-event stream emitting named `feed` events and connection heartbeats. The dashboard consumes these events automatically, with reconnect and display-pause controls. Pausing the display does not stop the paper worker.

There is **no historical-file fallback** in normal browsing. If the worker/database is unavailable, the endpoint returns `503`; the interface shows connecting/disconnected/stale states instead of displaying an archived dataset. Opening a stream does not itself prove fresh prices: frames must carry verified source timestamps. The old `/api/snapshot` route is a compatibility alias for live-only data, not a saved-file response.

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

Vite proxies the relative `/api` endpoints to the adapter. For production, the Dockerfile builds the frontend and enables the live paper worker on port 3000. `GET /health` is a cheap liveness check; `/api/optimizer` exposes actual worker ownership, source freshness/errors, and portfolio status.

## Three watch periods and six positions

Every token displays Short, Medium, and Long watch checkpoints. Three sliders save the actual duration parameters, with increasing 15-second increments, 350-millisecond debouncing, revision checks, and visible saving/failure/retry states. The engine captures settings when a new watch starts, so changing a slider does not rewrite an in-progress watch. Maximum open paper positions stays fixed at **six**.

`GET /api/strategy` reads the effective settings. `PUT /api/strategy` accepts only:

```json
{"watchPeriods":{"short":90,"medium":180,"long":300},"revision":0}
```

Use the current returned revision, not this illustrative zero. The user's currently saved settings are preserved. MySQL persists configuration, observations, positions, reviews, and worker coordination; restart does not reset the bankroll or champion. Local configuration editing without a database remains available, but it does not make a live feed available.

## Holder-risk RPC

Set `SOLANA_RPC_URL` as a protected server-side secret to a reliable Solana mainnet HTTPS endpoint supporting `getTokenLargestAccounts`, `getTokenSupply`, and `getTokenAccountsByOwner`. Shared public RPC can return 429 or 403. Unknown or unverifiable concentrations remain entry-blocking; they are not silently treated as safe zero values.

Real live prices were verified in this session, but public holder RPC access is rate-limited and no paper fills are claimed from that readiness check. The configured watch windows and risk gates must pass before entry. The UI can therefore show real market paths with zero paper trades, without substituting offline test fills.

## Strategy and hourly analysis

The worker reviews at 30-minute boundaries, considering at most seven neighboring watch configurations. Chronological training and held-out cohorts have no future-data leakage. Promotion requires at least 30 completed holdout trades, positive modeled net expectancy, profit factor above 1.1, improvement over the champion on the same held-out set, and drawdown no greater than 10%. Insufficient evidence produces a waiting decision, not an arbitrary change.

Costs explicitly assume **1% fees plus 1% slippage per side**, not measured fills or a provider quote. Gross and modeled net results remain separate. Audited rollback can restore a previously validated champion after excessive forward drawdown. Repeated testing can still overfit; neither a software check nor a profitable paper window guarantees future profit.

Hourly capability reports evaluate contiguous 3,600-second operation windows on the same portfolio. They record completed-trade gross/net results, modeled costs, equity, observed drawdown, fresh-source coverage, and strategy revisions. Long data gaps are insufficient evidence. A no-trade hour is not a win, and mixed strategy revisions are not a controlled enhancement comparison.

The separately requested ten-minute agent-led GitHub improvement schedule is not active: creation was rejected while the project was undeployed. Paid always-on hosting still requires its product confirmation and publication. A current Preview worker is not a confirmed 24/7 deployment.

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

Each authorized cycle pulls the latest GitHub `main` first, implements and validates the changes, fetches `main` again before delivery, merges any intervening updates, reruns affected checks, and pushes the integrated result back to `main`. There is no force-push or overwrite of independent edits. This workflow does not itself mean the deployment-dependent ten-minute timer is active.

The managed dashboard is connected to the durable forward-paper optimizer. The separately available legacy `src.live_feed.Feed` implementation and its newly pulled v8 helper modules are preserved in this repository, but are a distinct runner. Active strategy telemetry reports the durable worker's actual limits rather than incorrectly advertising unused legacy thresholds. A future strategy integration must be validated against the active runner, not assumed from changes to another entry point.

## Health check from Manus Preview

Open this project's Preview and use `/health` on that same origin. The development proxy forwards it to the real API and returns JSON, just as production does. `http://127.0.0.1:3001/health` is a sandbox-local address; opening it in a browser on your Mac does not reach this cloud server. A healthy response is `{"ok":true,"optimizer":"requested"}`. This is liveness only, not proof of a profitable strategy or fresh market data.
