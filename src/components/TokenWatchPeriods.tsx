import { Check, Clock3 } from "lucide-react";
import {
  duration,
  signed,
  WATCH_TIERS,
  type Token,
  type WatchPeriods,
} from "../lib/data";

type Props = {
  token: Token;
  periods: WatchPeriods;
  snapshotTs: number;
  detailed?: boolean;
};
export default function TokenWatchPeriods({
  token,
  periods,
  snapshotTs,
  detailed = false,
}: Props) {
  const started = token.watch_started_ts ?? token.ts_seen;
  const elapsed = started == null ? null : Math.max(0, snapshotTs - started);
  return (
    <div
      className={`token-watch-periods ${detailed ? "detailed" : ""}`}
      aria-label={`${token.symbol} watch periods`}
    >
      {WATCH_TIERS.map(({ key, label }) => {
        const checkpoint = token.watch_periods?.[key];
        const seconds = checkpoint?.duration_s ?? periods[key];
        const observed = checkpoint?.status === "observed";
        const time =
          observed || checkpoint?.status === "expired"
            ? (checkpoint?.elapsed_s ?? elapsed)
            : (elapsed ?? checkpoint?.elapsed_s ?? null);
        const progress =
          time == null ? 0 : Math.min(100, (time / Math.max(seconds, 1)) * 100);
        const status = observed
          ? "Observed"
          : checkpoint?.status === "expired"
            ? "Expired"
            : time == null
              ? "Unmeasured"
              : time >= seconds
                ? "Elapsed · no sample"
                : "Watching";
        const delta = checkpoint?.curve_delta;
        return (
          <div
            className={`token-watch-tier ${key} ${observed ? "observed" : ""}`}
            key={key}
            title={`${label}: ${seconds}s. ${status}${delta == null ? (observed ? ". Curve delta unavailable." : ". No curve checkpoint recorded.") : `. Curve change ${signed(delta, " points")}.`}`}
          >
            <div className="token-watch-label">
              <span>{!detailed && key === "medium" ? "Med" : label}</span>
              <strong>{duration(seconds)}</strong>
              {observed ? <Check size={9} /> : <Clock3 size={9} />}
            </div>
            <div className="token-watch-track">
              <span style={{ width: `${progress}%` }} />
            </div>
            <small>
              {observed && delta != null
                ? detailed
                  ? `${signed(delta, " pts")} · observed`
                  : "Observed"
                : detailed
                  ? status
                  : status === "Elapsed · no sample"
                    ? "No sample"
                    : status}
            </small>
          </div>
        );
      })}
    </div>
  );
}
