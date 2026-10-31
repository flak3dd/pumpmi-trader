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
  Zap,
} from "lucide-react";
import { duration, money } from "../lib/data";
import { timeLabel, type OptimizerStatus } from "../lib/optimizer";

type Props = { status: OptimizerStatus | null; error: string };
type EntryReadiness = NonNullable<OptimizerStatus["entryReadiness"]>;

function count(value: number) {
  return Math.max(0, Math.trunc(Number.isFinite(value) ? value : 0));
}

function EntryReadinessStrip({
  readiness,
  priceLabel,
  priceTone,
}: {
  readiness?: EntryReadiness;
  priceLabel: string;
  priceTone: "live" | "waiting" | "stale" | "loading";
}) {
  if (!readiness) {
    return (
      <section
        className="entry-readiness unavailable"
        aria-label="Entry readiness"
      >
        <div className="entry-readiness-summary">
          <span className={`entry-price-state ${priceTone}`}>
            <Radio size={12} /> {priceLabel}
          </span>
          <div>
            <span className="entry-readiness-eyebrow">ENTRY READINESS</span>
            <h3>Entry verification telemetry unavailable</h3>
            <p>
              This optimizer status does not report watcher verification yet.
              Live-price collection and paper-entry checks are reported
              separately.
            </p>
          </div>
        </div>
        <div
          className="entry-readiness-stats"
          aria-label="Entry verification counts"
        >
          <div>
            <span>Current watchers</span>
            <b>—</b>
          </div>
          <div>
            <span>Holders verified</span>
            <b>—</b>
          </div>
          <div>
            <span>Reserves verified</span>
            <b>—</b>
          </div>
          <div>
            <span>Overdue</span>
            <b>—</b>
          </div>
          <div>
            <span>Expired total</span>
            <b>—</b>
          </div>
        </div>
      </section>
    );
  }

  const pending = count(readiness.pending);
  const holderUnknown = Math.min(pending, count(readiness.holderUnknown));
  const curveUnknown = Math.min(pending, count(readiness.curveUnknown));
  const overdue = count(readiness.overdue);
  const expiredTotal = count(readiness.expiredTotal);
  const holdersVerified = pending - holderUnknown;
  const reservesVerified = pending - curveUnknown;
  const allHoldersUnknown = pending > 0 && holderUnknown === pending;
  const verificationPending =
    pending > 0 && (holderUnknown > 0 || curveUnknown > 0);
  const blocker = readiness.blocker?.trim() || null;
  const tone = allHoldersUnknown
    ? "blocked"
    : verificationPending
      ? "pending"
      : blocker
        ? "blocked"
        : pending === 0
          ? "no-signal"
          : "verified";
  const heading = allHoldersUnknown
    ? "Paper entries blocked"
    : verificationPending
      ? "Verification pending"
      : blocker
        ? "Entry checks blocked"
        : pending === 0
          ? "No entry candidates yet"
          : "Verification current";
  const description = allHoldersUnknown
    ? "Every current watcher is missing holder verification. A provisioned read-only SOLANA_RPC_URL is required — not a wallet seed or signing key."
    : verificationPending
      ? "Some watcher checks are still incomplete. Paper entries await verification; this is not a trade or profit signal."
      : blocker
        ? "A backend safeguard is preventing entry evaluation. This status does not imply a paper trade or profit."
        : pending === 0
          ? "Live prices may still be collecting. No watcher currently qualifies for entry evaluation."
          : "Watcher checks are current. This is verification status only, not an entry, trade, or profit signal.";

  return (
    <section
      className={`entry-readiness ${tone}`}
      aria-label="Entry readiness"
      aria-live="polite"
    >
      <div className="entry-readiness-summary">
        <span className={`entry-price-state ${priceTone}`}>
          <Radio size={12} /> {priceLabel}
        </span>
        <span className="entry-readiness-icon" aria-hidden="true">
          <Zap size={18} />
        </span>
        <div>
          <span className="entry-readiness-eyebrow">ENTRY READINESS</span>
          <h3>{heading}</h3>
          <p>{description}</p>
          {blocker && (
            <p className="entry-readiness-blocker">
              <span>Backend blocker</span>
              {blocker}
            </p>
          )}
        </div>
      </div>
      <div
        className="entry-readiness-stats"
        aria-label="Entry verification counts"
      >
        <div>
          <span>Current watchers</span>
          <b>{pending.toLocaleString()}</b>
        </div>
        <div>
          <span>Holders verified</span>
          <b>
            {holdersVerified.toLocaleString()} / {pending.toLocaleString()}
          </b>
        </div>
        <div>
          <span>Reserves verified</span>
          <b>
            {reservesVerified.toLocaleString()} / {pending.toLocaleString()}
          </b>
        </div>
        <div>
          <span>Overdue</span>
          <b>{overdue.toLocaleString()}</b>
        </div>
        <div>
          <span>Expired total</span>
          <b>{expiredTotal.toLocaleString()}</b>
        </div>
      </div>
    </section>
  );
}

export default function OptimizerPanel({ status, error }: Props) {
  const fresh =
    status?.source.lastObservation != null &&
    Date.now() / 1000 - status.source.lastObservation < 180;
  const liveSource = Boolean(status?.enabled && fresh);
  const sharedReplica = Boolean(
    status?.enabled &&
      !status.leaseOwned &&
      fresh &&
      (status.runtime === "running" || status.runtime === "standby"),
  );
  const priceLabel = !status
    ? "Live price telemetry loading"
    : fresh
      ? "Live prices collecting"
      : status.source.lastObservation == null
        ? "Awaiting first live price"
        : "Live prices stale";
  const priceTone = !status
    ? "loading"
    : fresh
      ? "live"
      : status.source.lastObservation == null
        ? "waiting"
        : "stale";
  const runtimeLabel = !status
    ? "Loading optimizer telemetry"
    : !status.enabled
      ? "Optimizer disabled"
      : sharedReplica
        ? "Shared live feed · replica standby"
        : status.runtime === "running"
          ? fresh
            ? "Collecting market data"
            : "Awaiting fresh prices"
          : status.runtime === "warming_up"
            ? "Warming up"
            : status.runtime === "standby"
              ? "Replica standby · shared feed"
              : "Data collection interrupted";
  const validation = status?.validation;
  const portfolio = status?.portfolio;
  const decision = !status
    ? "Loading strategy telemetry"
    : validation?.state === "promoted"
      ? "Validated candidate promoted"
      : validation?.state === "rolled_back"
        ? "Prior champion restored"
        : validation?.state === "kept"
          ? "Champion retained"
          : "Waiting for evidence";
  const decisionReason = !status
    ? "Waiting for optimizer telemetry. Paper strategy settings remain read-only here."
    : validation?.reason ||
      "Waiting for validation evidence. No strategy change will be made without held-out validation.";
  const windows = status?.windows.slice(-12) || [];
  const max = Math.max(1, ...windows.map((w) => Math.abs(w.net)));
  return (
    <section
      className="optimizer-panel panel"
      aria-label="Paper strategy optimizer"
    >
      <div className="panel-heading">
        <span>
          <BrainCircuit size={17} /> Strategy optimizer{" "}
          <span className="dim-label">/ 30-MINUTE REVIEW CYCLE</span>
        </span>
        <span className={`optimizer-runtime ${liveSource ? "running" : ""}`}>
          <span />
          {runtimeLabel}
        </span>
      </div>
      <EntryReadinessStrip
        readiness={status?.entryReadiness}
        priceLabel={priceLabel}
        priceTone={priceTone}
      />
      <div className="optimizer-grid">
        <div className="optimizer-performance">
          <div className="optimizer-eyebrow">
            <FlaskConical size={12} /> FORWARD PAPER · MODELED EXECUTION COSTS
          </div>
          <div className="optimizer-net">
            <span>Net realized P&amp;L</span>
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
              <p>{decisionReason}</p>
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
          {timeLabel(status?.lastReview ?? null)}· next{" "}
          {timeLabel(status?.nextReview ?? null)}
        </span>
        <span>
          <Database size={12} />{" "}
          {!status
            ? "Optimizer telemetry loading"
            : status.enabled
              ? "Durable database state"
              : "Optimizer disabled"}
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
          <strong>Hourly capability &amp; profitability</strong>
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
            {!status
              ? "Hourly reporting status loading."
              : status.enabled
                ? "First full-hour report pending. No overlapping bot copies or bankroll resets."
                : "Hourly reports are unavailable while the optimizer is disabled."}
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
