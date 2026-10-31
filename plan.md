# Always-on paper optimizer

## Approved scope

The user approved always-on paper optimization with the disclosed usage-based hosting costs and requested `git push`. The service collects fresh market data continuously, reviews parameter candidates every 30 minutes, tracks half-hour paper net P&L, and does not guarantee profit. No wallet signing or real-money orders are enabled.

## Implementation approach

Keep the existing high-voltage terminal, six-position cap, user watch settings, and paper-only risk controls. Add a deterministic application-owned worker within the reserved published Python process, backed by the managed MySQL database for durable observations, paper portfolios, revisions, and optimizer reports. The authoritative user watch baseline is Short 30s, Medium 180s, Long 1065s at revision 7; always read the current durable config rather than replacing user edits with defaults. Keep fixture simulation isolated from this live-market-data paper dataset.

The worker performs bounded market-data collection with retry/backoff, explicit freshness/coverage diagnostics, and a database-backed lease to avoid duplicate workers during rolling deployments. It will not silently substitute fixtures if a public data endpoint fails. Source connectivity must be verified before reporting the feed active. Collection, cohort replay, and 30-minute review run within the service; do not use sandbox cron or traffic pingers to simulate persistent hosting.

Maintain a champion paper strategy and a bounded set of neighboring Short/Medium/Long duration candidates. Replay recorded market paths chronologically with the same paper sizing, six-position limit, unchanged exit/holder-risk rules, and explicit fee/slippage assumptions. Training selects a candidate; a chronologically later holdout evaluates it. No parameters are promoted until there is sufficient recent fresh data, at least 30 completed holdout trades, positive modeled holdout net expectancy, profit factor above 1.1, improved performance relative to the champion on the same holdout, and acceptable drawdown. Do not use the checked-in ledger as full candle history or count nonexistent/no-trade intervals as profitable. Sparse data yields a waiting-for-evidence report, not a forced config edit. Reviews and automatic promotions remain auditable, and rollback uses the last validated champion if live paper performance deteriorates.

Report gross P&L, modeled execution costs, net realized P&L, mark-to-market equity, drawdown, completed trade count, data freshness, last/next review, and validation state. Fee/slippage assumptions are labeled modeling assumptions, not claimed measured fills. Do not loosen the six-position cap, stop-loss, or sizing constraints to manufacture profit.

## Project structure

- `pump-board-engine/src/optimizer/`: durable store, source collector, chronological replay/evaluation, policy, and worker coordination.
- `scripts/serve.py`: existing API/static server plus status/config integration and production worker lifecycle.
- `scripts/migrate_optimizer.py`: additive, versioned database setup and baseline seed; never overwrite saved user state on boot.
- `src/components/OptimizerPanel.tsx`: high-voltage operational status, P&L windows, candidate evidence, and failure states within the terminal.
- `tests` / `scripts/test_*`: isolated test stores and controlled source fixtures used only for software verification, not evidence of profitable trading.
- `Dockerfile`: pinned frontend build and production Python service with managed database connectivity.

## Deployment and data safety

Enable managed database storage and request `hosting.mode=always_on` through the product's required confirmation flow. Publish only the committed version. Development and production share the managed database: seed only new optimizer namespaces and retain all existing strategy/user data. Runtime states survive restarts in MySQL, not container files. Keep secrets server-side and do not transfer sandbox-only model credentials. Push the source to the user's existing repository branch without force or changes to its remotes; keep managed canonical version saving separate.

## Concurrency repair

Use transactional lease ownership/expiry fencing for durable worker commits, atomic portfolio/ledger/state changes, fresh authoritative trading-state reads on handoff, and idempotent initialization. Keep slow network collection outside database locks. Capture a single strategy revision before validation; configuration plus audit and rollback/promotion markers commit as one operation. User saves cannot be overwritten by a candidate evaluated against an older revision or rolled back by obsolete promotion lineage. Refresh config after collection for newly created watches while existing timelines remain immutable. Serialize local file-backed CAS with a stable advisory lock; retain responsive telemetry caching but never use it as authoritative owner trading state. No risk threshold, live dataset, chosen duration, or six-position limit is relaxed to induce trades.
