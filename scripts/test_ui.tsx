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
const recorded = JSON.parse(
  readFileSync(
    new URL("../public/data/snapshot.json", import.meta.url),
    "utf8",
  ),
);
recorded.meta.simulationAvailable = true;
const nativeFetch = globalThis.fetch;
const fixtureResponse = await nativeFetch(
  "http://127.0.0.1:3001/api/simulate",
  {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  },
);
const fixture = await fixtureResponse.json();
assert.equal(fixture.meta.kind, "fixture");
const requests: string[] = [];
let strategyState = {
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
  watchPeriods: { short: 90, medium: 180, long: 300 },
  revision: 0,
  editable: true,
  paperOnly: true,
};
let failNextSave = false;
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
    watchPeriods: { short: 90, medium: 180, long: 300 },
    revision: 0,
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
  if (url === "/api/optimizer") return Response.json(optimizerStatus);
  if (url === "/api/snapshot") return Response.json(recorded);
  if (url === "/api/simulate") {
    assert.equal(options?.body, "{}");
    return Response.json(fixture);
  }
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
    strategyState = {
      ...strategyState,
      watchPeriods: body.watchPeriods,
      revision: strategyState.revision + 1,
    };
    return Response.json(strategyState);
  }
  if (url === "/api/strategy") return Response.json(strategyState);
  throw new Error(`Unexpected endpoint ${url}`);
};
const React = await import("react");
const { render, screen, within, waitFor, cleanup, fireEvent, act } =
  await import("@testing-library/react");
const userEvent = (await import("@testing-library/user-event")).default;
const App = (await import("../src/App")).default;
const user = userEvent.setup({ document: dom.window.document });
const result = render(React.createElement(App));
await screen.findByText("RECORDED SNAPSHOT");
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
const shortSlider = screen.getByRole("slider", {
  name: "Short watch period",
}) as HTMLInputElement;
const mediumSlider = screen.getByRole("slider", {
  name: "Medium watch period",
}) as HTMLInputElement;
const longSlider = screen.getByRole("slider", {
  name: "Long watch period",
}) as HTMLInputElement;
fireEvent.change(shortSlider, { target: { value: "105" } });
fireEvent.change(mediumSlider, { target: { value: "240" } });
fireEvent.change(longSlider, { target: { value: "420" } });
await waitFor(() =>
  assert.deepEqual(strategyState.watchPeriods, {
    short: 105,
    medium: 240,
    long: 420,
  }),
);
await screen.findByText("Config saved");
assert.equal(
  requests.filter((r) => r === "PUT /api/strategy").length,
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
  2,
);
assert.equal(
  result.container.querySelectorAll(".board-lane.holding .token-card").length,
  0,
);
assert.ok(result.container.querySelector(".board-lane.holding .lane-empty"));
const firstClosedCard = result.container.querySelector(
  ".board-lane.done .token-card",
) as HTMLElement;
assert.ok(firstClosedCard.textContent?.includes("CHILLGIVIN"));
assert.ok(
  firstClosedCard.textContent?.includes("+33.2%"),
  "trade return must come from all ledger legs",
);
await user.click(firstClosedCard);
let dialog = screen.getByRole("dialog", { name: "Signal inspection" });
assert.ok(within(dialog).getByText("$43.16"));
assert.ok(within(dialog).getByText(/Latest board decision/));
assert.equal(within(dialog).getAllByText("Unknown").length, 3);
assert.equal(dialog.querySelectorAll(".token-watch-tier").length, 3);
await user.keyboard("{Escape}");
assert.equal(screen.queryByRole("dialog"), null);
await user.click(screen.getByRole("button", { name: "Show all done signals" }));
await user.click(screen.getByRole("button", { name: /View all .* signals/ }));
const ghost = [
  ...result.container.querySelectorAll(".board-lane.done .token-card"),
].find((e) => e.textContent?.includes("GHOSTY"));
assert.ok(
  ghost?.querySelector(".token-tag")?.textContent === "SKIPPED",
  "skip decisions must not be relabeled closed",
);
await user.click(screen.getByRole("button", { name: "Trade journal" }));
assert.equal(
  result.container.querySelectorAll("tbody tr").length,
  Object.keys(recorded.trades).length,
);
await user.selectOptions(
  screen.getByRole("combobox", { name: "Filter trade outcome" }),
  "losses",
);
assert.equal(
  result.container.querySelectorAll("tbody tr").length,
  Object.values(recorded.trades).filter((t: any) => t.exitPnl < 0).length,
);
await user.type(
  screen.getByRole("textbox", { name: "Search recorded trades" }),
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
await user.click(screen.getByRole("button", { name: /Run paper simulation/ }));
await screen.findByText("FIXTURE SIMULATION");
assert.ok(requests.includes("POST /api/simulate"));
assert.ok(screen.getAllByText("Fixture unit price").length > 0);
await user.click(screen.getByRole("button", { name: "Return to snapshot" }));
await screen.findByText("RECORDED SNAPSHOT");
assert.ok(screen.getByText("$1,108.36"));
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
  watchPeriods: { short: 135, medium: 255, long: 435 },
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
    watchPeriods: { short: 90, medium: 240, long: 420 },
  }),
);
assert.equal(
  configHook.result.current.periods.short,
  150,
  "automatic promotion must not overwrite unsaved user draft",
);
configHook.unmount();
console.log(
  "UI checks passed: 3 token timeframes, 6 slots, debounced config sliders, failed-save retry, snapshot, lanes, consistent ledger returns, skip verdicts, detail, journal search/filter, strategy, preferences, simulation, and snapshot restoration.",
);
