import { useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  CircleDashed,
  Radar,
  ShieldAlert,
  XCircle,
  Zap,
} from "lucide-react";
import type { OptimizerStatus, PumpGate, PumpRadarRow } from "../lib/optimizer";

type Props = {
  status: OptimizerStatus | null;
  error: string;
};

const MODE_LABEL: Record<string, string> = {
  off: "OFF",
  radar: "RADAR · NO ORDERS",
  paper: "PAPER ENTRIES ON",
};

function money(value: number | null | undefined, digits = 2) {
  if (value == null || !Number.isFinite(value)) return "—";
  const sign = value < 0 ? "−" : "";
  return `${sign}$${Math.abs(value).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

function mcap(value: number | null) {
  if (value == null || !Number.isFinite(value)) return "—";
  return value >= 1000 ? `$${(value / 1000).toFixed(1)}k` : `$${value.toFixed(0)}`;
}

function pct(value: number | null | undefined, digits = 1) {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}%`;
}

function clock(at: number) {
  return new Date(at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function GateIcon({ gate }: { gate: PumpGate }) {
  if (gate.status === "pass") return <CheckCircle2 size={11} aria-label="pass" />;
  if (gate.status === "fail") return <XCircle size={11} aria-label="fail" />;
  return <CircleDashed size={11} aria-label="unknown" />;
}

function actionTone(action: string) {
  if (action === "BUY" || action === "BUY QUEUED") return "buy";
  if (action === "SELL" || action === "EXIT QUEUED" || action === "SCALE" || action === "SCALE QUEUED") return "sell";
  if (action === "RADAR") return "radar";
  if (action === "MISSED") return "missed";
  return "skip";
}

function RadarRow({ row, entrySignal }: { row: PumpRadarRow; entrySignal: string }) {
  const [open, setOpen] = useState(false);
  const failing = row.gates.filter((gate) => gate.status !== "pass");
  const active = Boolean(row.signals[entrySignal]);
  return (
    <>
      <tr className={`pump-row${row.held ? " held" : ""}${active ? " active" : ""}`} onClick={() => setOpen((v) => !v)}>
        <td className="pump-symbol">
          <ChevronDown size={11} className={open ? "open" : ""} />
          <strong>{row.symbol}</strong>
          {row.held && <span className="pump-tag held">HELD</span>}
          {row.pending && <span className="pump-tag pending">FILL PENDING</span>}
        </td>
        <td>{mcap(row.mcap)}</td>
        <td>{row.curvePct == null ? "—" : `${row.curvePct.toFixed(0)}%`}</td>
        <td className={row.momentum30sPct != null && row.momentum30sPct > 0 ? "up" : "down"}>{pct(row.momentum30sPct)}</td>
        <td className="pump-signals">
          {Object.entries(row.signals).map(([name, on]) => (
            <span key={name} className={`pump-chip${on ? " on" : ""}${name === entrySignal ? " live" : ""}`}>{name}</span>
          ))}
        </td>
        <td>
          <div className="pump-meter" title="Share of pump-mode gates currently passing. Not a probability.">
            <i style={{ width: `${Math.max(2, Math.min(100, row.readinessPct))}%` }} />
            <span>{row.readinessPct.toFixed(0)}%</span>
          </div>
        </td>
        <td className="pump-gates">
          {row.gates.map((gate) => (
            <span key={gate.key} className={`pump-gate ${gate.status}`} title={`${gate.label}: ${gate.detail}`}>
              <GateIcon gate={gate} />
            </span>
          ))}
        </td>
        <td className={row.netPct != null && row.netPct >= 0 ? "up" : "down"}>{row.held ? pct(row.netPct) : ""}</td>
      </tr>
      {open && (
        <tr className="pump-detail">
          <td colSpan={8}>
            <ul>
              {row.gates.map((gate) => (
                <li key={gate.key} className={gate.status}>
                  <GateIcon gate={gate} /> <b>{gate.label}</b> — {gate.detail}
                </li>
              ))}
            </ul>
            {failing.length === 0 && active && <p className="pump-ready">Every gate passes on the latest verified read.</p>}
            <code>{row.mint}</code>
            {row.readAgeS != null && <span className="pump-age">verified read {row.readAgeS.toFixed(1)} s ago</span>}
          </td>
        </tr>
      )}
    </>
  );
}

export default function PumpRadarPanel({ status, error }: Props) {
  const pump = status?.pumpMode;
  const [onlyActive, setOnlyActive] = useState(false);
  const rows = useMemo(() => {
    if (!pump) return [];
    return onlyActive ? pump.radar.filter((row) => row.held || row.pending || row.signals[pump.variant.entry]) : pump.radar;
  }, [pump, onlyActive]);
  const events = useMemo(() => (pump ? [...pump.events].reverse().slice(0, 40) : []), [pump]);

  if (!pump) {
    return (
      <section className="panel pump-panel">
        <div className="panel-heading pump-heading">
          <span><Zap size={13} /> Pump capture</span>
          <span className="pump-mode off">{error ? "UNAVAILABLE" : "LOADING"}</span>
        </div>
      </section>
    );
  }
  const book = pump.book;
  const source = pump.source;
  const mode = MODE_LABEL[pump.mode] ?? pump.mode.toUpperCase();
  return (
    <section className="panel pump-panel" aria-label="Pump capture radar">
      <div className="panel-heading pump-heading">
        <span>
          <Zap size={13} /> Pump capture · {pump.strategy} · {pump.variant.entry}/{pump.variant.exit}
        </span>
        <span className={`pump-mode ${pump.mode}`}>{mode}</span>
        <span className={`pump-live${pump.fresh ? " fresh" : ""}`}>
          <Radar size={11} /> {pump.fresh ? `live · every ~${pump.cadenceSeconds.toFixed(0)} s` : "stale"}
          {source.route ? ` · ${source.route}` : ""}
          {source.hot != null ? ` · ${source.hot} hot` : ""}
        </span>
      </div>
      <div className="pump-notice">
        <ShieldAlert size={14} />
        <p>
          {pump.disclaimer} <em>{pump.holderGate}.</em> Costs: {pump.costModel.feePct.toFixed(2)}% curve fee per side, ${pump.costModel.networkUsd.toFixed(2)} network
          per transaction, {pump.costModel.impact}; fills at the {pump.costModel.fill}. Ticket {money(pump.ticketUsd)}.
        </p>
      </div>
      {source.error && (
        <div className="pump-alert">
          <AlertTriangle size={13} /> {source.error}
        </div>
      )}
      {book.paused && (
        <div className="pump-alert">
          <AlertTriangle size={13} /> {book.paused}
        </div>
      )}
      <div className="pump-book">
        <div><span>Pump trades</span><strong>{book.closed ?? 0}</strong></div>
        <div><span>Wins / losses</span><strong>{book.wins ?? 0} / {book.losses ?? 0}</strong></div>
        <div><span>Pump net (paper)</span><strong className={(book.net ?? 0) >= 0 ? "up" : "down"}>{money(book.net)}</strong></div>
        <div><span>Book drawdown</span><strong>{(book.drawdownPct ?? 0).toFixed(1)}% <small>max {(book.maxDrawdownPct ?? 0).toFixed(1)}%</small></strong></div>
        <div><span>Missed fills</span><strong>{book.missed ?? 0}</strong></div>
        <div><span>Loss streak</span><strong>{book.streak ?? 0}</strong></div>
      </div>
      {pump.pending.length > 0 && (
        <div className="pump-pending">
          {pump.pending.map((order) => (
            <span key={`${order.kind}-${order.mint}`}>
              <Activity size={11} /> {order.kind.toUpperCase()} {order.symbol} — {order.reason}
            </span>
          ))}
        </div>
      )}
      <div className="pump-columns">
        <div className="pump-radar">
          <div className="pump-subhead">
            <span>Hot tokens on verified reserves</span>
            <label>
              <input type="checkbox" checked={onlyActive} onChange={(e) => setOnlyActive(e.target.checked)} /> signal / held only
            </label>
          </div>
          {rows.length === 0 ? (
            <p className="pump-empty">{pump.mode === "off" ? "Pump mode is off." : "No hot tokens yet; the radar fills as tokens cross $4k."}</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Token</th><th>Mcap</th><th>Curve</th><th>30 s</th><th>Signals</th><th title="Not a probability">Gate share</th><th>Gates</th><th>Net</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <RadarRow key={row.mint} row={row} entrySignal={pump.variant.entry} />
                ))}
              </tbody>
            </table>
          )}
        </div>
        <div className="pump-log">
          <div className="pump-subhead"><span>Pump reasoning log</span></div>
          {events.length === 0 ? (
            <p className="pump-empty">No pump decisions yet.</p>
          ) : (
            <ol>
              {events.map((event, index) => (
                <li key={`${event.at}-${event.mint}-${index}`} className={actionTone(event.action)}>
                  <time>{clock(event.at)}</time>
                  <b>{event.action}</b>
                  <strong>{event.symbol}</strong>
                  <span>{event.reason}</span>
                </li>
              ))}
            </ol>
          )}
        </div>
      </div>
    </section>
  );
}
