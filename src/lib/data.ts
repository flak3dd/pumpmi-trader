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
  short: 90,
  medium: 180,
  long: 300,
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
export type Trade = {
  metric?: "unit_price" | "market_cap";
  series: { label: string; value: number }[];
  signals: Signal[];
  gatesBuy: { name: string; detail: string; pass: boolean }[];
  gatesSell: { name: string; detail: string; pass: boolean }[];
  entry: number;
  exitPnl: number;
  exitPp: number;
  dur: number;
  _exitTs?: number;
};
export type Snapshot = {
  strategyConfig?: StrategyConfig;
  board: {
    ts: number;
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
    kind: "snapshot" | "fixture";
    label: string;
    updated?: string;
    simulationAvailable: boolean;
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
export async function loadSnapshot(): Promise<Snapshot> {
  try {
    const response = await fetch("/api/snapshot", { cache: "no-store" });
    if (response.ok && response.headers.get("content-type")?.includes("json"))
      return await response.json();
  } catch {
    /* Static deployments expose the same truthful snapshot without simulation. */
  }
  const response = await fetch("/data/snapshot.json", { cache: "no-store" });
  if (!response.ok)
    throw new Error(
      "Snapshot unavailable. Check the paper-engine adapter and try again.",
    );
  const snapshot = (await response.json()) as Snapshot;
  snapshot.meta.simulationAvailable = false;
  return snapshot;
}
