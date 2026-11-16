import { useEffect, useState } from "react";
import { api, JobEvent, onJobEvents, secs, Stats, System } from "./api";
import { Alert, Icon, PageHeader, Progress, Tabs } from "./ui";

const RANGES = [7, 14, 30];
const dayLabel = (day: number, long = false) =>
  new Date(day * 1000).toLocaleDateString(undefined, long ? { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" } : { month: "short", day: "numeric", timeZone: "UTC" });
const sum = (days: Stats["days"], k: "done" | "failed") => days.reduce((n, d) => n + d[k], 0);

/** What the engine has done and is doing. The shell polls the system and passes it in. */
export default function Status({ system }: { system: System | null }) {
  const [range, setRange] = useState(7);
  const [stats, setStats] = useState<Stats | null>(null);
  const [before, setBefore] = useState<{ done: number; failed: number } | null>(null);
  const [jobs, setJobs] = useState<Record<string, JobEvent>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // The previous period is the double range minus this one.
    const load = () =>
      Promise.all([api<Stats>(`/api/v1/stats?days=${range}`), api<Stats>(`/api/v1/stats?days=${range * 2}`)]).then(
        ([now, both]) => {
          setStats(now);
          const prev = both.days.slice(0, both.days.length - now.days.length);
          setBefore({ done: sum(prev, "done"), failed: sum(prev, "failed") });
        },
        (e) => setError(e.message),
      );
    load();
    return onJobEvents((e) => {
      setJobs((all) => ({ ...all, [e.id]: e }));
      if (["done", "failed", "cancelled"].includes(e.event)) load();
    });
  }, [range]);

  const t = stats?.totals;
  const active = (j: JobEvent) => j.status === "running" || j.status === "queued";
  const recent = Object.values(jobs)
    .sort((a, b) => Number(active(b)) - Number(active(a)) || b.created - a.created)
    .slice(0, 6);
  const q = system?.queue;

  return (
    <>
      <PageHeader title="Dashboard" lead="What the engine has done and what it's doing now">
        <label className="inline-field">
          Range
          <select value={range} onChange={(e) => setRange(Number(e.target.value))}>
            {RANGES.map((n) => (
              <option key={n} value={n}>
                Last {n} days
              </option>
            ))}
          </select>
        </label>
      </PageHeader>

      {error && <Alert>{error}</Alert>}
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
          {t && before && <Delta now={t.done} then={before.done} days={range} />}
        </div>
        <div className="card tile">
          <span>Failed</span>
          <span className="figure">{t ? t.failed : "–"}</span>
          {t && before && <Delta now={t.failed} then={before.failed} days={range} worseUp />}
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

    </>
  );
}

/** Change against the previous period: an arrow and a word, never color alone. */
function Delta({ now, then, days, worseUp }: { now: number; then: number; days: number; worseUp?: boolean }) {
  const diff = now - then;
  if (diff === 0) return <span className="delta">Same as the previous {days} days</span>;
  return (
    <span className={worseUp && diff > 0 ? "delta bad" : "delta"}>
      <Icon name={diff > 0 ? "up" : "down"} size={14} />
      <span>
        <span className="sr-only">{diff > 0 ? "up " : "down "}</span>
        {Math.abs(diff)} vs previous {days} days
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
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Images per day for the last ${days.length} days: ${total} in all. Switch to Table for each day.`}>
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
