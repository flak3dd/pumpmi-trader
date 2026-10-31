import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
import type {
  OptimizerStatus,
  SignalAnalysis,
  SignalAnalysisCandidate,
  SignalAnalysisEvent,
} from "../src/lib/optimizer";

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

let requests = 0;
globalThis.fetch = async () => {
  requests += 1;
  throw new Error("Signal panel DOM tests must not make telemetry requests.");
};

const React = await import("react");
const { act, cleanup, fireEvent, render, screen, within } = await import(
  "@testing-library/react"
);
const userEvent = (await import("@testing-library/user-event")).default;
const SignalAnalysisPanel = (
  await import("../src/components/SignalAnalysisPanel")
).default;
const user = userEvent.setup({ document: dom.window.document });

const now = 1_793_500_000;

function candidate(
  values: Partial<SignalAnalysisCandidate> &
    Pick<SignalAnalysisCandidate, "mint" | "symbol">,
): SignalAnalysisCandidate {
  return {
    mint: values.mint,
    symbol: values.symbol,
    side: "buy",
    readinessPct: 70,
    status: "approaching",
    observedAt: now,
    reason: "Server-supplied gate reasoning.",
    gates: [],
    ...values,
  };
}

function event(
  values: Partial<SignalAnalysisEvent> & Pick<SignalAnalysisEvent, "id">,
): SignalAnalysisEvent {
  return {
    id: values.id,
    at: now,
    mint: "mint-event",
    symbol: "EVENT",
    side: "buy",
    action: "WAIT",
    readinessPct: 55,
    reason: "Server-supplied event reason.",
    ...values,
  };
}

function analysis(values: Partial<SignalAnalysis> = {}): SignalAnalysis {
  return {
    asOf: now,
    model: "gate-readiness-v1",
    calibrated: false,
    disclaimer:
      "Server readiness is a gate measurement, not a calibrated execution or profit probability.",
    candidates: [],
    events: [],
    ...values,
  };
}

function status(signalAnalysis?: SignalAnalysis): OptimizerStatus {
  return {
    enabled: true,
    runtime: "running",
    paperOnly: true,
    intervalSeconds: 1800,
    lastTick: now,
    lastReview: now - 60,
    nextReview: now + 1800,
    bootId: "isolated-signal-panel-test",
    leaseOwned: true,
    ...(signalAnalysis ? { signalAnalysis } : {}),
    source: {
      name: "Isolated test source",
      lastObservation: now,
      error: null,
      observations: 10,
      trackedTokens: 4,
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
    costModel: { feeBps: 100, slippageBps: 100, label: "Test costs" },
    windows: [],
    validation: {
      state: "waiting",
      reason: "No held-out trades yet",
      trainTrades: 0,
      testTrades: 0,
      testPF: null,
      testNet: null,
      maxDrawdown: null,
    },
    reviews: [],
  };
}

const candidates = [
  candidate({
    mint: "mint-alpha",
    symbol: "ALPHA",
    readinessPct: 82,
    observedAt: now - 4,
    reason: "Long observation is close to its required duration.",
    gates: [
      {
        key: "watch",
        label: "Long observation",
        state: "pending",
        actual: "51%",
        required: "100% observed",
        progressPct: 51,
      },
    ],
  }),
  candidate({
    mint: "mint-blocked",
    symbol: "BLOCKED",
    readinessPct: 99,
    status: "blocked",
    observedAt: now - 1,
    reason: "Holder concentration remains over the policy ceiling.",
    gates: [
      {
        key: "holders",
        label: "Top-holder concentration",
        state: "fail",
        actual: "51%",
        required: "≤ 30%",
        progressPct: 99,
      },
    ],
  }),
  candidate({
    mint: "mint-exit",
    symbol: "EXIT",
    side: "sell",
    readinessPct: 65,
    observedAt: now - 3,
    reason: "Nearest take-profit exit trigger is approaching.",
  }),
  candidate({
    mint: "mint-stale",
    symbol: "STALE",
    readinessPct: null,
    status: "stale",
    observedAt: null,
    reason: "The latest quote is stale; no score is available.",
  }),
];
const initialEvents = [
  event({
    id: "ready-1",
    symbol: "ALPHA",
    action: "READY",
    readinessPct: 100,
    reason: "Ready reasoning marker.",
    at: now - 1,
  }),
  event({
    id: "buy-1",
    symbol: "ALPHA",
    action: "BUY",
    readinessPct: 100,
    reason: "Actual paper buy marker.",
    at: now - 2,
  }),
  event({
    id: "block-1",
    symbol: "BLOCKED",
    action: "BLOCK",
    readinessPct: 99,
    reason: "Blocked reasoning marker.",
    at: now - 3,
  }),
  event({
    id: "expiry-1",
    symbol: "EXPIRED_ONLY",
    action: "EXPIRE",
    readinessPct: null,
    reason: "Expired watch without a position or cash mutation.",
    at: now - 4,
  }),
];

// Candidate ordering, high-score blocked/stale truthfulness, gate facts, and safe board-backed selection.
{
  const selected: string[] = [];
  const result = render(
    React.createElement(SignalAnalysisPanel, {
      status: status(analysis({ candidates, events: initialEvents })),
      error: "",
      canInspectCandidate: (item: SignalAnalysisCandidate) =>
        item.mint === "mint-alpha",
      onSelectCandidate: (item: SignalAnalysisCandidate) =>
        selected.push(item.mint),
    }),
  );
  assert.ok(screen.getByLabelText("Live signal analysis"));
  assert.ok(
    screen.getByText(
      /Buy readiness and sell proximity are strategy-gate measurements, not calibrated probabilities/,
    ),
  );
  assert.ok(
    screen.getByText(
      "Server readiness is a gate measurement, not a calibrated execution or profit probability.",
    ),
  );
  const order = [
    ...result.container.querySelectorAll(".signal-candidate-symbol"),
  ].map((node) => node.textContent);
  assert.deepEqual(order, ["BLOCKED", "ALPHA", "EXIT", "STALE"]);
  const blocked = result.container.querySelector('[data-mint="mint-blocked"]')!;
  assert.equal(
    blocked.querySelector(".signal-status-chip")?.textContent,
    "blocked",
    "a high source score must still show the blocked policy state",
  );
  const expiry = [...result.container.querySelectorAll(".signal-log-row")].find(
    (row) => row.textContent?.includes("EXPIRED_ONLY"),
  );
  assert.ok(expiry);
  assert.ok(expiry.textContent?.includes("Watch outcome"));
  assert.ok(!expiry.classList.contains("execution"));
  assert.ok(!expiry.textContent?.includes("Paper execution"));
  assert.ok(
    result.container
      .querySelector('[data-mint="mint-stale"]')
      ?.textContent?.includes("Quote unavailable"),
    "stale/null source scores must remain unavailable rather than becoming a client score",
  );
  const blockedGateSummary = blocked.querySelector("summary")!;
  fireEvent.click(blockedGateSummary);
  assert.ok(within(blocked).getByText("Measured"));
  assert.ok(within(blocked).getByText("≤ 30%"));
  assert.ok(within(blocked).getByText("51%"));
  await user.click(screen.getByRole("button", { name: "Inspect ALPHA" }));
  assert.deepEqual(selected, ["mint-alpha"]);
  assert.equal(
    screen.queryByRole("button", { name: "Inspect BLOCKED" }),
    null,
    "non-board candidates cannot create a fake inspection target",
  );
  const nearBuy = screen.getByRole("button", { name: /Near buy/ });
  assert.equal(nearBuy.getAttribute("aria-pressed"), "false");
  await user.click(nearBuy);
  assert.equal(nearBuy.getAttribute("aria-pressed"), "true");
  assert.deepEqual(
    [...result.container.querySelectorAll(".signal-candidate-symbol")].map(
      (node) => node.textContent,
    ),
    ["BLOCKED", "ALPHA"],
    "near-buy uses the server score threshold while retaining a blocked status",
  );
  cleanup();
}

// Absence of sell candidates must not create a synthetic sell from holdings, trades, or a null score.
{
  const result = render(
    React.createElement(SignalAnalysisPanel, {
      status: status(analysis({ candidates: [candidates[0]], events: [] })),
      error: "",
    }),
  );
  assert.ok(screen.getByText("No current sell candidates reported"));
  assert.equal(screen.queryByText("Sell proximity"), null);
  await user.click(screen.getByRole("button", { name: /Near sell/ }));
  assert.ok(screen.getByText("No current sell candidates"));
  assert.ok(
    screen.getByText(
      /No sell score is being inferred from holdings or prior trades/,
    ),
  );
  assert.equal(
    result.container.querySelectorAll(".signal-candidate").length,
    0,
  );
  cleanup();
}

// Loading and optional-contract outage states are explicit and do not substitute sample candidates or reasons.
{
  const loading = render(
    React.createElement(SignalAnalysisPanel, { status: null, error: "" }),
  );
  assert.ok(screen.getByText("Loading live signal analysis"));
  assert.equal(
    loading.container.querySelectorAll(".signal-candidate").length,
    0,
  );
  cleanup();
  const unavailable = render(
    React.createElement(SignalAnalysisPanel, {
      status: status(),
      error: "Optimizer read temporarily unavailable.",
    }),
  );
  assert.ok(screen.getByText("Signal analysis telemetry unavailable"));
  assert.ok(
    screen.getByText(
      /No client-side scores, reasons, or candidate events are created/,
    ),
  );
  assert.equal(
    unavailable.container.querySelectorAll(".signal-candidate").length,
    0,
  );
  cleanup();
}

// The persistent log is newest-first, bounded in display, distinguishable by event type, searchable, and independently pausable.
{
  const manyEvents = Array.from({ length: 80 }, (_, index) =>
    event({
      id: `event-${index}`,
      symbol: index % 2 ? "BUYLOG" : "SELLLOG",
      side: index % 2 ? "buy" : "sell",
      action:
        index === 79
          ? "BUY"
          : index === 78
            ? "READY"
            : index % 4 === 0
              ? "WAIT"
              : index % 4 === 1
                ? "BLOCK"
                : index % 4 === 2
                  ? "SELL"
                  : "SCALE",
      readinessPct: index,
      reason: `marker-${index}`,
      at: now + index,
    }),
  );
  const initial = analysis({ candidates: [candidates[0]], events: manyEvents });
  const result = render(
    React.createElement(SignalAnalysisPanel, {
      status: status(initial),
      error: "",
    }),
  );
  await screen.findByText("marker-79");
  const rows = result.container.querySelectorAll(".signal-log-row");
  assert.equal(rows.length, 50, "the displayed log must remain bounded");
  assert.ok(
    rows[0].textContent?.includes("marker-79"),
    "log sorts newest first",
  );
  const buyRow = [...rows].find((row) =>
    row.textContent?.includes("marker-79"),
  )!;
  const readyRow = [...rows].find((row) =>
    row.textContent?.includes("marker-78"),
  )!;
  assert.ok(buyRow.classList.contains("execution"));
  assert.ok(readyRow.classList.contains("reasoning"));
  assert.ok(buyRow.textContent?.includes("Paper execution"));
  assert.ok(readyRow.textContent?.includes("Reasoning state"));
  await user.click(screen.getByRole("button", { name: "Pause log" }));
  const afterPause = analysis({
    candidates: [candidates[0]],
    events: [
      event({
        id: "after-pause",
        symbol: "NEW",
        action: "BLOCK",
        reason: "new after pause",
        at: now + 1000,
      }),
      ...manyEvents,
    ],
  });
  result.rerender(
    React.createElement(SignalAnalysisPanel, {
      status: status(afterPause),
      error: "",
    }),
  );
  await act(async () => {
    await Promise.resolve();
  });
  assert.equal(screen.queryByText("new after pause"), null);
  assert.ok(
    screen.getByText(/Display paused; live candidate telemetry continues/),
  );
  await user.click(screen.getByRole("button", { name: "Resume log" }));
  await screen.findByText("new after pause");
  const search = screen.getByRole("textbox", { name: "Search reasoning log" });
  await user.clear(search);
  await user.type(search, "marker-78");
  assert.equal(result.container.querySelectorAll(".signal-log-row").length, 1);
  assert.ok(screen.getByText("marker-78"));
  await user.clear(search);
  await user.selectOptions(
    screen.getByRole("combobox", { name: "Filter reasoning log action" }),
    "BUY",
  );
  assert.ok(
    [...result.container.querySelectorAll(".signal-log-row")].every((row) =>
      row.textContent?.includes("BUY"),
    ),
  );
  cleanup();
}

// The panel owns accessible labels and explicit narrow/reduced-motion stylesheet paths.
{
  const css = readFileSync(
    new URL("../src/styles.css", import.meta.url),
    "utf8",
  );
  assert.match(
    css,
    /@media \(max-width: 760px\) \{[\s\S]*\.signal-log-row/,
    "narrow layouts must restructure dense log rows",
  );
  assert.match(
    css,
    /@media \(prefers-reduced-motion: reduce\) \{[\s\S]*\.signal-analysis-panel/,
    "reduced-motion users must not receive panel transitions",
  );
}

// Symbol collisions cannot select an unrelated board asset.
{
  const { tokenBySignalMint } = await import("../src/lib/signalIdentity");
  const board = [
    { mint: "real-a", symbol: "SAME" },
    { mint: "real-b", symbol: "SAME" },
  ];
  assert.equal(tokenBySignalMint(board, { mint: "missing" }), null);
  assert.equal(tokenBySignalMint(board, { mint: "" }), null);
  assert.equal(tokenBySignalMint(board, { mint: "real-b" }), board[1]);
}

assert.equal(requests, 0, "panel rendering must not make telemetry requests");
console.log(
  "Signal analysis DOM checks passed: server-only ranking, filters, gate facts, stale/null truthfulness, no synthetic sells, optional-contract states, execution distinctions, bounded paused logs, accessibility, responsive, and reduced-motion coverage.",
);
