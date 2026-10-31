import { JSDOM } from "jsdom";
import assert from "node:assert/strict";

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "http://localhost:3000",
});
for (const key of [
  "window",
  "document",
  "HTMLElement",
  "Node",
  "MutationObserver",
  "getComputedStyle",
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
const { renderHook, act, waitFor, cleanup } = await import(
  "@testing-library/react"
);
const useStrategyConfig = (await import("../src/lib/useStrategyConfig"))
  .default;

const baseline = () => ({
  revision: 7,
  editable: true,
  paperOnly: true,
  strategy: { max_open: 6 },
  watchPeriods: { short: 30, medium: 180, long: 1065 },
});
async function status(result: any, expected: string) {
  await waitFor(() => assert.equal(result.current.status, expected), {
    timeout: 2500,
  });
}

// A prior uncertain write can have succeeded; conflict reload plus no-op retry must
// clear stale error text, without another write or changing the six-position cap.
{
  let current = baseline();
  let puts = 0;
  globalThis.fetch = async (_url, options) => {
    if (options?.method === "PUT") {
      puts++;
      const payload = JSON.parse(String(options.body));
      current = { ...current, revision: 8, watchPeriods: payload.watchPeriods };
      return Response.json({ error: "revision conflict" }, { status: 409 });
    }
    return Response.json(current);
  };
  const { result, unmount } = renderHook(() => useStrategyConfig());
  await status(result, "saved");
  act(() => result.current.updateTier("short", 45));
  await status(result, "error");
  assert.ok(result.current.error);
  assert.equal(result.current.periods.short, 45);
  await act(async () => result.current.retry());
  assert.equal(result.current.status, "saved");
  assert.equal(result.current.error, "");
  assert.equal(puts, 1);
  assert.equal(result.current.config.strategy.max_open, 6);
  unmount();
}

// A second drag while PUT #1 is pending must survive its response and be sent as
// PUT #2 with the newer revision, not be overwritten by the first saved payload.
{
  let current = baseline();
  let release!: (response: Response) => void;
  const payloads: any[] = [];
  globalThis.fetch = async (_url, options) => {
    if (options?.method !== "PUT") return Response.json(current);
    const payload = JSON.parse(String(options.body));
    payloads.push(payload);
    if (payloads.length === 1)
      return await new Promise<Response>((resolve) => {
        release = resolve;
      });
    assert.equal(payload.revision, 8);
    current = { ...current, revision: 9, watchPeriods: payload.watchPeriods };
    return Response.json(current);
  };
  const { result, unmount } = renderHook(() => useStrategyConfig());
  await status(result, "saved");
  act(() => result.current.updateTier("short", 45));
  await status(result, "saving");
  act(() => result.current.updateTier("medium", 195));
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 400));
  });
  assert.equal(payloads.length, 1);
  current = { ...current, revision: 8, watchPeriods: payloads[0].watchPeriods };
  await act(async () => release(Response.json(current)));
  await status(result, "saved");
  assert.equal(payloads.length, 2);
  assert.deepEqual(
    payloads.map((p) => p.revision),
    [7, 8],
  );
  assert.deepEqual(result.current.periods, {
    short: 45,
    medium: 195,
    long: 1065,
  });
  assert.equal(current.revision, 9);
  for (const payload of payloads)
    assert.deepEqual(Object.keys(payload).sort(), ["revision", "watchPeriods"]);
  unmount();
}

// A real conflicting user save preserves the local draft for deliberate retry.
{
  let current = baseline();
  let puts = 0;
  globalThis.fetch = async (_url, options) => {
    if (options?.method !== "PUT") return Response.json(current);
    const payload = JSON.parse(String(options.body));
    if (++puts === 1) {
      current = {
        ...current,
        revision: 8,
        watchPeriods: { short: 60, medium: 210, long: 1080 },
      };
      return Response.json({ error: "revision conflict" }, { status: 409 });
    }
    assert.equal(payload.revision, 8);
    current = { ...current, revision: 9, watchPeriods: payload.watchPeriods };
    return Response.json(current);
  };
  const { result, unmount } = renderHook(() => useStrategyConfig());
  await status(result, "saved");
  act(() => result.current.updateTier("short", 45));
  await status(result, "error");
  assert.equal(result.current.config.revision, 8);
  assert.deepEqual(result.current.periods, {
    short: 45,
    medium: 180,
    long: 1065,
  });
  await act(async () => result.current.retry());
  await status(result, "saved");
  assert.equal(result.current.config.revision, 9);
  assert.equal(result.current.error, "");
  unmount();
}

// Unmount cancels a debounce before it can send any update.
{
  let puts = 0;
  globalThis.fetch = async (_url, options) => {
    if (options?.method === "PUT") puts++;
    return Response.json(baseline());
  };
  const { result, unmount } = renderHook(() => useStrategyConfig());
  await status(result, "saved");
  act(() => result.current.updateTier("short", 45));
  unmount();
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 400));
  });
  assert.equal(puts, 0);
}
cleanup();
console.log(
  "Slider concurrency checks passed: no-op conflict retry, two in-flight drags, deliberate conflict retry, immutable cap, and canceled debounce on unmount.",
);
