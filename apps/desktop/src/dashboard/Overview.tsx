import { useEffect, useRef, useState } from "react";
import { api, JobEvent, onJobEvents, secs, Stats, System } from "./api";
import { Alert, Icon, PageHeader, Progress, Tabs } from "./ui";
import ServerCard from "./ServerCard";

const RANGES = [
  ["today", "Today"],
  ["yesterday", "Yesterday"],
  ["7d", "Last 7 days"],
  ["30d", "Last 30 days"],
  ["90d", "Last 90 days"],
  ["month", "This month"],
];
// Day values are local days counted from 1970, so they read right as UTC dates.
const dayLabel = (day: number, long = false) =>
  new Date(day * 1000).toLocaleDateString(undefined, long ? { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" } : { month: "short", day: "numeric", timeZone: "UTC" });

/** What the engine has done and is doing. The shell polls the system and passes it in. */
export default function Status({ system }: { system: System | null }) {
  const [range, setRange] = useState("7d");
  const [model, setModel] = useState("");
  const [scope, setScope] = useState<"all" | "session">("all");
  const [stats, setStats] = useState<Stats | null>(null);
  const [jobs, setJobs] = useState<Record<string, JobEvent>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const q = new URLSearchParams({ range, scope, tz: String(-new Date().getTimezoneOffset()) });
    if (model) q.set("model", model);
    const load = () => api<Stats>(`/api/v1/stats?${q}`).then(setStats, (e) => setError(e.message));
    load();
    return onJobEvents((e) => {
      setJobs((all) => ({ ...all, [e.id]: e }));
      if (["done", "failed", "cancelled"].includes(e.event)) load();
    });
  }, [range, model, scope]);

  const t = stats?.totals;
  const before = stats?.previous;
  const span = stats?.days.length ?? 1;
  const since = stats && new Date(stats.session_started * 1000).toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" });
  const active = (j: JobEvent) => j.status === "running" || j.status === "queued";
  const recent = Object.values(jobs)
    .sort((a, b) => Number(active(b)) - Number(active(a)) || b.created - a.created)
    .slice(0, 6);
  const q = system?.queue;

  return (
    <>
      <PageHeader title="Dashboard" lead="What the engine has done and what it's doing now">
        <div className="usage-filters">
          <Tabs label="Period" value={scope} onChange={setScope} options={[["all", "All time"], ["session", "This session"]]} />
          <label className="inline-field">
            Range
            <select value={range} onChange={(e) => setRange(e.target.value)}>
              {RANGES.map(([id, text]) => (
                <option key={id} value={id}>
                  {text}
                </option>
              ))}
            </select>
          </label>
          <label className="inline-field">
            Model
            <select value={model} onChange={(e) => setModel(e.target.value)}>
              <option value="">All models</option>
              {(stats?.all_models ?? []).map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </label>
        </div>
      </PageHeader>

      <ServerCard system={system} />
      {error && <Alert>{error}</Alert>}
      {stats && !stats.recording && (
        <Alert info>
          Usage recording is off, so new images aren't counted. <a href="#settings">Turn it on in Settings</a>
        </Alert>
      )}
      {system && !system.engine.up && (
        <Alert>
          <strong>The image engine isn't running.</strong>
          {system.engine.error && <span className="muted">{system.engine.error}</span>}
        </Alert>
      )}

      <section className="tiles" aria-label="Totals">
        <div className="card tile">
          <span>Images</span>
          <span className="figure">{t ? t.done : "–"}</span>
          {t && (before ? <Delta now={t.done} then={before.done} days={span} /> : <span className="delta">Since {since}</span>)}
        </div>
        <div className="card tile">
          <span>Failed</span>
          <span className="figure">{t ? t.failed : "–"}</span>
          {t && (before ? <Delta now={t.failed} then={before.failed} days={span} worseUp /> : <span className="delta">Since {since}</span>)}
        </div>
        <div className="card tile">
          <span>In queue</span>
          <span className="figure">{q ? q.running + q.queued : "–"}</span>
          {q && <span className="delta">{q.running} running, {q.queued} waiting</span>}
        </div>
      </section>

      <div className="grid12 even">
        <ChartCard days={stats?.days ?? null} />

        <section className="card span4" aria-labelledby="running-title">
          <h2 id="running-title">Running now</h2>
          {recent.length === 0 ? (
            <p className="muted">Nothing yet. Jobs show up here as they arrive.</p>
          ) : (
            <ul className="jobs">
              {recent.map((j) => (
                <li key={j.id} className={`job ${j.status}`}>
                  <div className="job-line">
                    <span>{j.model}</span>
                    <span>
                      {j.status === "running"
                        ? `${Math.round(j.progress * 100)}%`
                        : j.status === "queued"
                          ? `Waiting${j.queue_position ? ` · #${j.queue_position}` : ""}`
                          : j.status === "done"
                            ? secs(j.elapsed_ms)
                            : j.status === "failed"
                              ? "Failed"
                              : "Cancelled"}
                    </span>
                  </div>
                  {j.status === "running" && <Progress value={j.progress} label={`${j.model} progress`} />}
                  {(active(j) || j.error) && (
                    <div className="job-meta">
                      <span>{j.error ? <code>{j.error.code}</code> : j.status === "running" ? "Generating" : "Next in line"}</span>
                      {active(j) && (
                        <button className="ghost" onClick={() => api(`/api/v1/jobs/${j.id}`, { method: "DELETE" }).catch((e) => setError(e.message))}>
                          Cancel
                        </button>
                      )}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
          <p className="cap push">Jobs from this computer, paired phones and apps with a key</p>
        </section>
      </div>

      {stats && (
        <div className="grid12 even">
          <section className="card span8" aria-labelledby="models-title">
            <h2 id="models-title">Most used models</h2>
            {stats.models.length === 0 ? (
              <p className="muted">No images yet in this range.</p>
            ) : (
              <table className="data">
                <thead>
                  <tr>
                    <th scope="col">Model</th>
                    <th scope="col" className="n">Images</th>
                    <th scope="col" className="n">Typical time</th>
                    <th scope="col" className="n">Slowest 5%</th>
                  </tr>
                </thead>
                <tbody>
                  {stats.models.map((m) => (
                    <tr key={m.model}>
                      <td>{m.model}</td>
                      <td className="n">{m.jobs}</td>
                      <td className="n">{secs(m.p50_ms)}</td>
                      <td className="n">{secs(m.p95_ms)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
          <div className="span4 col">
            <section className="card" aria-labelledby="errors-title">
              <h2 id="errors-title">Why jobs failed</h2>
              <Counts rows={stats.errors.map((e) => [e.code, e.count])} empty="No failures in this range." code />
            </section>
            <section className="card" aria-labelledby="styles-title">
              <h2 id="styles-title">Styles used</h2>
              <Counts rows={stats.styles.map((s) => [s.style, s.count])} empty="No styles used in this range." />
            </section>
          </div>
        </div>
      )}

      {stats && <YearCard year={stats.year} />}
      {stats && (
        <div className="grid12 even">
          <HeatmapCard heat={stats.heatmap} />
          <MonthsCard year={stats.year} />
        </div>
      )}
    </>
  );
}

const WEEKDAYS = Array.from({ length: 7 }, (_, i) =>
  new Date(Date.UTC(1970, 0, 5 + i)).toLocaleDateString(undefined, { weekday: "short", timeZone: "UTC" }),
); // 5 Jan 1970 was a Monday
const hourLabel = (h: number) => `${String(h).padStart(2, "0")}:00`;

/** Shade 0 is no images; 1–4 split the busiest hour's count in quarters, like GitHub's contribution chart. */
const heatLevel = (n: number, max: number) => (n ? Math.ceil((4 * n) / max) : 0);

const Legend = () => (
  <span className="heat-legend" aria-hidden="true">
    Less
    {[0, 1, 2, 3, 4].map((l) => <i key={l} style={{ background: `var(--heat-${l})` }} />)}
    More
  </span>
);

const DAY_S = 86400;
/** Day numbers are local days, so a UTC date reads them right. */
const dayDate = (t: number, opts: Intl.DateTimeFormatOptions) =>
  new Date(t * 1000).toLocaleDateString(undefined, { ...opts, timeZone: "UTC" });

/** The past year, GitHub-style: a column per week, Monday at the top, one square per day. */
function YearCard({ year }: { year: { from: number; done: number[] } }) {
  const [hover, setHover] = useState<number | null>(null);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => { if (box.current) box.current.scrollLeft = box.current.scrollWidth; }, []); // a phone opens on this week
  const days = year.done;
  const max = Math.max(1, ...days);
  const total = days.reduce((a, b) => a + b, 0);
  const best = days.indexOf(Math.max(...days));
  const weeks = Math.ceil(days.length / 7);
  const step = 14, size = 11, left = 30, top = 18, W = left + weeks * step, H = top + 7 * step;
  const at = (i: number) => year.from + i * DAY_S;
  // A month label over the first week that starts in it, dropped if it would crowd the one before.
  const months: [number, string][] = [];
  for (let w = 0; w < weeks; w++) {
    const m = dayDate(at(w * 7), { month: "short" });
    if (w === 0 || m !== dayDate(at((w - 1) * 7), { month: "short" })) months.push([w, m]);
  }
  if (months.length > 1 && months[1][0] - months[0][0] < 3) months.shift();
  const col = hover === null ? 0 : Math.floor(hover / 7), row = hover === null ? 0 : hover % 7;
  return (
    <section className="card heat-card" aria-labelledby="year-title">
      <div className="card-head">
        <h2 id="year-title">Images in the past year</h2>
      </div>
      <div className="scroll-x" ref={box}>
        <div className="chart year">
          <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Images each day over the past year: ${total} in all. The By month table lists them.`} onMouseLeave={() => setHover(null)}>
            {months.map(([w, m]) => (
              <text key={w} x={left + w * step} y={top - 7} className="axis">{m}</text>
            ))}
            {[0, 2, 4].map((d) => (
              <text key={d} x={left - 6} y={top + d * step + size - 1} textAnchor="end" className="axis">{WEEKDAYS[d]}</text>
            ))}
            {days.map((n, i) => (
              <rect
                key={i}
                x={left + Math.floor(i / 7) * step}
                y={top + (i % 7) * step}
                width={size}
                height={size}
                rx={2}
                className={hover === i ? "cell on" : "cell"}
                style={{ fill: `var(--heat-${heatLevel(n, max)})` }}
                onMouseEnter={() => setHover(i)}
              />
            ))}
          </svg>
          {hover !== null && (
            // Below the square on the top rows, so the scroll box doesn't clip it; pinned in at the ends.
            <div className="tooltip" style={{
              left: `${((left + col * step + size / 2) / W) * 100}%`,
              top: `${((top + row * step + (row < 3 ? size + 4 : -4)) / H) * 100}%`,
              transform: `translate(${col < 6 ? "-10%" : col > weeks - 7 ? "-90%" : "-50%"}, ${row < 3 ? "0" : "-100%"})`,
            }}>
              <span>{dayDate(at(hover), { weekday: "short", day: "numeric", month: "short", year: "numeric" })}</span>
              <span>{days[hover]} image{days[hover] === 1 ? "" : "s"}</span>
            </div>
          )}
        </div>
      </div>
      <div className="heat-foot">
        <span className="muted">
          {total} image{total === 1 ? "" : "s"} in the past year
          {total > 0 && ` · most on ${dayDate(at(best), { day: "numeric", month: "short", year: "numeric" })} (${days[best]})`}
        </span>
        <Legend />
      </div>
    </section>
  );
}

/** The past year by month, newest first: the year calendar as a table. */
function MonthsCard({ year }: { year: { from: number; done: number[] } }) {
  const byMonth = new Map<string, number>();
  year.done.forEach((n, i) => {
    const m = dayDate(year.from + i * DAY_S, { month: "short", year: "numeric" });
    byMonth.set(m, (byMonth.get(m) ?? 0) + n);
  });
  return (
    <section className="card span4" aria-labelledby="months-title">
      <div className="card-head">
        <h2 id="months-title">By month</h2>
      </div>
      <table className="data compact">
        <thead>
          <tr><th scope="col">Month</th><th scope="col" className="n">Images</th></tr>
        </thead>
        <tbody>
          {[...byMonth].reverse().map(([m, n]) => (
            <tr key={m}><td>{m}</td><td className="n">{n}</td></tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

/** When images get made: weekday by local hour, one square each, darker for more. Same Chart/Table switch as the day chart. */
function HeatmapCard({ heat }: { heat: number[][] }) {
  const [view, setView] = useState<"chart" | "table">("chart");
  const [hover, setHover] = useState<[number, number] | null>(null);
  const max = Math.max(1, ...heat.flat());
  const total = heat.flat().reduce((a, b) => a + b, 0);
  let busiest: [number, number] | null = null;
  heat.forEach((row, d) => row.forEach((n, h) => { if (n && (!busiest || n > heat[busiest[0]][busiest[1]])) busiest = [d, h]; }));
  const step = 24, size = 20, left = 40, top = 20, W = left + 24 * step, H = top + 7 * step;
  const tip = hover && heat[hover[0]][hover[1]];
  return (
    <section className="card heat-card span8" aria-labelledby="heat-title">
      <div className="card-head">
        <h2 id="heat-title">When images get made</h2>
        <Tabs label="View" value={view} onChange={setView} options={[["chart", "Chart"], ["table", "Table"]]} />
      </div>
      {view === "table" ? (
        <div className="scroll-x">
          <table className="data heat-table">
            <thead>
              <tr>
                <th scope="col">Day</th>
                {heat[0].map((_, h) => (
                  <th key={h} scope="col" className="n">{hourLabel(h)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {heat.map((row, d) => (
                <tr key={d}>
                  <th scope="row">{WEEKDAYS[d]}</th>
                  {row.map((n, h) => (
                    <td key={h} className="n">{n}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <>
          <div className="chart heatmap">
            <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Images by weekday and hour, in your time zone: ${total} in all. Switch to Table for each hour.`} onMouseLeave={() => setHover(null)}>
              {[0, 3, 6, 9, 12, 15, 18, 21].map((h) => (
                <text key={h} x={left + h * step} y={top - 8} className="axis">{hourLabel(h)}</text>
              ))}
              {heat.map((row, d) => (
                <g key={d}>
                  <text x={left - 8} y={top + d * step + size / 2 + 4} textAnchor="end" className="axis">{WEEKDAYS[d]}</text>
                  {row.map((n, h) => (
                    <rect
                      key={h}
                      x={left + h * step}
                      y={top + d * step}
                      width={size}
                      height={size}
                      rx={4}
                      className={hover?.[0] === d && hover?.[1] === h ? "cell on" : "cell"}
                      style={{ fill: `var(--heat-${heatLevel(n, max)})` }}
                      onMouseEnter={() => setHover([d, h])}
                    />
                  ))}
                </g>
              ))}
            </svg>
            {hover && (
              <div className="tooltip" style={{ left: `${((left + hover[1] * step + size / 2) / W) * 100}%`, top: `${((top + hover[0] * step - 4) / H) * 100}%`, transform: "translate(-50%, -100%)" }}>
                <span>{WEEKDAYS[hover[0]]} {hourLabel(hover[1])}–{hourLabel((hover[1] + 1) % 24)}</span>
                <span>{tip} image{tip === 1 ? "" : "s"}</span>
              </div>
            )}
          </div>
          <div className="heat-foot">
            <span className="muted">
              {total} image{total === 1 ? "" : "s"}
              {busiest && ` · busiest ${WEEKDAYS[busiest[0]]} ${hourLabel(busiest[1])}`}
            </span>
            <Legend />
          </div>
        </>
      )}
    </section>
  );
}

/** Change against the previous period: an arrow and a word, never color alone. */
function Delta({ now, then, days, worseUp }: { now: number; then: number; days: number; worseUp?: boolean }) {
  const diff = now - then;
  const period = days === 1 ? "the day before" : `the previous ${days} days`;
  if (diff === 0) return <span className="delta">Same as {period}</span>;
  return (
    <span className={worseUp && diff > 0 ? "delta bad" : "delta"}>
      <Icon name={diff > 0 ? "up" : "down"} size={14} />
      <span>
        <span className="sr-only">{diff > 0 ? "up " : "down "}</span>
        {Math.abs(diff)} vs {period}
      </span>
    </span>
  );
}

function Counts({ rows, empty, code }: { rows: [string, number][]; empty: string; code?: boolean }) {
  if (rows.length === 0) return <p className="muted">{empty}</p>;
  return (
    <table className="data">
      <tbody>
        {rows.map(([k, n]) => (
          <tr key={k}>
            <td>{code ? <code>{k}</code> : k}</td>
            <td className="n">{n}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** One series (images made) as bars; hover a day for its numbers, or switch to the table. */
function ChartCard({ days }: { days: Stats["days"] | null }) {
  const [view, setView] = useState<"chart" | "table">("chart");
  const [hover, setHover] = useState<number | null>(null);
  return (
    <section className="card span8" aria-labelledby="chart-title">
      <div className="card-head">
        <h2 id="chart-title">Images per day</h2>
        <Tabs label="View" value={view} onChange={setView} options={[["chart", "Chart"], ["table", "Table"]]} />
      </div>
      {!days ? (
        <p className="muted">Loading…</p>
      ) : view === "table" ? (
        <table className="data">
          <thead>
            <tr>
              <th scope="col">Day</th>
              <th scope="col" className="n">Images</th>
              <th scope="col" className="n">Failed</th>
            </tr>
          </thead>
          <tbody>
            {[...days].reverse().map((d) => (
              <tr key={d.day}>
                <td>{dayLabel(d.day, true)}</td>
                <td className="n">{d.done}</td>
                <td className="n">{d.failed}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <Chart days={days} hover={hover} setHover={setHover} />
      )}
    </section>
  );
}

function Chart({ days, hover, setHover }: { days: Stats["days"]; hover: number | null; setHover: (i: number | null) => void }) {
  const W = 720, H = 260, top = 12, bottom = 24, left = 32;
  const max = Math.max(1, ...days.map((d) => d.done));
  const step = Math.max(1, Math.ceil(max / 3));
  const yMax = step * Math.ceil(max / step);
  const ticks = Array.from({ length: yMax / step + 1 }, (_, i) => i * step);
  const y = (v: number) => top + (H - top - bottom) * (1 - v / yMax);
  const slot = (W - left) / days.length;
  const bw = Math.min(40, slot - 2);
  const every = Math.ceil(days.length / 7);
  const total = days.reduce((n, d) => n + d.done, 0);

  return (
    <div className="chart" onMouseLeave={() => setHover(null)}>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Images per day, ${dayLabel(days[0].day)} to ${dayLabel(days[days.length - 1].day)}: ${total} in all. Switch to Table for each day.`}>
        {ticks.map((v) => (
          <g key={v}>
            <line x1={left} x2={W} y1={y(v)} y2={y(v)} className={v === 0 ? "base" : "grid"} />
            <text x={left - 8} y={y(v) + 4} textAnchor="end" className="axis">{v}</text>
          </g>
        ))}
        {days.map((d, i) => {
          const x = left + i * slot + (slot - bw) / 2;
          return (
            <g key={d.day}>
              {d.done > 0 && <path d={bar(x, y(d.done), bw, y(0) - y(d.done))} className={hover === i ? "bar on" : "bar"} />}
              {(days.length - 1 - i) % every === 0 && (
                <text x={x + bw / 2} y={H - 6} textAnchor="middle" className={hover === i ? "axis on" : "axis"}>
                  {dayLabel(d.day)}
                </text>
              )}
              <rect x={left + i * slot} y={top} width={slot} height={H - top - bottom} className="hit" onMouseEnter={() => setHover(i)} />
            </g>
          );
        })}
      </svg>
      {hover !== null && (
        <div className="tooltip" style={{ left: `${((left + (hover + 0.5) * slot) / W) * 100}%` }}>
          <span>{dayLabel(days[hover].day, true)}</span>
          <span>
            <strong>{days[hover].done}</strong> images · {days[hover].failed} failed
          </span>
        </div>
      )}
    </div>
  );
}

/** A bar from the baseline up with 4px rounded top corners. */
function bar(x: number, yTop: number, w: number, h: number) {
  if (h <= 0) return "";
  const r = Math.min(4, w / 2, h);
  return `M${x},${yTop + h}V${yTop + r}Q${x},${yTop} ${x + r},${yTop}H${x + w - r}Q${x + w},${yTop} ${x + w},${yTop + r}V${yTop + h}Z`;
}
