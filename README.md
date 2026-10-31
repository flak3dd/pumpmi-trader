# Pumpmi high-voltage paper terminal

Pumpmi is a React terminal for the existing paper-only pump.fun board engine. It reads recorded market snapshots, inspects historical paper executions, runs isolated offline fixture simulations, and exposes narrowly scoped watch-duration configuration. It never connects a wallet, signs a transaction, or executes a real-money order.

## Run locally

Install dependencies at the repository root, then start the frontend:

```bash
corepack enable
pnpm install --frozen-lockfile
python3 -m pip install -r pump-board-engine/requirements.txt
pnpm dev
```

In a second terminal, start the API:

```bash
pnpm dev:api
```

Vite serves port 3000 and proxies `/api` to the Python adapter on port 3001. For production, use `pnpm build && pnpm start`; the adapter serves the built frontend on port 3000. The Dockerfile builds the same frontend and Python runtime.

## Three watch periods per token

Every token card and inspection view shows Short, Medium, and Long watch windows. Their defaults are **90 seconds, 180 seconds, and 300 seconds**. The three tier sliders adjust these actual duration parameters, not a cosmetic risk score. Moving a slider debounces an automatic save by 350 milliseconds. Saving, saved, failure, and reconnect states are visible; failed drafts stay available for retry.

Durations must be increasing integers in 15-second increments, from 30 seconds to 30 minutes. Each slider respects its neighboring durations. The paper feed captures the duration configuration when a new token enters Checking, collects curve observations at the three due checkpoints, and applies its existing entry gates only after the long window completes. New config applies to newly watched tokens; in-progress watches keep their captured timing.

A recorded snapshot is not a running market feed. The displayed windows use the snapshot timestamp rather than an invented live countdown. Observed checkpoints are marked only when the engine supplies a measurement. A historical window with no measurement stays explicitly unmeasured, even if its duration has elapsed.

The maximum number of open paper positions is **6**. The strategy configuration, dashboard slots, market-data paper feed, and fixture engine all enforce or display that same limit.

## Configuration API and persistence

`GET /api/strategy` returns the effective numeric strategy, `watchPeriods`, `revision`, `editable`, and `paperOnly`. `PUT /api/strategy` accepts only this shape:

```json
{"watchPeriods":{"short":90,"medium":180,"long":300},"revision":0}
```

Without a managed database, the adapter validates values and atomically saves `pump-board-engine/strategy.json`. With the durable optimizer enabled, updates are revision-checked in MySQL instead. Revision conflicts return `409` rather than silently overwriting another change. The frontend retains the draft and offers a retry after reading the current revision. Other risk controls remain read-only, and the capacity stays fixed at six.

Local file configuration survives adapter/feed restarts in the same working directory but not necessarily a deployment replacement. The always-on optimizer persists configuration, observations, paper positions, and reviews in the managed database. Its initial baseline is the current user-edited strategy document, seeded only into a missing namespace; saved database settings are never reset from a repository file on boot. Changing sliders alone does not enable a worker.

## Snapshots, simulations, and charts

`GET /api/snapshot` returns the recorded `out/board.json` and `out/trade_charts.json`, plus configuration metadata. `POST /api/simulate` accepts only `{}` and runs the existing fixture engine once in temporary storage. Original snapshots, ledgers, and fixture files are preserved. Fixture simulation has no market-time checkpoint samples and is labeled accordingly. `GET /health` reports adapter readiness.

The terminal includes separate Checking, Holding, and Done lanes, token inspection, an interactive trade spotlight, a searchable journal with outcome filters and CSV export, and a strategy console. A re-entry skip remains Skipped even if that token has older trade history. Total P&L and return consistently include all ledger exit legs; the latest board decision is labeled separately because it may describe the final leg or a later skip.

Recorded market-cap charts use the original ledger interpolation. Fixture charts show exact unit-price BUY/SELL executions, retaining decimal precision rather than claiming those values are USD market caps. The legacy `signals[].mcap` schema key stores unit prices only when `metric` is `unit_price`. Neither chart type is a full candle history.

`python3 scripts/export_snapshot.py` regenerates the static fallback. Without the adapter, recorded browsing still works, but simulation and config saving are unavailable.

## Validation

```bash
pnpm build
pnpm test:api
pnpm test:ui  # API must be running on port 3001
cd pump-board-engine && python3 -m pytest -q tests
```

API and engine checks cover configuration validation/persistence, revision conflicts, three real watch checkpoints, six-position enforcement, exact fixture price data, and isolation from recorded datasets. DOM checks cover three token windows, three debounced sliders, failed-save retry, slots, lanes, journal filtering, inspection, preferences, simulation, and snapshot restoration. No browser is needed for these checks.

Energy flow respects system reduced-motion preferences and can be disabled in Settings. Keyboard users can focus search with Command/Ctrl+K and close dialogs with Escape. For the original engine architecture and risks, see [pump-board-engine/README.md](pump-board-engine/README.md).

## Always-on forward-paper optimizer

The production Docker service enables a deterministic worker with `PUMPMI_OPTIMIZER_ENABLED=1`. It requires a managed MySQL `DATABASE_URL` and reserved always-on hosting; a Preview process or a sleeping on-demand container is not a 24/7 deployment. The optimizer collects public pump.fun market-cap observations, rotates bounded token refreshes, and records a strategy review at each 30-minute boundary. Public-source failures are visible and never replaced with fixture prices or fixture sentiment. LunarCrush sentiment is currently unavailable and is not used as a fake signal.

This forward-paper portfolio is distinct from the checked-in historical ledger. It starts with a $1,500 simulated bankroll, retains the six-position cap and paper exit constraints, and records gross results, modeled execution costs, net results, equity, drawdown, and completed trades. Costs assume **1% fees plus 1% slippage per side**; these are explicit modeling assumptions, not measured fills or a quoted provider fee schedule. Sampling and gap risk remain limitations of the model.

At most seven neighboring Short/Medium/Long configurations are considered. Candidate selection uses earlier chronological cohorts; evaluation uses later held-out cohorts, with training prices cut off at the split. No promotion occurs before at least 30 completed held-out trades, positive modeled net expectancy, profit factor above 1.1, improvement over the champion on the same held-out cohort set, and drawdown no greater than 10%. Open trades are not artificially closed for a favorable score. Sparse or stale data produces a waiting-for-evidence report rather than an arbitrary config change. A promoted strategy can be rolled back to the prior validated champion if forward paper drawdown exceeds the threshold. Repeated testing can still overfit; none of these gates guarantees future profit or profit in every half-hour.

`GET /api/optimizer` reports worker state, lease ownership, source freshness/errors, champion settings, evidence decisions, cost assumptions, six recent half-hour windows, and review history. When the forward feed has observations, `GET /api/snapshot` can return a `live` forward-paper board and sampled market-cap paths. Pausing UI monitoring does **not** stop the independent worker. Fixture simulation remains isolated.

Initialize the additive optimizer schema and preserve any existing seed:

```bash
python3 scripts/migrate_optimizer.py
```

The managed database is shared by development and production. Software checks use injected SQLite stores and source mocks, never production market records as fabricated profit evidence. Do not run live integration writes as disposable tests. The worker uses a database lease so overlapping deployment processes do not independently run the same paper portfolio. Container restarts recover durable state rather than creating new bankrolls.

```bash
python3 scripts/test_optimizer.py
```

Reserved hosting must be confirmed in the product's hosting card and the accepted source version published before this is reported as an active always-on service. The UI displays disabled, warming-up, degraded, and waiting states explicitly. Keep the project's visibility restricted to authorized collaborators if strategy configuration or performance data is private.

## Hourly capability and profitability analysis

Once the published always-on paper worker is active, its monitor evaluates contiguous 3,600-second operation windows from activation. It never launches an overlapping copy or resets the bankroll. Hourly reports persist net/gross completed-trade P&L, modeled execution costs, equity change, observed drawdown, trade count, win rate, fresh-source coverage, and strategy revisions. Sparse or stale coverage is marked insufficient; a full hour with no completed trades is not labeled a win. A window spanning several strategy revisions is an operational summary, not a controlled comparison proving that an enhancement caused profit. Hourly reports appear in the optimizer status panel.

The separately requested agent-led GitHub improvement cadence is every 10 minutes, targeting two tested, evidence-based enhancements and pushes to `main`. Its scheduling request was rejected while this project was undeployed. It is **not enabled** until the hosting confirmation and deployment prerequisites are completed and scheduling succeeds. This code does not fake that timer with a sandbox loop.

## Production holder-risk RPC

Set `SOLANA_RPC_URL` as a protected **server-side project secret** to a reliable Solana mainnet HTTPS endpoint supporting `getTokenLargestAccounts`, `getTokenSupply`, and `getTokenAccountsByOwner`. Do not put a provider URL containing an API key in frontend code or a Git commit. Without it, the bot tries Solana's shared public endpoint and retains unknown-risk entry blocking on rate limits.

The real 2026-10-31 readiness run collected live pump.fun prices, but the public Solana endpoint returned 429 and the documented PublicNode alternative returned 403 for holder reads. No access-denied endpoint was bypassed and no paper entry was forced. The worker can collect actual prices while entries await both the configured watch windows and validated risk metadata. Continuous hosting and the requested ten-minute agent schedule remain separate deployment prerequisites.
