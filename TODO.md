# Pumpmi high-voltage UI

- [x] High-voltage trading terminal shell with electric energy-flow visuals, bold contrast, glow treatments, and dense market-console hierarchy. Reusable frontend components and visual tokens integrated into the cloned repository without replacing or altering the existing board engine in the initial visual upgrade.
- [x] Live board overview presents the engine’s checking, holding, and done datasets as clearly separated workflow lanes. Token and position rows surface essential market, status, timing, and paper-trade information for rapid scanning.
- [x] Interactive selection and detail view inspects an asset or paper position without disrupting the board workflow. Existing trade chart data drives terminal-style axes, indicators, tooltips, and state-aware visual emphasis.
- [x] Paper-only controls and actions are wired to the existing engine with unmistakable simulation labeling, no wallet signing, and no real-money execution. Fixture simulations are isolated from the existing recorded dataset.
- [x] Status transitions, loading, empty, error, and action-feedback states accurately reflect existing behavior. Stored snapshots are clearly labeled rather than falsely presented as a running live market feed.
- [x] Responsive layout preserves terminal usability on desktop and narrower screens. Energy-flow animation respects reduced-motion preferences and can be disabled.
- [x] Complete a read-only implementation review against these outcomes, run build and existing engine checks, and save the deliverable version.

## Initial visual-upgrade evidence

The first visual upgrade passed its production build, 31 engine tests (one live-API test skipped), five adapter tests, and DOM interaction checks. The read-only review's confirmed defects were corrected: skips are not relabeled closed, P&L includes all ledger exit legs consistently, polling reconciles chart selection and ignores stale requests after mode changes, fixture chart prices retain exact decimals and correct units, and the responsive minimum is 320 pixels. That initial upgrade preserved the original engine source and outputs.

## Three-tier watch configuration

- [x] Each token displays three watch-period time frames: Short, Medium, and Long, defaulting to 90, 180, and 300 seconds. Checkpoint status distinguishes observed engine data from an elapsed but unmeasured recorded-snapshot window. The paper feed records all three checkpoints and evaluates entry only after the long watch completes.
- [x] Maximum open positions is 6 in the engine configuration, strategy view, dashboard capacity label, slot indicators, and fixture-engine enforcement.
- [x] Three tier sliders adjust the Short, Medium, and Long watch duration config parameters when moved, with debounced automatic saves, valid increasing durations, backend validation, atomic config persistence, and visible saving/saved/error states. Updated config is read by the paper engine rather than remaining cosmetic frontend state.
- [x] Keep the high-voltage visual style, recorded-data provenance, isolated fixture simulation, and paper-only safeguards. Verify config persistence, invalid-value rejection, real engine checkpoints/capacity, slider interaction, and existing functionality before saving the update.

## Three-tier implementation evidence

The updated TypeScript/Vite production build passes. The engine suite passes 34 tests with one credential-dependent live test skipped; six API tests pass, including persisted slider updates, invalid values, optimistic revision conflicts, and unchanged recorded datasets. DOM checks pass for exactly three editable sliders, six slot indicators, all token-card watch tiers, 350-millisecond debouncing, failure/retry preservation, inspection windows, accurate pending progress, and a StrictMode stale-load regression. Existing journal, preferences, navigation, and isolated simulation checks still pass.

The read-only review's three concrete frontend findings are corrected: stale loads cannot overwrite newer saves, compact token tiers expose their observation/sample state, and observed checkpoints without a delta do not claim no checkpoint existed. The feed captures actual watch-start timestamps and preserves each token's in-flight timing when defaults change. Adapter configuration is stored locally in `pump-board-engine/strategy.json`; local restarts preserve it, while runtime container edits need backup before deployment replacement.

## Always-on paper optimizer

- [ ] Continuously collect fresh market data in an approved always-on application runtime and review strategy candidates every 30 minutes. No fixture fallback, live spending, wallet signing, or guarantee of profitable half-hour windows.
- [x] Persist observations, user configuration, six-position paper portfolios, optimizer review history, and worker coordination in durable managed storage; preserve the user's current watch settings and existing recorded data.
- [x] Validate bounded neighboring watch-duration candidates chronologically on separate training/holdout data, with explicit modeled execution costs, minimum 30 completed holdout trades, positive net expectancy, profit factor above 1.1, champion-relative improvement, and drawdown limits before any automatic promotion. Retain risk caps and record waiting-for-evidence states when data is insufficient.
- [x] Surface live-source freshness/errors, champion settings, net/gross P&L and cost assumptions, half-hour results, drawdown, trade counts, last/next reviews, validation decisions, and rollback history in the existing terminal.
- [ ] Verify software, source readiness, durable deployment, and actual worker health; distinguish configured/published/running/waiting-for-data states. Enable the approved paid hosting through the product confirmation and publish the accepted version.
- [x] Push the completed intended source changes to the user's existing GitHub repository branch without force, preserving any independent edits and using authorized credentials.

### GitHub source delivery

The completed high-voltage UI and three-tier controls were pushed to `flak3dd/pumpmi-trader` on branch `ui/high-voltage`, commit `0b8d8e3`. The GitHub default branch `main` was not changed. The always-on optimizer is a subsequent update and is not included in that initial push. Reserved hosting was requested through the product confirmation at 18:32 AEDT; no confirmation or active deployment has been reported yet.

At the user's subsequent request, the completed UI commit `0b8d8e3` was fast-forwarded and pushed to GitHub `main` at 18:44 AEDT. The later optimizer update remains separate until its validation checks pass.

## Ten-minute repository improvement runs

- [ ] Every 10 minutes, safely pull/analyze GitHub `main`, target two substantive evidence-based bot enhancements, validate with isolated tests, and push tested changes to `main`. Keep paper-only operation, six-position/risk caps, user settings, and no guaranteed-profit claims. Report insufficient evidence rather than padding changes.

Schedule creation on 2026-10-31 at 18:46 AEDT was rejected by the service: `failed_precondition: project not deployed yet`. No 10-minute schedule is active. Complete the pending product hosting confirmation and publish the verified project before retrying the same scheduled instruction. The app worker's own strategy-review interval remains 30 minutes; the requested 10-minute interval is for agent-led repository analysis/enhancement.

## Hourly full-operation analysis

- [ ] Every hour, evaluate a full contiguous hour of paper-bot operation for capability and profitability without launching overlapping bot instances or resetting the bankroll. Persist gross/net P&L, modeled costs, equity, drawdown, completed trades, source coverage/errors, and strategy revisions. Label stale/no-trade windows honestly and do not treat mixed revisions as a controlled profit comparison. This application-native report begins only when the approved always-on worker is deployed and active.

## Live-paper release evidence

The production build passes. The original engine suite passed 34 tests with one credential-dependent test skipped. The expanded optimizer suite passes 22 tests, the API suite passes seven, hourly reporting passes four, RPC configuration/error-redaction passes three, and the terminal DOM checks pass. Standalone Python checking reports zero errors for the optimizer package. The original captured board and fills hashes remain unchanged.

The read-only review's confirmed defects are fixed: an hour cannot be called complete with only two fresh samples and a long outage; the next-hour baseline is not copied from before the boundary; unverified/failed/stale source data is not labeled fresh/live; nullable timestamps and ISO metadata are explicit. Connection pooling, bulk observations, four-second HTTP/RPC limits, and cheap liveness checks keep database handshakes and network waits from starving live polling. Private read-only RPC configuration is server-side and errors redact provider credentials.

Real-data operation was verified in the current Preview runtime: 49 public pump.fun observations across 32 tracked tokens, with a durable worker lease and the user's unchanged revision-7 watch settings (30/180/1065 seconds). There are zero open and zero completed paper trades so far. Holder RPC reads are rate-limited on the default public service; the independently documented PublicNode endpoint denied holder reads. Unknown risk remains entry-blocking. Configure a reliable protected `SOLANA_RPC_URL` before expecting risk-approved entries. No positive-profit result or always-on deployment is claimed from this readiness run.

Hourly report code is integrated into the active worker and preserves its database session across restarts, but a full real hour has not completed yet. Reserved hosting confirmation/publication and the rejected ten-minute agent schedule remain unresolved rather than falsely marked active.

The tested live-market paper optimizer source was pushed successfully to GitHub `main`, commit `4842ccd`, on 2026-10-31 at 19:06 AEDT. The managed website has not been published, and neither paid always-on hosting nor the ten-minute schedule is confirmed active.
