import { useEffect, useState } from "react";
import type { WatchPeriods } from "./data";

export type SignalAnalysisGate = {
  key: string;
  label: string;
  state: "pass" | "fail" | "unknown" | "pending";
  actual: string;
  required: string;
  progressPct: number;
};

export type SignalAnalysisCandidate = {
  mint: string;
  symbol: string;
  side: "buy" | "sell";
  readinessPct: number | null;
  status: "ready" | "approaching" | "blocked" | "waiting" | "stale";
  observedAt: number | null;
  reason: string;
  gates: SignalAnalysisGate[];
};

export type SignalAnalysisEvent = {
  id: string;
  at: number;
  mint: string;
  symbol: string;
  side: "buy" | "sell";
  action:
    | "WAIT"
    | "BLOCK"
    | "READY"
    | "BUY"
    | "SCALE"
    | "SELL"
    | "SKIP"
    | "EXPIRE";
  readinessPct: number | null;
  reason: string;
};

export type PumpGate = {
  key: string;
  label: string;
  status: "pass" | "fail" | "unknown";
  detail: string;
};

export type PumpRadarRow = {
  mint: string;
  symbol: string;
  mcap: number | null;
  curvePct: number | null;
  momentum30sPct: number | null;
  signals: Record<string, boolean>;
  gates: PumpGate[];
  readinessPct: number;
  held: boolean;
  pending: boolean;
  readAgeS: number | null;
  netPct: number | null;
};

export type PumpEvent = {
  at: number;
  action: string;
  mint: string;
  symbol: string;
  reason: string;
};

export type PumpMode = {
  strategy: string;
  mode: "off" | "radar" | "paper";
  variant: { entry: string; exit: string };
  asOf: number | null;
  fresh: boolean;
  disclaimer: string;
  holderGate: string;
  calibrated: boolean;
  cadenceSeconds: number;
  ticketUsd: number;
  costModel: { feePct: number; networkUsd: number; impact: string; fill: string };
  book: {
    closed?: number;
    wins?: number;
    losses?: number;
    net?: number;
    drawdownPct?: number;
    maxDrawdownPct?: number;
    streak?: number;
    paused?: string | null;
    missed?: number;
    buys?: number;
  };
  source: {
    route?: string;
    error?: string | null;
    hot?: number;
    lastRead?: number | null;
    verifiedPoints?: number;
    launchReads?: number;
  };
  pending: { kind: string; mint: string; symbol: string; decided_at: number; reason: string }[];
  radar: PumpRadarRow[];
  events: PumpEvent[];
};

export type SignalAnalysis = {
  asOf: number | null;
  model: "gate-readiness-v1";
  calibrated: false;
  disclaimer: string;
  candidates: SignalAnalysisCandidate[];
  events: SignalAnalysisEvent[];
};

export type OptimizerStatus = {
  enabled: boolean;
  runtime: "running" | "standby" | "degraded" | "warming_up" | "disabled";
  paperOnly: boolean;
  intervalSeconds: number;
  lastTick: number | null;
  lastReview: number | null;
  nextReview: number | null;
  bootId: string;
  leaseOwned: boolean;
  entryReadiness?: {
    pending: number;
    holderUnknown: number;
    curveUnknown: number;
    overdue: number;
    expiredTotal: number;
    blocker: string | null;
  };
  signalAnalysis?: SignalAnalysis;
  pumpMode?: PumpMode;
  source: {
    name: string;
    lastObservation: number | null;
    error: string | null;
    observations: number;
    trackedTokens: number;
    social: string;
  };
  champion: { watchPeriods: WatchPeriods; revision: number };
  portfolio: {
    equity: number;
    cash: number;
    bankroll: number;
    grossRealized: number;
    modeledCosts: number;
    netRealized: number;
    openPositions: number;
    drawdownPct: number;
    closedTrades: number;
  };
  costModel: { feeBps: number; slippageBps: number; label: string };
  hourlyReports?: {
    id: string;
    start: number;
    end: number;
    classification: string;
    net: number;
    completedTrades: number;
    maxObservedDrawdownPct: number;
    freshSampleFraction: number;
    strategyRevisions: number[];
  }[];
  windows: {
    start: number;
    end: number;
    gross: number;
    costs: number;
    net: number;
    closedTrades: number;
    hasTrades: boolean;
  }[];
  validation: {
    state: "waiting" | "kept" | "promoted" | "rolled_back";
    reason: string;
    trainTrades: number;
    testTrades: number;
    testPF: number | null;
    testNet: number | null;
    maxDrawdown: number | null;
  };
  reviews: {
    id: string;
    at: number;
    decision: string;
    reason: string;
    metrics: Record<string, unknown>;
  }[];
};
export function useOptimizerStatus() {
  const [status, setStatus] = useState<OptimizerStatus | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    let inFlight = false;
    let controller: AbortController | null = null;
    let timeout: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      if (inFlight) return;
      inFlight = true;
      const request = new AbortController();
      controller = request;
      let timedOut = false;
      timeout = setTimeout(() => {
        timedOut = true;
        request.abort();
      }, 45_000);
      try {
        const r = await fetch("/api/optimizer", {
          cache: "no-store",
          signal: request.signal,
        });
        if (!r.ok)
          throw new Error(
            r.status === 404
              ? "Optimizer telemetry is not available in this version."
              : "Optimizer telemetry is temporarily unavailable.",
          );
        const data = (await r.json()) as OptimizerStatus;
        if (active) {
          setStatus(data);
          setError("");
        }
      } catch (e) {
        if (active && !request.signal.aborted)
          setError(
            e instanceof Error ? e.message : "Unable to reach the optimizer.",
          );
        else if (active && timedOut)
          setError("Optimizer telemetry request timed out after 45 seconds.");
      } finally {
        clearTimeout(timeout);
        if (controller === request) controller = null;
        inFlight = false;
      }
    };
    void load();
    const interval = setInterval(() => void load(), 15000);
    return () => {
      active = false;
      clearInterval(interval);
      clearTimeout(timeout);
      controller?.abort();
    };
  }, []);
  return { status, error };
}
export function timeLabel(ts: number | null) {
  return ts == null
    ? "Not yet"
    : new Date(ts * 1000).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      });
}
