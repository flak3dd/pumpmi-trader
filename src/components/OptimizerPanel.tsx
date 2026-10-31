import {
  Activity,
  ArrowUpRight,
  BrainCircuit,
  CheckCircle2,
  Clock3,
  Database,
  FlaskConical,
  Radio,
  ShieldCheck,
  TriangleAlert,
} from "lucide-react";
import { duration, money } from "../lib/data";
import { timeLabel, type OptimizerStatus } from "../lib/optimizer";

type Props = { status: OptimizerStatus | null; error: string };
export default function OptimizerPanel({ status, error }: Props) {
  const active = status?.enabled && status.runtime === "running";
  const fresh =
    status?.source.lastObservation != null &&
    Date.now() / 1000 - status.source.lastObservation < 180;
  const runtimeLabel = !status
    ? "Connecting"
    : !status.enabled
      ? "Worker not enabled"
      : status.runtime === "running"
        ? fresh
          ? "Collecting market data"
          : "Awaiting fresh prices"
        : status.runtime === "warming_up"
          ? "Warming up"
          : status.runtime === "standby"
            ? "Worker standby"
            : "Data collection interrupted";
  const validation = status?.validation;
  const portfolio = status?.portfolio;
  const decision =
    validation?.state === "promoted"
      ? "Validated candidate promoted"
      : validation?.state === "rolled_back"
        ? "Prior champion restored"
        : validation?.state === "kept"
          ? "Champion retained"
          : "Waiting for evidence";
  const windows = status?.windows.slice(-12) || [];
  const max = Math.max(1, ...windows.map((w) => Math.abs(w.net)));
  return (
    <section
      className="optimizer-panel panel"
      aria-label="Always-on paper optimizer"
    >
      <div className="panel-heading">
        <span>
          <BrainCircuit size={17} /> Strategy optimizer{" "}
          <span className="dim-label">/ 30-MINUTE REVIEW CYCLE</span>
        </span>
        <span
          className={`optimizer-runtime ${active && fresh ? "running" : ""}`}
        >
          <span />
          {runtimeLabel}
        </span>
      </div>
      <div className="optimizer-grid">
        <div className="optimizer-performance">
          <div className="optimizer-eyebrow">
            <FlaskConical size={12} /> FORWARD PAPER · MODELED EXECUTION COSTS
          </div>
          <div className="optimizer-net">
            <span>Net realized P&L</span>
            <strong
              className={
                portfolio && portfolio.netRealized < 0
                  ? "red-text"
                  : "lime-text"
              }
            >
              {portfolio ? money(portfolio.netRealized) : "—"}
            </strong>
            <span className="optimizer-trade-count">
              {portfolio?.closedTrades ?? 0} completed trades
            </span>
          </div>
          <div className="optimizer-numbers">
            <div>
              <span>Gross realized</span>
              <b>{portfolio ? money(portfolio.grossRealized) : "—"}</b>
            </div>
            <div>
              <span>Modeled costs</span>
              <b>{portfolio ? money(portfolio.modeledCosts) : "—"}</b>
            </div>
            <div>
              <span>Drawdown</span>
              <b>{portfolio ? `${portfolio.drawdownPct.toFixed(1)}%` : "—"}</b>
            </div>
          </div>
          <div className="optimizer-windows-heading">
            <span>HALF-HOUR NET RESULTS</span>
            <span>Empty windows are not wins</span>
          </div>
          <div
            className="optimizer-window-chart"
            aria-label="Net realized paper results by half-hour"
          >
            {windows.length ? (
              windows.map((w) => (
                <div
                  key={w.start}
                  className={`optimizer-window ${!w.hasTrades ? "empty" : w.net < 0 ? "loss" : "gain"}`}
                  title={`${timeLabel(w.start)}–${timeLabel(w.end)}: ${w.hasTrades ? money(w.net) + " modeled net · " + w.closedTrades + " closed trades" : "No closed trades"}`}
                >
                  <div className="window-bar-track">
                    <span
                      style={{
                        height: `${Math.max(w.hasTrades ? 3 : 0, (Math.abs(w.net) / max) * 100)}%`,
                      }}
                    />
                  </div>
                  <small>{timeLabel(w.start)}</small>
                </div>
              ))
            ) : (
              <div className="optimizer-no-windows">
                <Activity size={16} />
                <span>No completed half-hour windows yet.</span>
              </div>
            )}
          </div>
        </div>
        <div className="optimizer-evidence">
          <div className="optimizer-decision">
            <span className="evidence-icon">
              {validation?.state === "promoted" ? (
                <CheckCircle2 size={20} />
              ) : (
                <ShieldCheck size={20} />
              )}
            </span>
            <div>
              <h3>{decision}</h3>
              <p>
                {validation?.reason ||
                  "Start the published worker to collect fresh observations. No strategy change will be made without held-out validation."}
              </p>
            </div>
          </div>
          <div className="evidence-progress">
            <div>
              <span>Completed holdout trades</span>
              <b>
                {validation?.testTrades ?? 0} <span>/ 30 minimum</span>
              </b>
            </div>
            <div className="evidence-track">
              <span
                style={{
                  width: `${Math.min(100, ((validation?.testTrades ?? 0) / 30) * 100)}%`,
                }}
              />
            </div>
          </div>
          <div className="optimizer-checks">
            <span>
              <ShieldCheck size={12} /> Six-position cap stays fixed
            </span>
            <span>
              <CheckCircle2 size={12} /> Positive held-out net expectancy
            </span>
            <span>
              <CheckCircle2 size={12} /> Profit factor &gt; 1.1 · drawdown ≤ 10%
            </span>
            <span>
              <CheckCircle2 size={12} /> Must beat champion on the same holdout
            </span>
          </div>
          <div className="optimizer-evidence-metrics">
            <div>
              <span>Training trades</span>
              <b>{validation?.trainTrades ?? 0}</b>
            </div>
            <div>
              <span>Holdout net</span>
              <b>
                {validation?.testNet == null ? "—" : money(validation.testNet)}
              </b>
            </div>
            <div>
              <span>Holdout PF</span>
              <b>
                {validation?.testPF == null
                  ? "—"
                  : Number.isFinite(validation.testPF)
                    ? validation.testPF.toFixed(2)
                    : "∞"}
              </b>
            </div>
          </div>
        </div>
      </div>
      <div className="optimizer-monitor">
        <span>
          <Radio size={12} />{" "}
          {status?.source.observations.toLocaleString() ?? "0"} observations ·{" "}
          {status?.source.trackedTokens ?? 0} tracked
        </span>
        <span>
          <Clock3 size={12} /> Last review{" "}
          {timeLabel(status?.lastReview ?? null)} · next{" "}
          {timeLabel(status?.nextReview ?? null)}
        </span>
        <span>
          <Database size={12} />{" "}
          {status?.enabled
            ? "Durable database state"
            : "Forward worker disabled"}
        </span>
      </div>
      {status?.champion && (
        <div className="optimizer-champion">
          <span>ACTIVE CHAMPION</span>
          <b>S {duration(status.champion.watchPeriods.short)}</b>
          <b>M {duration(status.champion.watchPeriods.medium)}</b>
          <b>L {duration(status.champion.watchPeriods.long)}</b>
          <span className="champion-revision">
            Revision {status.champion.revision}
          </span>
        </div>
      )}
      {(error || status?.source.error) && (
        <div className="optimizer-error" role="alert">
          <TriangleAlert size={15} />
          <span>{error || status?.source.error}</span>
        </div>
      )}
      <div className="optimizer-disclaimer">
        <span>
          {status?.costModel
            ? `${status.costModel.feeBps / 100}% fee + ${status.costModel.slippageBps / 100}% slippage assumed per side.`
            : "Execution costs will be modeled explicitly."}{" "}
          Social data: {status?.source.social || "unavailable"}.
        </span>
        <span>No guaranteed profit. No real orders.</span>
      </div>
      <div className="optimizer-hourly">
        <div>
          <Clock3 size={13} />
          <strong>Hourly capability & profitability</strong>
          <span>Same paper portfolio · contiguous 1-hour windows</span>
        </div>
        {status?.hourlyReports?.length ? (
          status.hourlyReports
            .slice(-3)
            .reverse()
            .map((report) => (
              <div className="hourly-report-row" key={report.id}>
                <span>
                  {timeLabel(report.start)}–{timeLabel(report.end)}
                </span>
                <b className={report.net < 0 ? "red-text" : "lime-text"}>
                  {money(report.net)} net
                </b>
                <span>
                  {report.completedTrades} closed · DD{" "}
                  {report.maxObservedDrawdownPct.toFixed(1)}%
                </span>
                <small>
                  {report.classification.replaceAll("_", " ")} ·{" "}
                  {(report.freshSampleFraction * 100).toFixed(0)}% fresh samples
                </small>
              </div>
            ))
        ) : (
          <p>
            {status?.enabled
              ? "First full-hour report pending. No overlapping bot copies or bankroll resets."
              : "Hourly sessions begin when the approved published worker is active."}
          </p>
        )}
      </div>
      {!!status?.reviews.length && (
        <details className="optimizer-review-history">
          <summary>
            Review audit trail <ArrowUpRight size={12} />
          </summary>
          {status.reviews.slice(0, 5).map((r) => (
            <div key={r.id}>
              <span>{timeLabel(r.at)}</span>
              <b>{r.decision.replaceAll("_", " ")}</b>
              <p>{r.reason}</p>
            </div>
          ))}
        </details>
      )}
    </section>
  );
}
