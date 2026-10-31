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

The adapter validates values and atomically saves `pump-board-engine/strategy.json`. Revision conflicts return `409` rather than silently overwriting another change. The frontend retains the draft and offers a retry after reading the current revision. Other risk controls remain read-only, and the capacity stays fixed at six.

Configuration survives adapter/feed restarts in the same working directory. A container's writable filesystem is temporary: copy or commit configuration before replacing a deployment if those runtime changes must survive redeployment. No database is provisioned by this project. The paper feed reads persisted configuration at tick boundaries, but changing sliders does not automatically start a feed or fetch new market data.

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
