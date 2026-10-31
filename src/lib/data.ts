export type Lane = "checking" | "holding" | "done";
export type WatchTier = "short" | "medium" | "long";
export type WatchPeriods = Record<WatchTier, number>;
export type StrategyConfig = {
  strategy: Record<string, number>;
  watchPeriods: WatchPeriods;
  revision: number;
  editable: boolean;
  paperOnly: boolean;
};
export const DEFAULT_WATCH_PERIODS: WatchPeriods = {
  short: 30,
  medium: 180,
  long: 1065,
};
export const WATCH_TIERS: { key: WatchTier; label: string; code: string }[] = [
  { key: "short", label: "Short", code: "S" },
  { key: "medium", label: "Medium", code: "M" },
  { key: "long", label: "Long", code: "L" },
];
export const duration = (seconds: number) =>
  seconds < 60
    ? `${seconds}s`
    : seconds % 60 === 0
      ? `${seconds / 60}m`
      : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
export type Token = {
  symbol: string;
  column: Lane;
  name?: string;
  mint?: string;
  verdict?: string;
  reason?: string;
  mcap?: number | null;
  curve_pct?: number | null;
  top1_pct?: number | null;
  top10_pct?: number | null;
  dev_pct?: number | null;
  bundle_pct?: number | null;
  pnl?: number;
  pnl_pct?: number;
  ts_seen?: number;
  watch_started_ts?: number;
  sentiment_0_100?: number;
  sentiment?: number;
  mentions?: number;
  replies?: number;
  watch_periods?: Partial<
    Record<
      WatchTier,
      {
        duration_s: number;
        elapsed_s: number;
        status: "watching" | "observed" | "expired";
        curve_delta?: number;
      }
    >
  >;
};
export type Signal = {
  id: string;
  kind: "buy" | "scale" | "sell";
  at: number;
  label: string;
  t: string;
  mcap: number;
  usd: number;
  pnl: number | null;
  reason: string;
  rule?: string | null;
};
export type RiskGate = {
  name: string;
  detail: string;
  /** null/omitted is intentionally unresolved rather than a passed gate. */
  pass?: boolean | null;
};
export type Trade = {
  metric?: "unit_price" | "market_cap";
  series: { label: string; value: number }[];
  signals: Signal[];
  gatesBuy: RiskGate[];
  gatesSell: RiskGate[];
  entry: number;
  exitPnl: number;
  exitPp: number;
  dur: number;
  _exitTs?: number;
  /** A live observed quote path; it is not a paper execution or closed trade. */
  isMarketWatch?: boolean;
};
export type LiveFeedKind = "live" | "awaiting" | "stale";
export type Snapshot = {
  strategyConfig?: StrategyConfig;
  board: {
    ts: number | null;
    mode: string;
    bankroll: number;
    equity: number;
    multiple: number;
    columns: Record<Lane, Token[]>;
    updated?: string;
    source?: string;
    log?: string[];
    stats?: {
      ticks: number;
      scanned: number;
      buys: number;
      wins: number;
      losses: number;
      skips: number;
      realized: number;
    };
  };
  trades: Record<string, Trade>;
  meta: {
    kind: LiveFeedKind;
    label: string;
    updated?: string;
  };
};
export const money = (n: number | null | undefined, digits = 2) =>
  n == null
    ? "—"
    : new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        maximumFractionDigits: digits,
        minimumFractionDigits: digits,
      }).format(n);
export const compact = (n: number | null | undefined) =>
  n == null
    ? "—"
    : new Intl.NumberFormat("en-US", {
        notation: "compact",
        maximumFractionDigits: 1,
      }).format(n);
export const signed = (n: number, suffix = "") =>
  `${n >= 0 ? "+" : "−"}${Math.abs(n).toFixed(1)}${suffix}`;
export const shortSymbol = (s: string) => s.split("·")[0].trim();
export const identifier = (s: string) => s.split("·")[1] || "";
export const pct = (n: number | null | undefined) =>
  n == null ? "Unknown" : `${n.toFixed(1)}%`;
export const chartMetric = (trade?: Trade) =>
  trade?.metric === "unit_price" ? "Fixture unit price" : "Market cap";
export const chartNumber = (n: number, trade?: Trade) =>
  trade?.metric === "unit_price"
    ? n.toLocaleString("en-US", { maximumFractionDigits: 4 })
    : `$${compact(n)}`;
export const allTokens = (s: Snapshot) => Object.values(s.board.columns).flat();
export function tokenForTrade(symbol: string, snapshot: Snapshot): Token {
  return (
    allTokens(snapshot).find((t) => t.symbol === symbol) || {
      symbol,
      column: "done",
      verdict: "EXIT",
      pnl: snapshot.trades[symbol]?.exitPnl,
      pnl_pct: snapshot.trades[symbol]?.exitPp,
    }
  );
}
export const AVATAR_COLORS = ["mint", "amber", "purple", "cyan", "pink"];
export const avatarColor = (symbol: string) =>
  AVATAR_COLORS[
    Array.from(symbol).reduce((v, c) => v + c.charCodeAt(0), 0) %
      AVATAR_COLORS.length
  ];

type JsonRecord = Record<string, unknown>;
const isRecord = (value: unknown): value is JsonRecord =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const isLiveFeedKind = (value: unknown): value is LiveFeedKind =>
  value === "live" || value === "awaiting" || value === "stale";

/**
 * Validates both the JSON bootstrap response and EventSource payloads. Frames
 * from historical snapshot/fixture sources deliberately fail this boundary.
 */
export function validateLiveFeed(value: unknown): Snapshot {
  if (!isRecord(value) || !isRecord(value.meta) || !isRecord(value.board))
    throw new Error("Live feed returned malformed data.");
  if (!isLiveFeedKind(value.meta.kind) || typeof value.meta.label !== "string")
    throw new Error("Live feed returned an unsupported frame kind.");
  if (!isRecord(value.trades) || !isRecord(value.board.columns))
    throw new Error("Live feed returned malformed board data.");
  const columns = value.board.columns;
  if (
    !Array.isArray(columns.checking) ||
    !Array.isArray(columns.holding) ||
    !Array.isArray(columns.done)
  )
    throw new Error("Live feed returned incomplete board lanes.");
  if (
    (value.board.ts !== null && typeof value.board.ts !== "number") ||
    typeof value.board.bankroll !== "number" ||
    typeof value.board.equity !== "number"
  )
    throw new Error("Live feed returned invalid market metadata.");
  return value as Snapshot;
}

export async function loadLiveFeed({
  signal,
}: {
  signal?: AbortSignal;
} = {}): Promise<Snapshot> {
  const controller = new AbortController();
  let timedOut = false;
  const timeout = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, 35_000);
  const abortFromCaller = () => controller.abort();
  signal?.addEventListener("abort", abortFromCaller, { once: true });
  try {
    const response = await fetch("/api/live-feed", {
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok) {
      let message = `Live feed unavailable (${response.status}).`;
      try {
        const body = await response.json();
        if (isRecord(body) && typeof body.error === "string") message = body.error;
      } catch {
        /* A non-JSON error response is still an unavailable live feed. */
      }
      throw new Error(message);
    }
    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      throw new Error("Live feed returned malformed JSON.");
    }
    return validateLiveFeed(payload);
  } catch (error) {
    if (timedOut)
      throw new Error("Live feed request timed out after 35 seconds.");
    throw error;
  } finally {
    clearTimeout(timeout);
    signal?.removeEventListener("abort", abortFromCaller);
  }
}
