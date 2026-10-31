import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Zap,
  LayoutDashboard,
  ArrowUpRight,
  ArrowDownRight,
  ChartNoAxesCombined,
  SlidersHorizontal,
  RefreshCw,
  Search,
  ChevronDown,
  ChevronRight,
  Activity,
  ShieldCheck,
  Radio,
  Layers,
  ScanLine,
  CircleCheck,
  CircleDashed,
  Pause,
  Play,
  X,
  ExternalLink,
  Copy,
  Check,
  Download,
  Clock3,
  ArrowRight,
  Cpu,
  Signal,
  FlaskConical,
  Wallet,
  Settings2,
  BookOpen,
  AlertTriangle,
  Eye,
  EyeOff,
  Terminal,
  CheckCheck,
  Filter,
  Power,
  MoreHorizontal,
} from "lucide-react";
import TradeChart from "./components/TradeChart";
import WatchControls from "./components/WatchControls";
import OptimizerPanel from "./components/OptimizerPanel";
import { useOptimizerStatus } from "./lib/optimizer";
import TokenWatchPeriods from "./components/TokenWatchPeriods";
import useStrategyConfig from "./lib/useStrategyConfig";
import {
  allTokens,
  avatarColor,
  chartMetric,
  chartNumber,
  compact,
  identifier,
  loadSnapshot,
  money,
  pct,
  shortSymbol,
  signed,
  tokenForTrade,
  type Lane,
  type Snapshot,
  type Token,
  type Trade,
  type WatchPeriods,
  type StrategyConfig,
} from "./lib/data";

type View = "terminal" | "journal" | "strategy";
const NAV = [
  { id: "terminal" as View, label: "Terminal", icon: LayoutDashboard },
  { id: "journal" as View, label: "Journal", icon: ChartNoAxesCombined },
  { id: "strategy" as View, label: "Strategy", icon: SlidersHorizontal },
];
const LANE_META = {
  checking: {
    title: "Checking",
    label: "Signals under review",
    icon: ScanLine,
    color: "amber",
  },
  holding: {
    title: "Holding",
    label: "Active paper positions",
    icon: Layers,
    color: "lime",
  },
  done: {
    title: "Done",
    label: "Closed & filtered signals",
    icon: CircleCheck,
    color: "slate",
  },
};
function Avatar({ token, small = false }: { token: Token; small?: boolean }) {
  const name = shortSymbol(token.symbol).replace(/^\$/, "");
  return (
    <span
      className={`token-avatar ${avatarColor(token.symbol)} ${small ? "small" : ""}`}
    >
      {name.slice(0, 2).toUpperCase()}
    </span>
  );
}
function Modal({
  children,
  onClose,
  title,
  wide = false,
}: {
  children: ReactNode;
  onClose: () => void;
  title: string;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const keyHandler = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeRef.current();
      if (e.key === "Tab") {
        const controls = ref.current?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), a[href], input, select, [tabindex="0"]',
        );
        if (!controls?.length) return;
        const first = controls[0],
          last = controls[controls.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        }
        if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", keyHandler);
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", keyHandler);
      document.body.style.overflow = overflow;
      previous?.focus();
    };
  }, []);
  return (
    <div
      className="modal-overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={ref}
        className={`modal ${wide ? "wide" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="modal-heading">
          <div>
            <span className="eyebrow">PUMPMI TERMINAL</span>
            <h2>{title}</h2>
          </div>
          <button
            className="icon-button"
            onClick={onClose}
            aria-label="Close dialog"
          >
            <X size={20} />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}
function EnergyPulse({ snapshot }: { snapshot: Snapshot }) {
  const checking = snapshot.board.columns.checking.length;
  return (
    <section className="engine-pulse panel">
      <div className="panel-heading">
        <span>
          <Cpu size={16} /> Engine pulse
        </span>
        <span className="tiny-tag">v7</span>
      </div>
      <div className="pulse-visual" aria-hidden="true">
        <div className="pulse-grid" />
        <div className="orbit orbit-outer" />
        <div className="orbit orbit-middle" />
        <div className="orbit orbit-inner" />
        <div className="pulse-core">
          <Zap size={34} strokeWidth={1.5} />
        </div>
        <span className="orbit-node node-one" />
        <span className="orbit-node node-two" />
        <span className="orbit-node node-three" />
        <div className="pulse-label label-left">SIGNAL IN</div>
        <div className="pulse-label label-right">RISK OUT</div>
      </div>
      <div className="pulse-status">
        <span className="status-dot" />
        <strong>
          {snapshot.meta.kind === "fixture"
            ? "Fixture run complete"
            : snapshot.meta.kind === "live"
              ? "Forward paper data"
              : snapshot.meta.kind === "awaiting"
                ? "Connecting to live market"
                : snapshot.meta.kind === "stale"
                  ? "Live source interrupted"
                  : "Snapshot loaded"}
        </strong>
        <span>Paper mode</span>
      </div>
      <div className="engine-steps">
        <div>
          <ScanLine size={14} />
          <span>Scan</span>
          <b>
            {compact(
              snapshot.board.stats?.scanned ?? allTokens(snapshot).length,
            )}
          </b>
        </div>
        <ArrowRight size={12} />
        <div>
          <ShieldCheck size={14} />
          <span>Check</span>
          <b>{checking}</b>
        </div>
        <ArrowRight size={12} />
        <div>
          <Layers size={14} />
          <span>Hold</span>
          <b>{snapshot.board.columns.holding.length}</b>
        </div>
      </div>
      <div className="pulse-foot">
        <ShieldCheck size={13} /> No keys. No signing. No live orders.
      </div>
    </section>
  );
}
function TokenCard({
  token,
  trade,
  onSelect,
  snapshotTs,
  periods,
}: {
  token: Token;
  trade?: Trade;
  onSelect: () => void;
  snapshotTs: number;
  periods: WatchPeriods;
}) {
  const gain = trade?.exitPp ?? token.pnl_pct;
  const positive = gain !== undefined && gain >= 0;
  const tag =
    token.column === "checking"
      ? "WATCHING"
      : token.column === "holding"
        ? "PAPER BUY"
        : token.verdict === "EXIT"
          ? "CLOSED"
          : "SKIPPED";
  const age = token.ts_seen
    ? Math.max(0, Math.round((snapshotTs - token.ts_seen) / 60))
    : null;
  return (
    <button className={`token-card ${token.column}`} onClick={onSelect}>
      <div className="token-card-heading">
        <Avatar token={token} />
        <div className="token-name">
          <strong>{shortSymbol(token.symbol)}</strong>
          <span>
            {token.name?.trim() ||
              `Mint ${identifier(token.symbol) || "unavailable"}`}
          </span>
        </div>
        <span
          className={`token-tag ${token.column === "checking" ? "amber-text" : token.column === "holding" ? "lime-text" : ""}`}
        >
          {tag}
        </span>
      </div>
      <div className="token-market">
        <span>
          Market cap
          <strong>
            {token.mcap != null ? `$${compact(token.mcap)}` : "—"}
          </strong>
        </span>
        <span>
          {gain !== undefined
            ? token.verdict === "SKIP"
              ? "Historical return"
              : "Total paper return"
            : "Bonding curve"}
          <strong
            className={
              gain !== undefined ? (positive ? "lime-text" : "red-text") : ""
            }
          >
            {gain !== undefined
              ? signed(gain, "%")
              : token.curve_pct != null
                ? `${token.curve_pct.toFixed(1)}%`
                : "—"}
          </strong>
        </span>
        {trade && (
          <svg
            className={`mini-spark ${positive ? "up" : "down"}`}
            viewBox="0 0 70 28"
            aria-hidden="true"
          >
            <polyline
              points={trade.series
                .map((p, i) => {
                  const vals = trade.series.map((v) => v.value);
                  const min = Math.min(...vals),
                    max = Math.max(...vals);
                  return `${(i / Math.max(vals.length - 1, 1)) * 68 + 1},${25 - ((p.value - min) / Math.max(max - min, 1)) * 22}`;
                })
                .join(" ")}
            />
          </svg>
        )}
      </div>
      {token.curve_pct != null && (
        <div className="curve-bar">
          <span
            style={{ width: `${Math.min(100, Math.max(0, token.curve_pct))}%` }}
          />
        </div>
      )}
      <TokenWatchPeriods
        token={token}
        periods={periods}
        snapshotTs={snapshotTs}
      />
      <p className="token-reason">
        {token.reason || "Paper engine evaluation recorded."}
      </p>
      <div className="token-card-foot">
        <span>
          {token.column === "checking" ? (
            <Radio size={11} />
          ) : (
            <Clock3 size={11} />
          )}
          {age === null ? "Recorded decision" : `${age}m since first seen`}
        </span>
        <span>
          Inspect <ArrowUpRight size={12} />
        </span>
      </div>
    </button>
  );
}
function Board({
  snapshot,
  query,
  onSelect,
  periods,
}: {
  snapshot: Snapshot;
  query: string;
  onSelect: (t: Token) => void;
  periods: WatchPeriods;
}) {
  const [expanded, setExpanded] = useState(false);
  const [closedOnly, setClosedOnly] = useState(true);
  return (
    <div className="workflow-board">
      {(["checking", "holding", "done"] as Lane[]).map((lane) => {
        const meta = LANE_META[lane],
          Icon = meta.icon;
        const raw = snapshot.board.columns[lane];
        const filtered = raw
          .filter((t) =>
            `${t.symbol} ${t.name || ""} ${t.reason || ""}`
              .toLowerCase()
              .includes(query.toLowerCase()),
          )
          .filter(
            (t) => lane !== "done" || !closedOnly || t.verdict === "EXIT",
          );
        const rows =
          lane === "done"
            ? [...filtered].sort(
                (a, b) =>
                  (snapshot.trades[b.symbol]?.exitPnl ?? b.pnl ?? -100000) -
                  (snapshot.trades[a.symbol]?.exitPnl ?? a.pnl ?? -100000),
              )
            : filtered;
        return (
          <section className={`board-lane ${lane}`} key={lane}>
            <header className="lane-heading">
              <div>
                <span className={`lane-icon ${meta.color}`}>
                  <Icon size={16} />
                </span>
                <h3>{meta.title}</h3>
                <span className="count-badge">{raw.length}</span>
              </div>
              {lane === "done" ? (
                <button
                  className={`icon-button ${closedOnly ? "active" : ""}`}
                  onClick={() => setClosedOnly((v) => !v)}
                  aria-label={
                    closedOnly
                      ? "Show all done signals"
                      : "Show closed paper trades only"
                  }
                  title={
                    closedOnly
                      ? "Showing trades · click to include skipped signals"
                      : "Showing all signals"
                  }
                >
                  <Filter size={14} />
                </button>
              ) : (
                <span className="lane-flow-dot" />
              )}
            </header>
            <p className="lane-description">
              {meta.label}
              {lane === "done" && closedOnly ? " · trades only" : ""}
            </p>
            <div className="lane-content">
              {rows.length ? (
                rows
                  .slice(0, lane === "done" && !expanded ? 3 : 1000)
                  .map((t) => (
                    <TokenCard
                      key={t.symbol}
                      token={t}
                      trade={snapshot.trades[t.symbol]}
                      onSelect={() => onSelect(t)}
                      snapshotTs={snapshot.board.ts ?? 0}
                      periods={periods}
                    />
                  ))
              ) : (
                <div className="lane-empty">
                  <span className="empty-icon">
                    <Icon size={26} strokeWidth={1.2} />
                  </span>
                  <strong>
                    {query
                      ? "No matching signals"
                      : lane === "holding"
                        ? "Waiting for the right signal"
                        : "Nothing in this lane yet"}
                  </strong>
                  <p>
                    {query
                      ? "Try another token name or symbol."
                      : lane === "holding"
                        ? "Positions appear here when the paper engine passes its entry gates."
                        : "New engine decisions will appear here."}
                  </p>
                  {lane === "holding" && !query && (
                    <span className="empty-safe">
                      <ShieldCheck size={12} /> Patience is part of the strategy
                    </span>
                  )}
                </div>
              )}
              {lane === "checking" && rows.length > 0 && (
                <div className="scan-placeholder">
                  <div className="scanner-line" />
                  <ScanLine size={20} />
                  <span>Listening for the next signal</span>
                  <small>
                    {snapshot.meta.kind === "live"
                      ? "Forward paper data · UI monitoring"
                      : "Snapshot monitoring · no live feed running"}
                  </small>
                </div>
              )}
            </div>
            {lane === "done" && rows.length > 3 && (
              <button
                className="lane-expand"
                onClick={() => setExpanded((v) => !v)}
              >
                {expanded
                  ? "Show less"
                  : `View all ${rows.length} ${closedOnly ? "paper trades" : "signals"}`}
                <ChevronDown size={13} className={expanded ? "rotate" : ""} />
              </button>
            )}
          </section>
        );
      })}
    </div>
  );
}
function Detail({
  token,
  snapshot,
  onClose,
  periods,
}: {
  token: Token;
  snapshot: Snapshot;
  onClose: () => void;
  periods: WatchPeriods;
}) {
  const [copied, setCopied] = useState(false);
  const trade = snapshot.trades[token.symbol];
  const pnl = trade?.exitPnl ?? token.pnl;
  async function copyMint() {
    try {
      await navigator.clipboard.writeText(token.mint || token.symbol);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }
  return (
    <Modal title="Signal inspection" onClose={onClose} wide>
      <div className="detail-identity">
        <Avatar token={token} />
        <div>
          <h3>
            {shortSymbol(token.symbol)} <span>{token.name}</span>
          </h3>
          <button className="mint-copy" onClick={copyMint}>
            {token.mint
              ? `${token.mint.slice(0, 12)}…${token.mint.slice(-6)}`
              : token.symbol}
            {copied ? <Check size={12} /> : <Copy size={12} />}
          </button>
        </div>
        <span className="paper-badge">
          <FlaskConical size={13} /> PAPER ONLY
        </span>
      </div>
      <div className="detail-metrics">
        <div>
          <span>Market cap</span>
          <strong>{money(token.mcap, 0)}</strong>
        </div>
        <div>
          <span>Bonding curve</span>
          <strong>{pct(token.curve_pct)}</strong>
        </div>
        <div>
          <span>{trade ? "Total trade P&L" : "Board paper P&L"}</span>
          <strong className={pnl != null && pnl < 0 ? "red-text" : "lime-text"}>
            {pnl == null ? "—" : money(pnl)}
          </strong>
        </div>
        <div>
          <span>Workflow</span>
          <strong className="capitalize">{token.column}</strong>
        </div>
      </div>
      <div className="detail-watch">
        <div className="section-label">
          THREE-TIER WATCH PERIODS{" "}
          <span>Recorded checkpoints · not live timers</span>
        </div>
        <TokenWatchPeriods
          token={token}
          periods={periods}
          snapshotTs={snapshot.board.ts ?? 0}
          detailed
        />
      </div>
      <div className="detail-chart">
        <div className="section-label">
          RECORDED TRADE PATH{" "}
          <span>
            {trade
              ? `${trade.dur} min · ${trade.metric === "unit_price" ? "fixture execution prices" : snapshot.meta.kind === "live" ? "sampled market observations" : "interpolated ledger data"}`
              : "No completed trade chart"}
          </span>
        </div>
        <TradeChart trade={trade} symbol={shortSymbol(token.symbol)} />
      </div>
      <div className="detail-reason">
        <ShieldCheck size={17} />
        <div>
          <strong>Latest board decision · {token.verdict || "WATCH"}</strong>
          <p>{token.reason || "No decision reason supplied."}</p>
          {trade && (
            <p className="small-note">
              Total trade P&L includes all ledger exit legs. The board reason
              describes its latest decision, which may be a final leg or a later
              re-entry skip.
            </p>
          )}
        </div>
      </div>
      <div className="detail-bottom">
        <section>
          <h4>Holder concentration</h4>
          {[
            ["Developer", token.dev_pct],
            ["Top 10 holders", token.top10_pct],
            ["Bundle / cluster", token.bundle_pct],
          ].map(([label, value]) => (
            <div className="data-row" key={String(label)}>
              <span>{label}</span>
              <b className={value == null ? "muted" : ""}>
                {pct(value as number | undefined)}
              </b>
            </div>
          ))}
          <p className="small-note">Unknown values are not treated as zero.</p>
        </section>
        <section>
          <h4>{trade ? "Execution events" : "Signal metadata"}</h4>
          {trade ? (
            trade.signals.map((s) => (
              <div className="execution-event" key={s.id}>
                <span className={`event-dot ${s.kind}`} />
                <div>
                  <strong>
                    {s.label} <small>{s.t}</small>
                  </strong>
                  <span>
                    {money(s.usd)} at {chartNumber(s.mcap, trade)}{" "}
                    {chartMetric(trade).toLowerCase()}
                  </span>
                </div>
              </div>
            ))
          ) : (
            <>
              <div className="data-row">
                <span>Replies</span>
                <b>{token.replies ?? "—"}</b>
              </div>
              <div className="data-row">
                <span>Sentiment</span>
                <b>
                  {token.sentiment_0_100 != null
                    ? `${token.sentiment_0_100}/100`
                    : "Unknown"}
                </b>
              </div>
            </>
          )}
        </section>
      </div>
      <div className="modal-foot">
        <ShieldCheck size={14} />
        <span>
          Read-only inspection. No wallet connection or real-money execution.
        </span>
        {token.mint && (
          <a
            href={`https://pump.fun/coin/${encodeURIComponent(token.mint)}`}
            target="_blank"
            rel="noreferrer"
          >
            View token <ExternalLink size={13} />
          </a>
        )}
      </div>
    </Modal>
  );
}
function Journal({
  snapshot,
  onSelect,
  query,
}: {
  snapshot: Snapshot;
  onSelect: (t: Token) => void;
  query: string;
}) {
  const [filter, setFilter] = useState("all");
  const trades = Object.entries(snapshot.trades)
    .filter(
      ([symbol, t]) =>
        symbol.toLowerCase().includes(query.toLowerCase()) &&
        (filter === "all" ||
          (filter === "wins" ? t.exitPnl > 0 : t.exitPnl < 0)),
    )
    .sort((a, b) => (b[1]._exitTs || 0) - (a[1]._exitTs || 0));
  function download() {
    const csv = [
      "Symbol,Entry,Paper PnL,Return %,Duration min,Entry unit",
      ...trades.map(
        ([s, t]) =>
          `"${s.replaceAll('"', '""')}",${t.entry},${t.exitPnl},${t.exitPp},${t.dur},${chartMetric(t)}`,
      ),
    ].join("\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    a.download = "pumpmi-paper-journal.csv";
    a.click();
    URL.revokeObjectURL(a.href);
  }
  return (
    <section className="journal panel">
      <div className="panel-heading">
        <span>
          <BookOpen size={16} /> Paper trade journal{" "}
          <span className="count-badge">{trades.length}</span>
        </span>
        <div className="journal-actions">
          <select
            aria-label="Filter trade outcome"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          >
            <option value="all">All outcomes</option>
            <option value="wins">Winners</option>
            <option value="losses">Losses</option>
          </select>
          <button className="button secondary small-button" onClick={download}>
            <Download size={14} /> Export CSV
          </button>
        </div>
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Asset</th>
              <th>Entry value</th>
              <th>Paper P&L</th>
              <th>Return</th>
              <th>Duration</th>
              <th>Outcome</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {trades.map(([symbol, t]) => {
              const token = tokenForTrade(symbol, snapshot);
              return (
                <tr key={symbol}>
                  <td>
                    <button
                      className="journal-asset"
                      onClick={() => onSelect(token)}
                    >
                      <Avatar token={token} small />
                      <span>
                        <strong>{shortSymbol(symbol)}</strong>
                        <small>{identifier(symbol)}</small>
                      </span>
                    </button>
                  </td>
                  <td>
                    {chartNumber(t.entry, t)}
                    <small className="entry-unit">{chartMetric(t)}</small>
                  </td>
                  <td className={t.exitPnl >= 0 ? "lime-text" : "red-text"}>
                    {money(t.exitPnl)}
                  </td>
                  <td className={t.exitPp >= 0 ? "lime-text" : "red-text"}>
                    {signed(t.exitPp, "%")}
                  </td>
                  <td>{t.dur} min</td>
                  <td>
                    <span
                      className={`outcome-pill ${t.exitPnl >= 0 ? "win" : "loss"}`}
                    >
                      {t.exitPnl > 0
                        ? "Profit"
                        : t.exitPnl < 0
                          ? "Loss"
                          : "Flat"}
                    </span>
                  </td>
                  <td>
                    <button
                      className="icon-button"
                      aria-label={`Inspect ${shortSymbol(symbol)}`}
                      onClick={() => onSelect(token)}
                    >
                      <ArrowUpRight size={16} />
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {!trades.length && (
          <div className="table-empty">
            No recorded trades match this filter.
          </div>
        )}
      </div>
      <div className="journal-foot">
        <ShieldCheck size={13} /> Historical paper executions only · recorded
        market-cap paths or exact fixture execution prices, not full candles.
      </div>
    </section>
  );
}
function Strategy({ config }: { config: StrategyConfig }) {
  const groups = [
    {
      title: "01 / Entry signal",
      icon: ScanLine,
      description: "A rising curve. Not just a rising price.",
      rows: [
        ["Bonding curve band", "min_curve_pct", "max_curve_pct", "%"],
        ["Short watch", "watch_short_s", null, " sec"],
        ["Medium watch", "watch_medium_s", null, " sec"],
        ["Long watch", "watch_long_s", null, " sec"],
        ["Market cap ceiling", "max_mcap_usd", null, " USD"],
      ],
    },
    {
      title: "02 / Risk firewall",
      icon: ShieldCheck,
      description: "Concentration gates before paper allocation.",
      rows: [
        ["Top 10 holder maximum", "p0_top10_max", null, "%"],
        ["Developer maximum", "p0_dev_max", null, "%"],
        ["Bundle maximum", "p0_bundle_max", null, "%"],
      ],
    },
    {
      title: "03 / Position discipline",
      icon: Layers,
      description: "Exit rules are part of the entry.",
      rows: [
        ["Maximum open positions", "max_open", null, ""],
        ["Hard stop loss", "stop_loss_pct", null, "%"],
        ["Scale-out target", "scale_out_pct", null, "%"],
        ["Trailing giveback", "trail_giveback_pct", null, "%"],
      ],
    },
  ];
  const data: Record<string, number> = {
    ...config.strategy,
    watch_short_s: config.watchPeriods.short,
    watch_medium_s: config.watchPeriods.medium,
    watch_long_s: config.watchPeriods.long,
  };
  return (
    <>
      <div className="strategy-intro">
        <span className="strategy-mark">
          <ShieldCheck size={25} />
        </span>
        <div>
          <h2>Signal first. Risk always.</h2>
          <p>
            {config.editable
              ? "Watch durations are editable above. Other risk controls remain read-only."
              : "Default configuration. Connect the paper adapter to save watch durations."}{" "}
            Fixture simulations use the separate fixture engine configuration.
          </p>
        </div>
        <span className="tiny-tag">PAPER CONFIG</span>
      </div>
      <div className="strategy-cards">
        {groups.map((g) => {
          const Icon = g.icon;
          return (
            <section className="strategy-card panel" key={g.title}>
              <Icon size={22} />
              <h3>{g.title}</h3>
              <p>{g.description}</p>
              {g.rows.map(([name, key, maxKey, unit]) => (
                <div className="data-row" key={name}>
                  <span>{name}</span>
                  <b>
                    {data[key!] == null ? "—" : compact(data[key!])}
                    {maxKey ? `–${data[maxKey]}` : ""}
                    {unit}
                  </b>
                </div>
              ))}
            </section>
          );
        })}
      </div>
      <div className="strategy-notice">
        <AlertTriangle size={19} />
        <div>
          <strong>
            Paper trading is a testing environment, not a prediction.
          </strong>
          <p>
            Sentiment can be manipulated. Missing holder data is logged by the
            engine but does not automatically block a signal. No strategy
            guarantees profit.
          </p>
        </div>
      </div>
    </>
  );
}
export default function App() {
  const watchSettings = useStrategyConfig();
  const optimizer = useOptimizerStatus();
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [simulating, setSimulating] = useState(false);
  const [view, setView] = useState<View>("terminal");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Token | null>(null);
  const [chartSymbol, setChartSymbol] = useState("CHILLGIVIN·5M35");
  const [paused, setPaused] = useState(false);
  const [settings, setSettings] = useState(false);
  const [help, setHelp] = useState(false);
  const [showSignals, setShowSignals] = useState(true);
  const [motion, setMotion] = useState(() => {
    try {
      return localStorage.getItem("pumpmi-motion") !== "off";
    } catch {
      return true;
    }
  });
  const [toast, setToast] = useState("");
  const toastTimer = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined,
  );
  const queryRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (snapshot?.strategyConfig)
      watchSettings.adoptConfig(snapshot.strategyConfig);
  }, [snapshot?.strategyConfig?.revision]);
  function notify(message: string) {
    setToast(message);
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(""), 4000);
  }
  async function refresh(showFeedback = true) {
    setBusy(true);
    setError("");
    try {
      const s = await loadSnapshot();
      setSnapshot(s);
      setChartSymbol((current) =>
        s.trades[current] ? current : Object.keys(s.trades)[0] || "",
      );
      if (showFeedback)
        notify(
          s.meta.kind === "live"
            ? "Forward paper market data refreshed."
            : s.meta.kind === "awaiting"
              ? "Waiting for verified live-market prices."
              : s.meta.kind === "stale"
                ? "Live market data is stale or interrupted."
                : "Recorded engine snapshot refreshed.",
        );
    } catch (e) {
      setError(
        e instanceof Error ? e.message : "Unable to load the engine snapshot.",
      );
    } finally {
      setBusy(false);
    }
  }
  useEffect(() => {
    void refresh(false);
    return () => clearTimeout(toastTimer.current);
  }, []);
  useEffect(() => {
    if (paused || snapshot?.meta.kind === "fixture") return;
    let active = true;
    const interval = setInterval(() => {
      loadSnapshot()
        .then((next) => {
          if (!active) return;
          setSnapshot(next);
          setChartSymbol((current) =>
            next.trades[current] ? current : Object.keys(next.trades)[0] || "",
          );
        })
        .catch(() => {
          if (active)
            setError("Snapshot monitoring interrupted. Refresh to reconnect.");
        });
    }, 15000);
    return () => {
      active = false;
      clearInterval(interval);
    };
  }, [paused, snapshot?.meta.kind]);
  useEffect(() => {
    try {
      localStorage.setItem("pumpmi-motion", motion ? "on" : "off");
    } catch {
      /* Preferences remain usable when storage is blocked. */
    }
  }, [motion]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        queryRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  async function runSimulation() {
    setSimulating(true);
    setError("");
    try {
      const r = await fetch("/api/simulate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || "Paper simulation failed.");
      setSnapshot(data);
      setChartSymbol(Object.keys(data.trades)[0] || "");
      setSelected(null);
      notify(
        "Fixture simulation complete. Original market snapshot preserved.",
      );
    } catch (e) {
      setError(
        e instanceof Error
          ? e.message
          : "Paper simulation unavailable. Check the engine adapter.",
      );
    } finally {
      setSimulating(false);
    }
  }
  function inspect(token: Token) {
    setSelected(token);
    setChartSymbol(token.symbol);
  }
  const board = snapshot?.board;
  const trade = snapshot?.trades[chartSymbol];
  const chartToken = useMemo(
    () => (snapshot ? tokenForTrade(chartSymbol, snapshot) : null),
    [snapshot, chartSymbol],
  );
  const stats = board?.stats;
  const returnPct = board ? (board.equity / board.bankroll - 1) * 100 : 0;
  const winRate =
    stats && stats.wins + stats.losses > 0
      ? (stats.wins / (stats.wins + stats.losses)) * 100
      : null;
  const viewTitles = {
    terminal: "Command center",
    journal: "Trade journal",
    strategy: "Strategy console",
  };
  return (
    <div className={`app ${motion ? "" : "motion-off"}`}>
      <aside className="side-rail">
        <button
          className="brand-mark"
          aria-label="Pumpmi command center"
          onClick={() => setView("terminal")}
        >
          <Zap size={26} fill="currentColor" strokeWidth={1.5} />
        </button>
        <div className="rail-divider" />
        <nav>
          {NAV.map((n) => {
            const Icon = n.icon;
            return (
              <button
                key={n.id}
                className={`rail-link ${view === n.id ? "selected" : ""}`}
                onClick={() => {
                  setView(n.id);
                  setQuery("");
                }}
                title={n.label}
              >
                <Icon size={21} />
                <span>{n.label}</span>
              </button>
            );
          })}
        </nav>
        <div className="rail-bottom">
          <button
            className="rail-link"
            onClick={() => setHelp(true)}
            title="Terminal guide"
          >
            <BookOpen size={20} />
            <span>Guide</span>
          </button>
          <button
            className="rail-link"
            onClick={() => setSettings(true)}
            title="Interface settings"
          >
            <Settings2 size={20} />
            <span>Settings</span>
          </button>
          <div className="operator-avatar" title="Paper operator">
            P
          </div>
        </div>
      </aside>
      <div className="app-body">
        <header className="topbar">
          <button className="wordmark" onClick={() => setView("terminal")}>
            pumpmi<span className="wordmark-dot">.</span>
            <span className="wordmark-sub">TRADING TERMINAL</span>
          </button>
          <div className="top-tabs">
            {NAV.map((n) => (
              <button
                className={view === n.id ? "active" : ""}
                key={n.id}
                onClick={() => {
                  setView(n.id);
                  setQuery("");
                }}
              >
                {n.id === "terminal"
                  ? "Command center"
                  : n.id === "journal"
                    ? "Trade journal"
                    : "Strategy"}
                {view === n.id && <span />}
              </button>
            ))}
          </div>
          <div className="topbar-status">
            <span className="network-label">
              <span className="status-dot" /> SOLANA
            </span>
            <span className="paper-badge">
              <FlaskConical size={12} /> PAPER MODE
            </span>
            <button
              className="icon-button"
              onClick={() => setSettings(true)}
              aria-label="Interface settings"
            >
              <Settings2 size={17} />
            </button>
          </div>
        </header>
        <main>
          <div className="page-heading">
            <div>
              <div className="eyebrow">
                <span className="status-dot" /> PUMP.FUN INTELLIGENCE{" "}
                <span className="eyebrow-line" />
              </div>
              <h1>
                {viewTitles[view]}
                <span className="heading-slash">/</span>
              </h1>
              <p>
                {view === "terminal"
                  ? "Track the signal. Control the risk. Stay ahead of the curve."
                  : view === "journal"
                    ? "Every decision leaves a trace. Inspect your paper executions."
                    : "The rules behind the flow. Transparent by design."}
              </p>
            </div>
            <div className="page-actions">
              <button
                className="button secondary"
                onClick={() => void refresh()}
                disabled={busy || simulating}
              >
                <RefreshCw size={15} className={busy ? "spin" : ""} /> Refresh
              </button>
              <button
                className="button primary"
                onClick={() => void runSimulation()}
                disabled={
                  !snapshot?.meta.simulationAvailable || simulating || busy
                }
                title={
                  !snapshot?.meta.simulationAvailable
                    ? "Connect the Python paper-engine adapter to enable simulations"
                    : "Run a finite, offline fixture simulation without changing the recorded dataset"
                }
              >
                <Zap
                  size={15}
                  fill="currentColor"
                  className={simulating ? "spin" : ""}
                />
                {simulating ? "Running paper engine…" : "Run paper simulation"}
                <ArrowUpRight size={14} />
              </button>
            </div>
          </div>
          {error && (
            <div className="error-banner" role="alert">
              <AlertTriangle size={17} />
              <span>{error}</span>
              <button onClick={() => void refresh()}>Retry</button>
              <button aria-label="Dismiss error" onClick={() => setError("")}>
                <X size={14} />
              </button>
            </div>
          )}
          {!snapshot ? (
            <div className="loading-panel">
              <span className="loading-bolt">
                <Zap size={35} />
              </span>
              <h2>
                {error
                  ? "Engine snapshot unavailable"
                  : "Powering up the terminal"}
              </h2>
              <p>
                {error
                  ? "Refresh to reconnect to the paper engine."
                  : "Reading the board and recorded trade paths…"}
              </p>
            </div>
          ) : (
            <>
              <section className="metrics-strip">
                <div className="metric equity-metric">
                  <div className="metric-label">
                    <Wallet size={14} /> Paper equity{" "}
                    <span className="metric-live">USD</span>
                  </div>
                  <div className="metric-value">
                    {money(board!.equity)}
                    <span
                      className={`change-chip ${returnPct >= 0 ? "positive" : "negative"}`}
                    >
                      {returnPct >= 0 ? (
                        <ArrowUpRight size={12} />
                      ) : (
                        <ArrowDownRight size={12} />
                      )}
                      {signed(returnPct, "%")}
                    </span>
                  </div>
                  <div className="metric-caption">
                    From {money(board!.bankroll, 0)} initial bankroll{" "}
                    <span className="equity-circuit" />
                  </div>
                </div>
                <div className="metric">
                  <div className="metric-label">
                    <ScanLine size={14} /> Signals scanned
                  </div>
                  <div className="metric-value">
                    {(
                      stats?.scanned ?? allTokens(snapshot).length
                    ).toLocaleString()}
                    <span className="metric-unit">signals</span>
                  </div>
                  <div className="metric-caption">
                    <span className="lime-text">{stats?.ticks ?? "—"}</span>{" "}
                    engine ticks recorded{" "}
                    <Signal size={14} className="metric-corner" />
                  </div>
                </div>
                <div className="metric">
                  <div className="metric-label">
                    <Layers size={14} /> Open positions
                  </div>
                  <div className="metric-value">
                    {board!.columns.holding.length}
                    <span className="metric-unit">
                      / {watchSettings.config.strategy.max_open || 6} slots
                    </span>
                  </div>
                  <div className="metric-caption">
                    <span className="status-dot muted-dot" /> Paper allocation
                    only{" "}
                    <div className="slot-indicators">
                      {Array.from(
                        { length: watchSettings.config.strategy.max_open || 6 },
                        (_, i) => i,
                      ).map((i) => (
                        <span
                          key={i}
                          className={
                            i < board!.columns.holding.length ? "occupied" : ""
                          }
                        />
                      ))}
                    </div>
                  </div>
                </div>
                <div className="metric">
                  <div className="metric-label">
                    <ChartNoAxesCombined size={14} /> Recorded win rate
                  </div>
                  <div className="metric-value">
                    {winRate === null ? "—" : `${winRate.toFixed(1)}%`}
                    <span className="metric-unit">closed trades</span>
                  </div>
                  <div className="metric-caption">
                    <span className="lime-text">{stats?.wins ?? "—"} wins</span>
                    <span className="caption-dot">·</span>
                    <span>{stats?.losses ?? "—"} losses</span>
                    <div className="win-meter">
                      <span style={{ width: `${winRate || 0}%` }} />
                    </div>
                  </div>
                </div>
              </section>
              <div
                className={`snapshot-bar ${snapshot.meta.kind === "fixture" ? "fixture-mode" : ""}`}
              >
                <div>
                  <span className="snapshot-icon">
                    {snapshot.meta.kind === "fixture" ? (
                      <FlaskConical size={14} />
                    ) : (
                      <Radio size={14} />
                    )}
                  </span>
                  <strong>
                    {snapshot.meta.kind === "fixture"
                      ? "FIXTURE SIMULATION"
                      : snapshot.meta.kind === "live"
                        ? "FORWARD PAPER DATA"
                        : snapshot.meta.kind === "awaiting"
                          ? "CONNECTING LIVE MARKET"
                          : snapshot.meta.kind === "stale"
                            ? "LIVE FEED INTERRUPTED"
                            : "RECORDED SNAPSHOT"}
                  </strong>
                  <span className="snapshot-divider" />
                  <span>
                    {snapshot.meta.kind === "fixture"
                      ? "Offline fixture data · original snapshot preserved"
                      : board!.ts == null
                        ? "Waiting for the first verified market observation"
                        : `Market data captured ${board!.updated || new Date(board!.ts * 1000).toLocaleString()}`}
                  </span>
                </div>
                <button
                  onClick={() => {
                    if (snapshot.meta.kind === "fixture") {
                      void refresh();
                    } else {
                      setPaused((v) => !v);
                      notify(
                        paused
                          ? "Snapshot monitoring resumed."
                          : "UI monitoring paused. The paper worker continues independently.",
                      );
                    }
                  }}
                >
                  {snapshot.meta.kind === "fixture" ? (
                    <>
                      <RefreshCw size={12} /> Return to snapshot
                    </>
                  ) : paused ? (
                    <>
                      <Play size={12} /> Resume monitoring
                    </>
                  ) : (
                    <>
                      <Pause size={12} /> Pause monitoring
                    </>
                  )}
                </button>
              </div>
              {view !== "journal" && (
                <>
                  <OptimizerPanel {...optimizer} />
                  <WatchControls {...watchSettings} />
                </>
              )}
              {view === "terminal" && (
                <>
                  <div className="monitor-grid">
                    <section className="market-panel panel">
                      <div className="panel-heading">
                        <span>
                          <ChartNoAxesCombined size={16} /> Trade spotlight{" "}
                          <span className="dim-label">
                            /{" "}
                            {snapshot.meta.kind === "live"
                              ? "SAMPLED MARKET PATH"
                              : "RECORDED EXECUTION"}
                          </span>
                        </span>
                        <button
                          className="icon-button"
                          onClick={() => setShowSignals((v) => !v)}
                          aria-label={
                            showSignals
                              ? "Hide execution markers"
                              : "Show execution markers"
                          }
                          title="Toggle execution markers"
                        >
                          {showSignals ? (
                            <Eye size={15} />
                          ) : (
                            <EyeOff size={15} />
                          )}
                        </button>
                      </div>
                      <div className="spotlight-heading">
                        <div className="spotlight-asset">
                          {chartToken && <Avatar token={chartToken} />}
                          <div>
                            <label
                              className="sr-only"
                              htmlFor="spotlight-asset"
                            >
                              Choose recorded trade
                            </label>
                            <div className="asset-select">
                              <select
                                id="spotlight-asset"
                                value={chartSymbol}
                                onChange={(e) => setChartSymbol(e.target.value)}
                              >
                                {!snapshot.trades[chartSymbol] &&
                                  chartSymbol && (
                                    <option value={chartSymbol}>
                                      {shortSymbol(chartSymbol)}
                                    </option>
                                  )}
                                {Object.keys(snapshot.trades).map((s) => (
                                  <option key={s} value={s}>
                                    {shortSymbol(s)}
                                  </option>
                                ))}
                              </select>
                              <ChevronDown size={14} />
                            </div>
                            <span>
                              {chartToken?.name || "Recorded paper trade"}{" "}
                              <span className="spotlight-id">
                                {identifier(chartSymbol)}
                              </span>
                            </span>
                          </div>
                          <span className="market-network">pump.fun</span>
                        </div>
                        <div className="spotlight-price">
                          <strong>
                            {trade
                              ? chartNumber(
                                  trade.series[trade.series.length - 1]?.value,
                                  trade,
                                )
                              : money(chartToken?.mcap, 0)}
                          </strong>
                          <span
                            className={
                              trade && trade.exitPp < 0
                                ? "red-text"
                                : "lime-text"
                            }
                          >
                            {trade
                              ? `${signed(trade.exitPp, "%")} return`
                              : "Awaiting trade data"}
                          </span>
                        </div>
                      </div>
                      <div className="chart-toolbar">
                        <div className="chart-mode">
                          <Activity size={12} /> {chartMetric(trade)}{" "}
                          <span>
                            {trade?.metric === "unit_price" ? "UNITS" : "USD"}
                          </span>
                        </div>
                        <span className="chart-period">
                          FULL TRADE <span className="status-dot" />
                        </span>
                      </div>
                      <TradeChart
                        trade={trade}
                        showSignals={showSignals}
                        symbol={shortSymbol(chartSymbol)}
                      />
                      <div className="chart-bottom">
                        <div>
                          <span className="legend-line" /> Recorded path{" "}
                          <span className="legend-entry" /> Entry level{" "}
                          <span className="chart-data-note">
                            {trade?.metric === "unit_price"
                              ? "Fixture executions"
                              : snapshot.meta.kind === "live"
                                ? "Sampled market observations"
                                : "Ledger interpolation"}
                          </span>
                        </div>
                        <button
                          onClick={() => chartToken && setSelected(chartToken)}
                        >
                          Trade details <ArrowUpRight size={12} />
                        </button>
                      </div>
                    </section>
                    <EnergyPulse snapshot={snapshot} />
                  </div>
                  <div className="board-toolbar">
                    <div>
                      <h2>
                        Signal flow{" "}
                        <span className="heading-count">
                          {allTokens(snapshot).length}
                        </span>
                      </h2>
                      <span className="board-subtitle">
                        From discovery to decision.
                      </span>
                    </div>
                    <div className="board-controls">
                      <div className="board-flow-label">
                        <span className="status-dot" />{" "}
                        {paused ? "MONITORING PAUSED" : "PAPER PIPELINE"}
                      </div>
                      <label className="search-box">
                        <Search size={15} />
                        <input
                          ref={queryRef}
                          value={query}
                          onChange={(e) => setQuery(e.target.value)}
                          placeholder="Search signals…"
                          aria-label="Search signals"
                        />
                        <kbd>⌘ K</kbd>
                        {query && (
                          <button
                            aria-label="Clear search"
                            onClick={() => setQuery("")}
                          >
                            <X size={13} />
                          </button>
                        )}
                      </label>
                    </div>
                  </div>
                  <Board
                    snapshot={snapshot}
                    query={query}
                    onSelect={inspect}
                    periods={watchSettings.config.watchPeriods}
                  />
                  <section className="activity-panel panel">
                    <div className="panel-heading">
                      <span>
                        <Terminal size={15} /> Engine activity{" "}
                        <span className="dim-label">
                          / LATEST RECORDED EVENTS
                        </span>
                      </span>
                      <span className="activity-count">
                        {board!.log?.length || 0} events
                      </span>
                    </div>
                    <div className="activity-list">
                      {board!.log?.length ? (
                        board!.log.slice(0, 4).map((line, i) => {
                          const kind = line.includes(" EXIT ")
                            ? "EXIT"
                            : line.includes(" BUY ")
                              ? "BUY"
                              : line.includes("SKIP")
                                ? "SKIP"
                                : "INFO";
                          return (
                            <div className="activity-row" key={`${i}-${line}`}>
                              <span className="activity-time">
                                {line.slice(0, 8)}
                              </span>
                              <span
                                className={`activity-type ${kind.toLowerCase()}`}
                              >
                                {kind}
                              </span>
                              <span>
                                {line
                                  .slice(9)
                                  .replace(/^(SKIP|EXIT|BUY)\s+/, "")}
                              </span>
                            </div>
                          );
                        })
                      ) : (
                        <div className="table-empty">
                          No activity log supplied by this engine snapshot.
                        </div>
                      )}
                    </div>
                    <button
                      className="activity-more"
                      onClick={() => setHelp(true)}
                    >
                      How to read the engine <ArrowUpRight size={12} />
                    </button>
                  </section>
                </>
              )}
              {view === "journal" && (
                <>
                  <div className="journal-search">
                    <label className="search-box">
                      <Search size={15} />
                      <input
                        ref={queryRef}
                        placeholder="Search recorded trades…"
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                        aria-label="Search recorded trades"
                      />
                    </label>
                    <span>
                      {Object.keys(snapshot.trades).length} recorded trade
                      charts
                    </span>
                  </div>
                  <Journal
                    snapshot={snapshot}
                    query={query}
                    onSelect={inspect}
                  />
                </>
              )}
              {view === "strategy" && (
                <Strategy config={watchSettings.config} />
              )}
            </>
          )}
          <footer className="footer">
            <span>
              <Zap size={12} /> Built for the signal. Not the noise.
            </span>
            <span>
              <ShieldCheck size={12} /> Paper only{" "}
              <span className="caption-dot">·</span> No live orders{" "}
              <span className="caption-dot">·</span>
              <button onClick={() => setHelp(true)}>
                Terminal guide <ArrowUpRight size={11} />
              </button>
            </span>
          </footer>
        </main>
      </div>
      {toast && (
        <div className="toast" role="status">
          <CheckCheck size={17} />
          <span>{toast}</span>
          <button
            onClick={() => setToast("")}
            aria-label="Dismiss notification"
          >
            <X size={13} />
          </button>
        </div>
      )}
      {selected && snapshot && (
        <Detail
          token={selected}
          snapshot={snapshot}
          onClose={() => setSelected(null)}
          periods={watchSettings.config.watchPeriods}
        />
      )}
      {settings && (
        <Modal title="Tune your terminal" onClose={() => setSettings(false)}>
          <p className="modal-description">
            Interface preferences only. Trading rules remain unchanged.
          </p>
          <div className="preference">
            <div>
              <strong>Energy flow</strong>
              <p>Ambient orbit, circuit, and scanning animations.</p>
            </div>
            <button
              className={`toggle ${motion ? "on" : ""}`}
              role="switch"
              aria-checked={motion}
              aria-label="Energy flow animation"
              onClick={() => setMotion((v) => !v)}
            >
              <span />
            </button>
          </div>
          <div className="preference">
            <div>
              <strong>Execution markers</strong>
              <p>Show buy, scale-out, and exit labels on charts.</p>
            </div>
            <button
              className={`toggle ${showSignals ? "on" : ""}`}
              role="switch"
              aria-checked={showSignals}
              aria-label="Execution markers"
              onClick={() => setShowSignals((v) => !v)}
            >
              <span />
            </button>
          </div>
          <div className="preference">
            <div>
              <strong>Snapshot monitoring</strong>
              <p>
                Re-read engine output every 15 seconds. Does not start a live
                feed.
              </p>
            </div>
            <button
              className={`toggle ${!paused ? "on" : ""}`}
              role="switch"
              aria-checked={!paused}
              aria-label="Snapshot monitoring"
              onClick={() => setPaused((v) => !v)}
            >
              <span />
            </button>
          </div>
          <div className="modal-foot">
            <ShieldCheck size={14} /> System reduced-motion preferences are
            always respected.
          </div>
        </Modal>
      )}
      {help && (
        <Modal title="Read the flow" onClose={() => setHelp(false)}>
          <div className="guide-content">
            <div>
              <span className="guide-number">01</span>
              <section>
                <h3>Checking → Holding → Done</h3>
                <p>
                  The board mirrors existing engine decisions. Checking signals
                  are under review, holding contains active paper positions, and
                  done includes exits and skips. Inspect a card to see its
                  recorded reason.
                </p>
              </section>
            </div>
            <div>
              <span className="guide-number">02</span>
              <section>
                <h3>Snapshot, not a running live feed</h3>
                <p>
                  The terminal reads the repository’s recorded market data.
                  Refresh and monitoring re-read those files; they do not fetch
                  new markets or place orders. Start the existing paper feed
                  separately if you want to update its output.
                </p>
              </section>
            </div>
            <div>
              <span className="guide-number">03</span>
              <section>
                <h3>Run a safe fixture simulation</h3>
                <p>
                  Run paper simulation executes the original fixture engine once
                  using offline data in isolated storage. Your recorded dataset
                  is preserved. Return to snapshot restores the original view.
                </p>
              </section>
            </div>
            <div>
              <span className="guide-number">04</span>
              <section>
                <h3>Charts explain executions</h3>
                <p>
                  Recorded market-cap paths interpolate the existing ledger’s
                  key points. Fixture charts show exact unit-price executions.
                  Neither represents full market candle histories. Equity and
                  aggregate wins/losses come from board statistics; journal
                  charts may cover only a subset.
                </p>
              </section>
            </div>
          </div>
          <div className="modal-foot">
            <FlaskConical size={15} /> No private keys, wallet signing, or
            real-money execution.
          </div>
        </Modal>
      )}
    </div>
  );
}
