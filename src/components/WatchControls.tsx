import {
  Activity,
  CheckCheck,
  Clock3,
  LoaderCircle,
  RefreshCw,
  ShieldCheck,
  SlidersHorizontal,
} from "lucide-react";
import {
  duration,
  WATCH_TIERS,
  type StrategyConfig,
  type WatchPeriods,
  type WatchTier,
} from "../lib/data";
import type { SaveStatus } from "../lib/useStrategyConfig";

type Props = {
  config: StrategyConfig;
  periods: WatchPeriods;
  status: SaveStatus;
  error: string;
  updateTier: (tier: WatchTier, value: number) => void;
  retry: () => void;
};
export default function WatchControls({
  config,
  periods,
  status,
  error,
  updateTier,
  retry,
}: Props) {
  const labels = {
    loading: "Connecting…",
    saved: "Config saved",
    pending: "Unsaved changes",
    saving: "Saving config…",
    error: "Save failed",
    offline: "Adapter offline",
  };
  return (
    <section
      className="watch-controls panel"
      aria-label="Three-tier watch configuration"
    >
      <div className="panel-heading">
        <span>
          <SlidersHorizontal size={16} /> Watch period controls{" "}
          <span className="dim-label">/ THREE-TIER SIGNAL REVIEW</span>
        </span>
        <span className={`config-status ${status}`} role="status">
          {status === "saved" ? (
            <CheckCheck size={12} />
          ) : status === "saving" || status === "loading" ? (
            <LoaderCircle size={12} className="spin" />
          ) : (
            <Activity size={12} />
          )}
          {labels[status]}
        </span>
      </div>
      <div className="tier-sliders">
        {WATCH_TIERS.map(({ key, label, code }, index) => {
          const min =
            key === "short"
              ? 30
              : key === "medium"
                ? periods.short + 15
                : periods.medium + 15;
          const max =
            key === "long"
              ? 1800
              : key === "medium"
                ? periods.long - 15
                : periods.medium - 15;
          const position = Math.min(
            100,
            Math.max(0, ((periods[key] - min) / Math.max(1, max - min)) * 100),
          );
          return (
            <div className={`tier-slider ${key}`} key={key}>
              <div className="tier-slider-heading">
                <span className="tier-code">{code}</span>
                <div>
                  <label htmlFor={`watch-${key}`}>{label} watch</label>
                  <span>
                    {index === 0
                      ? "Early signal"
                      : index === 1
                        ? "Momentum confirmation"
                        : "Full observation window"}
                  </span>
                </div>
                <output htmlFor={`watch-${key}`}>
                  {duration(periods[key])}
                </output>
              </div>
              <input
                id={`watch-${key}`}
                type="range"
                min={min}
                max={max}
                step="15"
                value={periods[key]}
                onChange={(e) => updateTier(key, Number(e.target.value))}
                disabled={!config.editable || status === "loading"}
                aria-label={`${label} watch period`}
                aria-valuetext={`${periods[key]} seconds`}
                style={
                  { "--range-fill": `${position}%` } as React.CSSProperties
                }
              />
              <div className="tier-slider-scale">
                <span>{duration(min)}</span>
                <span className="tier-parameter">
                  watch_{key === "medium" ? "medium" : key}_s
                </span>
                <span>{duration(max)}</span>
              </div>
            </div>
          );
        })}
      </div>
      <div className="watch-controls-foot">
        <span>
          <ShieldCheck size={12} /> {config.strategy.max_open || 6} paper
          positions max
        </span>
        <span>
          <Clock3 size={12} /> Auto-saves after moving · 15-second steps
        </span>
        <span>Applies to newly watched tokens</span>
      </div>
      {error && (
        <div className="config-error" role="alert">
          <span>{error}</span>
          <button onClick={retry}>
            <RefreshCw size={12} />{" "}
            {status === "offline" ? "Reconnect" : "Retry save"}
          </button>
        </div>
      )}
    </section>
  );
}
