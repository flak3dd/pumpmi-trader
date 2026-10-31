import { useEffect, useRef, useState } from "react";
import {
  DEFAULT_WATCH_PERIODS,
  type StrategyConfig,
  type WatchPeriods,
  type WatchTier,
} from "./data";

const DEFAULT_STRATEGY: StrategyConfig = {
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
  watchPeriods: DEFAULT_WATCH_PERIODS,
  revision: 0,
  editable: false,
  paperOnly: true,
};
const matches = (a: WatchPeriods, b: WatchPeriods) =>
  a.short === b.short && a.medium === b.medium && a.long === b.long;
export type SaveStatus =
  | "loading"
  | "saved"
  | "pending"
  | "saving"
  | "error"
  | "offline";

export default function useStrategyConfig() {
  const [config, setConfig] = useState<StrategyConfig>(DEFAULT_STRATEGY);
  const [periods, setPeriods] = useState<WatchPeriods>({
    ...DEFAULT_WATCH_PERIODS,
  });
  const [status, setStatus] = useState<SaveStatus>("loading");
  const [error, setError] = useState("");
  const savedRef = useRef(DEFAULT_STRATEGY);
  const draftRef = useRef({ ...DEFAULT_WATCH_PERIODS });
  const saving = useRef(false);
  const active = useRef(true);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const loadId = useRef(0);

  async function readConfig() {
    const r = await fetch("/api/strategy", { cache: "no-store" });
    if (!r.ok) throw new Error("The configuration adapter is unavailable.");
    const next = (await r.json()) as StrategyConfig;
    if (!next.watchPeriods || !next.editable || !next.paperOnly)
      throw new Error(
        "Connect the updated paper adapter to enable tier controls.",
      );
    return next;
  }
  async function load() {
    const requestId = ++loadId.current;
    setStatus("loading");
    try {
      const next = await readConfig();
      if (!active.current || requestId !== loadId.current) return;
      savedRef.current = next;
      draftRef.current = { ...next.watchPeriods };
      setConfig(next);
      setPeriods({ ...next.watchPeriods });
      setStatus("saved");
      setError("");
    } catch (e) {
      if (!active.current || requestId !== loadId.current) return;
      setStatus("offline");
      setError(
        e instanceof Error
          ? e.message
          : "Unable to load the watch configuration.",
      );
    }
  }
  async function flush() {
    clearTimeout(timer.current);
    if (saving.current || !savedRef.current.editable || !active.current) return;
    if (matches(draftRef.current, savedRef.current.watchPeriods)) {
      setStatus("saved");
      return;
    }
    const sent = { ...draftRef.current };
    saving.current = true;
    ++loadId.current;
    setStatus("saving");
    setError("");
    let succeeded = false;
    try {
      const r = await fetch("/api/strategy", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          watchPeriods: sent,
          revision: savedRef.current.revision,
        }),
      });
      const next = await r.json();
      if (!r.ok) {
        if (r.status === 409) {
          const current = await readConfig();
          if (active.current) {
            savedRef.current = current;
            setConfig(current);
          }
          throw new Error(
            "Configuration changed in another session. Review these values, then retry to apply your draft.",
          );
        }
        throw new Error(next.error || "Watch periods could not be saved.");
      }
      if (!active.current) return;
      savedRef.current = next;
      setConfig(next);
      succeeded = true;
      if (matches(draftRef.current, sent)) {
        draftRef.current = { ...next.watchPeriods };
        setPeriods({ ...next.watchPeriods });
        setStatus("saved");
      } else setStatus("pending");
    } catch (e) {
      if (active.current) {
        setStatus("error");
        setError(
          e instanceof Error
            ? e.message
            : "Save failed. Your draft has not been applied.",
        );
      }
    } finally {
      saving.current = false;
      if (
        active.current &&
        succeeded &&
        !matches(draftRef.current, savedRef.current.watchPeriods)
      )
        timer.current = setTimeout(() => void flush(), 350);
    }
  }
  function updateTier(tier: WatchTier, raw: number) {
    if (!savedRef.current.editable) return;
    const current = draftRef.current;
    const minimum =
      tier === "short"
        ? 30
        : tier === "medium"
          ? current.short + 15
          : current.medium + 15;
    const maximum =
      tier === "long"
        ? 1800
        : tier === "medium"
          ? current.long - 15
          : current.medium - 15;
    const value = Math.min(
      maximum,
      Math.max(minimum, Math.round(raw / 15) * 15),
    );
    draftRef.current = { ...current, [tier]: value };
    setPeriods(draftRef.current);
    setStatus("pending");
    setError("");
    clearTimeout(timer.current);
    timer.current = setTimeout(() => void flush(), 350);
  }
  useEffect(() => {
    active.current = true;
    void load();
    return () => {
      active.current = false;
      ++loadId.current;
      clearTimeout(timer.current);
    };
  }, []);
  return {
    config,
    periods,
    status,
    error,
    updateTier,
    retry: () => (status === "offline" ? void load() : void flush()),
  };
}
