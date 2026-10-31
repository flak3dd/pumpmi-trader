import { useId, useState } from "react";
import { Crosshair, Activity } from "lucide-react";
import type { Trade } from "../lib/data";
import { chartMetric, chartNumber } from "../lib/data";

type Props = {
  trade?: Trade;
  compactMode?: boolean;
  showSignals?: boolean;
  symbol?: string;
};
export default function TradeChart({
  trade,
  compactMode = false,
  showSignals = true,
  symbol,
}: Props) {
  const id = useId().replace(/:/g, "");
  const [hover, setHover] = useState<number | null>(null);
  if (!trade?.series?.length)
    return (
      <div className="chart-empty">
        <Activity size={30} />
        <strong>No live market path or paper execution series</strong>
        <span>This asset has no sampled path yet.</span>
      </div>
    );
  const isMarketWatch = trade.isMarketWatch === true;
  const width = 900,
    height = compactMode ? 180 : 260;
  const pad = { left: 12, right: 68, top: 30, bottom: 32 };
  const values = trade.series.map((p) => p.value);
  const rawMin = Math.min(...values, ...(isMarketWatch ? [] : [trade.entry]));
  const rawMax = Math.max(...values, ...(isMarketWatch ? [] : [trade.entry]));
  const range = Math.max(
    rawMax - rawMin,
    rawMax * 0.06,
    trade.metric === "unit_price" ? 0.01 : 1,
  );
  const min = rawMin - range * 0.22,
    max = rawMax + range * 0.28;
  const x = (i: number) =>
    pad.left +
    (i / Math.max(values.length - 1, 1)) * (width - pad.left - pad.right);
  const y = (v: number) =>
    pad.top + ((max - v) / (max - min)) * (height - pad.top - pad.bottom);
  const line = values
    .map((v, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(v)}`)
    .join(" ");
  const fill = `${line} L${x(values.length - 1)},${height - pad.bottom} L${x(0)},${height - pad.bottom} Z`;
  const safeHover = hover !== null && hover < values.length ? hover : null;
  const active = safeHover === null ? null : trade.series[safeHover];
  const activeSignal =
    safeHover === null
      ? undefined
      : trade.signals.find((s) => s.at === safeHover);
  const lastValue = values[values.length - 1];
  const pathName = isMarketWatch ? "live market path" : "paper execution path";
  return (
    <div className={`trade-chart ${compactMode ? "compact-chart" : ""}`}>
      {active && (
        <div className="chart-tooltip">
          <Crosshair size={13} />
          <span>
            {symbol} · point {(safeHover || 0) + 1}
          </span>
          <b>{chartNumber(active.value, trade)}</b>
          {activeSignal && (
            <span>
              {activeSignal.label} · {activeSignal.t}
            </span>
          )}
        </div>
      )}
      <svg
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={
          isMarketWatch
            ? `${symbol || "Asset"} live market path. Current ${chartNumber(lastValue, trade)}. Observed quotes; paper executions are shown separately.`
            : `${symbol || "Trade"} paper execution path. Entry ${chartNumber(trade.entry, trade)}, final ${chartNumber(lastValue, trade)}.`
        }
        onMouseLeave={() => setHover(null)}
      >
        <defs>
          <linearGradient id={`fill-${id}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#c5f85b" stopOpacity=".21" />
            <stop offset="100%" stopColor="#c5f85b" stopOpacity="0" />
          </linearGradient>
          <filter id={`glow-${id}`}>
            <feGaussianBlur stdDeviation="3" />
          </filter>
        </defs>
        {[0, 1, 2, 3, 4].map((i) => {
          const v = min + ((max - min) * i) / 4;
          return (
            <g key={i}>
              <line
                x1={pad.left}
                x2={width - pad.right}
                y1={y(v)}
                y2={y(v)}
                className="chart-grid"
              />
              <text
                x={width - pad.right + 15}
                y={y(v) + 4}
                className="chart-axis"
              >
                {chartNumber(v, trade)}
              </text>
            </g>
          );
        })}
        {[0, 1, 2, 3, 4, 5, 6].map((i) => (
          <line
            key={i}
            x1={pad.left + (i / 6) * (width - pad.left - pad.right)}
            x2={pad.left + (i / 6) * (width - pad.left - pad.right)}
            y1={pad.top}
            y2={height - pad.bottom}
            className="chart-grid vertical"
          />
        ))}
        <path d={fill} fill={`url(#fill-${id})`} />
        {!isMarketWatch && (
          <>
            <line
              x1={pad.left}
              x2={width - pad.right}
              y1={y(trade.entry)}
              y2={y(trade.entry)}
              className="entry-line"
            />
            <text
              x={pad.left + 6}
              y={y(trade.entry) - 7}
              className="entry-label"
            >
              ENTRY {chartNumber(trade.entry, trade)}
            </text>
          </>
        )}
        <path
          d={line}
          fill="none"
          stroke="#c5f85b"
          strokeWidth="6"
          opacity=".22"
          filter={`url(#glow-${id})`}
        />
        <path
          d={line}
          fill="none"
          stroke="#c5f85b"
          strokeWidth="2.5"
          strokeLinejoin="round"
          className="chart-price-line"
        />
        {showSignals &&
          !isMarketWatch &&
          trade.signals.map((s) => (
            <g key={s.id} className={`chart-signal ${s.kind}`}>
              <line
                x1={x(s.at)}
                x2={x(s.at)}
                y1={y(values[s.at] ?? s.mcap)}
                y2={s.kind === "buy" ? height - pad.bottom : pad.top}
              />
              <circle cx={x(s.at)} cy={y(values[s.at] ?? s.mcap)} r="5" />
              <rect
                x={Math.min(
                  Math.max(x(s.at) - 26, pad.left),
                  width - pad.right - 60,
                )}
                y={s.kind === "buy" ? height - pad.bottom - 22 : pad.top - 23}
                width="54"
                height="19"
                rx="4"
              />
              <text
                x={Math.min(
                  Math.max(x(s.at) + 1, pad.left + 27),
                  width - pad.right - 33,
                )}
                y={s.kind === "buy" ? height - pad.bottom - 9 : pad.top - 10}
                textAnchor="middle"
              >
                {s.kind === "buy"
                  ? "BUY"
                  : s.kind === "scale"
                    ? "½ SELL"
                    : "EXIT"}
              </text>
            </g>
          ))}
        {trade.series.map((p, i) =>
          p.label ? (
            <text
              key={i}
              x={x(i)}
              y={height - 7}
              textAnchor={
                i === 0 ? "start" : i === values.length - 1 ? "end" : "middle"
              }
              className="chart-axis"
            >
              {p.label}
            </text>
          ) : null,
        )}
        {safeHover !== null && (
          <g>
            <line
              x1={x(safeHover)}
              x2={x(safeHover)}
              y1={pad.top}
              y2={height - pad.bottom}
              className="crosshair"
            />
            <circle
              cx={x(safeHover)}
              cy={y(values[safeHover])}
              r="5"
              fill="#101710"
              stroke="#c5f85b"
              strokeWidth="2"
            />
          </g>
        )}
        {values.map((_, i) => (
          <rect
            key={i}
            x={x(i) - (width - pad.right) / values.length / 2}
            y="0"
            width={(width - pad.right) / values.length}
            height={height}
            fill="transparent"
            tabIndex={0}
            aria-label={`${pathName} point ${i + 1}: ${chartNumber(values[i], trade)}`}
            onMouseEnter={() => setHover(i)}
            onFocus={() => setHover(i)}
            onBlur={() => setHover(null)}
          />
        ))}
      </svg>
    </div>
  );
}
