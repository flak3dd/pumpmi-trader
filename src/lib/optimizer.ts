import { useEffect, useState } from "react";
import type { WatchPeriods } from "./data";

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
    const load = async () => {
      try {
        const r = await fetch("/api/optimizer", { cache: "no-store" });
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
        if (active)
          setError(
            e instanceof Error ? e.message : "Unable to reach the optimizer.",
          );
      }
    };
    void load();
    const interval = setInterval(() => void load(), 15000);
    return () => {
      active = false;
      clearInterval(interval);
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
