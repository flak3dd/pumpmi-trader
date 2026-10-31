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
