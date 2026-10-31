import assert from "node:assert/strict";
import { JSDOM } from "jsdom";
import type { OptimizerStatus, PumpMode } from "../src/lib/optimizer";

const dom = new JSDOM("<!doctype html><html><body></body></html>", { url: "http://localhost:3000" });
for (const key of ["window", "document", "HTMLElement", "HTMLInputElement", "Node", "MutationObserver", "getComputedStyle"] as const) {
  Object.defineProperty(globalThis, key, { configurable: true, value: (dom.window as any)[key] });
}
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
globalThis.fetch = async () => {
  throw new Error("Pump panel DOM tests must not make requests.");
};

const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
const { default: PumpRadarPanel } = await import("../src/components/PumpRadarPanel");
const React = await import("react");

const now = Date.now() / 1000;
const gates = [
  { key: "signal", label: "Entry signal E2", status: "pass", detail: "pump signal active on the latest point" },
  { key: "verified_read", label: "Verified curve read ≤10 s", status: "pass", detail: "read 1.2s ago" },
  { key: "creator_share", label: "Creator holds ≤5%", status: "fail", detail: "creator holds 8.00%" },
  { key: "launch_take", label: "Launch take ≤25%", status: "unknown", detail: "launch not read within 20 s of creation" },
] as const;

function pumpMode(overrides: Partial<PumpMode> = {}): PumpMode {
  return {
    strategy: "pump-capture-v1",
    mode: "radar",
    variant: { entry: "E2", exit: "X2" },
    asOf: now,
    fresh: true,
    disclaimer: "Paper only. Pump readiness is the share of pump-mode gates currently passing, not a probability of a pump, a fill or a profit.",
    holderGate: "Top-10 holder check substituted in pump mode: creator balance, creator selling and launch take (verified on-chain)",
    calibrated: false,
    cadenceSeconds: 4,
    ticketUsd: 65,
    costModel: { feePct: 1.25, networkUsd: 0.05, impact: "constant-product on verified reserves", fill: "next verified read after the decision" },
    book: { closed: 2, wins: 1, losses: 1, net: -3.21, drawdownPct: 0.4, maxDrawdownPct: 0.6, streak: 1, paused: null, missed: 1 },
    source: { route: "public fallback", error: null, hot: 2, lastRead: now, verifiedPoints: 2 },
    pending: [],
    radar: [
      { mint: "MintActive1111", symbol: "ZAP", mcap: 12_400, curvePct: 31, momentum30sPct: 14.2, signals: { E1: false, E2: true, E3: false },
        gates: gates.map((g) => ({ ...g })), readinessPct: 50, held: false, pending: false, readAgeS: 1.2, netPct: null },
      { mint: "MintQuiet2222", symbol: "NAP", mcap: 6_100, curvePct: 12, momentum30sPct: -2.1, signals: { E1: false, E2: false, E3: false },
        gates: gates.map((g) => ({ ...g, status: g.key === "signal" ? "fail" : g.status })), readinessPct: 25, held: false, pending: false,
        readAgeS: 1.4, netPct: null },
    ],
    events: [
      { at: now - 5, action: "SKIP", mint: "MintActive1111", symbol: "ZAP", reason: "E2 active; blocked by Creator holds ≤5% (creator holds 8.00%)" },
      { at: now - 2, action: "RADAR", mint: "MintOther", symbol: "OTH", reason: "E2 active and every gate passes; radar mode places no paper order" },
    ],
    ...overrides,
  } as PumpMode;
}

function status(pm: PumpMode | undefined): OptimizerStatus {
  return { pumpMode: pm } as unknown as OptimizerStatus;
}

const container = document.createElement("div");
document.body.appendChild(container);
const root = createRoot(container);

async function render(pm: PumpMode | undefined, error = "") {
  await act(async () => {
    root.render(React.createElement(PumpRadarPanel, { status: status(pm), error }));
  });
}

// 1. labelled radar state
await render(pumpMode());
let text = container.textContent ?? "";
assert.match(text, /RADAR · NO ORDERS/);
assert.match(text, /not a probability/);
assert.match(text, /Top-10 holder check substituted in pump mode/);
assert.match(text, /1\.25% curve fee per side/);
assert.match(text, /next verified read/);
assert.match(text, /−\$3\.21/);
assert.equal(container.querySelectorAll("tr.pump-row").length, 2);
assert.ok(container.querySelector('[title="Share of pump-mode gates currently passing. Not a probability."]'));
assert.equal(container.querySelectorAll(".pump-gate.fail").length, 3); // ZAP creator; NAP signal + creator
assert.equal(container.querySelectorAll(".pump-gate.unknown").length, 2);
const logItems = [...container.querySelectorAll(".pump-log li")];
assert.equal(logItems[0].querySelector("b")?.textContent, "RADAR"); // newest first

// 2. expanding a row shows every gate detail
await act(async () => {
  (container.querySelector("tr.pump-row") as HTMLElement).dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
});
text = container.textContent ?? "";
assert.match(text, /creator holds 8\.00%/);
assert.match(text, /launch not read within 20 s of creation/);

// 3. signal-only filter hides tokens without the live signal
await act(async () => {
  (container.querySelector(".pump-subhead input") as HTMLInputElement).click();
});
assert.equal(container.querySelectorAll("tr.pump-row").length, 1);

// 4. paper mode, pause and source error are surfaced, stale feed is labelled
await render(pumpMode({ mode: "paper", fresh: false, book: { paused: "pump book drawdown 11.0% > 10%; new pump entries paused" },
  source: { error: "verified reads unavailable on every route" } }));
text = container.textContent ?? "";
assert.match(text, /PAPER ENTRIES ON/);
assert.match(text, /new pump entries paused/);
assert.match(text, /verified reads unavailable on every route/);
assert.match(text, /stale/);

// 5. missing pump state never invents data
await render(undefined, "network down");
text = container.textContent ?? "";
assert.match(text, /UNAVAILABLE/);
assert.equal(container.querySelectorAll("tr.pump-row").length, 0);

await act(async () => root.unmount());
console.log("Pump panel DOM checks passed: labelled radar, non-probability gate share, substituted-holder notice, costs, gate details, signal filter, paper/pause/error/stale states, no invented data.");
