import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  ArrowUpRight,
  CheckCircle2,
  ChevronDown,
  CircleDashed,
  Clock3,
  Pause,
  Play,
  Radio,
  Search,
  ShieldAlert,
  SlidersHorizontal,
} from "lucide-react";
import type {
  OptimizerStatus,
  SignalAnalysisCandidate,
  SignalAnalysisEvent,
  SignalAnalysisGate,
} from "../lib/optimizer";

type CandidateFilter = "all" | "buy" | "sell";
type LogSideFilter = "all" | "buy" | "sell";
type LogActionFilter = "all" | SignalAnalysisEvent["action"];

type Props = {
  status: OptimizerStatus | null;
  error: string;
  onSelectCandidate?: (candidate: SignalAnalysisCandidate) => void;
  canInspectCandidate?: (candidate: SignalAnalysisCandidate) => boolean;
};

const NEAR_THRESHOLD = 60;
const MAX_STORED_EVENTS = 500;
const MAX_DISPLAYED_EVENTS = 50;

const EVENT_ACTIONS: SignalAnalysisEvent["action"][] = [
  "WAIT",
  "BLOCK",
  "READY",
  "BUY",
  "SCALE",
  "SELL",
  "SKIP",
  "EXPIRE",
];

function isFiniteScore(value: number | null): value is number {
  return value != null && Number.isFinite(value);
}

function displayScore(value: number | null) {
  return isFiniteScore(value) ? `${value.toFixed(0)}%` : "—";
}

function exactTime(value: number | null) {
  if (value == null || !Number.isFinite(value)) return "Quote unavailable";
  const date = new Date(value * 1000);
  if (Number.isNaN(date.getTime())) return "Quote unavailable";
  return date.toLocaleString([], {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function candidateOrder(
  a: SignalAnalysisCandidate,
  b: SignalAnalysisCandidate,
) {
  const aScore = isFiniteScore(a.readinessPct) ? a.readinessPct : -1;
  const bScore = isFiniteScore(b.readinessPct) ? b.readinessPct : -1;
  if (bScore !== aScore) return bScore - aScore;
  if ((b.observedAt ?? -1) !== (a.observedAt ?? -1))
    return (b.observedAt ?? -1) - (a.observedAt ?? -1);
  return a.symbol.localeCompare(b.symbol);
}

function eventOrder(a: SignalAnalysisEvent, b: SignalAnalysisEvent) {
  if (b.at !== a.at) return b.at - a.at;
  return a.id.localeCompare(b.id);
}

function isPaperExecution(action: SignalAnalysisEvent["action"]) {
  return action === "BUY" || action === "SCALE" || action === "SELL";
}

function GateRow({ gate }: { gate: SignalAnalysisGate }) {
  const progress = Number.isFinite(gate.progressPct)
    ? Math.max(0, Math.min(100, gate.progressPct))
    : 0;
  return (
    <div className={`signal-gate ${gate.state}`}>
      <div className="signal-gate-heading">
        <span>{gate.label}</span>
        <b>{gate.state}</b>
      </div>
      <div className="signal-gate-values">
        <span>
          <small>Measured</small>
          {gate.actual}
        </span>
        <span>
          <small>Required</small>
          {gate.required}
        </span>
      </div>
      <div className="signal-gate-progress">
        <span style={{ width: `${progress}%` }} />
      </div>
      <small className="signal-gate-progress-label">
        Reported gate progress {displayScore(gate.progressPct)}
      </small>
    </div>
  );
}

function CandidateCard({
  candidate,
  inspectable,
  onSelect,
}: {
  candidate: SignalAnalysisCandidate;
  inspectable: boolean;
  onSelect?: (candidate: SignalAnalysisCandidate) => void;
}) {
  const scoreLabel =
    candidate.side === "buy" ? "Buy readiness" : "Sell proximity";
  const time = exactTime(candidate.observedAt);
  const score = candidate.readinessPct;
  const hasScore = isFiniteScore(score);
  return (
    <article
      className={`signal-candidate ${candidate.side} ${candidate.status}`}
      data-mint={candidate.mint}
    >
      <div className="signal-candidate-main">
        <div className="signal-candidate-identity">
          <span className={`signal-side-mark ${candidate.side}`}>
            {candidate.side === "buy" ? "B" : "S"}
          </span>
          <div>
            <div className="signal-candidate-symbol-row">
              <strong className="signal-candidate-symbol">
                {candidate.symbol}
              </strong>
              <span className={`signal-status-chip ${candidate.status}`}>
                {candidate.status}
              </span>
            </div>
            <span className="signal-quote-time">
              <Clock3 size={11} /> {time}
            </span>
          </div>
        </div>
        <div
          className="signal-score"
          aria-label={`${scoreLabel} ${displayScore(candidate.readinessPct)}`}
        >
          <span>{scoreLabel}</span>
          <strong className={!hasScore ? "unavailable" : ""}>
            {displayScore(candidate.readinessPct)}
          </strong>
          <small>
            {hasScore ? "Server-reported gate score" : "Quote unavailable"}
          </small>
        </div>
      </div>
      <div className="signal-score-track" aria-hidden="true">
        <span
          style={{
            width: hasScore ? `${Math.max(0, Math.min(100, score))}%` : "0%",
          }}
        />
      </div>
      <div className="signal-candidate-reason">
        <ShieldAlert size={14} />
        <div>
          <span>Current reason</span>
          <p>
            {candidate.reason || "No reasoning text supplied by the server."}
          </p>
        </div>
      </div>
      <div className="signal-candidate-foot">
        <details className="signal-gates">
          <summary>
            <span>
              <SlidersHorizontal size={12} /> Measured gate facts
            </span>
            <span>
              {candidate.gates.length} gate
              {candidate.gates.length === 1 ? "" : "s"}
              <ChevronDown size={12} />
            </span>
          </summary>
          <div className="signal-gate-list">
            {candidate.gates.length ? (
              candidate.gates.map((gate) => (
                <GateRow gate={gate} key={gate.key} />
              ))
            ) : (
              <p>No measured gate facts were supplied for this candidate.</p>
            )}
          </div>
        </details>
        {inspectable && onSelect ? (
          <button
            className="signal-inspect-button"
            onClick={() => onSelect(candidate)}
            aria-label={`Inspect ${candidate.symbol}`}
          >
            Inspect token <ArrowUpRight size={12} />
          </button>
        ) : (
          <span className="signal-board-unavailable">Not on current board</span>
        )}
      </div>
    </article>
  );
}

export default function SignalAnalysisPanel({
  status,
  error,
  onSelectCandidate,
  canInspectCandidate,
}: Props) {
  const analysis = status?.signalAnalysis;
  const [candidateFilter, setCandidateFilter] =
    useState<CandidateFilter>("all");
  const [logSide, setLogSide] = useState<LogSideFilter>("all");
  const [logAction, setLogAction] = useState<LogActionFilter>("all");
  const [logSearch, setLogSearch] = useState("");
  const [logPaused, setLogPaused] = useState(false);
  const [eventHistory, setEventHistory] = useState<SignalAnalysisEvent[]>([]);
  const [frozenEvents, setFrozenEvents] = useState<SignalAnalysisEvent[]>([]);

  useEffect(() => {
    if (!analysis?.events.length) return;
    setEventHistory((previous) => {
      const merged = new Map(previous.map((event) => [event.id, event]));
      let changed = false;
      for (const event of analysis.events) {
        const current = merged.get(event.id);
        if (
          !current ||
          current.at !== event.at ||
          current.reason !== event.reason ||
          current.action !== event.action ||
          current.readinessPct !== event.readinessPct
        ) {
          merged.set(event.id, event);
          changed = true;
        }
      }
      if (!changed) return previous;
      return [...merged.values()].sort(eventOrder).slice(0, MAX_STORED_EVENTS);
    });
  }, [analysis?.events]);

  const rankedCandidates = useMemo(
    () => [...(analysis?.candidates || [])].sort(candidateOrder),
    [analysis?.candidates],
  );
  const visibleCandidates = useMemo(() => {
    if (candidateFilter === "all") return rankedCandidates;
    return rankedCandidates.filter(
      (candidate) =>
        candidate.side === candidateFilter &&
        isFiniteScore(candidate.readinessPct) &&
        candidate.readinessPct >= NEAR_THRESHOLD,
    );
  }, [candidateFilter, rankedCandidates]);
  const sourceEvents = logPaused ? frozenEvents : eventHistory;
  const filteredEvents = useMemo(() => {
    const needle = logSearch.trim().toLowerCase();
    return sourceEvents
      .filter((event) => logSide === "all" || event.side === logSide)
      .filter((event) => logAction === "all" || event.action === logAction)
      .filter(
        (event) =>
          !needle ||
          `${event.symbol} ${event.mint} ${event.action} ${event.reason}`
            .toLowerCase()
            .includes(needle),
      )
      .sort(eventOrder);
  }, [logAction, logSearch, logSide, sourceEvents]);
  const displayedEvents = filteredEvents.slice(0, MAX_DISPLAYED_EVENTS);
  const hasSellCandidates = rankedCandidates.some(
    (candidate) => candidate.side === "sell",
  );
  const analysisState = !status
    ? error
      ? "outage"
      : "loading"
    : !analysis
      ? "unavailable"
      : "available";
  const emptyHeading =
    candidateFilter === "sell" && !hasSellCandidates
      ? "No current sell candidates"
      : candidateFilter === "buy"
        ? "No near-buy candidates"
        : candidateFilter === "sell"
          ? "No near-sell candidates"
          : "No current signal candidates";
  const emptyDescription =
    candidateFilter === "sell" && !hasSellCandidates
      ? "The server reports no current sell candidates. No sell score is being inferred from holdings or prior trades."
      : candidateFilter === "all"
        ? "The server has not reported a current screened candidate. Zero pending watchers does not rule out later analysis."
        : `No current ${candidateFilter} candidates meet the ${NEAR_THRESHOLD}% near threshold.`;

  function toggleLogPause() {
    setLogPaused((paused) => {
      if (!paused) setFrozenEvents(eventHistory);
      else setFrozenEvents([]);
      return !paused;
    });
  }

  return (
    <section
      className="signal-analysis-panel panel"
      aria-label="Live signal analysis"
    >
      <div className="panel-heading signal-analysis-heading">
        <span>
          <Activity size={17} /> Live signal analysis{" "}
          <span className="dim-label">/ GATE REASONING</span>
        </span>
        <span className={`signal-analysis-state ${analysisState}`}>
          <Radio size={11} />
          {analysisState === "available"
            ? "Server telemetry"
            : analysisState === "loading"
              ? "Loading telemetry"
              : "Telemetry unavailable"}
        </span>
      </div>

      {analysisState === "loading" && (
        <div className="signal-analysis-notice loading" role="status">
          <CircleDashed size={18} />
          <div>
            <strong>Loading live signal analysis</strong>
            <p>
              Waiting for the optimizer to report current gate reasoning. No
              score is shown until the server supplies it.
            </p>
          </div>
        </div>
      )}
      {analysisState === "outage" && (
        <div className="signal-analysis-notice outage" role="alert">
          <AlertTriangle size={18} />
          <div>
            <strong>Signal analysis outage</strong>
            <p>{error || "Optimizer telemetry is temporarily unavailable."}</p>
          </div>
        </div>
      )}
      {analysisState === "unavailable" && (
        <div className="signal-analysis-notice unavailable" role="status">
          <AlertTriangle size={18} />
          <div>
            <strong>Signal analysis telemetry unavailable</strong>
            <p>
              This optimizer status does not include the optional gate-readiness
              analysis contract. No client-side scores, reasons, or candidate
              events are created.
            </p>
            {error && (
              <p className="signal-analysis-error">
                Latest telemetry error: {error}
              </p>
            )}
          </div>
        </div>
      )}

      {analysis && (
        <>
          <div className="signal-analysis-disclaimer">
            <ShieldAlert size={16} />
            <div>
              <strong>Readiness is not a probability.</strong>
              <p>{analysis.disclaimer}</p>
              <p>
                Buy readiness and sell proximity are strategy-gate measurements,
                not calibrated probabilities of buying, selling, profit, or
                execution.
              </p>
            </div>
          </div>
          <div className="signal-analysis-meta">
            <span>
              <Radio size={11} /> {analysis.model}
            </span>
            <span>
              <Clock3 size={11} /> Analysis as of {exactTime(analysis.asOf)}
            </span>
            <span>
              <CheckCircle2 size={11} /> 100% is reserved for current
              policy-ready data
            </span>
          </div>
          <div className="signal-candidate-toolbar">
            <div
              className="signal-filter-tabs"
              aria-label="Signal candidate filters"
            >
              <button
                className={candidateFilter === "all" ? "active" : ""}
                onClick={() => setCandidateFilter("all")}
                aria-pressed={candidateFilter === "all"}
              >
                All screened <span>{rankedCandidates.length}</span>
              </button>
              <button
                className={candidateFilter === "buy" ? "active" : ""}
                onClick={() => setCandidateFilter("buy")}
                aria-pressed={candidateFilter === "buy"}
              >
                Near buy <span>≥{NEAR_THRESHOLD}%</span>
              </button>
              <button
                className={candidateFilter === "sell" ? "active" : ""}
                onClick={() => setCandidateFilter("sell")}
                aria-pressed={candidateFilter === "sell"}
              >
                Near sell <span>≥{NEAR_THRESHOLD}%</span>
              </button>
            </div>
            {!hasSellCandidates && (
              <span className="signal-no-sell">
                No current sell candidates reported
              </span>
            )}
          </div>
          {visibleCandidates.length ? (
            <div className="signal-candidate-list" aria-live="polite">
              {visibleCandidates.map((candidate) => (
                <CandidateCard
                  candidate={candidate}
                  inspectable={Boolean(canInspectCandidate?.(candidate))}
                  key={`${candidate.mint}-${candidate.side}`}
                  onSelect={onSelectCandidate}
                />
              ))}
            </div>
          ) : (
            <div className="signal-candidate-empty">
              <CircleDashed size={24} />
              <div>
                <strong>{emptyHeading}</strong>
                <p>{emptyDescription}</p>
              </div>
            </div>
          )}
        </>
      )}

      <div className="signal-reasoning-log">
        <div className="signal-log-heading">
          <div>
            <span className="signal-log-eyebrow">STRUCTURED REASONING LOG</span>
            <h3>Server decision trail</h3>
            <p>
              WAIT, BLOCK, and READY are diagnostic states; BUY, SCALE, and SELL
              are paper executions. SKIP and EXPIRE are watch outcomes, not
              trades.
            </p>
          </div>
          <button
            className={`signal-log-pause ${logPaused ? "paused" : ""}`}
            onClick={toggleLogPause}
            aria-pressed={logPaused}
          >
            {logPaused ? <Play size={13} /> : <Pause size={13} />}
            {logPaused ? "Resume log" : "Pause log"}
          </button>
        </div>
        <div className="signal-log-controls">
          <label className="signal-log-search">
            <Search size={14} />
            <input
              value={logSearch}
              onChange={(event) => setLogSearch(event.target.value)}
              placeholder="Search token or reason"
              aria-label="Search reasoning log"
            />
          </label>
          <label>
            <span className="sr-only">Filter reasoning log side</span>
            <select
              value={logSide}
              onChange={(event) =>
                setLogSide(event.target.value as LogSideFilter)
              }
              aria-label="Filter reasoning log side"
            >
              <option value="all">All sides</option>
              <option value="buy">Buy side</option>
              <option value="sell">Sell side</option>
            </select>
          </label>
          <label>
            <span className="sr-only">Filter reasoning log action</span>
            <select
              value={logAction}
              onChange={(event) =>
                setLogAction(event.target.value as LogActionFilter)
              }
              aria-label="Filter reasoning log action"
            >
              <option value="all">All actions</option>
              {EVENT_ACTIONS.map((action) => (
                <option key={action} value={action}>
                  {action}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="signal-log-list" aria-live="polite">
          {displayedEvents.length ? (
            displayedEvents.map((event) => {
              const execution = isPaperExecution(event.action);
              const watchOutcome =
                event.action === "SKIP" || event.action === "EXPIRE";
              return (
                <div
                  className={`signal-log-row ${execution ? "execution" : "reasoning"} ${event.action.toLowerCase()}`}
                  key={event.id}
                >
                  <span className="signal-log-time">{exactTime(event.at)}</span>
                  <span className="signal-log-action">{event.action}</span>
                  <div className="signal-log-copy">
                    <strong>{event.symbol}</strong>
                    <span>{event.reason}</span>
                  </div>
                  <span
                    className={`signal-log-classification ${execution ? "execution" : "reasoning"}`}
                  >
                    {execution
                      ? "Paper execution"
                      : watchOutcome
                        ? "Watch outcome"
                        : "Reasoning state"}
                  </span>
                  <span className="signal-log-score">
                    {displayScore(event.readinessPct)}
                  </span>
                </div>
              );
            })
          ) : (
            <div className="signal-log-empty">
              {logPaused
                ? "Log display paused before any matching events were captured."
                : analysis
                  ? "No server reasoning events match these filters."
                  : "No reasoning log has been supplied by the server."}
            </div>
          )}
        </div>
        <div className="signal-log-foot">
          <span>
            {logPaused
              ? "Display paused; live candidate telemetry continues."
              : "Live display; candidate telemetry continues."}
          </span>
          <span>
            Showing {displayedEvents.length} of {filteredEvents.length} matching
            events · latest {MAX_DISPLAYED_EVENTS} displayed
          </span>
        </div>
      </div>
    </section>
  );
}
