import assert from "node:assert/strict";
import { JSDOM } from "jsdom";
import type { OptimizerStatus } from "../src/lib/optimizer";

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
  throw new Error("This isolated panel test must not make requests.");
};

const React = await import("react");
const { cleanup, render, screen } = await import("@testing-library/react");
const OptimizerPanel = (await import("../src/components/OptimizerPanel"))
  .default;

type StatusOptions = {
  entryReadiness?: OptimizerStatus["entryReadiness"];
  sourceAgeSeconds?: number | null;
  leaseOwned?: boolean;
  runtime?: OptimizerStatus["runtime"];
  enabled?: boolean;
};

function makeStatus({
  entryReadiness,
  sourceAgeSeconds = 10,
  leaseOwned = true,
  runtime = "running",
  enabled = true,
}: StatusOptions = {}): OptimizerStatus {
  const lastObservation =
    sourceAgeSeconds == null ? null : Date.now() / 1000 - sourceAgeSeconds;
  return {
    enabled,
    runtime,
    paperOnly: true,
    intervalSeconds: 1800,
    lastTick: lastObservation,
    lastReview: null,
    nextReview: Date.now() / 1000 + 1800,
    bootId: "isolated-entry-status-test",
    leaseOwned,
    ...(entryReadiness ? { entryReadiness } : {}),
    source: {
      name: "Isolated test source",
      lastObservation,
      error: null,
      observations: 4300,
      trackedTokens: 228,
      social: "unavailable",
    },
    champion: {
      watchPeriods: { short: 10, medium: 90, long: 210 },
      revision: 705,
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

function panel(status: OptimizerStatus | null) {
  return render(React.createElement(OptimizerPanel, { status, error: "" }));
}

// Null status is a loading state, never a claim that an on-demand worker is disabled.
{
  const result = panel(null);
  assert.ok(screen.getByLabelText("Paper strategy optimizer"));
  assert.ok(screen.getByText("Loading optimizer telemetry"));
  assert.ok(screen.getByText("Live price telemetry loading"));
  assert.ok(screen.getByText("Loading strategy telemetry"));
  assert.equal(screen.queryByText("Forward worker disabled"), null);
  assert.equal(
    screen.queryByText(
      /Start the published worker to collect fresh observations/,
    ),
    null,
  );
  result.unmount();
}

// A fresh price feed is separately visible when all holder checks block paper entries.
{
  const result = panel(
    makeStatus({
      entryReadiness: {
        pending: 64,
        holderUnknown: 64,
        curveUnknown: 0,
        overdue: 64,
        expiredTotal: 71,
        blocker: "RPC 429 holder budget exhausted",
      },
    }),
  );
  assert.ok(screen.getByText("Live prices collecting"));
  assert.ok(screen.getByText("Paper entries blocked"));
  assert.ok(
    screen.getByText(
      /A provisioned read-only SOLANA_RPC_URL is required — not a wallet seed or signing key/,
    ),
  );
  assert.ok(screen.getByText("0 / 64"));
  assert.equal(
    screen.getAllByText("64").length,
    2,
    "watchers and overdue counts should both be displayed",
  );
  assert.ok(screen.getByText("71"));
  assert.ok(screen.getByText("RPC 429 holder budget exhausted"));
  result.unmount();
}

// Partial uncertainty remains explicitly pending and backend text is rendered as text, not HTML.
{
  const blocker = "<strong>RPC backoff</strong> · holder refresh queued";
  const result = panel(
    makeStatus({
      entryReadiness: {
        pending: 10,
        holderUnknown: 4,
        curveUnknown: 3,
        overdue: 2,
        expiredTotal: 8,
        blocker,
      },
    }),
  );
  assert.ok(screen.getByText("Verification pending"));
  assert.ok(screen.getByText("6 / 10"));
  assert.ok(screen.getByText("7 / 10"));
  assert.ok(screen.getByText(blocker));
  assert.equal(
    result.container.querySelector(".entry-readiness-blocker strong"),
    null,
    "backend blocker text must never become markup",
  );
  result.unmount();
}

// A known-ready telemetry payload with no watchers is a no-signal state, not a blocked state.
{
  const result = panel(
    makeStatus({
      entryReadiness: {
        pending: 0,
        holderUnknown: 0,
        curveUnknown: 0,
        overdue: 0,
        expiredTotal: 0,
        blocker: null,
      },
    }),
  );
  assert.ok(screen.getByText("No entry candidates yet"));
  assert.equal(
    screen.getAllByText("0 / 0").length,
    2,
    "zero watchers should show both holder and reserve verification counts",
  );
  assert.equal(screen.queryByText("Paper entries blocked"), null);
  result.unmount();
}

// Existing backends that omit the optional contract retain a useful, non-blocking panel.
{
  const result = panel(makeStatus());
  assert.ok(screen.getByText("Entry verification telemetry unavailable"));
  assert.ok(screen.getByText("Live prices collecting"));
  assert.equal(screen.queryByText("Paper entries blocked"), null);
  result.unmount();
}

// An old observation is surfaced independently of entry data.
{
  const result = panel(
    makeStatus({
      sourceAgeSeconds: 181,
      entryReadiness: {
        pending: 4,
        holderUnknown: 0,
        curveUnknown: 0,
        overdue: 0,
        expiredTotal: 0,
        blocker: null,
      },
    }),
  );
  assert.ok(screen.getByText("Live prices stale"));
  assert.ok(screen.getByText("Verification current"));
  result.unmount();
}

// A non-lease-owning replica can show a fresh shared feed without asserting a broken worker.
{
  const result = panel(
    makeStatus({
      leaseOwned: false,
      runtime: "running",
      entryReadiness: {
        pending: 1,
        holderUnknown: 0,
        curveUnknown: 0,
        overdue: 0,
        expiredTotal: 0,
        blocker: null,
      },
    }),
  );
  assert.ok(screen.getByText("Shared live feed · replica standby"));
  assert.ok(screen.getByText("Live prices collecting"));
  result.unmount();
}

cleanup();
assert.equal(requests, 0, "entry-status rendering must not make live requests");
console.log(
  "Entry readiness DOM checks passed: loading, holder block, partial verification, no signal, legacy compatibility, stale source, replica standby, and no requests.",
);
