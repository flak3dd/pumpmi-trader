import { JSDOM } from "jsdom";
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "http://localhost:3000",
});
for (const key of [
  "window",
  "document",
  "HTMLElement",
  "HTMLInputElement",
  "Node",
  "MutationObserver",
  "getComputedStyle",
  "localStorage",
] as const) {
  Object.defineProperty(globalThis, key, {
    configurable: true,
    value: (dom.window as any)[key],
  });
}
Object.defineProperty(globalThis, "navigator", {
  configurable: true,
  value: dom.window.navigator,
});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

/** The checked-in data is an explicit software-test seed, never a UI fallback. */
const recorded = JSON.parse(
  readFileSync(
    new URL("./fixtures/recorded-test-seed.json", import.meta.url),
    "utf8",
  ),
);
const strategyState = {
  strategy: {
    min_curve_pct: 25,
    max_curve_pct: 85,
    max_mcap_usd: 250000,
    p0_top10_max: 30,
    p0_dev_max: 5,
    p0_bundle_max: 25,
    max_open: 6,
    stop_loss_pct: -15,
    scale_out_pct: 25,
    trail_giveback_pct: 12,
  },
  watchPeriods: { short: 30, medium: 180, long: 1065 },
  revision: 7,
  editable: true,
  paperOnly: true,
};
const watchSymbol = "WATCHPATH·LIVE";
const liveFrame = structuredClone(recorded);
liveFrame.meta = {
  kind: "live",
  label: "Software-test live feed",
  updated: "2026-10-31T08:00:00.000Z",
};
liveFrame.strategyConfig = structuredClone(strategyState);
liveFrame.board.ts = 1_793_428_800;
liveFrame.board.updated = "2026-10-31T08:00:00.000Z";
liveFrame.board.columns.checking.push({
  symbol: watchSymbol,
  name: "Watched Market Path",
  column: "checking",
  verdict: "WATCH",
  reason: "Actual risk checkpoints are pending.",
  mcap: 125000,
  curve_pct: 44,
  ts_seen: 1_793_427_700,
  watch_started_ts: 1_793_427_700,
  watch_periods: {
    short: { duration_s: 30, elapsed_s: 30, status: "observed" },
    medium: { duration_s: 180, elapsed_s: 90, status: "watching" },
    long: { duration_s: 1065, elapsed_s: 90, status: "watching" },
  },
});
liveFrame.trades[watchSymbol] = {
  metric: "market_cap",
  series: [
    { label: "08:00", value: 118000 },
    { label: "08:01", value: 121500 },
    { label: "08:02", value: 125000 },
  ],
  signals: [],
  gatesBuy: [
    { name: "Holder risk", detail: "RPC observation pending", pass: null },
    { name: "Long watch", detail: "90s of 1065s observed", pass: null },
  ],
  gatesSell: [],
  entry: 0,
  exitPnl: 0,
  exitPp: 0,
  dur: 2,
  isMarketWatch: true,
};

class MockEventSource {
  static instances: MockEventSource[] = [];
  readonly listeners = new Map<string, Set<(event: Event) => void>>();
  onerror: ((event: Event) => void) | null = null;
  closed = false;
  constructor(readonly url: string) {
    MockEventSource.instances.push(this);
  }
  addEventListener(type: string, listener: (event: Event) => void) {
    const listeners = this.listeners.get(type) || new Set();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }
  close() {
    this.closed = true;
  }
  emitFeed(payload: unknown) {
    const event = {
      type: "feed",
      data: JSON.stringify(payload),
    } as MessageEvent;
    this.listeners.get("feed")?.forEach((listener) => listener(event));
  }
  emitError() {
    this.onerror?.(new Event("error"));
  }
}
(window as any).EventSource = MockEventSource;
(globalThis as any).EventSource = MockEventSource;

let failNextSave = false;
let deferredBootstrap: Promise<Response> | null = null;
const requests: string[] = [];
const optimizerStatus = {
  enabled: true,
  runtime: "running",
  paperOnly: true,
  intervalSeconds: 1800,
  lastTick: Date.now() / 1000,
  lastReview: null,
  nextReview: Date.now() / 1000 + 1800,
  bootId: "test-only",
  leaseOwned: true,
  source: {
    name: "Software-test source",
    lastObservation: Date.now() / 1000,
    error: null,
    observations: 80,
    trackedTokens: 32,
    social: "unavailable",
  },
  champion: {
    watchPeriods: { short: 30, medium: 180, long: 1065 },
    revision: 7,
  },
  portfolio: {
    equity: 1500,
    cash: 1500,
    bankroll: 1500,
    grossRealized: 0,
    modeledCosts: 0,
    netRealized: 0,
    openPositions: 0,
    drawdownPct: 0,
    closedTrades: 0,
  },
  costModel: {
    feeBps: 100,
    slippageBps: 100,
    label: "Software-test cost assumptions",
  },
  windows: [
    {
      start: 1800,
      end: 3600,
      gross: 0,
      costs: 0,
      net: 0,
      closedTrades: 0,
      hasTrades: false,
    },
  ],
  validation: {
    state: "waiting",
    reason: "Insufficient held-out trades",
    trainTrades: 0,
    testTrades: 0,
    testPF: null,
    testNet: null,
    maxDrawdown: null,
  },
  reviews: [],
};
(globalThis as any).fetch = async (url: string, options?: RequestInit) => {
  requests.push(`${options?.method || "GET"} ${url}`);
  if (url === "/api/live-feed")
    return deferredBootstrap || Response.json(structuredClone(liveFrame));
  if (url === "/api/optimizer") return Response.json(optimizerStatus);
  if (url === "/api/strategy" && options?.method === "PUT") {
    if (failNextSave) {
      failNextSave = false;
      return Response.json(
        { error: "Temporary configuration error. Your draft was not saved." },
        { status: 500 },
      );
    }
    const body = JSON.parse(String(options.body));
    assert.equal(body.revision, strategyState.revision);
    assert.deepEqual(Object.keys(body).sort(), ["revision", "watchPeriods"]);
    strategyState.watchPeriods = body.watchPeriods;
    strategyState.revision += 1;
    return Response.json(structuredClone(strategyState));
  }
  if (url === "/api/strategy")
    return Response.json(structuredClone(strategyState));
  throw new Error(`Unexpected endpoint ${url}`);
};

const React = await import("react");
const { render, screen, within, waitFor, cleanup, fireEvent, act } =
  await import("@testing-library/react");
const userEvent = (await import("@testing-library/user-event")).default;
const App = (await import("../src/App")).default;
const user = userEvent.setup({ document: dom.window.document });
const result = render(React.createElement(App));
await screen.findByText("LIVE FEED · VERIFIED");
await screen.findByText("Config saved");
await screen.findByText("Collecting market data");
assert.ok(screen.getByText("Waiting for evidence"));
assert.ok(screen.getByText("Empty windows are not wins"));
assert.ok(screen.getByText("No guaranteed profit. No real orders."));
assert.ok(screen.getByText(/1% fee.*1% slippage assumed per side/));
assert.ok(screen.getByText("Insufficient held-out trades"));
assert.equal(screen.getAllByRole("slider").length, 3);
assert.ok(screen.getByText("/ 6 slots"));
assert.equal(
  result.container.querySelectorAll(".slot-indicators span").length,
  6,
);
for (const card of result.container.querySelectorAll(".token-card"))
  assert.equal(card.querySelectorAll(".token-watch-tier").length, 3);
assert.ok(
  [
    ...result.container.querySelectorAll(".board-lane.checking .token-card"),
  ].some((card) => card.textContent?.includes("WATCHPATH")),
);
assert.equal(MockEventSource.instances.length, 1);
assert.equal(MockEventSource.instances[0].url, "/api/live-feed/events");

const shortSlider = screen.getByRole("slider", {
  name: "Short watch period",
}) as HTMLInputElement;
const mediumSlider = screen.getByRole("slider", {
  name: "Medium watch period",
}) as HTMLInputElement;
const longSlider = screen.getByRole("slider", {
  name: "Long watch period",
}) as HTMLInputElement;
assert.equal(shortSlider.value, "30");
assert.equal(mediumSlider.value, "180");
assert.equal(longSlider.value, "1065");
fireEvent.change(shortSlider, { target: { value: "105" } });
fireEvent.change(mediumSlider, { target: { value: "240" } });
fireEvent.change(longSlider, { target: { value: "1200" } });
await waitFor(() =>
  assert.deepEqual(strategyState.watchPeriods, {
    short: 105,
    medium: 240,
    long: 1200,
  }),
);
await screen.findByText("Config saved");
assert.equal(
  requests.filter((request) => request === "PUT /api/strategy").length,
  1,
  "rapid slider moves should debounce into one update",
);
assert.ok(
  result.container
    .querySelector(".token-card .token-watch-tier.short")
    ?.textContent?.includes("1m 45s"),
);
failNextSave = true;
fireEvent.change(shortSlider, { target: { value: "120" } });
await screen.findByText("Save failed");
assert.equal(
  strategyState.watchPeriods.short,
  105,
  "a failed save must not alter the applied config",
);
assert.equal(
  shortSlider.value,
  "120",
  "failed draft stays available for retry",
);
await user.click(screen.getByRole("button", { name: "Retry save" }));
await waitFor(() => assert.equal(strategyState.watchPeriods.short, 120));
await screen.findByText("Config saved");

assert.ok(screen.getByText("$1,108.36"));
assert.equal(
  result.container.querySelectorAll(".board-lane.checking .token-card").length,
  3,
);
assert.equal(
  result.container.querySelectorAll(".board-lane.holding .token-card").length,
  0,
);
assert.ok(result.container.querySelector(".board-lane.holding .lane-empty"));
const watchCard = [
  ...result.container.querySelectorAll(".board-lane.checking .token-card"),
].find((card) => card.textContent?.includes("WATCHPATH")) as HTMLElement;
assert.ok(watchCard.textContent?.includes("Unknown"));
await user.click(watchCard);
let dialog = screen.getByRole("dialog", { name: "Signal inspection" });
assert.ok(within(dialog).getByText("LIVE MARKET PATH"));
assert.equal(within(dialog).getAllByText("Checking / unknown").length, 2);
assert.ok(within(dialog).getByText("Current market quote"));
assert.equal(within(dialog).queryByText("Total trade P&L"), null);
assert.equal(within(dialog).queryByText("Flat"), null);
await user.keyboard("{Escape}");

const firstClosedCard = result.container.querySelector(
  ".board-lane.done .token-card",
) as HTMLElement;
assert.ok(firstClosedCard.textContent?.includes("CHILLGIVIN"));
assert.ok(
  firstClosedCard.textContent?.includes("+33.2%"),
  "trade return must come from all ledger legs",
);
await user.click(firstClosedCard);
dialog = screen.getByRole("dialog", { name: "Signal inspection" });
assert.ok(within(dialog).getByText("$43.16"));
assert.ok(within(dialog).getByText(/Latest board decision/));
assert.equal(dialog.querySelectorAll(".token-watch-tier").length, 3);
await user.keyboard("{Escape}");
await user.click(screen.getByRole("button", { name: "Show all done signals" }));
await user.click(screen.getByRole("button", { name: /View all .* signals/ }));
const ghost = [
  ...result.container.querySelectorAll(".board-lane.done .token-card"),
].find((element) => element.textContent?.includes("GHOSTY"));
assert.ok(
  ghost?.querySelector(".token-tag")?.textContent === "SKIPPED",
  "skip decisions must not be relabeled closed",
);

await user.click(screen.getByRole("button", { name: "Trade journal" }));
assert.equal(
  result.container.querySelectorAll("tbody tr").length,
  Object.values(liveFrame.trades).filter((trade: any) => !trade.isMarketWatch)
    .length,
);
assert.equal(
  screen.queryByText("WATCHPATH"),
  null,
  "watch paths stay out of the journal",
);
await user.selectOptions(
  screen.getByRole("combobox", { name: "Filter trade outcome" }),
  "losses",
);
assert.equal(
  result.container.querySelectorAll("tbody tr").length,
  Object.values(liveFrame.trades).filter(
    (trade: any) => !trade.isMarketWatch && trade.exitPnl < 0,
  ).length,
);
await user.type(
  screen.getByRole("textbox", { name: "Search live paper trades" }),
  "MARSCOIN",
);
assert.equal(result.container.querySelectorAll("tbody tr").length, 1);
await user.click(screen.getAllByRole("button", { name: "Strategy" })[0]);
await screen.findByText("25–85%");
await user.click(screen.getByRole("button", { name: /^Settings$/ }));
dialog = screen.getByRole("dialog", { name: "Tune your terminal" });
const motionSwitch = within(dialog).getByRole("switch", {
  name: "Energy flow animation",
});
await user.click(motionSwitch);
assert.equal(motionSwitch.getAttribute("aria-checked"), "false");
assert.ok(result.container.querySelector(".motion-off"));
await user.keyboard("{Escape}");

await user.click(screen.getByRole("button", { name: /^Terminal$/ }));
const updatedFrame = structuredClone(liveFrame);
updatedFrame.board.updated = "2026-10-31T08:05:00.000Z";
updatedFrame.trades[watchSymbol].series.push({ label: "08:05", value: 131000 });
updatedFrame.board.columns.checking.find(
  (token: any) => token.symbol === watchSymbol,
).mcap = 131000;
await act(async () => MockEventSource.instances[0].emitFeed(updatedFrame));
await screen.findAllByText("$131K");
const requestsBeforeDisconnect = [...requests];
await act(async () => MockEventSource.instances[0].emitError());
await screen.findByText("LIVE FEED DISCONNECTED");
assert.ok(screen.getByText(/Live feed interrupted/));
assert.equal(
  MockEventSource.instances[0].closed,
  false,
  "a transient error must not disable native EventSource automatic reconnection",
);
assert.deepEqual(
  requests,
  requestsBeforeDisconnect,
  "a stream disconnect must retain the live frame rather than load history",
);
await act(async () => MockEventSource.instances[0].emitFeed(updatedFrame));
assert.equal(
  screen.queryByText("LIVE FEED DISCONNECTED"),
  null,
  "a recovered event on the same stream resumes display without a user click",
);
assert.ok(
  !requests.some(
    (request) =>
      request.includes("/data/snapshot.json") ||
      request.includes("/api/snapshot") ||
      request.includes("/api/simulate"),
  ),
  "normal UI may only use the live feed, optimizer, and strategy endpoints",
);
await user.click(
  result.container.querySelector(
    ".page-actions .button.secondary",
  ) as HTMLElement,
);
await screen.findByText("LIVE FEED · VERIFIED");

// A live holding's observed path is not an unexecuted/pending-entry position.
const holdingFrame = structuredClone(liveFrame);
const holdingToken = holdingFrame.board.columns.checking.find(
  (token: any) => token.symbol === watchSymbol,
);
holdingFrame.board.columns.checking =
  holdingFrame.board.columns.checking.filter(
    (token: any) => token.symbol !== watchSymbol,
  );
holdingFrame.board.columns.holding.push({
  ...holdingToken,
  column: "holding",
  verdict: "BUY",
  pnl: 5,
  pnl_pct: 1,
});
const resumedStream = MockEventSource.instances.at(-1)!;
await act(async () => resumedStream.emitFeed(holdingFrame));
await user.click(
  [
    ...result.container.querySelectorAll(".board-lane.holding .token-card"),
  ].find((card) => card.textContent?.includes("WATCHPATH")) as HTMLElement,
);
assert.ok(screen.getByText("Open position watch context"));
assert.ok(screen.getByText(/observed market-cap path · open paper position/));
await user.keyboard("{Escape}");

// SSE wins over an older deferred JSON bootstrap, even if fetch ignores abort.
let resolveDeferred!: (response: Response) => void;
deferredBootstrap = new Promise<Response>((resolve) => {
  resolveDeferred = resolve;
});
await user.click(
  result.container.querySelector(
    ".page-actions .button.secondary",
  ) as HTMLElement,
);
await act(async () => {
  await new Promise((resolve) => setTimeout(resolve, 0));
});
const racingStream = MockEventSource.instances.at(-1)!;
await act(async () => racingStream.emitFeed(updatedFrame));
await screen.findAllByText("$131K");
await act(async () =>
  resolveDeferred(Response.json(structuredClone(liveFrame))),
);
assert.ok(
  screen.getAllByText("$131K").length > 0,
  "a delayed JSON response must not replace a newer live event",
);

// A failed stream invalidates its outstanding bootstrap and retains disconnect.
deferredBootstrap = new Promise<Response>((resolve) => {
  resolveDeferred = resolve;
});
await user.click(
  result.container.querySelector(
    ".page-actions .button.secondary",
  ) as HTMLElement,
);
await act(async () => {
  await new Promise((resolve) => setTimeout(resolve, 0));
});
await act(async () => MockEventSource.instances.at(-1)!.emitError());
await screen.findByText("LIVE FEED DISCONNECTED");
await act(async () =>
  resolveDeferred(Response.json(structuredClone(liveFrame))),
);
assert.ok(
  screen.getByText("LIVE FEED DISCONNECTED"),
  "a late bootstrap must not clear a stream failure",
);
deferredBootstrap = null;
assert.ok(
  requests.filter((request) => request === "GET /api/live-feed").length >= 2,
  "manual reconnect must bootstrap the current live feed",
);
cleanup();

const TokenWatchPeriods = (await import("../src/components/TokenWatchPeriods"))
  .default;
const watchView = render(
  React.createElement(TokenWatchPeriods, {
    token: {
      symbol: "WATCH",
      column: "checking",
      ts_seen: 0,
      watch_started_ts: 1000,
      watch_periods: {
        short: { duration_s: 90, elapsed_s: 90, status: "observed" },
        medium: { duration_s: 180, elapsed_s: 0, status: "watching" },
        long: { duration_s: 300, elapsed_s: 0, status: "watching" },
      },
    },
    periods: { short: 90, medium: 180, long: 300 },
    snapshotTs: 1090,
  }),
);
assert.ok(
  watchView.container
    .querySelector(".short")
    ?.textContent?.includes("Observed"),
);
assert.ok(
  watchView.container
    .querySelector(".short")
    ?.getAttribute("title")
    ?.includes("Curve delta unavailable"),
);
assert.equal(
  (
    watchView.container.querySelector(
      ".medium .token-watch-track span",
    ) as HTMLElement
  ).style.width,
  `${((1090 - 1000) / 180) * 100}%`,
);
cleanup();

const baseFetch = globalThis.fetch;
const staleConfig = structuredClone(strategyState);
let strategyReads = 0;
let resolveStale!: (response: Response) => void;
(globalThis as any).fetch = async (url: string, options?: RequestInit) => {
  if (
    url === "/api/strategy" &&
    options?.method !== "PUT" &&
    ++strategyReads === 1
  )
    return new Promise<Response>((resolve) => {
      resolveStale = resolve;
    });
  return baseFetch(url, options);
};
render(React.createElement(React.StrictMode, null, React.createElement(App)));
await screen.findByText("Config saved");
const strictSlider = screen.getByRole("slider", {
  name: "Short watch period",
}) as HTMLInputElement;
fireEvent.change(strictSlider, { target: { value: "135" } });
await waitFor(() => assert.equal(strategyState.watchPeriods.short, 135));
await screen.findByText("Config saved");
await act(async () => {
  resolveStale(Response.json(staleConfig));
  await new Promise((resolve) => setTimeout(resolve, 10));
});
assert.equal(
  strictSlider.value,
  "135",
  "stale StrictMode load must never overwrite a newer slider save",
);
cleanup();

const useStrategyConfig = (await import("../src/lib/useStrategyConfig"))
  .default;
const { renderHook } = await import("@testing-library/react");
const configHook = renderHook(() => useStrategyConfig());
await waitFor(() => assert.equal(configHook.result.current.status, "saved"));
const promoted = {
  ...strategyState,
  revision: strategyState.revision + 1,
  watchPeriods: { short: 135, medium: 255, long: 1215 },
};
act(() => configHook.result.current.adoptConfig(promoted));
assert.equal(
  configHook.result.current.periods.medium,
  255,
  "validated automatic promotion updates saved sliders",
);
act(() => configHook.result.current.updateTier("short", 150));
act(() =>
  configHook.result.current.adoptConfig({
    ...promoted,
    revision: promoted.revision + 1,
    watchPeriods: { short: 90, medium: 240, long: 1200 },
  }),
);
assert.equal(
  configHook.result.current.periods.short,
  150,
  "automatic promotion must not overwrite unsaved user draft",
);
configHook.unmount();
console.log(
  "UI checks passed: live bootstrap and EventSource feed, no historical fallback, disconnected stale frame, market-watch paths, 3 timeframes, 6 slots, debounced config sliders, failed-save retry, StrictMode stale-load protection, journal filtering, preferences, and live paper execution semantics.",
);
